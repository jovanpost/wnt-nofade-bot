"""Everything time-related. All decisions are made in US Central Time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import config as C


def now_ct() -> datetime:
    return datetime.now(timezone.utc).astimezone(C.CT)


def today_ct() -> str:
    return now_ct().strftime("%Y-%m-%d")


def _at(date_str: str, hhmm: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=C.CT)


def cancel_deadline(date_str: str) -> datetime:
    """Streamlit / GitHub cancel-and-verify at 5:29 CT."""
    return _at(date_str, C.CANCEL_TIME_CT)


def expiry_deadline(date_str: str) -> datetime:
    """Kalshi kills the order itself at 5:28 CT."""
    return _at(date_str, C.EXPIRY_TIME_CT)


def depth_deadline(date_str: str) -> datetime:
    return _at(date_str, C.DEPTH_END_CT)


def in_active_window(when: datetime | None = None) -> bool:
    """True between the morning start and the cancel time."""
    when = when or now_ct()
    d = when.strftime("%Y-%m-%d")
    return _at(d, C.ACTIVE_WINDOW_START_CT) <= when <= cancel_deadline(d)


def seconds_until(target: datetime) -> float:
    return (target - now_ct()).total_seconds()


def expiry_epoch_seconds(date_str: str) -> int:
    """Unix seconds handed to Kalshi as server-side order expiry."""
    return int(expiry_deadline(date_str).timestamp())


def event_date_from_ticker(event_ticker: str) -> str | None:
    """'KXWORLDNEWSMENTION-26AUG26' -> '2026-08-26'."""
    try:
        stamp = event_ticker.split("-")[1]
        return datetime.strptime("20" + stamp, "%Y%b%d").strftime("%Y-%m-%d")
    except Exception:
        return None


def fmt(when: datetime | None) -> str:
    if when is None:
        return "never"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(C.CT).strftime("%-I:%M:%S %p CT")


def parse_api_time(raw: str | None) -> datetime | None:
    """Kalshi returns RFC3339 with a trailing Z."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


__all__ = [
    "now_ct", "today_ct", "cancel_deadline", "expiry_deadline", "depth_deadline",
    "in_active_window", "seconds_until", "expiry_epoch_seconds",
    "event_date_from_ticker", "fmt", "parse_api_time", "timedelta",
]
