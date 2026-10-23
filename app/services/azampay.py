"""AzamPay Checkout API client."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from flask import current_app

logger = logging.getLogger(__name__)


class AzamPayError(Exception):
    """Raised when an AzamPay request or verification fails."""

    def __init__(
        self,
        message: str,
        payload: dict | None = None,
    ):
        super().__init__(message)
        self.payload = payload or {}


class AzamPayClient:
    """Small client for the AzamPay Tanzania Checkout API."""

    def __init__(
        self,
        base_url: str,
        auth_url: str,
        app_name: str,
        client_id: str,
        client_secret: str,
        api_key: str = "",
        timeout: int = 45,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth_url = auth_url.rstrip("/")
        self.app_name = app_name
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_key = api_key
        self.timeout = timeout

    @classmethod
    def from_app(cls) -> "AzamPayClient":
        cfg = current_app.config

        return cls(
            base_url=cfg["AZAMPAY_BASE_URL"],
            auth_url=cfg["AZAMPAY_AUTH_URL"],
            app_name=cfg.get("AZAMPAY_APP_NAME", ""),
            client_id=cfg.get("AZAMPAY_CLIENT_ID", ""),
            client_secret=cfg.get("AZAMPAY_CLIENT_SECRET", ""),
            api_key=cfg.get("AZAMPAY_API_KEY", ""),
        )

    def configured(self) -> bool:
        """Return True when the required credentials are available."""

        return bool(
            self.app_name
            and self.client_id
            and self.client_secret
        )

    def _headers(self) -> dict[str, str]:
        access_token = self.get_access_token()

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        }

        if self.api_key:
            headers["X-API-KEY"] = self.api_key

        return headers

    def get_access_token(self) -> str:
        """Generate an AzamPay sandbox access token."""

        if not self.configured():
            raise AzamPayError(
                "AzamPay is not configured. "
                "Set AZAMPAY_APP_NAME, AZAMPAY_CLIENT_ID "
                "and AZAMPAY_CLIENT_SECRET."
            )

        url = f"{self.auth_url}/AppRegistration/GenerateToken"

        payload = {
            "appName": self.app_name,
            "clientId": self.client_id,
            "clientSecret": self.client_secret,
        }
    
        try:
            response = requests.post(
                url,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AzamPayError(
                f"Network error talking to AzamPay authentication: {exc}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise AzamPayError(
                f"Invalid JSON from AzamPay authentication "
                f"(HTTP {response.status_code})",
                {"body": response.text[:500]},
            ) from exc


        if response.status_code >= 400:
            raise AzamPayError(
                data.get("message")
                or f"AzamPay authentication HTTP {response.status_code}",
                data,
            )

        if not data.get("success"):
            raise AzamPayError(
                data.get("message") or "AzamPay authentication failed.",
                data,
            )

        access_token = (data.get("data") or {}).get("accessToken")

        if not access_token:
            raise AzamPayError(
                "AzamPay authentication succeeded but no access token was returned.",
                data,
            )

        return access_token

    def get_public_key(self) -> str:
        """Fetch AzamPay's RSA public key used to verify callbacks."""

        if not self.configured():
            raise AzamPayError(
                "AzamPay is not configured. "
                "Set AZAMPAY_APP_NAME, AZAMPAY_CLIENT_ID "
                "and AZAMPAY_CLIENT_SECRET."
            )

        access_token = self.get_access_token()

        url = f"{self.base_url}/azampay/v1/public-key"
    
        try:
            response = requests.get(
                url,
                params={"format": "Pem"},
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AzamPayError(
                f"Network error fetching AzamPay public key: {exc}"
            ) from exc

        if not response.text.strip():
            raise AzamPayError(
                f"AzamPay public-key endpoint returned an empty response "
                f"(HTTP {response.status_code})."
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise AzamPayError(
                "Invalid JSON from AzamPay public-key endpoint.",
                {
                    "body": response.text[:500],
                    "status_code": response.status_code,
                },
            ) from exc

        if response.status_code >= 400:
            raise AzamPayError(
                data.get("message")
                or f"AzamPay public-key HTTP {response.status_code}",
                data,
            )

        public_key = data.get("publicKey")

        if not public_key:
            raise AzamPayError(
                "AzamPay public-key response did not contain publicKey.",
                data,
            )

        return public_key

    def _post(
        self,
        path: str,
        data: dict[str, Any],
    ) -> dict:
        if not self.configured():
            raise AzamPayError(
                "AzamPay is not configured. "
                "Set AZAMPAY_APP_NAME, AZAMPAY_CLIENT_ID "
                "and AZAMPAY_CLIENT_SECRET."
            )

        url = f"{self.base_url}{path}"

        logger.info(
            "AzamPay POST %s externalId=%s",
            path,
            data.get("externalId"),
        )

        try:
            access_token = self.get_access_token()

            response = requests.post(
                url,
                json=data,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
                timeout=self.timeout,
            )

        except requests.RequestException as exc:
            raise AzamPayError(
                f"Network error talking to AzamPay: {exc}"
            ) from exc

        if not response.text.strip():
            return {
                "success": response.ok,
                "status_code": response.status_code,
                "message": "AzamPay returned an empty response body.",
            }

        try:
            payload = response.json()
        except ValueError as exc:
            raise AzamPayError(
                f"Invalid JSON from AzamPay "
                f"(HTTP {response.status_code})",
                {
                    "body": response.text[:500],
                    "status_code": response.status_code,
                },
            ) from exc

        if response.status_code >= 400:
            raise AzamPayError(
                payload.get("message")
                or f"AzamPay HTTP {response.status_code}",
                payload,
            )

        return payload

    def checkout(
        self,
        *,
        account_number: str,
        amount: int,
        external_id: str,
        provider: str,
        currency: str = "TZS",
    ) -> dict:
        """
        Start an AzamPay MNO checkout.

        Providers currently supported by the Tanzania
        Checkout API include:

            Airtel
            Tigo
            Halopesa
            Azampesa
            Mpesa
        """

        data = {
            "accountNumber": account_number,
            "amount": int(amount),
            "currency": currency,
            "externalId": external_id,
            "provider": provider,
        }

        return self._post(
            "/azampay/mno/checkout",
            data,
        )


def verify_callback_signature(
    *,
    signature: str,
    utilityref: str,
    externalreference: str,
    transactionstatus: str,
    operator: str,
    public_key_pem: str,
) -> bool:
    """
    Verify an AzamPay callback RSA/SHA-256 signature.

    According to the dedicated signature-verification section
    of the AzamPay documentation, the signed string is:

        utilityref + externalreference
        + transactionstatus + operator
    """

    if not signature or not public_key_pem:
        return False

    signed_string = (
        f"{utilityref}"
        f"{externalreference}"
        f"{transactionstatus}"
        f"{operator}"
    )

    try:
        public_key = serialization.load_pem_public_key(
            public_key_pem.encode("utf-8")
        )

        signature_bytes = base64.b64decode(signature)

        public_key.verify(
            signature_bytes,
            signed_string.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

        return True

    except Exception:
        logger.warning(
            "AzamPay callback signature verification failed",
            exc_info=True,
        )
        return False


def dump_json(data: Any) -> str:
    """Safely serialize API/callback data for database logging."""

    return json.dumps(data, default=str)