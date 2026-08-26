#!/usr/bin/env python3
"""Nightly settlement sweep."""
from __future__ import annotations

import sys

from wnt import analytics, notify, store
from wnt.kalshi import KalshiClient


def resolve_result(client: KalshiClient, ticker: str) -> str | None:
    try:
        market = client.get_market(ticker)
    except Exception:
        return None
    if market.get("status") not in ("settled", "finalized", "closed"):
        return None
    raw = (market.get("result") or market.get("settlement_result") or "").lower()
    return raw if raw in ("yes", "no") else None


def main() -> int:
    store.init_db()
    client = KalshiClient()

    pending = [
        row for row in store.all_orders(limit=5000)
        if (row.get("filled_contracts") or 0) > 0
        and row.get("result") not in ("yes", "no")
        and not row.get("dry_run")
    ]
    print(f"{len(pending)} filled order(s) awaiting a settlement result")

    settlements: dict[str, dict] = {}
    try:
        for entry in client.get_settlements(limit=200):
            if entry.get("ticker"):
                settlements[entry["ticker"]] = entry
    except Exception as exc:
        print(f"(settlements endpoint unavailable: {exc})")

    updated = 0
    for row in pending:
        ticker = row["market_ticker"]
        record = settlements.get(ticker)
        result = None
        if record:
            raw = (record.get("market_result") or record.get("result") or "").lower()
            if raw in ("yes", "no"):
                result = raw
        if result is None:
            result = resolve_result(client, ticker)
        if result is None:
            continue

        from wnt import analytics as A
        merged = dict(row, result=result)
        pnl = A.order_pnl(merged)
        store.update_order(row["client_order_id"], result=result, realized_pnl=pnl)
        updated += 1
        print(f"  {ticker[:48]:48} -> {result.upper():3}  "
              f"${pnl:+.2f}" if pnl is not None else "")

    print(f"\nUpdated {updated} order(s).")

    stats = analytics.summarise()
    report = analytics.format_report(stats)
    print(report.replace("<b>", "").replace("</b>", "")
          .replace("<pre>", "").replace("</pre>", ""))

    if updated:
        notify.send(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
