"""
Central configuration. Every tunable lives here and nowhere else.

Secrets resolve in this order:
  1. Streamlit secrets  (st.secrets)  -- how the app runs on Streamlit Cloud
  2. Environment variables            -- how scripts and GitHub Actions run
  3. The default in the code
"""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo


def _secret(key: str, default: str | None = None) -> str | None:
    """Read a secret from Streamlit if available, else the environment."""
    try:
        import streamlit as st  # noqa: PLC0415
        val = st.secrets.get(key)
        if val not in (None, ""):
            return str(val)
    except Exception:
        pass
    val = os.environ.get(key)
    return val if val not in (None, "") else default


def _flag(key: str, default: bool) -> bool:
    raw = _secret(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _num(key: str, default: float) -> float:
    raw = _secret(key)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


DRY_RUN = _flag("DRY_RUN", True)
USE_DEMO = _flag("USE_DEMO", False)
VERSION = "wnt-nofade-v1"

SERIES = _secret("SERIES", "KXWORLDNEWSMENTION")
NO_PRICE_CENTS = int(_num("NO_PRICE_CENTS", 26))
DOLLARS_PER_MARKET = _num("DOLLARS_PER_MARKET", 3.00)
CONTRACTS = max(0.01, round(DOLLARS_PER_MARKET / (NO_PRICE_CENTS / 100.0), 2))
MAX_MARKETS_PER_DAY = int(_num("MAX_MARKETS_PER_DAY", 25))
MAX_DAILY_COLLATERAL = _num("MAX_DAILY_COLLATERAL", 150.00)

CT = ZoneInfo("America/Chicago")
CANCEL_TIME_CT = _secret("CANCEL_TIME_CT", "17:29")
EXPIRY_TIME_CT = _secret("EXPIRY_TIME_CT", "17:28")
ACTIVE_WINDOW_START_CT = _secret("ACTIVE_WINDOW_START_CT", "11:00")
DEPTH_END_CT = _secret("DEPTH_END_CT", "18:15")

POLL_SECONDS_HOT = int(_num("POLL_SECONDS_HOT", 30))
POLL_SECONDS_DETECT = int(_num("POLL_SECONDS_DETECT", 5))
POLL_SECONDS_COLD = int(_num("POLL_SECONDS_COLD", 300))
FILL_POLL_SECONDS = int(_num("FILL_POLL_SECONDS", 120))
DEPTH_POLL_SECONDS = int(_num("DEPTH_POLL_SECONDS", 60))
DEPTH_LEVELS = int(_num("DEPTH_LEVELS", 10))

PROD_BASE = "https://external-api.kalshi.com"
DEMO_BASE = "https://external-api.demo.kalshi.co"
API_ROOT = "/trade-api/v2"
BASE_URL = DEMO_BASE if USE_DEMO else PROD_BASE

KALSHI_KEY_ID = _secret("KALSHI_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = _secret("KALSHI_PRIVATE_KEY_PATH", "")
KALSHI_PRIVATE_KEY_PEM = _secret("KALSHI_PRIVATE_KEY_PEM", "")
ORDER_API = _secret("ORDER_API", "v2")
POST_ONLY = _flag("POST_ONLY", True)
TAKE_IF_ALREADY_CHEAP = _flag("TAKE_IF_ALREADY_CHEAP", True)
USE_SERVER_SIDE_EXPIRY = _flag("USE_SERVER_SIDE_EXPIRY", True)
USER_AGENT = f"wnt-nofade-bot/{VERSION}"

DATABASE_URL = _secret("DATABASE_URL", "")
SQLITE_PATH = _secret("SQLITE_PATH", "wnt_bot.db")

TELEGRAM_TOKEN = _secret("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = _secret("TELEGRAM_CHAT_ID", "")
TELEGRAM_COMMANDS = _flag("TELEGRAM_COMMANDS", True)

BACKTEST_FILL_RATE = 0.50
BACKTEST_P_NO_GIVEN_FILL = 0.41
BACKTEST_MEAN_DAY = 16.23


def yes_price_cents() -> int:
    return 100 - NO_PRICE_CENTS


def collateral_per_market() -> float:
    return CONTRACTS * NO_PRICE_CENTS / 100.0


def summary() -> str:
    where = "DEMO (fake money)" if USE_DEMO else "PRODUCTION"
    mode = "DRY RUN (no orders sent)" if DRY_RUN else "LIVE"
    return (
        f"{VERSION} | {mode} | {where}\n"
        f"{SERIES}: buy NO @ {NO_PRICE_CENTS}c "
        f"(= ask YES @ {yes_price_cents()}c) x {CONTRACTS} contracts\n"
        f"${collateral_per_market():.2f}/market, max {MAX_MARKETS_PER_DAY} markets, "
        f"max ${MAX_DAILY_COLLATERAL:.2f} resting\n"
        f"cancel {CANCEL_TIME_CT} CT | post_only={POST_ONLY} | "
        f"take_if_cheap={TAKE_IF_ALREADY_CHEAP} | "
        f"server_expiry={USE_SERVER_SIDE_EXPIRY} | order_api={ORDER_API}"
    )
