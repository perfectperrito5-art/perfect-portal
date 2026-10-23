"""Theme / dynamic appearance settings stored in DB."""

from __future__ import annotations

from app.models.settings import SiteSetting

DEFAULTS = {
    "brand_name": "Perfect Portal",
    "tagline": "Connect. Chill. Browse.",
    "support_phone": "",
    "business_pay_number": "",
    "business_pay_name": "",
    "primary_color": "#0F766E",
    "accent_color": "#F59E0B",
    "text_color": "#0F172A",
    "muted_color": "#64748B",
    "surface_color": "rgba(255, 255, 255, 0.78)",
    "background_type": "gradient",  # gradient | image | solid
    "background_value": "linear-gradient(160deg, #0B4F4A 0%, #134E4A 35%, #1C1917 100%)",
    "background_image": "",
    "logo_path": "",
    "hero_title": "Wi‑Fi that just works",
    "hero_subtitle": "Choose a package, pay via the attendant, then redeem your voucher.",
    "show_voucher_tab": "1",
    "show_online_pay": "0",  # Azampay — off until is ready
    "footer_text": "Pay · get a voucher · redeem · browse",
}


def get_theme() -> dict[str, str]:
    theme = dict(DEFAULTS)
    rows = SiteSetting.query.filter(SiteSetting.key.in_(list(DEFAULTS.keys()))).all()
    for row in rows:
        theme[row.key] = row.value
    return theme


def save_theme(data: dict[str, str]) -> dict[str, str]:
    for key in DEFAULTS:
        if key in data and data[key] is not None:
            SiteSetting.set(key, str(data[key]))
    return get_theme()


def ensure_defaults() -> None:
    for key, value in DEFAULTS.items():
        if SiteSetting.query.filter_by(key=key).first() is None:
            SiteSetting.set(key, value)

    # Soft-migrate old Selcom-era copy to voucher-first wording
    migrations = {
        "hero_subtitle": (
            "Pick a package, pay on your phone, and you are online.",
            DEFAULTS["hero_subtitle"],
        ),
        "footer_text": (
            "Secure payments via Selcom · PIN stays on your phone",
            DEFAULTS["footer_text"],
        ),
    }
    for key, (old, new) in migrations.items():
        row = SiteSetting.query.filter_by(key=key).first()
        if row and row.value == old:
            SiteSetting.set(key, new)