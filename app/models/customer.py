"""Customer identity and wallet primitives for account-based portal access.

Customer accounts are deliberately separate from administrator login accounts.
"""
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class CustomerAccount(db.Model):
    __tablename__ = "customer_accounts"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(24), unique=True, nullable=False, index=True)
    token_balance = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    devices = db.relationship("CustomerDevice", back_populates="account", lazy="dynamic")
    transactions = db.relationship("WalletTransaction", back_populates="account", lazy="dynamic")


class CustomerDevice(db.Model):
    __tablename__ = "customer_devices"
    __table_args__ = (db.UniqueConstraint("account_id", "mac_address", name="uq_customer_device_account_mac"),)

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_accounts.id"), nullable=False, index=True)
    mac_address = db.Column(db.String(32), nullable=False, unique=True, index=True)
    last_ip_address = db.Column(db.String(64), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    first_seen_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_seen_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    account = db.relationship("CustomerAccount", back_populates="devices")


class DeviceLinkCode(db.Model):
    __tablename__ = "device_link_codes"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_accounts.id"), nullable=False, index=True)
    code_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class CustomerActivity(db.Model):
    __tablename__ = "customer_activities"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_accounts.id"), nullable=False, index=True)
    device_id = db.Column(db.Integer, db.ForeignKey("customer_devices.id"), nullable=True, index=True)
    event_type = db.Column(db.String(48), nullable=False, index=True)
    details = db.Column(db.String(255), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)


class WalletTransaction(db.Model):
    __tablename__ = "wallet_transactions"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("customer_accounts.id"), nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False)  # positive credit; negative debit
    transaction_type = db.Column(db.String(32), nullable=False, index=True)
    reference = db.Column(db.String(80), nullable=True, unique=True)
    note = db.Column(db.String(255), nullable=False, default="")
    actor_admin_id = db.Column(db.Integer, db.ForeignKey("admin_users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    account = db.relationship("CustomerAccount", back_populates="transactions")
