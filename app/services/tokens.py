"""Token wallet and account-level daily trial services."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import secrets

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.customer import CustomerAccount, WalletTransaction
from app.models.token import FreeTrialClaim

LOCAL_TZ = ZoneInfo("Africa/Dar_es_Salaam")


def local_today():
    return datetime.now(LOCAL_TZ).date().isoformat()


def purchasing_mode():
    from app.models.settings import SiteSetting
    value = SiteSetting.get("purchasing_mode", "voucher_payment")
    return value if value in {"tokens", "voucher_payment"} else "voucher_payment"


def credit_tokens(account_id: int, amount: int, *, transaction_type: str,
                  note: str = "", actor_admin_id: int | None = None,
                  reference: str | None = None):
    """Atomically credit wallet and append its ledger row."""
    if amount <= 0:
        raise ValueError("Token credit must be greater than zero.")
    ref = reference or f"{transaction_type}:{secrets.token_urlsafe(18)}"
    existing = WalletTransaction.query.filter_by(reference=ref).first()
    if existing:
        return existing
    account = db.session.get(CustomerAccount, account_id)
    if not account or not account.is_active:
        raise ValueError("Customer account is unavailable.")
    result = db.session.execute(
        update(CustomerAccount)
        .where(CustomerAccount.id == account_id, CustomerAccount.is_active.is_(True))
        .values(token_balance=CustomerAccount.token_balance + amount)
    )
    if result.rowcount != 1:
        db.session.rollback()
        raise ValueError("Customer account is unavailable.")
    db.session.add(WalletTransaction(
        account_id=account_id, amount=amount, transaction_type=transaction_type,
        reference=ref, note=note[:255], actor_admin_id=actor_admin_id,
    ))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = WalletTransaction.query.filter_by(reference=ref).first()
        if existing:
            return existing
        raise
    return WalletTransaction.query.filter_by(reference=ref).one()


def debit_tokens(account_id: int, amount: int, *, note: str,
                 reference: str):
    """Conditional debit prevents negative balances and duplicate charges."""
    if amount <= 0 or not reference:
        raise ValueError("Invalid token debit.")
    existing = WalletTransaction.query.filter_by(reference=reference).first()
    if existing:
        return existing
    result = db.session.execute(
        update(CustomerAccount)
        .where(CustomerAccount.id == account_id,
               CustomerAccount.is_active.is_(True),
               CustomerAccount.token_balance >= amount)
        .values(token_balance=CustomerAccount.token_balance - amount)
    )
    if result.rowcount != 1:
        db.session.rollback()
        raise ValueError("Insufficient tokens or account unavailable.")
    db.session.add(WalletTransaction(
        account_id=account_id, amount=-amount, transaction_type="package_purchase",
        reference=reference, note=note[:255],
    ))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = WalletTransaction.query.filter_by(reference=reference).first()
        if existing:
            return existing
        raise
    return WalletTransaction.query.filter_by(reference=reference).one()


def claim_daily_trial(account_id: int, device_id: int | None = None):
    """One trial claim per account per Tanzania calendar day."""
    claim = FreeTrialClaim(
        account_id=account_id, local_date=local_today(), device_id=device_id,
    )
    db.session.add(claim)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ValueError("Your free trial has already been used today.")
    return claim
