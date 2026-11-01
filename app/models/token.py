"""Token purchasing, trial and award records.

Amounts are integer tokens; monetary amounts are integer Tanzanian shillings.
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class TokenBundle(db.Model):
    __tablename__ = "token_bundles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    price_tzs = db.Column(db.Integer, nullable=False)
    token_amount = db.Column(db.Integer, nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class TokenPurchase(db.Model):
    __tablename__ = "token_purchases"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_accounts.id"), nullable=False, index=True)
    bundle_id = db.Column(db.Integer, db.ForeignKey("token_bundles.id"), nullable=True)
    bundle_name = db.Column(db.String(80), nullable=False)
    price_tzs = db.Column(db.Integer, nullable=False)
    token_amount = db.Column(db.Integer, nullable=False)
    payment_reference = db.Column(db.String(100), nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    credited_at = db.Column(db.DateTime, nullable=True)

    account = db.relationship("CustomerAccount")
    bundle = db.relationship("TokenBundle")


class FreeTrialClaim(db.Model):
    __tablename__ = "free_trial_claims"
    __table_args__ = (db.UniqueConstraint("account_id", "local_date", name="uq_trial_account_day"),)

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_accounts.id"), nullable=False, index=True)
    local_date = db.Column(db.String(10), nullable=False, index=True)
    device_id = db.Column(db.Integer, db.ForeignKey("customer_devices.id"), nullable=True)
    session_id = db.Column(db.Integer, db.ForeignKey("access_sessions.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class AwardRule(db.Model):
    __tablename__ = "award_rules"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    rule_type = db.Column(db.String(32), nullable=False, index=True)
    criteria_json = db.Column(db.Text, nullable=False, default="{}")
    reward_type = db.Column(db.String(20), nullable=False)  # tokens | minutes
    reward_amount = db.Column(db.Integer, nullable=False)
    repeatable = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class AwardGrant(db.Model):
    __tablename__ = "award_grants"

    id = db.Column(db.Integer, primary_key=True)
    rule_id = db.Column(db.Integer, db.ForeignKey("award_rules.id"), nullable=True, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_accounts.id"), nullable=False, index=True)
    reward_type = db.Column(db.String(20), nullable=False)
    reward_amount = db.Column(db.Integer, nullable=False)
    reason_snapshot = db.Column(db.String(255), nullable=False)
    idempotency_key = db.Column(db.String(120), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
