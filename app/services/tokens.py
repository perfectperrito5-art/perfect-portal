"""Token purchasing, daily trials and award evaluation."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import secrets

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.customer import CustomerAccount, WalletTransaction, CustomerActivity
from app.models.token import FreeTrialClaim, TokenBundle, TokenPurchase, AwardRule, AwardGrant

LOCAL_TZ = ZoneInfo("Africa/Dar_es_Salaam")


def local_today():
    return datetime.now(LOCAL_TZ).date().isoformat()


def purchasing_mode():
    from app.models.settings import SiteSetting
    value = SiteSetting.get("purchasing_mode", "voucher_payment")
    return value if value in {"tokens", "voucher_payment"} else "voucher_payment"


def _ledger(account_id, amount, kind, note="", actor_admin_id=None, reference=None):
    ref = reference or f"{kind}:{secrets.token_urlsafe(18)}"
    existing = WalletTransaction.query.filter_by(reference=ref).first()
    if existing:
        return existing
    result = db.session.execute(
        update(CustomerAccount).where(
            CustomerAccount.id == account_id,
            CustomerAccount.is_active.is_(True),
            (CustomerAccount.token_balance + amount >= 0),
        ).values(token_balance=CustomerAccount.token_balance + amount)
    )
    if result.rowcount != 1:
        db.session.rollback()
        raise ValueError("Customer account unavailable or insufficient tokens.")
    db.session.add(WalletTransaction(
        account_id=account_id, amount=amount, transaction_type=kind,
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


def credit_tokens(account_id, amount, *, transaction_type="manual_credit",
                  note="", actor_admin_id=None, reference=None):
    if amount <= 0:
        raise ValueError("Token credit must be greater than zero.")
    return _ledger(account_id, amount, transaction_type, note, actor_admin_id, reference)


def debit_tokens(account_id, amount, *, note, reference):
    if amount <= 0 or not reference:
        raise ValueError("Invalid token debit.")
    return _ledger(account_id, -amount, "package_purchase", note, reference=reference)


def claim_daily_trial(account_id, device_id=None):
    claim = FreeTrialClaim(account_id=account_id, local_date=local_today(), device_id=device_id)
    db.session.add(claim)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ValueError("Your free trial has already been used today.")
    return claim


def purchase_token_package(account, device, package, ip_address=""):
    """Deduct once, grant access, and compensate if firewall authorisation fails."""
    from app.services.access import grant_access, revoke_session
    from app.models.customer import CustomerActivity
    from app.models.token import AwardRule

    if purchasing_mode() != "tokens":
        raise ValueError("Token purchasing is currently disabled.")
    if not account or not account.is_active or not device or not device.is_active:
        raise ValueError("Customer account or device unavailable.")
    if not package or not package.is_active or package.token_cost <= 0:
        raise ValueError("This package is unavailable.")
    ref = f"pkg:{account.id}:{secrets.token_urlsafe(18)}"
    debit_tokens(account.id, package.token_cost, note=f"Package: {package.name}", reference=ref)
    try:
        access = grant_access(package=package, mac=device.mac_address, ip=ip_address)
        from flask import current_app
        if current_app.config.get("GATEWAY_ENABLED") and not access.gateway_authorized:
            revoke_session(access, reason="token_purchase_gateway_failure")
            credit_tokens(account.id, package.token_cost, transaction_type="purchase_refund",
                          note=f"Refund: gateway authorisation failed for {package.name}",
                          reference=f"refund:{ref}")
            raise ValueError("Wi-Fi could not be activated. Your tokens have been returned.")
        db.session.add(CustomerActivity(
            account_id=account.id, device_id=device.id, event_type="package_purchased",
            details=f"{package.name} purchased for {package.token_cost} tokens",
        ))
        db.session.commit()
        return access
    except Exception:
        # A refund is only applied if no refund ledger entry exists. Preserve
        # the original exception and avoid charging twice on retries.
        if not WalletTransaction.query.filter_by(reference=f"refund:{ref}").first():
            credit_tokens(account.id, package.token_cost, transaction_type="purchase_refund",
                          note="Refund after package activation error", reference=f"refund:{ref}")
        raise


def evaluate_device_awards(account):
    rules = AwardRule.query.filter_by(rule_type="device_count", is_active=True).all()
    count = account.devices.filter_by(is_active=True).count()
    for rule in rules:
        import json
        criteria = json.loads(rule.criteria_json or "{}")
        threshold = max(1, int(criteria.get("device_count", 2)))
        if count < threshold:
            continue
        key = f"award:{rule.id}:account:{account.id}:device-threshold:{threshold}"
        if AwardGrant.query.filter_by(idempotency_key=key).first():
            continue
        grant = AwardGrant(rule_id=rule.id, account_id=account.id,
                           reward_type=rule.reward_type, reward_amount=rule.reward_amount,
                           reason_snapshot=f"{rule.name}: reached {threshold} linked devices",
                           idempotency_key=key)
        db.session.add(grant)
        if rule.reward_type == "tokens":
            db.session.add(WalletTransaction(
                account_id=account.id, amount=rule.reward_amount, transaction_type="award",
                reference=key, note=grant.reason_snapshot,
            ))
            account.token_balance += rule.reward_amount
        db.session.commit()
