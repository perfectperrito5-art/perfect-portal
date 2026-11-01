"""Grant / revoke internet access after successful payment or voucher redeem."""

from __future__ import annotations

import logging
import os
import re
import subprocess

from datetime import datetime, timezone

from flask import current_app

from app.extensions import db
from app.models.package import Package
from app.models.payment import Payment
from app.models.session import AccessSession
from app.models.voucher import Voucher
from app.services.clock import now_unix, now_utc, seconds_remaining, session_expires_unix

logger = logging.getLogger(__name__)

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$")


def normalize_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    mac = mac.strip().replace("-", ":").upper()
    if not MAC_RE.match(mac):
        return None
    return mac


def lookup_mac_for_ip(ip: str | None) -> str | None:
    """Resolve client MAC from ARP table or dnsmasq leases (AP mode)."""
    if not ip:
        return None

    try:
        with open("/proc/net/arp", encoding="utf-8") as fh:
            next(fh, None)
            for line in fh:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip:
                    mac = normalize_mac(parts[3])
                    if mac and mac != "00:00:00:00:00:00":
                        return mac
    except OSError:
        pass

    lease_candidates = [
        "/home/admin_perfect/perfect_portal/gateway/runtime/dnsmasq.leases",
        "/var/lib/misc/dnsmasq.leases",
    ]
    for path in lease_candidates:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    # dnsmasq: <expiry> <mac> <ip> <hostname> <client-id>
                    parts = line.split()
                    if len(parts) >= 3 and parts[2] == ip:
                        mac = normalize_mac(parts[1])
                        if mac:
                            return mac
        except OSError:
            continue
    return None


def _seconds_remaining(session: AccessSession) -> int:
    return seconds_remaining(session)


def find_active_session(mac: str | None) -> AccessSession | None:
    mac_n = normalize_mac(mac)
    if not mac_n:
        return None
    candidates = (
        AccessSession.query.filter_by(mac_address=mac_n, revoked=False)
        .order_by(AccessSession.expires_at.desc())
        .all()
    )
    return next((s for s in candidates if s.is_active()), None)


def ensure_gateway_access(session: AccessSession) -> bool:
    """Make sure nft allow-list still has this MAC (e.g. after AP restart / reconnect)."""
    if not current_app.config.get("GATEWAY_ENABLED"):
        return False
    if not session or not session.is_active():
        return False
    mac = normalize_mac(session.mac_address)
    if not mac or mac == "UNKNOWN":
        return False
    remaining = _seconds_remaining(session)
    if remaining <= 0:
        return False
    ok = run_gateway("authorize", mac, session.ip_address or "", str(remaining))
    if ok and not session.gateway_authorized:
        session.gateway_authorized = True
        db.session.commit()
    return ok


def grant_access(
    *,
    package: Package,
    mac: str | None,
    ip: str | None = None,
    voucher: Voucher | None = None,
    payment: Payment | None = None,
) -> AccessSession:
    mac_n = normalize_mac(mac) or (mac or "UNKNOWN").upper()
    now = now_utc()
    duration_seconds = max(60, int(package.duration_minutes) * 60)
    started_unix = now_unix()
    expires_unix = started_unix + duration_seconds
    expires = datetime.fromtimestamp(expires_unix, tz=timezone.utc)

    # End any prior active sessions for this MAC
    prior = AccessSession.query.filter_by(mac_address=mac_n, revoked=False).all()
    for s in prior:
        if s.is_active():
            s.revoked = True

    session = AccessSession(
        mac_address=mac_n,
        ip_address=ip,
        package_id=package.id,
        voucher_id=voucher.id if voucher else None,
        payment_id=payment.id if payment else None,
        started_at=now,
        expires_at=expires,
        started_at_unix=started_unix,
        expires_at_unix=expires_unix,
        revoked=False,
        gateway_authorized=False,
    )
    db.session.add(session)
    db.session.commit()

    # Attach actual network-session activity to the matching customer account.
    from app.models.customer import CustomerDevice, CustomerActivity
    device = CustomerDevice.query.filter_by(mac_address=mac_n, is_active=True).first()
    if device:
        db.session.add(CustomerActivity(
            account_id=device.account_id, device_id=device.id,
            event_type="internet_session_started",
            details=f"{package.name} · {package.duration_label()}",
        ))
        db.session.commit()

    if current_app.config.get("GATEWAY_ENABLED"):
        ok = run_gateway("authorize", mac_n, ip or "", str(duration_seconds))
        session.gateway_authorized = ok
        db.session.commit()
        if not ok:
            logger.error(
                "Access granted in DB but firewall authorize FAILED for mac=%s",
                mac_n,
            )

    return session


def revoke_session(session: AccessSession, *, reason: str = "admin") -> bool:
    """Revoke DB session AND remove MAC from the live firewall allow-list."""
    session.revoked = True
    session.gateway_authorized = False
    from app.models.customer import CustomerDevice, CustomerActivity
    device = CustomerDevice.query.filter_by(mac_address=session.mac_address, is_active=True).first()
    if device:
        db.session.add(CustomerActivity(
            account_id=device.account_id, device_id=device.id,
            event_type="internet_session_ended", details=f"Session {session.id} revoked ({reason})",
        ))
    db.session.commit()

    ok = True
    mac = normalize_mac(session.mac_address) or session.mac_address
    if current_app.config.get("GATEWAY_ENABLED") and mac and mac != "UNKNOWN":
        ok = run_gateway("revoke", mac, session.ip_address or "", "0")
        if not ok:
            logger.error(
                "Session %s marked revoked (%s) but firewall revoke failed for %s",
                session.id,
                reason,
                mac,
            )
    return ok


def revoke_expired_sessions() -> int:
    now = now_unix()
    sessions = AccessSession.query.filter_by(revoked=False).all()
    count = 0
    for s in sessions:
        if session_expires_unix(s) <= now:
            revoke_session(s, reason="expired")
            count += 1
    return count


def run_gateway(action: str, mac: str, ip: str, minutes: str) -> bool:
    script = current_app.config.get("GATEWAY_SCRIPT")
    if not script:
        return False

    # Prefer direct root; otherwise sudo -n (sudoers installed by start-ap.sh)
    commands = [[script, action, mac, ip, minutes]]
    if os.geteuid() != 0:
        commands.insert(0, ["sudo", "-n", script, action, mac, ip, minutes])

    last_err = ""
    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0:
                logger.info("Gateway OK: %s", " ".join(cmd))
                return True
            last_err = (result.stdout or "") + (result.stderr or "")
        except (OSError, subprocess.SubprocessError) as exc:
            last_err = str(exc)
    logger.error("Gateway script failed for %s %s: %s", action, mac, last_err)
    return False


def redeem_voucher(code: str, mac: str | None, ip: str | None) -> AccessSession:
    code = (code or "").strip().upper()
    voucher = Voucher.query.filter_by(code=code).first()
    if not voucher or not voucher.is_redeemable():
        raise ValueError("Invalid or already used voucher code.")

    package = voucher.package
    if not package or not package.is_active:
        raise ValueError("Package for this voucher is no longer available.")

    voucher.status = "used"
    voucher.used_at = now_utc()
    voucher.used_by_mac = normalize_mac(mac) or mac
    db.session.commit()

    return grant_access(package=package, mac=mac, ip=ip, voucher=voucher)


def sync_active_sessions_to_gateway() -> int:
    """Re-push all active DB sessions into nft (call after AP start)."""
    count = 0
    sessions = AccessSession.query.filter_by(revoked=False).all()
    for s in sessions:
        if s.is_active() and ensure_gateway_access(s):
            count += 1
    return count