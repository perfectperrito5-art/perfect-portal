from flask import current_app, jsonify, request

from app.blueprints.api import bp
from app.extensions import csrf, limiter
from app.models.payment import Payment
from app.services.payments import (
    apply_webhook_payload,
    apply_azampay_callback,
)
from app.services.phone import detect_network, normalize_tz_msisdn
from app.services.selcom import verify_webhook_secret


@bp.route("/payments/status/<order_id>")
@limiter.limit("60/minute")
def payment_status(order_id: str):
    payment = Payment.query.filter_by(order_id=order_id).first_or_404()
    return jsonify(
        {
            "order_id": payment.order_id,
            "status": payment.status,
            "message": payment.selcom_message,
            "amount": payment.amount_tzs,
            "phone": payment.phone,
            "network": detect_network(payment.phone),
        }
    )


@bp.route("/payments/webhook", methods=["POST", "GET"])
@csrf.exempt
@limiter.limit("120/minute")
def payment_webhook():
    secret = request.args.get("secret") or request.headers.get("X-Webhook-Secret")
    expected = current_app.config.get("SELCOM_WEBHOOK_SECRET")
    if not verify_webhook_secret(secret, expected):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form.to_dict() or dict(request.args)

    payment = apply_webhook_payload(payload)
    if payment is None:
        return jsonify({"ok": False, "error": "unknown_order"}), 404
    return jsonify({"ok": True, "order_id": payment.order_id, "status": payment.status})

@bp.route("/payments/azampay/callback", methods=["POST"])
@csrf.exempt
@limiter.limit("120/minute")
def azampay_callback():
    payload = request.get_json(silent=True) or {}

    if not payload:
        return jsonify({
            "success": False,
            "message": "Empty callback payload",
        }), 400

    payment = apply_azampay_callback(payload)

    if payment is None:
        return jsonify({
            "success": False,
            "message": "Unknown payment",
        }), 404

    return jsonify({
        "success": True,
        "order_id": payment.order_id,
        "status": payment.status,
    }), 200

@bp.route("/network/detect")
@limiter.limit("30/minute")
def network_detect():
    phone = normalize_tz_msisdn(request.args.get("phone", ""))
    if not phone:
        return jsonify({"ok": False, "error": "invalid"}), 400
    return jsonify({"ok": True, "phone": phone, "network": detect_network(phone)})