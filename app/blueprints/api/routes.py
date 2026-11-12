from flask import jsonify, request

from app.blueprints.api import bp
from app.extensions import csrf, limiter
from app.models.payment import Payment
from app.services.payments import apply_azampay_callback
from app.services.phone import detect_network, normalize_tz_msisdn

@bp.route("/payments/status/<order_id>")
@limiter.limit("60/minute")
def payment_status(order_id: str):
    payment = Payment.query.filter_by(order_id=order_id).first_or_404()
    return jsonify(
        {
            "order_id": payment.order_id,
            "status": payment.status,
            "message": payment.provider_message,
            "amount": payment.amount_tzs,
            "phone": payment.phone,
            "network": detect_network(payment.phone),
        }
    )


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