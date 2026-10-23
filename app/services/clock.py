"""Wall-clock helpers. Sessions are stored as UTC unix seconds so duration is exact."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import current_app

from app.models.session import AccessSession


def display_tz() -> ZoneInfo:
    name = current_app.config.get("DISPLAY_TIMEZONE") or "Africa/Dar_es_Salaam"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Africa/Dar_es_Salaam")


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Historical SQLite rows were naive UTC wall-clock values.
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_unix() -> int:
    return int(now_utc().timestamp())


def session_started_unix(session: AccessSession) -> int:
    if getattr(session, "started_at_unix", None):
        return int(session.started_at_unix)
    started = as_utc(session.started_at)
    return int(started.timestamp()) if started else now_unix()


def session_expires_unix(session: AccessSession) -> int:
    if getattr(session, "expires_at_unix", None):
        return int(session.expires_at_unix)
    expires = as_utc(session.expires_at)
    return int(expires.timestamp()) if expires else 0


def seconds_remaining(session: AccessSession) -> int:
    return max(0, session_expires_unix(session) - now_unix())


def format_local(dt: datetime | None, *, with_tz: bool = True) -> str:
    utc = as_utc(dt)
    if utc is None:
        return "—"
    local = utc.astimezone(display_tz())
    stamp = local.strftime("%Y-%m-%d %H:%M")
    if not with_tz:
        return stamp
    tzname = local.tzname() or display_tz().key
    return f"{stamp} {tzname}"


def remaining_label(session: AccessSession | None) -> str:
    if session is None:
        return ""
    seconds = seconds_remaining(session)
    if seconds <= 0:
        return "expired"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        if hours:
            return f"{days}d {hours}h left"
        return f"{days}d left"
    if hours:
        if minutes:
            return f"{hours}h {minutes}m left"
        return f"{hours}h left"
    if minutes:
        return f"{minutes}m left"
    return f"{secs}s left"


def duration_to_minutes(amount: int, unit: str) -> int:
    unit = (unit or "minutes").lower()
    if unit == "hours":
        return int(amount) * 60
    if unit == "days":
        return int(amount) * 1440
    return int(amount)


def minutes_to_amount_unit(minutes: int) -> tuple[int, str]:
    minutes = int(minutes or 0)
    if minutes >= 1440 and minutes % 1440 == 0:
        return minutes // 1440, "days"
    if minutes >= 60 and minutes % 60 == 0:
        return minutes // 60, "hours"
    return minutes, "minutes"
