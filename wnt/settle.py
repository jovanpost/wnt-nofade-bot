"""Keep asking Kalshi until every order row has yes/no."""
from __future__ import annotations

import logging

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


def sweep(client: KalshiClient | None = None) -> int:
    client = client or KalshiClient()
    pending = pending_rows()
    if not pending:
        return 0

    updated = 0
    notes = []
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
        filled = float(row.get("filled_contracts") or 0)
        tag = "FILL" if filled > 0 else "no fill"
        pnl_s = f" ${pnl:+.2f}" if pnl is not None else ""
        notes.append(f"{row.get('title') or ticker}: {result.upper()} ({tag}){pnl_s}")

    if updated:
        notify.send(
            "📜 <b>Settlement update</b>\n"
            + "\n".join("• " + notify.esc(n) for n in notes[:30])
        )
        store.log_activity("settle", f"updated {updated} row(s)")
    return updated
