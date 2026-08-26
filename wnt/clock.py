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
    """The hard 5:29 PM CT moment. Non-negotiable."""
    return _at(date_str, C.CANCEL_TIME_CT)


def depth_deadline(date_str: str) -> datetime:
    return _at(date_str, C.DEPTH_END_CT)


def in_active_window(when: datetime | None = None) -> bool:
    """True between the morning start and the cancel time -- poll hard here."""
    when = when or now_ct()
    d = when.strftime("%Y-%m-%d")
    return _at(d, C.ACTIVE_WINDOW_START_CT) <= when <= cancel_deadline(d)


def seconds_until(target: datetime) -> float:
    return (target - now_ct()).total_seconds()


def expiry_epoch_seconds(date_str: str) -> int:
    """Unix seconds for the cancel deadline -- handed to Kalshi as a
    server-side order expiry so orders die even if this app does."""
    return int(cancel_deadline(date_str).timestamp())


def event_date_from_ticker(event_ticker: str) -> str | None:
    """'KXWORLDNEWSMENTION-26AUG26' -> '2026-08-26'.

    Returns None if the ticker doesn't parse, which the caller must treat as
    'not today' rather than guessing.
    """
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
    "now_ct", "today_ct", "cancel_deadline", "depth_deadline", "in_active_window",
    "seconds_until", "expiry_epoch_seconds", "event_date_from_ticker", "fmt",
    "parse_api_time", "timedelta",
]
