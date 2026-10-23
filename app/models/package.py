from datetime import datetime, timezone

from app.extensions import db


class Package(db.Model):
    __tablename__ = "packages"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(500), default="")
    price_tzs = db.Column(db.Integer, nullable=False)
    duration_minutes = db.Column(db.Integer, nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    vouchers = db.relationship("Voucher", back_populates="package", lazy="dynamic")
    payments = db.relationship("Payment", back_populates="package", lazy="dynamic")

    def duration_label(self) -> str:
        minutes = self.duration_minutes
        if minutes < 60:
            return f"{minutes} min"
        if minutes % 1440 == 0:
            days = minutes // 1440
            return f"{days} day{'s' if days != 1 else ''}"
        if minutes % 60 == 0:
            hours = minutes // 60
            return f"{hours} hour{'s' if hours != 1 else ''}"
        hours, mins = divmod(minutes, 60)
        return f"{hours}h {mins}m"