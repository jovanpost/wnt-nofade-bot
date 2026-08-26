#!/usr/bin/env python3
"""Place ONE order far from the market, then cancel it."""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

from wnt import clock, config as C
from wnt.kalshi import KalshiClient, KalshiError, _to_cents


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker")
    parser.add_argument("--no-price", type=int, default=5)
    parser.add_argument("--contracts", type=int, default=1)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    client = KalshiClient()
    print(f"Base URL: {client.base_url}")
    print(f"Exchange: {'DEMO (fake money)' if C.USE_DEMO else 'PRODUCTION (real money)'}")
    print(f"Order API shape: {C.ORDER_API}")
    if not client.authenticated:
        print("No private key loaded. Stopping.")
        return 1

    ticker = args.ticker
    if not ticker:
        events = client.get_events(C.SERIES, status="open")
        today = [e for e in events
                 if clock.event_date_from_ticker(e.get("event_ticker", ""))
                 == clock.today_ct()]
        pool = today or events
        if not pool:
            print(f"No open {C.SERIES} events. Pass --ticker.")
            return 1
        markets = [m for m in client.get_markets(pool[0]["event_ticker"])
                   if m.get("status") in ("active", "open")]
        if not markets:
            print("No active markets found.")
            return 1
        ticker = markets[0]["ticker"]

    risk = args.contracts * args.no_price / 100.0
    print(f"\nMarket   : {ticker}")
    print(f"Order    : buy NO {args.contracts} @ {args.no_price}c "
          f"(= ask YES @ {100 - args.no_price}c)")
    print(f"At risk  : ${risk:.2f}")
    if not C.USE_DEMO:
        confirm = input("\nThis places a REAL order. Type 'yes' to continue: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return 0

    coid = f"smoke-{uuid.uuid4().hex[:16]}"
    expiry = clock.expiry_epoch_seconds(clock.today_ct())

    print("\n[1] Submitting...")
    try:
        resp = client.create_no_order(
            ticker=ticker, no_price_cents=args.no_price, count=args.contracts,
            client_order_id=coid, post_only=C.POST_ONLY, expiration_epoch=expiry,
        )
    except KalshiError as exc:
        print(f"    REJECTED {exc.status}: {exc.body[:500]}")
        return 1

    order_id = resp.get("order_id")
    print(f"    accepted. order_id={order_id}")
    print(f"    fill_count={resp.get('fill_count')} remaining={resp.get('remaining_count')}")

    print("\n[2] Reading it back...")
    time.sleep(1.5)
    found = None
    for order in client.get_resting_orders():
        if order.get("order_id") == order_id:
            found = order
            break
    if not found:
        print("    NOT FOUND as resting.")
    else:
        print(json.dumps(found, indent=2, default=str)[:1600])
        outcome = found.get("outcome_side") or found.get("side")
        book_side = found.get("book_side")
        print(f"    outcome_side={outcome!r}  book_side={book_side!r}")
        if outcome == "no" or book_side == "ask":
            print("    CORRECT: this is a long-NO order.")
        else:
            print("    !!! WRONG DIRECTION. Stop.")
        exp = found.get("expiration_time") or found.get("expiration_ts")
        print(f"    expiration_time={exp}  wanted {expiry}")

    if args.keep:
        print("\n[4] --keep set, leaving the order resting.")
        return 0

    print("\n[4] Cancelling...")
    ok = client.cancel_order(order_id)
    print(f"    cancel reported: {ok}")
    time.sleep(1.5)
    still = [o for o in client.get_resting_orders() if o.get("order_id") == order_id]
    if still:
        print("    !!! STILL RESTING. Cancel it in the Kalshi UI right now.")
        return 1
    print("    verified gone.")
    print("\nSmoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
