"""Transactional helpers for one-device customer identities.

A verified network MAC resolves to one internal device, one customer and one wallet.
IP addresses are retained only as diagnostic metadata.
"""
from datetime import datetime, timedelta, timezone
import secrets

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.customer import CustomerAccount, CustomerActivity, CustomerDevice


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
