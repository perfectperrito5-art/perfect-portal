from datetime import datetime, timezone

from app.extensions import db


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    transid = db.Column(db.String(64), unique=True, nullable=False, index=True)
    package_id = db.Column(db.Integer, db.ForeignKey("packages.id"), nullable=False)
    phone = db.Column(db.String(20), nullable=False, index=True)
    amount_tzs = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False, index=True)
    # pending | success | failed | cancelled
    client_mac = db.Column(db.String(32), nullable=True)
    client_ip = db.Column(db.String(64), nullable=True)
    selcom_reference = db.Column(db.String(64), nullable=True)
    selcom_result = db.Column(db.String(64), nullable=True)
    selcom_message = db.Column(db.String(500), nullable=True)
    raw_webhook = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    paid_at = db.Column(db.DateTime, nullable=True)

    package = db.relationship("Package", back_populates="payments")
    voucher = db.relationship("Voucher", back_populates="payment", uselist=False)