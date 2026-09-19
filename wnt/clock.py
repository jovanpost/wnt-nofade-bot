"""Everything time-related. All decisions are made in US Central Time."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import config as C


def now_ct() -> datetime:
    return datetime.now(timezone.utc).astimezone(C.CT)


def today_ct() -> str:
    return now_ct().strftime("%Y-%m-%d")


def _at(date_str: str, hhmm: str) -> datetime:
    parts = [int(x) for x in hhmm.split(":")]
    hh, mm = parts[0], parts[1]
    ss = parts[2] if len(parts) > 2 else 0
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=C.CT)

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


_FRACTION = re.compile(r"\.(\d+)")


def parse_api_time(raw: str | None) -> datetime | None:
    """Kalshi returns RFC3339, sometimes with odd fractional seconds
    (like '...:13.83216+00:00'). Python 3.9 fromisoformat only accepts 3 or 6
    fraction digits, so pad or trim the fraction to 6 first."""
    if not raw:
        return None
    text = str(raw).strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def event_ticker_for_date(date_str: str) -> str:
    """'2026-09-18' -> 'KXWORLDNEWSMENTION-26SEP18'."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{C.SERIES}-{d.strftime('%y%b%d').upper()}"


def fmt_precise(when: datetime | None) -> str:
    """Like fmt() but with milliseconds, for the timing logs."""
    if when is None:
        return "never"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone(C.CT)
    return (local.strftime("%-I:%M:%S") + f".{local.microsecond // 1000:03d}"
            + local.strftime(" %p CT"))


__all__ = [
    "now_ct", "today_ct", "cancel_deadline", "expiry_deadline", "depth_deadline",
    "in_active_window", "seconds_until", "expiry_epoch_seconds",
    "event_date_from_ticker", "event_ticker_for_date", "fmt", "fmt_precise",
    "parse_api_time", "timedelta",
]
