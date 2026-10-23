from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models.package import Package
from app.models.voucher import Voucher


ALPHABET = string.ascii_uppercase + string.digits
# Avoid ambiguous characters
ALPHABET = ALPHABET.replace("O", "").replace("0", "").replace("I", "").replace("1", "")


def generate_code(length: int = 4) -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(length))

    return raw


def create_vouchers(
    package: Package,
    count: int,
    *,
    created_by: int | None = None,
    note: str = "",
    expires_days: int | None = None,
    payment_id: int | None = None,
) -> list[Voucher]:
    expires_at = None
    if expires_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)

    vouchers: list[Voucher] = []
    for _ in range(count):
        for _attempt in range(20):
            code = generate_code()
            if not Voucher.query.filter_by(code=code).first():
                break
        else:
            raise RuntimeError("Could not generate unique voucher code")

        v = Voucher(
            code=code,
            package_id=package.id,
            status="unused",
            created_by=created_by,
            note=note,
            expires_at=expires_at,
            payment_id=payment_id,
        )
        db.session.add(v)
        vouchers.append(v)

    db.session.commit()
    return vouchers