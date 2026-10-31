from urllib.parse import urlparse

from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_wtf import FlaskForm
from wtforms import HiddenField, StringField, SubmitField
from wtforms.validators import DataRequired, Length

from app.blueprints.portal import bp
from app.extensions import db, limiter
from app.models.package import Package
from app.models.payment import Payment
from app.services.access import (
    ensure_gateway_access,
    find_active_session,
    lookup_mac_for_ip,
    normalize_mac,
    redeem_voucher,
    run_gateway,
)
from app.services.payments import start_mobile_payment
from app.services.customers import (
    create_device_link_code, get_or_create_device_account, link_device_with_code,
)
from app.models.customer import CustomerActivity
import re
from app.services.phone import detect_network
from app.services.selcom import SelcomError


def _portal_href(path: str = "/") -> str:
    """Build an absolute portal URL (survives captive-DNS switch after unlock)."""
    base = (current_app.config.get("PORTAL_BASE_URL") or "").rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}" if base else path


def _portal_redirect(path: str):
    return redirect(_portal_href(path))


@bp.before_request
def _force_canonical_portal_host():
    """Send captive hostnames (e.g. connectivitycheck.*) to http://10.10.0.1:5000.

    Otherwise the session/CSRF cookie is for the wrong host, redeem POSTs fail
    with 'Bad Request — CSRF token missing', and the success page never shows.
    """
    if request.method != "GET":
        return None
    if request.endpoint in {None, "static"}:
        return None

    base = (current_app.config.get("PORTAL_BASE_URL") or "").rstrip("/")
    if not base:
        return None

    want_host = (urlparse(base).netloc or "").lower()
    got_host = (request.host or "").lower()
    if not want_host or got_host == want_host:
        return None

    # Keep local admin/dev access on 127.0.0.1 / localhost
    if got_host.startswith("127.0.0.1") or got_host.startswith("localhost"):
        return None

    target = base + request.path
    if request.query_string:
        target += "?" + request.query_string.decode("utf-8", errors="ignore")
    return redirect(target, code=302)


class PayForm(FlaskForm):
    package_id = HiddenField(validators=[DataRequired()])
    phone = StringField(
        "Mobile number",
        validators=[DataRequired(), Length(min=9, max=15)],
    )
    mac = HiddenField()
    submit = SubmitField("Pay now")


class VoucherForm(FlaskForm):
    code = StringField("Voucher code", validators=[DataRequired(), Length(max=32)])
    mac = HiddenField()
    submit = SubmitField("Redeem")


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def _client_mac() -> str | None:
    """Prefer the MAC currently on the Wi‑Fi link (ARP/lease) over cookies.

    Randomized MAC → device MAC switches left a stale cookie MAC authorized
    while the phone's real packets used another MAC (no internet).
    """
    ip = _client_ip()
    wire = lookup_mac_for_ip(ip)
    query_mac = normalize_mac(
        request.args.get("mac")
        or request.args.get("client_mac")
        or request.form.get("mac")
    )
    cookie = normalize_mac(session.get("client_mac"))

    mac = wire or query_mac or cookie
    if not mac:
        return None

    # If cookie still has an old paid session but the phone MAC changed, migrate it.
    if wire and cookie and wire != cookie:
        active_wire = find_active_session(wire)
        active_cookie = find_active_session(cookie)
        if active_cookie and not active_wire:
            active_cookie.mac_address = wire
            active_cookie.ip_address = ip
            db.session.commit()
            run_gateway("revoke", cookie, "", "0")
            ensure_gateway_access(active_cookie)

    session["client_mac"] = mac
    return mac


@bp.route("/")
def home():
    packages = (
        Package.query.filter_by(is_active=True)
        .order_by(Package.sort_order.asc(), Package.price_tzs.asc())
        .all()
    )
    mac = _client_mac()
    customer_account, customer_device = get_or_create_device_account(mac, _client_ip()) if mac else (None, None)
    active = find_active_session(mac)
    # Re-push firewall allow on every visit if session still valid (reconnect / AP restart)
    if active:
        if active.ip_address != _client_ip():
            active.ip_address = _client_ip()
            db.session.commit()
        ensure_gateway_access(active)

    pay_form = PayForm(mac=mac or "")
    voucher_form = VoucherForm(mac=mac or "")
    return render_template(
        "portal/home.html",
        packages=packages,
        pay_form=pay_form,
        voucher_form=voucher_form,
        active_session=active,
        client_mac=mac,
        customer_account=customer_account,
        customer_device=customer_device,
    )


@bp.route("/account/username", methods=["POST"])
@limiter.limit("5/minute")
def update_customer_username():
    mac = _client_mac()
    account, _device = get_or_create_device_account(mac, _client_ip()) if mac else (None, None)
    username = (request.form.get("username") or "").strip().upper()
    if not account or not re.fullmatch(r"[A-Z0-9]{3,24}", username):
        flash("Use 3–24 letters or numbers for your username.", "warning")
        return _portal_redirect("/")
    from app.models.customer import CustomerAccount
    if CustomerAccount.query.filter(CustomerAccount.username == username, CustomerAccount.id != account.id).first():
        flash("That username is already taken.", "warning")
        return _portal_redirect("/")
    old = account.username
    account.username = username
    db.session.add(CustomerActivity(account_id=account.id, event_type="username_changed", details=f"Username changed from {old} to {username}"))
    db.session.commit()
    flash("Username updated.", "success")
    return _portal_redirect("/")


@bp.route("/account/link-code", methods=["POST"])
@limiter.limit("3/minute")
def customer_link_code():
    mac = _client_mac()
    account, _device = get_or_create_device_account(mac, _client_ip()) if mac else (None, None)
    try:
        code = create_device_link_code(account)
    except ValueError as exc:
        flash(str(exc), "warning")
    else:
        flash(f"Device linking code: {code} (valid for 5 minutes).", "success")
    return _portal_redirect("/")


@bp.route("/account/link-device", methods=["POST"])
@limiter.limit("5/minute")
def customer_link_device():
    mac = _client_mac()
    try:
        account, device = link_device_with_code(request.form.get("code", "").strip(), mac or "", _client_ip())
        session["client_mac"] = device.mac_address
    except ValueError as exc:
        flash(str(exc), "warning")
    else:
        flash(f"This device is now linked to {account.username}.", "success")
    return _portal_redirect("/")


@bp.route("/pay", methods=["POST"])
@limiter.limit("8/minute")
def pay():
    form = PayForm()
    if not form.validate_on_submit():
        flash("Check your phone number and try again.", "warning")
        return redirect(url_for("portal.home"))

    package = Package.query.get(int(form.package_id.data))
    if not package or not package.is_active:
        flash("Package not found.", "danger")
        return redirect(url_for("portal.home"))

    mac = normalize_mac(form.mac.data) or _client_mac()
    try:
        payment = start_mobile_payment(
            package=package,
            phone_raw=form.phone.data,
            client_mac=mac,
            client_ip=_client_ip(),
        )
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("portal.home"))
    except SelcomError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("portal.home"))

    network = detect_network(payment.phone)
    return redirect(url_for("portal.waiting", order_id=payment.order_id, network=network))


@bp.route("/waiting/<order_id>")
def waiting(order_id: str):
    payment = Payment.query.filter_by(order_id=order_id).first_or_404()
    network = request.args.get("network") or detect_network(payment.phone)
    phone_display = payment.phone
    if phone_display.startswith("255"):
        phone_display = "0" + phone_display[3:]
    return render_template(
        "portal/waiting.html",
        payment=payment,
        network=network,
        phone_display=phone_display,
    )


@bp.route("/success/<order_id>")
def success(order_id: str):
    payment = Payment.query.filter_by(order_id=order_id).first_or_404()
    return render_template("portal/success.html", payment=payment)


@bp.route("/connected")
@bp.route("/connected/<int:session_id>")
def connected(session_id: int | None = None):
    """Success page after voucher (or when opening a live session)."""
    mac = _client_mac()
    access = None
    if session_id is not None:
        from app.models.session import AccessSession

        access = AccessSession.query.get_or_404(session_id)
        if access.is_active():
            ensure_gateway_access(access)
    if access is None:
        access = find_active_session(mac)
        if access:
            ensure_gateway_access(access)
    return render_template(
        "portal/connected.html",
        access=access,
        client_mac=mac,
    )


@bp.route("/redeem", methods=["POST"])
@limiter.limit("10/minute")
def redeem():
    form = VoucherForm()
    if not form.validate_on_submit():
        flash("Enter a valid voucher code.", "warning")
        return _portal_redirect("/")

    mac = normalize_mac(form.mac.data) or _client_mac()
    try:
        access = redeem_voucher(form.code.data, mac, _client_ip())
    except ValueError as exc:
        flash(str(exc), "danger")
        return _portal_redirect("/")

    if not access.gateway_authorized:
        flash(
            "Voucher saved, but Wi‑Fi unlock failed. Ask the attendant for help.",
            "warning",
        )
        return _portal_redirect("/")

    # Render success immediately (no redirect to a captive hostname).
    # Android "Use this network" / "Do not use this network" stay for the user.
    return render_template(
        "portal/connected.html",
        access=access,
        client_mac=mac,
    )


@bp.route("/status")
def status_page():
    mac = _client_mac()
    active = find_active_session(mac)
    if active:
        ensure_gateway_access(active)
    return render_template("portal/status.html", active_session=active, client_mac=mac)
    return render_template("portal/status.html", active_session=active, client_mac=mac)

