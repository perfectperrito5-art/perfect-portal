"""Tanzania mobile number helpers (Yas / Vodacom / Airtel / Halotel)."""

from __future__ import annotations

import re

# Prefix → network (after normalizing to 255…)
NETWORKS = {
    "25571": "Yas",
    "25565": "Yas",
    "25567": "Yas",
    "25574": "Yas",
    "25577": "Yas",
    "25575": "Vodacom",
    "25576": "Vodacom",
    "25578": "Airtel",
    "25568": "Airtel",
    "25569": "Airtel",
    "25562": "Halotel",
    "25561": "Halotel",
}


def normalize_tz_msisdn(raw: str) -> str | None:
    """Return 255XXXXXXXXX or None if invalid."""
    digits = re.sub(r"\D", "", (raw or "").strip())
    if digits.startswith("0") and len(digits) == 10:
        digits = "255" + digits[1:]
    elif digits.startswith("255") and len(digits) == 12:
        pass
    elif len(digits) == 9:
        digits = "255" + digits
    else:
        return None

    if not re.fullmatch(r"255[67]\d{8}", digits):
        # Tanzania mobiles typically 2556… or 2557…
        if not re.fullmatch(r"255\d{9}", digits):
            return None
    return digits


def detect_network(msisdn: str) -> str:
    msisdn = normalize_tz_msisdn(msisdn) or msisdn
    for prefix, name in NETWORKS.items():
        if msisdn.startswith(prefix):
            return name
    # Broad fallbacks by second digit group
    if msisdn.startswith("25575") or msisdn.startswith("25576"):
        return "Vodacom"
    if msisdn.startswith("25578") or msisdn.startswith("25568") or msisdn.startswith("25569"):
        return "Airtel"
    if msisdn.startswith("25571") or msisdn.startswith("25565") or msisdn.startswith("25567"):
        return "Yas"
    if msisdn.startswith("25562") or msisdn.startswith("25561"):
        return "Halotel"
    return "Mobile Money"