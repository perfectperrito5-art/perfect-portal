"""Token purchasing, daily trials and award evaluation."""
from datetime import datetime, timezone, timedelta
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


def purchase_token_package(account, device, package, ip_address="", idempotency_key=""):
    """Deduct once, grant access, and compensate if firewall authorisation fails."""
    from app.services.access import grant_access, revoke_session
    from app.models.customer import CustomerActivity
    from app.models.token import AwardRule

    if purchasing_mode() != "tokens":
        raise ValueError("Token purchasing is currently disabled.")
    if not account or not account.is_active or not device or not device.is_active:
        raise ValueError("Customer account or device unavailable.")
    from app.services.access import find_active_session
    if find_active_session(device.mac_address):
        raise ValueError("This device already has an active internet session.")
    if not package or not package.is_active or package.token_cost <= 0:
        raise ValueError("This package is unavailable.")
    if not idempotency_key or len(idempotency_key) > 80:
        raise ValueError("Refresh the portal and try your purchase again.")
    ref = f"pkg:{account.id}:{idempotency_key}"
    previous = WalletTransaction.query.filter_by(reference=ref).first()
    if previous:
        if previous.amount < 0:
            from app.models.session import AccessSession
            previous_session = AccessSession.query.filter_by(
                mac_address=device.mac_address, package_id=package.id, revoked=False
            ).order_by(AccessSession.started_at.desc()).first()
            if previous_session and previous_session.is_active():
                return previous_session
        raise ValueError("This purchase request has already been processed. Refresh the portal before trying again.")
    debit_tokens(account.id, package.token_cost, note=f"Package: {package.name}", reference=ref)
    access = None
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
    except Exception:
        # Never leave a successfully authorised session running after a
        # compensating refund.
        if access is not None and not access.revoked:
            revoke_session(access, reason="token_purchase_rollback")
        if not WalletTransaction.query.filter_by(reference=f"refund:{ref}").first():
            credit_tokens(account.id, package.token_cost, transaction_type="purchase_refund",
                          note="Refund after package activation error", reference=f"refund:{ref}")
        raise

    try:
        evaluate_consecutive_purchase_awards(account, access)
    except Exception:
        db.session.rollback()
        from flask import current_app
        current_app.logger.exception("Could not evaluate purchase awards for account %s", account.id)
    return access


def evaluate_device_awards(account):
    rules = AwardRule.query.filter_by(rule_type="device_count", is_active=True).all()
    count = account.devices.filter_by(is_active=True).count()
    for rule in rules:
        import json
        criteria = json.loads(rule.criteria_json or "{}")
        threshold = max(1, int(criteria.get("device_count", 2)))
        if count < threshold:
            continue
        milestone = count // threshold if rule.repeatable else 1
        key = f"award:{rule.id}:account:{account.id}:device-threshold:{threshold}:milestone:{milestone}"
        if AwardGrant.query.filter_by(idempotency_key=key).first():
            continue
        grant = AwardGrant(rule_id=rule.id, account_id=account.id,
                           reward_type=rule.reward_type, reward_amount=rule.reward_amount,
                           reason_snapshot=f"{rule.name}: reached {threshold} linked devices",
                           idempotency_key=key)
        db.session.add(grant)
        db.session.add(CustomerActivity(account_id=account.id, event_type="award_granted",
                                         details=grant.reason_snapshot))
        if rule.reward_type == "tokens":
            _ledger(account.id, rule.reward_amount, "award",
                    note=grant.reason_snapshot, reference=key)
        db.session.commit()



def evaluate_consecutive_purchase_awards(account, access_session):
    """Evaluate same-package purchase streaks by Tanzania local calendar date."""
    import json
    from app.models.session import AccessSession
    from app.services.clock import as_utc

    device_macs = [d.mac_address for d in account.devices.filter_by(is_active=True).all()]
    if not device_macs:
        return
    rules = AwardRule.query.filter_by(rule_type="consecutive_package_days", is_active=True).all()
    for rule in rules:
        criteria = json.loads(rule.criteria_json or "{}")
        required_days = max(2, int(criteria.get("consecutive_days", 3)))
        required_package = criteria.get("package_id")
        # Historical purchases remain qualifying even after the access session expires
        # or is revoked by the next package purchase.
        query = AccessSession.query.filter(
            AccessSession.mac_address.in_(device_macs),
        ).order_by(AccessSession.started_at.desc()).limit(500)
        sessions = query.all()
        day_package = {}
        for item in sessions:
            if required_package and item.package_id != int(required_package):
                continue
            stamp = as_utc(item.started_at)
            if stamp:
                local_day = stamp.astimezone(LOCAL_TZ).date()
                day_package.setdefault(local_day, item)
        if not day_package:
            continue
        latest = max(day_package)
        streak = [latest]
        while streak[-1] - timedelta(days=1) in day_package:
            streak.append(streak[-1] - timedelta(days=1))
        if len(streak) < required_days:
            continue
        if rule.repeatable and len(streak) % required_days != 0:
            continue
        key_suffix = f"{min(streak).isoformat()}:{len(streak) // required_days}" if rule.repeatable else "first"
        key = f"award:{rule.id}:account:{account.id}:streak:{key_suffix}"
        if AwardGrant.query.filter_by(idempotency_key=key).first():
            continue
        grant = AwardGrant(
            rule_id=rule.id, account_id=account.id, reward_type=rule.reward_type,
            reward_amount=rule.reward_amount,
            reason_snapshot=f"{rule.name}: {required_days} consecutive qualifying purchase days",
            idempotency_key=key,
        )
        db.session.add(grant)
        db.session.add(CustomerActivity(account_id=account.id, event_type="award_granted",
                                         details=grant.reason_snapshot))
        if rule.reward_type == "tokens":
            _ledger(account.id, rule.reward_amount, "award",
                    note=grant.reason_snapshot, reference=key)
            db.session.add(grant)
        elif rule.reward_type == "minutes":
            extra = rule.reward_amount * 60
            expires = access_session.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            expires = expires + timedelta(seconds=extra)
            access_session.expires_at = expires
            access_session.expires_at_unix = int(expires.timestamp())
            db.session.commit()
            from app.services.access import ensure_gateway_access
            ensure_gateway_access(access_session)
        db.session.commit()
