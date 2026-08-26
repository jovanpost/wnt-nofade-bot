#!/usr/bin/env python3
"""Read-only pre-flight. Places nothing."""
from __future__ import annotations

import sys

from wnt import clock, config as C, store
from wnt.kalshi import KalshiClient, book_metrics


def line(char="-", n=72):
    print(char * n)


def main() -> int:
    print(C.summary())
    line("=")

    client = KalshiClient()
    print(f"Base URL             : {client.base_url}")
    print(f"Key loaded           : {client.authenticated}")
    if not client.authenticated:
        print("\n!! No usable private key.")
        print("   Market-data checks below will still run.")
    line()

    if client.authenticated:
        try:
            balance = client.get_balance()
            cash = (balance.get("balance") or 0) / 100.0
            print(f"AUTH OK. Cash balance: ${cash:.2f}")
            need = C.collateral_per_market() * C.MAX_MARKETS_PER_DAY
            print(f"Worst-case resting collateral: ${need:.2f}")
            if cash < need:
                print(f"  !! ${need - cash:.2f} short of worst case.")
        except Exception as exc:
            print(f"AUTH FAILED: {exc}")
            return 1
        line()
        try:
            limits = client.get_account_limits()
            print(f"API tier             : {limits.get('usage_tier')}")
            print(f"Read budget          : {limits.get('read')}")
            print(f"Write budget         : {limits.get('write')}")
        except Exception as exc:
            print(f"Could not read account limits: {exc}")
        line()

    print("FEE STRUCTURE")
    try:
        series = client.get_series(C.SERIES)
        for key in ("ticker", "title", "category", "fee_type",
                    "fee_multiplier", "maker_fee_type", "maker_fee_multiplier"):
            if key in series:
                print(f"  {key:22}: {series[key]}")
    except Exception as exc:
        print(f"  Series lookup failed: {exc}")
    line()

    print(f"TODAY IS {clock.today_ct()} ({clock.now_ct():%-I:%M %p} CT)")
    print(f"Cancel deadline      : {clock.cancel_deadline(clock.today_ct())}")
    try:
        events = client.get_events(C.SERIES, status="open")
        print(f"Open events in {C.SERIES}: {len(events)}")
        for event in events[:5]:
            ticker = event.get("event_ticker")
            print(f"  {ticker}  ->  {clock.event_date_from_ticker(ticker)}")
        today_events = [e for e in events
                        if clock.event_date_from_ticker(e.get("event_ticker", ""))
                        == clock.today_ct()]
        if today_events:
            event_ticker = today_events[0]["event_ticker"]
            markets = [m for m in client.get_markets(event_ticker)
                       if m.get("status") in ("active", "open")]
            print(f"\nToday's event {event_ticker}: {len(markets)} active markets")
            for market in markets[:5]:
                book = client.get_orderbook(market["ticker"], depth=10)
                metrics = book_metrics(book, C.NO_PRICE_CENTS)
                print(f"  {(market.get('yes_sub_title') or market['ticker'])[:34]:34} "
                      f"yes_bid={metrics['best_yes_bid']} "
                      f"no_bid={metrics['best_no_bid']}")
        else:
            print("\nNo event for today yet.")
    except Exception as exc:
        print(f"Market data failed: {exc}")
    line()

    try:
        store.init_db()
        backend = "POSTGRES" if store.using_postgres() else "SQLITE (local only!)"
        print(f"Storage              : {backend}")
        print(f"Depth rows stored    : {store.depth_row_count():,}")
    except Exception as exc:
        print(f"Storage check FAILED : {exc}")
        return 1

    line("=")
    print("Pre-flight complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
