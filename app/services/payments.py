"""AzamPay checkout orchestration and payment fulfillment."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone


from app.extensions import db
from app.models.package import Package
from app.models.payment import Payment
from app.services.access import grant_access
from app.services.phone import normalize_tz_msisdn, detect_network
from app.services.azampay import AzamPayClient, AzamPayError, dump_json
from app.services.vouchers import create_vouchers

logger = logging.getLogger(__name__)


def _unique_ids() -> tuple[str, str]:
    token = secrets.token_hex(6).upper()
    order_id = f"PP{token}"
    transid = f"TX{token}"
    return order_id, transid


def start_mobile_payment(
    *,
    package: Package,
    phone_raw: str,
    client_mac: str | None,
    client_ip: str | None,
) -> Payment:
    phone = normalize_tz_msisdn(phone_raw)

    if not phone:
        raise ValueError(
            "Enter a valid Tanzania mobile number (e.g. 0754XXXXXX)."
        )

    if not package.is_active:
        raise ValueError("This package is not available.")

    order_id, transid = _unique_ids()

    payment = Payment(
        order_id=order_id,
        transid=transid,
        package_id=package.id,
        phone=phone,
        amount_tzs=package.price_tzs,
        status="pending",
        client_mac=client_mac,
        client_ip=client_ip,
    )

    db.session.add(payment)
    db.session.commit()

    client = AzamPayClient.from_app()

    if not client.configured():
        payment.provider_message = (
            "AzamPay not configured — payment left pending (dev mode)."
        )
        db.session.commit()
        return payment

    network = detect_network(phone)

    provider_map = {
        "Airtel": "Airtel",
        "Vodacom": "Mpesa",
        "Yas": "Tigo",
        "Halotel": "Halopesa",
    }

    provider = provider_map.get(network)

    if not provider:
        payment.status = "failed"
        payment.provider_message = (
            f"Unsupported mobile network: {network}"
        )
        db.session.commit()
        raise AzamPayError(
            f"Unsupported mobile network: {network}"
        )

    try:
        checkout_resp = client.checkout(
            account_number=phone,
            amount=package.price_tzs,
            external_id=order_id,
            provider=provider,
        )

        payment.provider_payload = dump_json(checkout_resp)

        payment.provider_reference = str(
            checkout_resp.get("reference")
            or checkout_resp.get("transactionId")
            or checkout_resp.get("transid")
            or ""
        )

        payment.provider_result = str(
            checkout_resp.get("result")
            or checkout_resp.get("status")
            or ""
        )

        payment.provider_message = str(
            checkout_resp.get("message")
            or ""
        )

        db.session.commit()

    except AzamPayError as exc:
        payment.status = "failed"
        payment.provider_message = str(exc)
        payment.provider_payload = dump_json(exc.payload)
        db.session.commit()
        raise

    return payment


def fulfill_payment(payment: Payment) -> None:
    """Mark paid, create voucher, grant access."""
    if payment.status == "success":
        return

    payment.status = "success"
    payment.paid_at = datetime.now(timezone.utc)
    db.session.commit()

    package = payment.package
    vouchers = create_vouchers(
        package,
        1,
        note=f"Auto from payment {payment.order_id}",
        payment_id=payment.id,
    )
    voucher = vouchers[0]
    voucher.status = "used"
    voucher.used_at = datetime.now(timezone.utc)
    voucher.used_by_mac = payment.client_mac
    db.session.commit()

    grant_access(
        package=package,
        mac=payment.client_mac,
        ip=payment.client_ip,
        voucher=voucher,
        payment=payment,
    )
    logger.info("Payment fulfilled order=%s mac=%s", payment.order_id, payment.client_mac)



def apply_azampay_callback(payload: dict) -> Payment | None:
    """Process an AzamPay callback and fulfill the matching payment."""

    external_reference = str(
        payload.get("externalreference") or ""
    ).strip()

    transaction_status = str(
        payload.get("transactionstatus") or ""
    ).strip().upper()

    if not external_reference:
        logger.warning(
            "AzamPay callback missing externalreference: %s",
            payload,
        )
        return None

    payment = Payment.query.filter_by(
        order_id=external_reference
    ).first()

    if payment is None:
        logger.warning(
            "AzamPay callback for unknown payment: %s",
            external_reference,
        )
        return None

    # Keep the complete callback for troubleshooting/auditing.
    payment.provider_payload = dump_json(payload)

    payment.provider_reference = str(
        payload.get("reference")
        or payload.get("utilityref")
        or payload.get("transid")
        or payment.provider_reference
        or ""
    )

    payment.provider_result = transaction_status

    payment.provider_message = str(
        payload.get("message")
        or ""
    )

    db.session.commit()

    if transaction_status in {
        "SUCCESS",
        "SUCCESSFUL",
        "COMPLETED",
        "PAID",
    }:
        fulfill_payment(payment)

    elif transaction_status in {
        "FAILED",
        "FAIL",
        "CANCELLED",
        "CANCELED",
    }:
        payment.status = "failed"
        db.session.commit()

    return payment