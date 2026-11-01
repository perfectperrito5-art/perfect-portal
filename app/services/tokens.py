"""Token purchasing, daily trials and award evaluation."""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import secrets

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.customer import CustomerAccount, WalletTransaction, CustomerActivity
from app.models.token import FreeTrialClaim, TokenBundle, TokenPurchase, AwardRule, AwardGrant, PackagePurchaseAttempt

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


def _refund_purchase_attempt(attempt, reason):
    """Compensate a committed debit exactly once and close the attempt."""
    from app.models.session import AccessSession
    from app.services.access import revoke_session

    session_row = AccessSession.query.filter_by(purchase_attempt_id=attempt.id).first()
    if session_row:
        from flask import current_app
        # Retry firewall removal even when the DB row is already revoked.
        revoked = revoke_session(session_row, reason="token_purchase_recovery")
        if current_app.config.get("GATEWAY_ENABLED") and not revoked:
            attempt.status = "recovery_required"
            attempt.failure_reason = (reason or "Gateway revoke is not confirmed")[:255]
            db.session.commit()
            return False

    debit_ref = f"pkg:{attempt.idempotency_key}"
    debit = WalletTransaction.query.filter_by(reference=debit_ref).first()
    if debit:
        credit_tokens(
            attempt.account_id, attempt.token_cost,
            transaction_type="purchase_refund",
            note=f"Refund: {reason or 'purchase could not be completed'}",
            reference=f"refund:{debit_ref}",
        )
    attempt.status = "refunded"
    attempt.failure_reason = (reason or "Purchase was not completed")[:255]
    db.session.commit()
    return True


def recover_package_purchase_attempt(attempt, *, force=False):
    """Reconcile one interrupted purchase using its durable session/debit links."""
    from flask import current_app
    from app.models.session import AccessSession
    from app.services.access import ensure_gateway_access

    if attempt.status in {"completed", "refunded", "failed"}:
        return attempt.status
    if not force:
        updated = attempt.updated_at or attempt.created_at
        if updated and updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if updated and (datetime.now(timezone.utc) - updated).total_seconds() < 120:
            return "in_progress"

    debit_ref = f"pkg:{attempt.idempotency_key}"
    debit = WalletTransaction.query.filter_by(reference=debit_ref).first()
    if not debit:
        attempt.status = "failed"
        attempt.failure_reason = "No committed wallet debit exists."
        db.session.commit()
        return attempt.status

    session_row = AccessSession.query.filter_by(purchase_attempt_id=attempt.id).first()
    if not session_row:
        _refund_purchase_attempt(attempt, "Application stopped before creating the internet session")
        return attempt.status

    if not session_row.is_active():
        _refund_purchase_attempt(attempt, "Internet session expired before purchase recovery")
        return attempt.status

    if current_app.config.get("GATEWAY_ENABLED"):
        if not ensure_gateway_access(session_row):
            _refund_purchase_attempt(attempt, "Gateway authorisation could not be confirmed")
            return attempt.status

    attempt.session_id = session_row.id
    attempt.status = "completed"
    attempt.failure_reason = ""
    db.session.add(CustomerActivity(
        account_id=attempt.account_id, device_id=attempt.device_id,
        event_type="package_purchased",
        details=f"{attempt.package_name} purchased for {attempt.token_cost} tokens",
    ))
    db.session.commit()
    return attempt.status


def recover_pending_package_purchases(limit=100, *, force=False):
    """Recover incomplete token purchases after startup or scheduled reconciliation."""
    attempts = PackagePurchaseAttempt.query.filter(
        PackagePurchaseAttempt.status.in_(("pending", "debited", "activating", "recovery_required"))
    ).order_by(PackagePurchaseAttempt.created_at.asc()).limit(max(1, min(int(limit), 500))).all()
    results = {"completed": 0, "refunded": 0, "failed": 0, "recovery_required": 0, "in_progress": 0}
    for attempt in attempts:
        try:
            status = recover_package_purchase_attempt(attempt, force=force)
            results[status] = results.get(status, 0) + 1
        except Exception:
            db.session.rollback()
            attempt = db.session.get(PackagePurchaseAttempt, attempt.id)
            if attempt:
                attempt.status = "recovery_required"
                attempt.failure_reason = "Recovery encountered an unexpected error."
                db.session.commit()
                results["recovery_required"] += 1
            from flask import current_app
            current_app.logger.exception("Purchase recovery failed for attempt %s", attempt.id if attempt else "unknown")
    return results


def purchase_token_package(account, device, package, ip_address="", idempotency_key=""):
    """Create a durable purchase intent before debiting, then recover safely."""
    from flask import current_app
    from app.services.access import grant_access, revoke_session
    from app.models.session import AccessSession

    if purchasing_mode() != "tokens":
        raise ValueError("Token purchasing is currently disabled.")
    if not account or not account.is_active or not device or not device.is_active:
        raise ValueError("Customer account or device unavailable.")
    if device.account_id != account.id:
        raise ValueError("This device does not belong to the selected account.")
    from app.services.access import find_active_session
    if find_active_session(device.mac_address):
        raise ValueError("This device already has an active internet session.")
    if not package or not package.is_active or package.token_cost <= 0:
        raise ValueError("This package is unavailable.")
    if not idempotency_key or len(idempotency_key) > 48:
        raise ValueError("Refresh the portal and try your purchase again.")

    attempt_key = f"{account.id}:{idempotency_key}"
    existing = PackagePurchaseAttempt.query.filter_by(idempotency_key=attempt_key).first()
    if existing:
        if existing.account_id != account.id or existing.device_id != device.id:
            raise ValueError("This purchase reference belongs to another device.")
        status = recover_package_purchase_attempt(existing)
        if status == "completed":
            session_row = AccessSession.query.filter_by(purchase_attempt_id=existing.id).first()
            if session_row:
                return session_row
        raise ValueError("This purchase request is being recovered or has already been processed. Please refresh shortly.")

    attempt = PackagePurchaseAttempt(
        account_id=account.id, device_id=device.id, package_id=package.id,
        package_name=package.name, token_cost=package.token_cost,
        mac_address=device.mac_address, ip_address=ip_address or "",
        idempotency_key=attempt_key, status="pending",
    )
    db.session.add(attempt)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = PackagePurchaseAttempt.query.filter_by(idempotency_key=attempt_key).first()
        if existing and existing.account_id == account.id and existing.device_id == device.id:
            status = recover_package_purchase_attempt(existing)
            if status == "completed":
                return AccessSession.query.filter_by(purchase_attempt_id=existing.id).one()
        raise ValueError("This purchase request is already being processed.")

    try:
        debit_tokens(
            account.id, package.token_cost,
            note=f"Package: {package.name}",
            reference=f"pkg:{attempt_key}",
        )
        attempt.status = "debited"
        db.session.commit()

        attempt.status = "activating"
        db.session.commit()
        access = grant_access(
            package=package, mac=device.mac_address, ip=ip_address,
            purchase_attempt_id=attempt.id,
        )
        attempt.session_id = access.id
        db.session.commit()

        if current_app.config.get("GATEWAY_ENABLED") and not access.gateway_authorized:
            revoked = revoke_session(access, reason="token_purchase_gateway_failure")
            if revoked:
                _refund_purchase_attempt(attempt, "Gateway authorisation failed")
                raise ValueError("Wi-Fi could not be activated. Your tokens have been returned.")
            attempt.status = "recovery_required"
            attempt.failure_reason = "Gateway authorisation failed and revoke is unconfirmed."
            db.session.commit()
            raise ValueError("Wi-Fi activation is being checked. Your tokens have not been refunded yet.")

        attempt.status = "completed"
        db.session.add(CustomerActivity(
            account_id=account.id, device_id=device.id, event_type="package_purchased",
            details=f"{package.name} purchased for {package.token_cost} tokens",
        ))
        db.session.commit()
    except ValueError:
        raise
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Token purchase interrupted for attempt %s", attempt.id)
        # Do not guess whether the gateway granted access. Leave a durable
        # recovery state; the reconciler will inspect the linked session and
        # either restore access or safely revoke/refund.
        attempt = db.session.get(PackagePurchaseAttempt, attempt.id)
        if attempt and attempt.status not in {"completed", "refunded"}:
            attempt.status = "recovery_required"
            attempt.failure_reason = "Purchase interrupted; automatic recovery is pending."
            db.session.commit()
        raise ValueError("The purchase was interrupted and is being safely recovered. Please refresh shortly.")

    try:
        evaluate_consecutive_purchase_awards(account, access)
    except Exception:
        db.session.rollback()
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
        if not required_package:
            continue
        required_package = int(required_package)
        # Include every package purchase: a different package on a date
        # interrupts this same-package streak rather than being ignored.
        sessions = AccessSession.query.filter(
            AccessSession.mac_address.in_(device_macs),
        ).order_by(AccessSession.started_at.desc()).limit(500).all()
        packages_by_day = {}
        for item in sessions:
            stamp = as_utc(item.started_at)
            if stamp:
                local_day = stamp.astimezone(LOCAL_TZ).date()
                packages_by_day.setdefault(local_day, set()).add(item.package_id)
        qualifying_days = {
            day for day, package_ids in packages_by_day.items()
            if package_ids == {required_package}
        }
        if not qualifying_days:
            continue
        latest = max(qualifying_days)
        streak = [latest]
        while streak[-1] - timedelta(days=1) in qualifying_days:
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
