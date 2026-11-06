from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask

from app.config import Config
from app.extensions import csrf, db, limiter, login_manager


def create_app(config_object: type = Config) -> Flask:
    app = Flask(
        __name__,
        instance_relative_config=True,
        static_folder="static",
        template_folder="templates",
    )
    app.config.from_object(config_object)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    _configure_logging(app)
    _init_extensions(app)
    _register_blueprints(app)
    _register_context(app)
    _bootstrap_db(app)
    _recover_interrupted_token_purchases(app)
    _start_session_janitor(app)

    return app


def _configure_logging(app: Flask) -> None:
    logging.basicConfig(
        level=logging.DEBUG if app.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _init_extensions(app: Flask) -> None:
    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "warning"

    from flask import flash, redirect
    from flask_wtf.csrf import CSRFError

    from app.models.user import AdminUser

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(AdminUser, int(user_id))

    @app.errorhandler(CSRFError)
    def _csrf_error(exc: CSRFError):
        # Captive phones often hit this when the page host ≠ portal IP.
        flash(
            "Please open the portal again, then redeem your voucher. "
            "(Session expired or wrong page address.)",
            "warning",
        )
        base = (app.config.get("PORTAL_BASE_URL") or "").rstrip("/") or "/"
        return redirect(base if base.endswith("5000") or base.startswith("http") else "/"), 302


def _register_blueprints(app: Flask) -> None:
    from app.blueprints.admin import bp as admin_bp
    from app.blueprints.api import bp as api_bp
    from app.blueprints.auth import bp as auth_bp
    from app.blueprints.portal import bp as portal_bp

    app.register_blueprint(portal_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(api_bp, url_prefix="/api")

    # Captive portal probes: already-paid devices get success; others → splash
    @app.route("/generate_204")
    @app.route("/gen_204")
    @app.route("/hotspot-detect.html")
    @app.route("/library/test/success.html")
    @app.route("/ncsi.txt")
    @app.route("/connecttest.txt")
    @app.route("/success.txt")
    @app.route("/portal-check")
    @app.route("/redirect")
    def captive_probes():
        from flask import redirect, request, url_for

        from app.services.access import (
            ensure_gateway_access,
            find_active_session,
            lookup_mac_for_ip,
        )

        # This endpoint is reached directly by captive clients. Do not trust
        # X-Forwarded-For here: a client can spoof it and impersonate another
        # device when no trusted reverse proxy is in front.
        ip = (request.remote_addr or "").strip()
        mac = lookup_mac_for_ip(ip)
        active = find_active_session(mac)
        if active:
            ensure_gateway_access(active)
            path = request.path
            if path.endswith("hotspot-detect.html"):
                return "Success", 200, {"Content-Type": "text/html"}
            if path.endswith("success.html") or path.endswith("success.txt"):
                return "Success", 200, {"Content-Type": "text/plain"}
            if path.endswith("ncsi.txt") or path.endswith("connecttest.txt"):
                return "Microsoft NCSI", 200, {"Content-Type": "text/plain"}
            return "", 204

        # Use the canonical portal host instead of a captive-probe hostname.
        # Relative redirects can leave the browser on connectivitycheck.*.
        portal_base = (app.config.get("PORTAL_BASE_URL") or "").rstrip("/")
        portal_url = f"{portal_base}{url_for('portal.home')}" if portal_base else url_for("portal.home")
        return redirect(portal_url, code=302)


def _register_context(app: Flask) -> None:
    @app.context_processor
    def inject_theme():
        from app.services.theme import get_theme

        # Always use the configured portal URL for assets/links.
        # After unlock, captive hostnames (e.g. connectivitycheck.gstatic.com)
        # resolve to the real internet — relative /connected links then fail.
        portal_base = app.config.get("PORTAL_BASE_URL", "").rstrip("/") or ""

        from app.services.clock import format_local, remaining_label

        try:
            theme = get_theme()
        except Exception:
            from app.services.theme import DEFAULTS

            theme = dict(DEFAULTS)

        return {
            "theme": theme,
            "portal_base": portal_base,
            "format_local": format_local,
            "remaining_label": remaining_label,
        }


def _bootstrap_db(app: Flask) -> None:
    from app.models import AdminUser, Package, TokenBundle, TokenPurchase, FreeTrialClaim, AwardRule, AwardGrant, PackagePurchaseAttempt  # noqa: F401
    from app.services.theme import ensure_defaults

    with app.app_context():
        db.create_all()
        # Bring customer identity schema forward without replacing existing records.
        _migrate_customer_identity_columns(app)
        # Add every column referenced by ORM queries before running data backfills.
        _migrate_purchase_recovery_columns(app)
        _migrate_token_columns(app)
        _migrate_session_unix_columns(app)
        ensure_defaults()
        from app.models.settings import SiteSetting
        if SiteSetting.query.filter_by(key="purchasing_mode").first() is None:
            SiteSetting.set("purchasing_mode", "voucher_payment")
        if TokenBundle.query.count() == 0:
            db.session.add_all([
                TokenBundle(name="Starter", price_tzs=500, token_amount=50, sort_order=1),
                TokenBundle(name="Standard", price_tzs=1000, token_amount=100, sort_order=2),
                TokenBundle(name="Plus", price_tzs=3000, token_amount=350, sort_order=3),
                TokenBundle(name="Value", price_tzs=6000, token_amount=750, sort_order=4),
            ])
            db.session.commit()

        if not AdminUser.query.filter_by(username=app.config["ADMIN_USERNAME"]).first():
            admin = AdminUser(username=app.config["ADMIN_USERNAME"])
            admin.set_password(app.config["ADMIN_PASSWORD"])
            db.session.add(admin)
            db.session.commit()
            app.logger.info("Created default admin user: %s", app.config["ADMIN_USERNAME"])

        if Package.query.count() == 0:
            samples = [
                Package(
                    name="Quick Chill",
                    description="Perfect for a short scroll.",
                    price_tzs=500,
                    duration_minutes=60,
                    sort_order=1,
                ),
                Package(
                    name="Half Day",
                    description="Work, stream, relax.",
                    price_tzs=1500,
                    duration_minutes=360,
                    sort_order=2,
                ),
                Package(
                    name="Full Day",
                    description="All-day access.",
                    price_tzs=2500,
                    duration_minutes=1440,
                    sort_order=3,
                ),
            ]
            db.session.add_all(samples)
            db.session.commit()
            app.logger.info("Seeded sample packages.")


def _migrate_session_unix_columns(app: Flask) -> None:
    """Add unix timestamp columns to existing SQLite DBs and backfill them."""
    from sqlalchemy import inspect, text

    from app.services.clock import as_utc

    insp = inspect(db.engine)
    if "access_sessions" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("access_sessions")}
    with db.engine.begin() as conn:
        if "started_at_unix" not in cols:
            conn.execute(text("ALTER TABLE access_sessions ADD COLUMN started_at_unix INTEGER"))
        if "expires_at_unix" not in cols:
            conn.execute(
                text("ALTER TABLE access_sessions ADD COLUMN expires_at_unix INTEGER")
            )

    from app.models.session import AccessSession

    updated = 0
    for row in AccessSession.query.all():
        changed = False
        if not row.started_at_unix and row.started_at:
            utc = as_utc(row.started_at)
            if utc:
                row.started_at_unix = int(utc.timestamp())
                changed = True
        if not row.expires_at_unix and row.expires_at:
            utc = as_utc(row.expires_at)
            if utc:
                row.expires_at_unix = int(utc.timestamp())
                changed = True
        if changed:
            updated += 1
    if updated:
        db.session.commit()
        app.logger.info("Backfilled unix timestamps on %s session(s).", updated)


def _start_session_janitor(app: Flask) -> None:
    """Every 30s: cut expired firewall entries and refresh remaining time."""
    import os
    import threading

    # Flask debug reloader parent should not start a second loop.
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return

    # Expiry enforcement remains frequent; gateway reconciliation runs on a
    # separate, slower cadence so firewall restarts or lost nft state recover.
    expiry_interval = max(10, int(app.config.get("SESSION_JANITOR_INTERVAL", 30)))
    gateway_interval = max(expiry_interval, int(app.config.get("GATEWAY_RECONCILE_INTERVAL", 300)))
    state = {"last_gateway_sync": 0.0}

    def _tick():
        import time
        with app.app_context():
            try:
                from app.services.access import revoke_expired_sessions
                revoke_expired_sessions()
                from app.services.tokens import recover_pending_package_purchases
                recover_pending_package_purchases(limit=50)
                now = time.monotonic()
                if app.config.get("GATEWAY_ENABLED") and (
                    not state["last_gateway_sync"]
                    or now - state["last_gateway_sync"] >= gateway_interval
                ):
                    from app.services.access import sync_active_sessions_to_gateway
                    sync_active_sessions_to_gateway()
                    state["last_gateway_sync"] = now
            except Exception:
                # Keep the last successful sync timestamp unchanged so a
                # failed reconciliation is retried on the next janitor tick.
                app.logger.exception("Session janitor failed")
        timer = threading.Timer(expiry_interval, _tick)
        timer.daemon = True
        timer.start()

    timer = threading.Timer(expiry_interval, _tick)
    timer.daemon = True
    timer.start()

def _migrate_customer_identity_columns(app: Flask) -> None:
    """Add display-name support and enforce one device per customer where safe."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if "customer_accounts" in tables:
        columns = {column["name"] for column in inspector.get_columns("customer_accounts")}
        if "display_name" not in columns:
            with db.engine.begin() as connection:
                connection.execute(text(
                    "ALTER TABLE customer_accounts ADD COLUMN display_name VARCHAR(40) NOT NULL DEFAULT 'Guest'"
                ))
            app.logger.info("Added display_name to existing customer accounts.")

    if "customer_devices" in tables:
        with db.engine.begin() as connection:
            duplicates = connection.execute(text(
                "SELECT account_id FROM customer_devices GROUP BY account_id HAVING COUNT(*) > 1 LIMIT 1"
            )).first()
            if duplicates:
                app.logger.warning(
                    "One-device-per-customer unique index was not added: legacy duplicate device ownership exists."
                )
            else:
                connection.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_device_one_per_account_idx ON customer_devices (account_id)"
                ))

    # Keep legacy rule/grant records for audit, but stop obsolete linked-device
    # rules from being eligible for future rewards.
    if "award_rules" in tables:
        with db.engine.begin() as connection:
            connection.execute(text(
                "UPDATE award_rules SET is_active = 0 WHERE rule_type = 'device_count'"
            ))


def _migrate_token_columns(app: Flask) -> None:
    """Add token pricing to existing package tables without resetting customer data."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "packages" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("packages")}
    if "token_cost" not in columns:
        with db.engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE packages ADD COLUMN token_cost INTEGER NOT NULL DEFAULT 0"
            ))
        app.logger.info("Added token_cost to existing packages table.")


def _migrate_purchase_recovery_columns(app: Flask) -> None:
    """Add the nullable session-to-purchase link to existing installations."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    if "access_sessions" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("access_sessions")}
    if "purchase_attempt_id" not in columns:
        with db.engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE access_sessions ADD COLUMN purchase_attempt_id INTEGER"
            ))
        app.logger.info("Added purchase_attempt_id to access_sessions.")


def _recover_interrupted_token_purchases(app: Flask) -> None:
    """Run one bounded recovery pass after the database is ready at startup."""
    with app.app_context():
        try:
            from app.services.tokens import recover_pending_package_purchases
            result = recover_pending_package_purchases(limit=100, force=True)
            if any(result.values()):
                app.logger.info("Recovered interrupted token purchases: %s", result)
        except Exception:
            app.logger.exception("Startup token-purchase recovery failed; janitor will retry.")
