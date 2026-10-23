"""Selcom Checkout API client (HMAC-SHA256 signed requests).

Secure payment model used here:
  1) create-order-minimal
  2) wallet-payment  → USSD push to customer's phone
  3) customer enters PIN on their handset (never on this portal)
  4) Selcom webhook confirms payment

Docs: https://developers.selcommobile.com/
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests
from flask import current_app

logger = logging.getLogger(__name__)


class SelcomError(Exception):
    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.payload = payload or {}


class SelcomClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        vendor: str,
        timeout: int = 45,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.vendor = vendor
        self.timeout = timeout

    @classmethod
    def from_app(cls) -> "SelcomClient":
        cfg = current_app.config
        return cls(
            base_url=cfg["SELCOM_BASE_URL"],
            api_key=cfg["SELCOM_API_KEY"],
            api_secret=cfg["SELCOM_API_SECRET"],
            vendor=cfg["SELCOM_VENDOR"],
        )

    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret and self.vendor)

    def _timestamp(self) -> str:
        # Selcom accepts ISO-8601 with timezone offset
        return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")

    def _auth_header(self) -> str:
        token = base64.b64encode(self.api_key.encode("utf-8")).decode("ascii")
        return f"SELCOM {token}"

    def _sign(self, timestamp: str, fields: dict[str, Any], signed_keys: list[str]) -> str:
        parts = [f"timestamp={timestamp}"]
        for key in signed_keys:
            value = fields.get(key, "")
            if value is None:
                value = ""
            parts.append(f"{key}={value}")
        signing_string = "&".join(parts)
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            signing_string.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    def _post(self, path: str, data: dict[str, Any], signed_keys: list[str]) -> dict:
        if not self.configured():
            raise SelcomError(
                "Selcom is not configured. Set SELCOM_API_KEY, SELCOM_API_SECRET, SELCOM_VENDOR."
            )

        timestamp = self._timestamp()
        digest = self._sign(timestamp, data, signed_keys)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self._auth_header(),
            "Digest-Method": "HS256",
            "Digest": digest,
            "Timestamp": timestamp,
            "Signed-Fields": ",".join(signed_keys),
        }
        url = f"{self.base_url}{path}"
        logger.info("Selcom POST %s order=%s", path, data.get("order_id") or data.get("transid"))
        try:
            resp = requests.post(url, json=data, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SelcomError(f"Network error talking to Selcom: {exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise SelcomError(
                f"Invalid JSON from Selcom (HTTP {resp.status_code})",
                {"body": resp.text[:500]},
            ) from exc

        if resp.status_code >= 400:
            raise SelcomError(
                payload.get("message") or f"Selcom HTTP {resp.status_code}",
                payload,
            )
        return payload

    @staticmethod
    def b64_url(url: str) -> str:
        return base64.b64encode(url.encode("utf-8")).decode("ascii")

    def create_order(
        self,
        *,
        order_id: str,
        buyer_phone: str,
        amount: int,
        buyer_name: str = "WiFi Customer",
        buyer_email: str = "wifi@portal.local",
        webhook_url: str,
        currency: str = "TZS",
    ) -> dict:
        data = {
            "vendor": self.vendor,
            "order_id": order_id,
            "buyer_email": buyer_email,
            "buyer_name": buyer_name,
            "buyer_phone": buyer_phone,
            "amount": int(amount),
            "currency": currency,
            "payment_methods": "MOBILEMONEYPULL",
            "webhook": self.b64_url(webhook_url),
            "buyer_remarks": "WiFi package",
            "merchant_remarks": "Perfect Portal",
            "no_of_items": 1,
        }
        signed = [
            "vendor",
            "order_id",
            "buyer_email",
            "buyer_name",
            "buyer_phone",
            "amount",
            "currency",
            "payment_methods",
            "webhook",
            "buyer_remarks",
            "merchant_remarks",
            "no_of_items",
        ]
        return self._post("/v1/checkout/create-order-minimal", data, signed)

    def wallet_payment(self, *, transid: str, order_id: str, msisdn: str) -> dict:
        """Trigger USSD push on the customer's phone."""
        data = {
            "transid": transid,
            "order_id": order_id,
            "msisdn": msisdn,
        }
        return self._post(
            "/v1/checkout/wallet-payment",
            data,
            ["transid", "order_id", "msisdn"],
        )

    def order_status(self, order_id: str) -> dict:
        # GET endpoints still need signed headers; use empty body fields
        timestamp = self._timestamp()
        path = f"/v1/checkout/order-status/{quote(order_id, safe='')}"
        signed_keys: list[str] = []
        digest = self._sign(timestamp, {}, signed_keys)
        headers = {
            "Accept": "application/json",
            "Authorization": self._auth_header(),
            "Digest-Method": "HS256",
            "Digest": digest,
            "Timestamp": timestamp,
            "Signed-Fields": "",
        }
        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise SelcomError(f"Failed to query order status: {exc}") from exc


def payment_success_codes(resultcode: str | int | None, result: str | None) -> bool:
    code = str(resultcode or "").strip()
    result_u = (result or "").upper()
    if result_u in {"SUCCESS", "COMPLETED", "PAID"}:
        return True
    return code in {"000", "0", "111"} and result_u == "SUCCESS"


def verify_webhook_secret(provided: str | None, expected: str | None) -> bool:
    """Optional shared-secret check for webhook URL query/header."""
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def dump_json(data: Any) -> str:
    return json.dumps(data, default=str)