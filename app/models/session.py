from datetime import datetime, timezone

from app.extensions import db


class AccessSession(db.Model):
    __tablename__ = "access_sessions"

    id = db.Column(db.Integer, primary_key=True)
    mac_address = db.Column(db.String(32), nullable=False, index=True)
    ip_address = db.Column(db.String(64), nullable=True)
    package_id = db.Column(db.Integer, db.ForeignKey("packages.id"), nullable=False)
    voucher_id = db.Column(db.Integer, db.ForeignKey("vouchers.id"), nullable=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"), nullable=True)
    purchase_attempt_id = db.Column(db.Integer, db.ForeignKey("package_purchase_attempts.id"), nullable=True, index=True)
    started_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    # Authoritative duration (UTC epoch seconds). Avoids SQLite TZ ambiguity.
    started_at_unix = db.Column(db.Integer, nullable=True)
    expires_at_unix = db.Column(db.Integer, nullable=True, index=True)
    revoked = db.Column(db.Boolean, default=False, nullable=False)
    gateway_authorized = db.Column(db.Boolean, default=False, nullable=False)

    package = db.relationship("Package")
    voucher = db.relationship("Voucher")
    payment = db.relationship("Payment")
    purchase_attempt = db.relationship("PackagePurchaseAttempt", foreign_keys=[purchase_attempt_id])

    def is_active(self) -> bool:
        if self.revoked:
            return False
        if self.expires_at_unix:
            return int(self.expires_at_unix) > int(datetime.now(timezone.utc).timestamp())
        now = datetime.now(timezone.utc)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > now