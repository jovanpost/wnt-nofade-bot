"""Keep asking Kalshi until every order row has yes/no."""
from __future__ import annotations

import logging
from collections import defaultdict

from . import analytics, notify, store
from .kalshi import KalshiClient

log = logging.getLogger("wnt.settle")


def pending_rows() -> list[dict]:
    return [
        row for row in store.all_orders(limit=5000)
        if row.get("status") != "rejected"
        and row.get("result") not in ("yes", "no")
    ]


def result_from_market(market: dict) -> str | None:
    raw = (market.get("result") or market.get("settlement_result") or "").lower()
    if raw in ("yes", "no"):
        return raw
    return None


def day_pnl_summary(event_date: str) -> str:
    """Dollar P/L for one event_date. One row per ticker, fills only."""
    return analytics.day_pnl_lines(event_date).split("\n")[0]


def sweep(client: KalshiClient | None = None) -> int:
    client = client or KalshiClient()
    pending = pending_rows()
    if not pending:
        return 0

    updated = 0
    notes_by_day: dict[str, list[str]] = defaultdict(list)
    for row in pending:
        ticker = row.get("market_ticker")
        if not ticker:
            continue
        try:
            market = client.get_market(ticker)
        except Exception as exc:
            log.debug("settle %s failed: %s", ticker, exc)
            continue
        result = result_from_market(market)
        if result is None:
            continue
        merged = dict(row, result=result)
        pnl = analytics.order_pnl(merged)
        store.update_order(row["client_order_id"], result=result, realized_pnl=pnl)
        updated += 1
        day = row.get("event_date") or "?"
        if float(row.get("filled_contracts") or 0) > 0:
            notes_by_day[day].append(day)

    if updated:
        chunks = ["📜 <b>Settlement update</b>"]
        for day in sorted(set(notes_by_day) | set(notes_by_day.keys())):
            chunks.append(analytics.day_pnl_lines(day))
        notify.send("\n".join(chunks))
        store.log_activity("settle", f"updated {updated} row(s)")
    return updated
