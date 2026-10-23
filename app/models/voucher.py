from datetime import datetime, timezone

from app.extensions import db


class Voucher(db.Model):
    __tablename__ = "vouchers"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False, index=True)
    package_id = db.Column(db.Integer, db.ForeignKey("packages.id"), nullable=False)
    status = db.Column(db.String(20), default="unused", nullable=False, index=True)
    # unused | used | revoked | expired
    created_by = db.Column(db.Integer, db.ForeignKey("admin_users.id"), nullable=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"), nullable=True)
    used_at = db.Column(db.DateTime, nullable=True)
    used_by_mac = db.Column(db.String(32), nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    note = db.Column(db.String(255), default="")
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    package = db.relationship("Package", back_populates="vouchers")
    payment = db.relationship("Payment", back_populates="voucher", uselist=False)

    def is_redeemable(self) -> bool:
        if self.status != "unused":
            return False
        if self.expires_at and self.expires_at.replace(tzinfo=timezone.utc) < datetime.now(
            timezone.utc
        ):
            return False
        return True