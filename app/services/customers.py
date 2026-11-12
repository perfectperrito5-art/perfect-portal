"""Transactional helpers for one-device customer identities.

A verified network MAC resolves to one internal device, one customer and one wallet.
IP addresses are retained only as diagnostic metadata.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import secrets

from flask import current_app

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.customer import CustomerAccount, CustomerActivity, CustomerDevice, DeviceLinkCode


def _utcnow():
    return datetime.now(timezone.utc)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def normalize_device_mac(value: str) -> str:
    """Normalize a MAC value supplied by the trusted network lookup."""
    return (value or "").strip().upper().replace("-", ":")


def get_or_create_device_account(mac_address: str, ip_address: str = ""):
    """Resolve MAC -> internal device -> customer -> wallet.

    IP never participates in identity lookup. A new verified MAC receives a
    separate customer and wallet. A known MAC always resolves to its existing
    customer, even when its DHCP address changes.
    """
    mac = normalize_device_mac(mac_address)
    if not mac:
        return None, None

    device = CustomerDevice.query.filter_by(mac_address=mac).first()
    if device:
        account = device.account
        if not device.is_active or not account or not account.is_active:
            return account, device

        now = _utcnow()
        previous_seen = _aware(device.last_seen_at)
        ip_changed = bool(ip_address and ip_address != device.last_ip_address)
        stale_seen = not previous_seen or (now - previous_seen) >= timedelta(minutes=1)
        if ip_changed or stale_seen:
            if ip_address:
                device.last_ip_address = ip_address
            device.last_seen_at = now
            db.session.commit()
        return account, device

    # The database uniqueness constraints on MAC and account_id protect the
    # one-device/one-customer mapping if two requests arrive simultaneously.
    for _ in range(8):
        username = f"USER{secrets.randbelow(90000) + 10000}"
        account = CustomerAccount(username=username, display_name="Guest")
        db.session.add(account)
        try:
            db.session.flush()
            device = CustomerDevice(
                account_id=account.id,
                mac_address=mac,
                last_ip_address=ip_address or None,
            )
            db.session.add(device)
            db.session.flush()
            db.session.add(CustomerActivity(
                account_id=account.id,
                device_id=device.id,
                event_type="account_created",
                details="Automatic one-device customer creation",
            ))
            db.session.commit()
            return account, device
        except IntegrityError:
            db.session.rollback()
            # A concurrent request may have created this MAC while this
            # request was allocating its customer. Reuse that exact identity.
            existing = CustomerDevice.query.filter_by(mac_address=mac).first()
            if existing:
                return existing.account, existing

    raise RuntimeError("Could not allocate a unique customer identity.")


def create_device_link_code(account: CustomerAccount, *, expires_minutes: int = 10) -> str:
    """Create a short-lived, single-use code for moving an account to a new device."""
    if not account or not account.is_active:
        raise ValueError("This customer account is not active.")
    if expires_minutes < 1 or expires_minutes > 60:
        raise ValueError("Link-code expiry must be between 1 and 60 minutes.")

    now = _utcnow()
    DeviceLinkCode.query.filter_by(account_id=account.id, used_at=None).delete(
        synchronize_session=False
    )
    code = secrets.token_hex(4).upper()
    db.session.add(
        DeviceLinkCode(
            account_id=account.id,
            code_hash=hashlib.sha256(code.encode("ascii")).hexdigest(),
            expires_at=now + timedelta(minutes=expires_minutes),
        )
    )
    db.session.commit()
    return code


def link_device_with_code(code: str, mac_address: str, ip_address: str = ""):
    """Consume a one-time code and move the account's device identity to this MAC."""
    normalized_code = "".join((code or "").split()).upper()
    mac = normalize_device_mac(mac_address)
    if not normalized_code or not mac:
        raise ValueError("Enter a valid device link code.")
    parts = mac.split(":")
    if len(parts) != 6 or any(len(part) != 2 for part in parts):
        raise ValueError("The device MAC address is invalid.")

    code_hash = hashlib.sha256(normalized_code.encode("ascii", errors="ignore")).hexdigest()
    link = DeviceLinkCode.query.filter_by(code_hash=code_hash).with_for_update().first()
    now = _utcnow()
    if not link or link.used_at or _aware(link.expires_at) <= now:
        raise ValueError("This device link code is invalid, expired, or already used.")

    account = db.session.get(CustomerAccount, link.account_id)
    if not account or not account.is_active:
        raise ValueError("This customer account is not active.")

    device_for_mac = CustomerDevice.query.filter_by(mac_address=mac).first()
    if device_for_mac and device_for_mac.account_id != account.id:
        raise ValueError("This device is already linked to another account.")

    device = CustomerDevice.query.filter_by(account_id=account.id).first()
    old_mac = device.mac_address if device else None
    if old_mac and old_mac != mac and current_app.config.get("GATEWAY_ENABLED"):
        from app.services.access import run_gateway
        if not run_gateway("revoke", old_mac, device.last_ip_address or "", "0"):
            raise ValueError("The previous device could not be safely disconnected. Please contact support.")

    if device:
        if old_mac and old_mac != mac:
            from app.models.session import AccessSession
            AccessSession.query.filter_by(mac_address=old_mac, revoked=False).update(
                {"revoked": True, "gateway_authorized": False},
                synchronize_session=False,
            )
        device.mac_address = mac
        device.last_ip_address = ip_address or device.last_ip_address
        device.is_active = True
        device.last_seen_at = now
    else:
        device = CustomerDevice(
            account_id=account.id,
            mac_address=mac,
            last_ip_address=ip_address or None,
        )
        db.session.add(device)

    link.used_at = now
    db.session.add(CustomerActivity(
        account_id=account.id,
        device_id=device.id,
        event_type="device_linked",
        details="Customer account linked to a new device",
    ))
    db.session.commit()
    return account, device
