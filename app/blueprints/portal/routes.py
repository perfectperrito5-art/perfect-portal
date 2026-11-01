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
from app.models.customer import CustomerActivity, WalletTransaction
from app.models.token import TokenBundle, TokenPurchase, AwardGrant
from app.services.tokens import purchasing_mode, purchase_token_package, claim_daily_trial
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
    mode = purchasing_mode()
    package_query = Package.query.filter_by(is_active=True).filter(Package.duration_minutes > 2)
    if mode == "tokens":
        package_query = package_query.filter(Package.token_cost > 0)
    packages = package_query.order_by(Package.sort_order.asc(), Package.price_tzs.asc()).all()
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
    token_bundles = TokenBundle.query.filter_by(is_active=True).order_by(TokenBundle.sort_order, TokenBundle.id).all() if mode == "tokens" else []
    wallet_history = WalletTransaction.query.filter_by(account_id=customer_account.id).order_by(WalletTransaction.created_at.desc()).limit(30).all() if mode == "tokens" and customer_account else []
    award_history = AwardGrant.query.filter_by(account_id=customer_account.id).order_by(AwardGrant.created_at.desc()).limit(20).all() if mode == "tokens" and customer_account else []
    return render_template(
        "portal/home.html",
        packages=packages,
        purchasing_mode=mode,
        token_bundles=token_bundles,
        wallet_history=wallet_history,
        award_history=award_history,
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


@bp.route("/tokens/package/<int:package_id>/buy", methods=["POST"])
@limiter.limit("5/minute")
def token_package_buy(package_id):
    if purchasing_mode() != "tokens":
        flash("Token purchasing is not enabled.", "warning")
        return _portal_redirect("/")
    mac = _client_mac()
    account, device = get_or_create_device_account(mac, _client_ip()) if mac else (None, None)
    package = Package.query.get_or_404(package_id)
    try:
        access = purchase_token_package(account, device, package, _client_ip())
    except ValueError as exc:
        flash(str(exc), "warning")
        return _portal_redirect("/")
    return redirect(url_for("portal.connected", session_id=access.id))


@bp.route("/tokens/trial", methods=["POST"])
@limiter.limit("5/minute")
def token_free_trial():
    if purchasing_mode() != "tokens":
        flash("Free trial is unavailable in this purchasing mode.", "warning")
        return _portal_redirect("/")
    mac = _client_mac()
    account, device = get_or_create_device_account(mac, _client_ip()) if mac else (None, None)
    if not account or not device:
        flash("Could not identify this device.", "warning")
        return _portal_redirect("/")
    from app.services.access import find_active_session
    if find_active_session(device.mac_address):
        flash("This device already has an active internet session.", "info")
        return _portal_redirect("/")
    trial_package = Package.query.filter_by(name="__ACCOUNT_DAILY_TRIAL__").first()
    if not trial_package:
        trial_package = Package(name="__ACCOUNT_DAILY_TRIAL__", description="Account daily trial",
                                price_tzs=0, token_cost=0, duration_minutes=2,
                                sort_order=-100, is_active=False)
        db.session.add(trial_package)
        db.session.commit()
    try:
        claim = claim_daily_trial(account.id, device.id)
        access = __import__("app.services.access", fromlist=["grant_access"]).grant_access(
            package=trial_package, mac=device.mac_address, ip=_client_ip()
        )
        from flask import current_app
        if current_app.config.get("GATEWAY_ENABLED") and not access.gateway_authorized:
            from app.services.access import revoke_session
            revoke_session(access, reason="trial_gateway_failure")
            db.session.delete(claim)
            db.session.commit()
            raise ValueError("Wi-Fi could not be activated. Please try again or contact support.")
        db.session.add(CustomerActivity(account_id=account.id, device_id=device.id,
                                         event_type="free_trial_started", details="Two-minute daily account trial"))
        db.session.commit()
    except ValueError as exc:
        flash(str(exc), "warning")
        return _portal_redirect("/")
    return redirect(url_for("portal.connected", session_id=access.id))


@bp.route("/tokens/bundle/<int:bundle_id>/request", methods=["POST"])
@limiter.limit("5/minute")
def token_bundle_request(bundle_id):
    if purchasing_mode() != "tokens":
        return _portal_redirect("/")
    account, _device = get_or_create_device_account(_client_mac(), _client_ip())
    bundle = TokenBundle.query.filter_by(id=bundle_id, is_active=True).first_or_404()
    if account:
        db.session.add(TokenPurchase(account_id=account.id, bundle_id=bundle.id,
                                     bundle_name=bundle.name, price_tzs=bundle.price_tzs,
                                     token_amount=bundle.token_amount, status="contact_support"))
        db.session.add(CustomerActivity(account_id=account.id, event_type="token_bundle_requested",
                                         details=f"Requested {bundle.name} bundle; pending support verification"))
        db.session.commit()
    flash("Contact support to complete payment and have tokens credited to your account.", "info")
    return _portal_redirect("/")


@bp.route("/pay", methods=["POST"])
@limiter.limit("8/minute")
def pay():
    if purchasing_mode() == "tokens":
        flash("Direct payment is disabled while token mode is active.", "warning")
        return _portal_redirect("/")
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
    if purchasing_mode() == "tokens":
        flash("Voucher redemption is disabled while token mode is active.", "warning")
        return _portal_redirect("/")
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

