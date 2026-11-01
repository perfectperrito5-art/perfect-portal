"""Small, transactional customer-account helpers."""
from datetime import datetime, timedelta, timezone
import hashlib
import secrets

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.customer import (
    CustomerAccount, CustomerActivity, CustomerDevice, DeviceLinkCode, WalletTransaction,
)


def _utcnow():
    return datetime.now(timezone.utc)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def get_or_create_device_account(mac_address: str, ip_address: str = ""):
    """Resolve a known MAC to its account or create an automatic USERxxxxx account."""
    mac = (mac_address or "").upper().replace("-", ":")
    if not mac:
        return None, None
    device = CustomerDevice.query.filter_by(mac_address=mac).first()
    if device:
        if not device.is_active or not device.account.is_active:
            return device.account, device
        now = _utcnow()
        # Avoid a database write on every captive-portal refresh. Refresh the
        # last-seen timestamp at most once per minute, or immediately on IP change.
        previous_seen = _aware(device.last_seen_at)
        ip_changed = bool(ip_address and ip_address != device.last_ip_address)
        stale_seen = not previous_seen or (now - previous_seen) >= timedelta(minutes=1)
        if ip_changed or stale_seen:
            if ip_address:
                device.last_ip_address = ip_address
            device.last_seen_at = now
            db.session.commit()
        return device.account, device

    # Retry username collisions without exposing a registration form.
    for _ in range(8):
        username = f"USER{secrets.randbelow(90000) + 10000}"
        account = CustomerAccount(username=username)
        db.session.add(account)
        try:
            db.session.flush()
            device = CustomerDevice(
                account_id=account.id, mac_address=mac,
                last_ip_address=ip_address or None,
            )
            db.session.add(device)
            db.session.flush()
            db.session.add(CustomerActivity(
                account_id=account.id, device_id=device.id,
                event_type="account_created", details="Automatic account creation",
            ))
            db.session.commit()
            from app.services.tokens import evaluate_device_awards
            evaluate_device_awards(account)
            return account, device
        except IntegrityError:
            db.session.rollback()
            existing = CustomerDevice.query.filter_by(mac_address=mac).first()
            if existing:
                return existing.account, existing
    raise RuntimeError("Could not allocate a unique customer username.")


def create_device_link_code(account: CustomerAccount) -> str:
    """Create a one-time, five-minute code. Only its hash is persisted."""
    if not account or not account.is_active:
        raise ValueError("This account is unavailable.")
    now = _utcnow()
    DeviceLinkCode.query.filter(
        DeviceLinkCode.account_id == account.id,
        DeviceLinkCode.used_at.is_(None),
    ).update({DeviceLinkCode.used_at: now}, synchronize_session=False)
    code = f"{secrets.randbelow(1_000_000):06d}"
    row = DeviceLinkCode(
        account_id=account.id, code_hash=_hash(code),
        expires_at=now + timedelta(minutes=5),
    )
    db.session.add(row)
    db.session.add(CustomerActivity(
        account_id=account.id, event_type="device_link_code_created",
        details="A temporary device-link code was generated",
    ))
    db.session.commit()
    return code


def link_device_with_code(code: str, mac_address: str, ip_address: str = ""):
    """Consume a valid code once and attach the current MAC to that account."""
    normalized = (mac_address or "").upper().replace("-", ":")
    if not normalized or len(code or "") != 6 or not (code or "").isdigit():
        raise ValueError("Enter a valid six-digit linking code.")
    now = _utcnow()
    row = DeviceLinkCode.query.filter_by(code_hash=_hash(code)).with_for_update().first()
    if not row or row.used_at or _aware(row.expires_at) <= now:
        raise ValueError("That linking code is invalid or has expired.")
    account = db.session.get(CustomerAccount, row.account_id)
    if not account or not account.is_active:
        raise ValueError("This account is unavailable.")
    consumed = db.session.execute(
        update(DeviceLinkCode)
        .where(DeviceLinkCode.id == row.id, DeviceLinkCode.used_at.is_(None),
               DeviceLinkCode.expires_at > now)
        .values(used_at=now)
    )
    if consumed.rowcount != 1:
        db.session.rollback()
        raise ValueError("That linking code has already been used or has expired.")
    row.used_at = now
    existing = CustomerDevice.query.filter_by(mac_address=normalized).first()
    if existing:
        if existing.account_id == account.id:
            row.used_at = now
            db.session.commit()
            return account, existing
        # A first visit auto-creates a placeholder account for an unrecognised MAC.
        # It may be merged later only if it has no wallet, trial, purchase, activity,
        # session, or other device, so no account history is discarded.
        placeholder = existing.account
        has_wallet = WalletTransaction.query.filter_by(account_id=placeholder.id).first()
        has_other_devices = CustomerDevice.query.filter(
            CustomerDevice.account_id == placeholder.id,
            CustomerDevice.id != existing.id,
        ).first()
        has_activity = CustomerActivity.query.filter(
            CustomerActivity.account_id == placeholder.id,
            CustomerActivity.event_type != "account_created",
        ).first()
        from app.models.token import FreeTrialClaim, TokenPurchase
        has_trial = FreeTrialClaim.query.filter_by(account_id=placeholder.id).first()
        has_purchase = TokenPurchase.query.filter_by(account_id=placeholder.id).first()
        from app.models.session import AccessSession
        has_session = AccessSession.query.filter_by(mac_address=normalized).first()
        if has_wallet or has_other_devices or has_activity or has_trial or has_purchase or has_session:
            raise ValueError("This device is already linked to another account.")
        existing.account_id = account.id
        existing.last_ip_address = ip_address or existing.last_ip_address
        existing.last_seen_at = now
        db.session.delete(placeholder)
        device = existing
    else:
        device = CustomerDevice(
            account_id=account.id, mac_address=normalized,
            last_ip_address=ip_address or None,
        )
        db.session.add(device)
    db.session.add(device)
    db.session.flush()
    db.session.add(CustomerActivity(
        account_id=account.id, device_id=device.id,
        event_type="device_linked", details="A device was linked using a one-time code",
    ))
    db.session.commit()
    from app.services.tokens import evaluate_device_awards
    evaluate_device_awards(account)
    return account, device
