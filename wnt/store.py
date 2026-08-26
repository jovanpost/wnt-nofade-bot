"""Postgres (or sqlite fallback) storage."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, Integer, MetaData, String, Table,
    Text, and_, create_engine, func, select,
)
from sqlalchemy.engine import Engine

from . import config as C

log = logging.getLogger("wnt.store")

metadata = MetaData()

days = Table(
    "days", metadata,
    Column("event_date", String(10), primary_key=True),
    Column("event_ticker", String(64)),
    Column("detected_at", DateTime(timezone=True)),
    Column("markets_seen", Integer, default=0),
    Column("orders_placed", Integer, default=0),
    Column("orders_rejected", Integer, default=0),
    Column("collateral", Float, default=0.0),
    Column("cancelled_at", DateTime(timezone=True)),
    Column("cancel_verified", Boolean, default=False),
    Column("mode", String(32)),
    Column("notes", Text),
)

orders = Table(
    "orders", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("client_order_id", String(64), unique=True, index=True),
    Column("event_date", String(10), index=True),
    Column("event_ticker", String(64)),
    Column("market_ticker", String(128), index=True),
    Column("title", String(255)),
    Column("no_price_cents", Integer),
    Column("yes_price_cents", Integer),
    Column("contracts", Integer),
    Column("collateral", Float),
    Column("placed_at", DateTime(timezone=True)),
    Column("order_id", String(64)),
    Column("dry_run", Boolean, default=True),
    Column("mode", String(32)),
    Column("yes_bid_at_place", Integer),
    Column("yes_ask_at_place", Integer),
    Column("no_bid_at_place", Integer),
    Column("no_ask_at_place", Integer),
    Column("book_at_place", JSON),
    Column("status", String(24), default="resting"),
    Column("reject_reason", Text),
    Column("filled_contracts", Float, default=0.0),
    Column("first_fill_at", DateTime(timezone=True)),
    Column("avg_fill_price_cents", Float),
    Column("fees_cents", Float, default=0.0),
    Column("cancelled_at", DateTime(timezone=True)),
    Column("expiration_epoch", Integer),
    Column("result", String(8)),
    Column("realized_pnl", Float),
)

depth = Table(
    "depth", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime(timezone=True), index=True),
    Column("event_date", String(10), index=True),
    Column("market_ticker", String(128), index=True),
    Column("our_no_cents", Integer),
    Column("best_yes_bid", Integer),
    Column("best_no_bid", Integer),
    Column("yes_size_total", Float),
    Column("no_size_total", Float),
    Column("no_size_ahead", Float),
    Column("no_size_at_our_price", Float),
    Column("yes_size_that_would_fill_us", Float),
    Column("yes_book", JSON),
    Column("no_book", JSON),
)

fills = Table(
    "fills", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("fill_id", String(64), unique=True, index=True),
    Column("order_id", String(64), index=True),
    Column("event_date", String(10), index=True),
    Column("market_ticker", String(128)),
    Column("contracts", Float),
    Column("price_cents", Integer),
    Column("is_taker", Boolean),
    Column("fee_cents", Float),
    Column("created_at", DateTime(timezone=True)),
    Column("raw", JSON),
)

bot_state = Table(
    "bot_state", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text),
    Column("updated_at", DateTime(timezone=True)),
)

activity = Table(
    "activity", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime(timezone=True), index=True),
    Column("level", String(16)),
    Column("kind", String(48)),
    Column("message", Text),
)

_engine: Engine | None = None
_lock = threading.Lock()


def using_postgres() -> bool:
    return bool(C.DATABASE_URL)


def engine() -> Engine:
    global _engine
    with _lock:
        if _engine is not None:
            return _engine
        if C.DATABASE_URL:
            url = C.DATABASE_URL
            if url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql+psycopg2://", 1)
            elif url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
            _engine = create_engine(
                url, pool_pre_ping=True, pool_recycle=280, pool_size=3,
                max_overflow=2, future=True,
            )
        else:
            log.warning(
                "DATABASE_URL is not set -- using local sqlite at %s.",
                C.SQLITE_PATH,
            )
            _engine = create_engine(
                f"sqlite:///{C.SQLITE_PATH}",
                connect_args={"check_same_thread": False}, future=True,
            )
        return _engine


def init_db() -> None:
    metadata.create_all(engine())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def log_activity(kind: str, message: str, level: str = "info") -> None:
    try:
        with engine().begin() as conn:
            conn.execute(activity.insert().values(
                ts=_now(), level=level, kind=kind, message=message[:4000],
            ))
    except Exception as exc:
        log.error("could not write activity row: %s", exc)


def upsert_day(event_date: str, **fields: Any) -> None:
    with engine().begin() as conn:
        exists = conn.execute(
            select(days.c.event_date).where(days.c.event_date == event_date)
        ).first()
        if exists:
            conn.execute(days.update().where(
                days.c.event_date == event_date).values(**fields))
        else:
            conn.execute(days.insert().values(event_date=event_date, **fields))


def day_handled(event_date: str) -> bool:
    with engine().connect() as conn:
        row = conn.execute(
            select(days.c.orders_placed, days.c.notes)
            .where(days.c.event_date == event_date)
        ).first()
    return row is not None


def get_day(event_date: str) -> dict | None:
    with engine().connect() as conn:
        row = conn.execute(
            select(days).where(days.c.event_date == event_date)).mappings().first()
    return dict(row) if row else None


def order_exists(client_order_id: str) -> bool:
    with engine().connect() as conn:
        row = conn.execute(
            select(orders.c.id).where(orders.c.client_order_id == client_order_id)
        ).first()
    return row is not None


def record_order(**fields: Any) -> None:
    with engine().begin() as conn:
        conn.execute(orders.insert().values(**fields))


def update_order(client_order_id: str, **fields: Any) -> None:
    with engine().begin() as conn:
        conn.execute(orders.update().where(
            orders.c.client_order_id == client_order_id).values(**fields))


def update_order_by_ticker(event_date: str, market_ticker: str, **fields: Any) -> None:
    with engine().begin() as conn:
        conn.execute(orders.update().where(and_(
            orders.c.event_date == event_date,
            orders.c.market_ticker == market_ticker,
        )).values(**fields))


def orders_for_day(event_date: str) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            select(orders).where(orders.c.event_date == event_date)
            .order_by(orders.c.market_ticker)
        ).mappings().all()
    return [dict(r) for r in rows]


def mark_all_resting_cancelled(event_date: str, when: datetime | None = None) -> int:
    with engine().begin() as conn:
        result = conn.execute(orders.update().where(and_(
            orders.c.event_date == event_date,
            orders.c.status == "resting",
        )).values(status="cancelled", cancelled_at=when or _now()))
    return result.rowcount or 0


def record_depth(**fields: Any) -> None:
    with engine().begin() as conn:
        conn.execute(depth.insert().values(ts=_now(), **fields))


def record_fill(fill_id: str, **fields: Any) -> bool:
    with engine().begin() as conn:
        exists = conn.execute(
            select(fills.c.id).where(fills.c.fill_id == fill_id)).first()
        if exists:
            return False
        conn.execute(fills.insert().values(fill_id=fill_id, **fields))
    return True


def set_state(key: str, value: Any) -> None:
    payload = json.dumps(value)
    with engine().begin() as conn:
        exists = conn.execute(
            select(bot_state.c.key).where(bot_state.c.key == key)).first()
        if exists:
            conn.execute(bot_state.update().where(bot_state.c.key == key)
                         .values(value=payload, updated_at=_now()))
        else:
            conn.execute(bot_state.insert().values(
                key=key, value=payload, updated_at=_now()))


def get_state(key: str, default: Any = None) -> Any:
    try:
        with engine().connect() as conn:
            row = conn.execute(
                select(bot_state.c.value).where(bot_state.c.key == key)).first()
        return json.loads(row[0]) if row else default
    except Exception:
        return default


def is_paused() -> bool:
    return bool(get_state("paused", False))


def heartbeat() -> None:
    set_state("heartbeat", _now().isoformat())


def recent_days(limit: int = 30) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            select(days).order_by(days.c.event_date.desc()).limit(limit)
        ).mappings().all()
    return [dict(r) for r in rows]


def all_orders(limit: int = 2000) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            select(orders).order_by(orders.c.id.desc()).limit(limit)
        ).mappings().all()
    return [dict(r) for r in rows]


def depth_for_market(event_date: str, market_ticker: str) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            select(depth).where(and_(
                depth.c.event_date == event_date,
                depth.c.market_ticker == market_ticker,
            )).order_by(depth.c.ts)
        ).mappings().all()
    return [dict(r) for r in rows]


def depth_row_count() -> int:
    with engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(depth)).scalar() or 0


def recent_activity(limit: int = 100) -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            select(activity).order_by(activity.c.id.desc()).limit(limit)
        ).mappings().all()
    return [dict(r) for r in rows]
