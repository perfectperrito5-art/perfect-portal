import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    _db_url = os.getenv("DATABASE_URL", "").strip()
    if not _db_url or _db_url.startswith("sqlite:///instance/"):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{BASE_DIR / 'instance' / 'portal.db'}"
    else:
        SQLALCHEMY_DATABASE_URI = _db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    PORTAL_BASE_URL = os.getenv("PORTAL_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
    DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "Africa/Dar_es_Salaam")

    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ChangeThisStrongPassword!")

    SELCOM_BASE_URL = os.getenv(
        "SELCOM_BASE_URL", "https://apigw.selcommobile.com"
    ).rstrip("/")
    SELCOM_API_KEY = os.getenv("SELCOM_API_KEY", "")
    SELCOM_API_SECRET = os.getenv("SELCOM_API_SECRET", "")
    SELCOM_VENDOR = os.getenv("SELCOM_VENDOR", "")
    SELCOM_WEBHOOK_SECRET = os.getenv("SELCOM_WEBHOOK_SECRET", "")

    # AzamPay
    AZAMPAY_BASE_URL = os.getenv(
        "AZAMPAY_BASE_URL",
        "https://sandbox.azampay.co.tz",
    )

    AZAMPAY_AUTH_URL = os.getenv(
        "AZAMPAY_AUTH_URL",
        "https://authenticator-sandbox.azampay.co.tz",
    )

    AZAMPAY_APP_NAME = os.getenv("AZAMPAY_APP_NAME", "")
    AZAMPAY_CLIENT_ID = os.getenv("AZAMPAY_CLIENT_ID", "")
    AZAMPAY_CLIENT_SECRET = os.getenv("AZAMPAY_CLIENT_SECRET", "")
    AZAMPAY_API_KEY = os.getenv("AZAMPAY_API_KEY", "")
    AZAMPAY_CALLBACK_URL = os.getenv("AZAMPAY_CALLBACK_URL", "")
    AZAMPAY_CALLBACK_TOKEN = os.getenv(
        "AZAMPAY_CALLBACK_TOKEN",
        "",
    )

    GATEWAY_ENABLED = os.getenv("GATEWAY_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    GATEWAY_SCRIPT = os.getenv(
        "GATEWAY_SCRIPT", str(BASE_DIR / "gateway" / "authorize.sh")
    )

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    REMEMBER_COOKIE_HTTPONLY = True
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 8

    WTF_CSRF_ENABLED = os.getenv("WTF_CSRF_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
    }
    WTF_CSRF_TIME_LIMIT = 3600

    RATELIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "120/hour")
    RATELIMIT_STORAGE_URI = "memory://"

    UPLOAD_FOLDER = BASE_DIR / "app" / "static" / "uploads"
    MAX_CONTENT_LENGTH = 4 * 1024 * 1024
    ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}

    DEBUG = os.getenv("FLASK_ENV", "production") == "development"