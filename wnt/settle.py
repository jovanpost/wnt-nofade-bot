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
    """Dollar P/L and % vs cash spent on settled fills for one event_date."""
    rows = store.orders_for_day(event_date)
    pnl_total = 0.0
    risked = 0.0
    n_settled = 0
    for row in rows:
        if row.get("status") == "rejected":
            continue
        filled = float(row.get("filled_contracts") or 0)
        if filled <= 0 or row.get("result") not in ("yes", "no"):
            continue
        pnl = analytics.order_pnl(row)
        if pnl is None:
            continue
        price = float(row.get("avg_fill_price_cents") or row.get("no_price_cents") or 0)
        pnl_total += pnl
        risked += filled * price / 100.0
        n_settled += 1
    if n_settled == 0:
        return f"{event_date}: no settled fills yet"
    pct = (100.0 * pnl_total / risked) if risked else 0.0
    return (
        f"{event_date}: {pnl_total:+.2f} dollars "
        f"({pct:+.1f}% on ${risked:.2f} filled, {n_settled} name(s))"
    )


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
        filled = float(row.get("filled_contracts") or 0)
        tag = "FILL" if filled > 0 else "no fill"
        pnl_s = f" ${pnl:+.2f}" if pnl is not None else ""
        day = row.get("event_date") or "?"
        notes_by_day[day].append(
            f"{row.get('title') or ticker}: {result.upper()} ({tag}){pnl_s}"
        )

    if updated:
        chunks = ["📜 <b>Settlement update</b>"]
        for day in sorted(notes_by_day):
            chunks.append(day_pnl_summary(day))
            chunks.extend("• " + notify.esc(n) for n in notes_by_day[day][:30])
        notify.send("\n".join(chunks))
        store.log_activity("settle", f"updated {updated} row(s)")
    return updated
