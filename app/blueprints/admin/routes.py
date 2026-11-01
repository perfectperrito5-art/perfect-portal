from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField
from werkzeug.utils import secure_filename
from wtforms import (
    BooleanField,
    IntegerField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional

from app.blueprints.admin import bp
from app.extensions import db
from app.models.package import Package
from app.models.payment import Payment
from app.models.session import AccessSession
from app.models.voucher import Voucher
from app.models.customer import CustomerAccount, CustomerDevice, CustomerActivity, WalletTransaction
from app.models.settings import SiteSetting
from app.models.token import TokenBundle, AwardRule
from app.services.tokens import credit_tokens
from app.services.access import revoke_expired_sessions, revoke_session
from app.services.clock import duration_to_minutes, minutes_to_amount_unit
from app.services.payments import fulfill_payment
from app.services.theme import DEFAULTS, get_theme, save_theme
from app.services.vouchers import create_vouchers


class PackageForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    description = TextAreaField("Description", validators=[Optional(), Length(max=500)])
    price_tzs = IntegerField(
        "Price (TZS)", validators=[DataRequired(), NumberRange(min=100, max=5_000_000)]
    )
    duration_minutes = IntegerField(
        "Duration (minutes)",
        validators=[Optional(), NumberRange(min=1, max=525_600)],
    )
    duration_amount = IntegerField(
        "Duration",
        default=1,
        validators=[DataRequired(), NumberRange(min=1, max=3650)],
    )
    duration_unit = SelectField(
        "Duration unit",
        choices=[
            ("minutes", "Minutes"),
            ("hours", "Hours"),
            ("days", "Days"),
        ],
        default="hours",
    )
    sort_order = IntegerField("Sort order", default=0, validators=[Optional()])
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Save package")


class VoucherBatchForm(FlaskForm):
    package_id = SelectField("Package", coerce=int, validators=[DataRequired()])
    count = IntegerField(
        "How many", default=3, validators=[DataRequired(), NumberRange(min=1, max=200)]
    )
    expires_days = IntegerField(
        "Expires in days (optional)", validators=[Optional(), NumberRange(min=1, max=3650)]
    )
    note = StringField("Note", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Generate vouchers")


class ThemeForm(FlaskForm):
    brand_name = StringField("Brand name", validators=[DataRequired(), Length(max=80)])
    tagline = StringField("Tagline", validators=[Optional(), Length(max=160)])
    support_phone = StringField(
        "Supporter phone (call / SMS)",
        validators=[Optional(), Length(max=40)],
    )
    business_pay_number = StringField(
        "Business payment number (Lipa / Till / wallet)",
        validators=[Optional(), Length(max=40)],
    )
    business_pay_name = StringField(
        "Business name on payment (optional)",
        validators=[Optional(), Length(max=80)],
    )
    primary_color = StringField("Primary color", validators=[DataRequired(), Length(max=32)])
    accent_color = StringField("Accent color", validators=[DataRequired(), Length(max=32)])
    text_color = StringField("Text color", validators=[DataRequired(), Length(max=32)])
    muted_color = StringField("Muted color", validators=[DataRequired(), Length(max=32)])
    surface_color = StringField("Surface color", validators=[DataRequired(), Length(max=64)])
    background_type = SelectField(
        "Background type",
        choices=[
            ("gradient", "Gradient / CSS"),
            ("image", "Image"),
            ("solid", "Solid color"),
        ],
    )
    background_value = TextAreaField(
        "Background value (CSS gradient or solid color)",
        validators=[Optional(), Length(max=500)],
    )
    background_image_file = FileField(
        "Upload background image",
        validators=[Optional(), FileAllowed(["jpg", "jpeg", "png", "webp", "gif"])],
    )
    logo_file = FileField(
        "Upload logo",
        validators=[Optional(), FileAllowed(["jpg", "jpeg", "png", "webp", "gif", "svg"])],
    )
    hero_title = StringField("Hero title", validators=[Optional(), Length(max=160)])
    hero_subtitle = TextAreaField("Hero subtitle", validators=[Optional(), Length(max=400)])
    show_voucher_tab = BooleanField("Show voucher redeem on portal", default=True)
    show_online_pay = BooleanField(
        "Show online Selcom pay (keep off until merchant access is ready)",
        default=False,
    )
    footer_text = StringField("Footer text", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Save appearance")


class PasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[DataRequired()])
    new_password = PasswordField(
        "New password", validators=[DataRequired(), Length(min=10, max=128)]
    )
    confirm_password = PasswordField(
        "Confirm new password", validators=[DataRequired(), Length(min=10, max=128)]
    )
    submit = SubmitField("Update password")


def _fill_package_duration_fields(form: PackageForm, minutes: int | None) -> None:
    amount, unit = minutes_to_amount_unit(minutes or 60)
    form.duration_amount.data = amount
    form.duration_unit.data = unit


def _minutes_from_package_form(form: PackageForm) -> int:
    return duration_to_minutes(form.duration_amount.data, form.duration_unit.data)


def _save_upload(file_storage, prefix: str) -> str:
    filename = secure_filename(file_storage.filename or "")
    if not filename:
        raise ValueError("Empty filename")
    ext = filename.rsplit(".", 1)[-1].lower()
    name = f"{prefix}_{secrets.token_hex(6)}.{ext}"
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], name)
    file_storage.save(path)
    return f"uploads/{name}"


@bp.route("/")
@login_required
def dashboard():
    revoke_expired_sessions()
    stats = {
        "packages": Package.query.count(),
        "active_packages": Package.query.filter_by(is_active=True).count(),
        "vouchers_unused": Voucher.query.filter_by(status="unused").count(),
        "payments_today": Payment.query.filter(
            Payment.created_at
            >= datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        ).count(),
        "payments_success": Payment.query.filter_by(status="success").count(),
        "active_sessions": sum(
            1 for s in AccessSession.query.filter_by(revoked=False).all() if s.is_active()
        ),
    }
    recent_payments = (
        Payment.query.order_by(Payment.created_at.desc()).limit(10).all()
    )
    return render_template(
        "admin/dashboard.html", stats=stats, recent_payments=recent_payments
    )


@bp.route("/customers")
@login_required
def customers():
    page = max(1, request.args.get("page", 1, type=int))
    accounts = CustomerAccount.query.order_by(CustomerAccount.created_at.desc()).paginate(
        page=page, per_page=30, error_out=False
    )
    return render_template("admin/customers.html", accounts=accounts)


@bp.route("/customers/<int:account_id>")
@login_required
def customer_detail(account_id: int):
    account = CustomerAccount.query.get_or_404(account_id)
    devices = CustomerDevice.query.filter_by(account_id=account.id).order_by(
        CustomerDevice.last_seen_at.desc()
    ).all()
    activities = CustomerActivity.query.filter_by(account_id=account.id).order_by(
        CustomerActivity.created_at.desc()
    ).limit(50).all()
    transactions = WalletTransaction.query.filter_by(account_id=account.id).order_by(
        WalletTransaction.created_at.desc()
    ).limit(50).all()
    return render_template(
        "admin/customer_detail.html", account=account, devices=devices,
        activities=activities, transactions=transactions,
    )


@bp.route("/tokens", methods=["GET", "POST"])
@login_required
def tokens():
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "mode":
            mode = request.form.get("purchasing_mode")
            if mode not in {"tokens", "voucher_payment"}:
                flash("Invalid purchasing mode.", "danger")
            else:
                SiteSetting.set("purchasing_mode", mode)
                flash("Purchasing mode updated.", "success")
        elif action == "bundle":
            name = (request.form.get("name") or "").strip()
            price = request.form.get("price_tzs", type=int)
            quantity = request.form.get("token_amount", type=int)
            if not name or not price or not quantity or price < 1 or quantity < 1:
                flash("Enter a name, valid TSh price and token quantity.", "warning")
            else:
                db.session.add(TokenBundle(name=name[:80], price_tzs=price, token_amount=quantity))
                db.session.commit()
                flash("Token bundle created.", "success")
        elif action == "bundle_toggle":
            bundle = TokenBundle.query.get_or_404(request.form.get("bundle_id", type=int))
            bundle.is_active = not bundle.is_active
            db.session.commit()
            flash("Token bundle updated.", "success")
        elif action == "credit":
            account = CustomerAccount.query.get_or_404(request.form.get("account_id", type=int))
            amount = request.form.get("amount", type=int)
            note = (request.form.get("note") or "").strip()
            try:
                credit_tokens(account.id, amount, transaction_type="manual_credit",
                              note=note or "Admin token credit", actor_admin_id=current_user.id)
                db.session.add(CustomerActivity(account_id=account.id, event_type="tokens_credited",
                                                 details=f"{amount} tokens credited by admin"))
                db.session.commit()
                flash("Tokens credited.", "success")
            except ValueError as exc:
                flash(str(exc), "warning")
        return redirect(url_for("admin.tokens"))
    bundles = TokenBundle.query.order_by(TokenBundle.sort_order, TokenBundle.id).all()
    return render_template("admin/tokens.html", bundles=bundles,
                           purchasing_mode=SiteSetting.get("purchasing_mode", "voucher_payment"))


@bp.route("/awards", methods=["GET", "POST"])
@login_required
def awards():
    if request.method == "POST":
        action = request.form.get("action", "create")
        rule_id = request.form.get("rule_id", type=int)
        rule = AwardRule.query.get(rule_id) if rule_id else None
        if action == "delete" and rule:
            rule.is_active = False
            db.session.commit()
            flash("Award rule archived; existing grants are preserved.", "success")
        else:
            name = (request.form.get("name") or "").strip()
            rule_type = request.form.get("rule_type")
            reward_type = request.form.get("reward_type")
            amount = request.form.get("reward_amount", type=int)
            if not name or rule_type not in {"device_count", "consecutive_package_days"} or reward_type not in {"tokens", "minutes"} or not amount or amount < 1:
                flash("Check the award details.", "warning")
            else:
                import json
                criteria = {"device_count": max(1, request.form.get("device_count", type=int) or 2),
                            "consecutive_days": max(2, request.form.get("consecutive_days", type=int) or 3),
                            "package_id": request.form.get("package_id", type=int)}
                if rule is None:
                    rule = AwardRule(name=name, rule_type=rule_type, reward_type=reward_type,
                                     reward_amount=amount, criteria_json=json.dumps(criteria),
                                     repeatable=request.form.get("repeatable") == "1")
                    db.session.add(rule)
                else:
                    rule.name=name; rule.rule_type=rule_type; rule.reward_type=reward_type
                    rule.reward_amount=amount; rule.criteria_json=json.dumps(criteria)
                    rule.repeatable=request.form.get("repeatable") == "1"
                    rule.is_active=True
                db.session.commit()
                flash("Award rule saved.", "success")
        return redirect(url_for("admin.awards"))
    return render_template("admin/awards.html", rules=AwardRule.query.order_by(AwardRule.created_at.desc()).all(),
                           packages=Package.query.filter_by(is_active=True).all())


@bp.route("/packages")
@login_required
def packages():
    items = Package.query.order_by(Package.sort_order.asc(), Package.id.desc()).all()
    return render_template("admin/packages.html", packages=items)


@bp.route("/packages/new", methods=["GET", "POST"])
@login_required
def package_new():
    form = PackageForm()
    if form.validate_on_submit():
        pkg = Package(
            name=form.name.data.strip(),
            description=(form.description.data or "").strip(),
            price_tzs=form.price_tzs.data,
            duration_minutes=_minutes_from_package_form(form),
            sort_order=form.sort_order.data or 0,
            is_active=bool(form.is_active.data),
        )
        db.session.add(pkg)
        db.session.commit()
        flash("Package created.", "success")
        return redirect(url_for("admin.packages"))
    return render_template("admin/package_form.html", form=form, title="New package")


@bp.route("/packages/<int:package_id>/edit", methods=["GET", "POST"])
@login_required
def package_edit(package_id: int):
    pkg = Package.query.get_or_404(package_id)
    form = PackageForm(obj=pkg)
    if request.method == "GET":
        _fill_package_duration_fields(form, pkg.duration_minutes)
    if form.validate_on_submit():
        pkg.name = form.name.data.strip()
        pkg.description = (form.description.data or "").strip()
        pkg.price_tzs = form.price_tzs.data
        pkg.duration_minutes = _minutes_from_package_form(form)
        pkg.sort_order = form.sort_order.data or 0
        pkg.is_active = bool(form.is_active.data)
        db.session.commit()
        flash("Package updated.", "success")
        return redirect(url_for("admin.packages"))
    return render_template(
        "admin/package_form.html", form=form, title="Edit package", package=pkg
    )


@bp.route("/packages/<int:package_id>/delete", methods=["POST"])
@login_required
def package_delete(package_id: int):
    pkg = Package.query.get_or_404(package_id)
    # Soft-delete preferred if vouchers/payments exist
    has_refs = pkg.vouchers.count() > 0 or pkg.payments.count() > 0
    if has_refs:
        pkg.is_active = False
        db.session.commit()
        flash("Package deactivated (it has payment/voucher history).", "warning")
    else:
        db.session.delete(pkg)
        db.session.commit()
        flash("Package deleted.", "success")
    return redirect(url_for("admin.packages"))


@bp.route("/vouchers", methods=["GET", "POST"])
@login_required
def vouchers():
    form = VoucherBatchForm()
    form.package_id.choices = [
        (p.id, f"{p.name} — {p.price_tzs} TZS / {p.duration_label()}")
        for p in Package.query.filter_by(is_active=True).order_by(Package.sort_order).all()
    ]
    if form.validate_on_submit():
        package = Package.query.get(form.package_id.data)
        if not package:
            flash("Package not found.", "danger")
        else:
            created = create_vouchers(
                package,
                form.count.data,
                created_by=current_user.id,
                note=form.note.data or "",
                expires_days=form.expires_days.data,
            )
            flash(f"Generated {len(created)} voucher(s).", "success")
            return redirect(url_for("admin.vouchers"))

    status = request.args.get("status", "unused")
    q = Voucher.query
    if status and status != "all":
        q = q.filter_by(status=status)
    items = q.order_by(Voucher.created_at.desc()).limit(200).all()
    return render_template(
        "admin/vouchers.html", form=form, vouchers=items, status=status
    )


@bp.route("/vouchers/<int:voucher_id>/revoke", methods=["POST"])
@login_required
def voucher_revoke(voucher_id: int):
    v = Voucher.query.get_or_404(voucher_id)
    if v.status == "unused":
        v.status = "revoked"
        db.session.commit()
        flash("Voucher revoked.", "success")
    else:
        flash("Only unused vouchers can be revoked.", "warning")
    return redirect(url_for("admin.vouchers"))


@bp.route("/payments")
@login_required
def payments():
    items = Payment.query.order_by(Payment.created_at.desc()).limit(100).all()
    return render_template("admin/payments.html", payments=items)


@bp.route("/payments/<int:payment_id>/mark-paid", methods=["POST"])
@login_required
def payment_mark_paid(payment_id: int):
    payment = Payment.query.get_or_404(payment_id)
    if payment.status == "success":
        flash("Already marked paid.", "info")
    else:
        fulfill_payment(payment)
        flash("Payment marked paid and access granted.", "success")
    return redirect(url_for("admin.payments"))


@bp.route("/sessions")
@login_required
def sessions():
    revoke_expired_sessions()
    items = AccessSession.query.order_by(AccessSession.started_at.desc()).limit(100).all()
    return render_template("admin/sessions.html", sessions=items)


@bp.route("/sessions/<int:session_id>/revoke", methods=["POST"])
@login_required
def session_revoke(session_id: int):
    s = AccessSession.query.get_or_404(session_id)
    if s.revoked:
        flash("Session was already revoked.", "info")
        return redirect(url_for("admin.sessions"))

    ok = revoke_session(s, reason="admin")
    if ok:
        flash(
            f"Session revoked for {s.mac_address}. Internet cut off for that device.",
            "success",
        )
    else:
        flash(
            f"Session marked revoked, but firewall revoke failed for {s.mac_address}. "
            "Run: sudo ./gateway/authorize.sh revoke " + (s.mac_address or ""),
            "warning",
        )
    return redirect(url_for("admin.sessions"))


@bp.route("/appearance", methods=["GET", "POST"])
@login_required
def appearance():
    theme = get_theme()
    form = ThemeForm(data=theme)
    form.show_voucher_tab.data = theme.get("show_voucher_tab") == "1"
    form.show_online_pay.data = theme.get("show_online_pay") == "1"

    print("REQUEST METHOD:", request.method)
    print("RAW FORM:", request.form)
    print("ONLINE PAY:", form.show_online_pay.data)

    if form.validate_on_submit():
        payload = {
            "brand_name": form.brand_name.data,
            "tagline": form.tagline.data or "",
            "support_phone": form.support_phone.data or "",
            "business_pay_number": form.business_pay_number.data or "",
            "business_pay_name": form.business_pay_name.data or "",
            "primary_color": form.primary_color.data,
            "accent_color": form.accent_color.data,
            "text_color": form.text_color.data,
            "muted_color": form.muted_color.data,
            "surface_color": form.surface_color.data,
            "background_type": form.background_type.data,
            "background_value": form.background_value.data or DEFAULTS["background_value"],
            "hero_title": form.hero_title.data or "",
            "hero_subtitle": form.hero_subtitle.data or "",
            "show_voucher_tab": "1" if form.show_voucher_tab.data else "0",
            "show_online_pay": "1" if form.show_online_pay.data else "0",
            "footer_text": form.footer_text.data or "",
        }
        if form.background_image_file.data:
            try:
                payload["background_image"] = _save_upload(
                    form.background_image_file.data, "bg"
                )
                payload["background_type"] = "image"
            except ValueError:
                flash("Could not save background image.", "danger")
        if form.logo_file.data:
            try:
                payload["logo_path"] = _save_upload(form.logo_file.data, "logo")
            except ValueError:
                flash("Could not save logo.", "danger")

        save_theme(payload)
        flash("Appearance saved. Refresh the portal to see changes.", "success")
        return redirect(url_for("admin.appearance"))

    return render_template("admin/appearance.html", form=form, theme=theme)


@bp.route("/security", methods=["GET", "POST"])
@login_required
def security():
    form = PasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash("Current password is incorrect.", "danger")
        elif form.new_password.data != form.confirm_password.data:
            flash("New passwords do not match.", "warning")
        else:
            current_user.set_password(form.new_password.data)
            db.session.commit()
            flash("Password updated.", "success")
            return redirect(url_for("admin.security"))
    return render_template("admin/security.html", form=form)