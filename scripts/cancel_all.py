#!/usr/bin/env python3
"""Cancel every resting order. Used by GitHub Actions as a backup."""
from __future__ import annotations

import argparse
import sys
import time

from wnt import clock, config as C, notify, store
from wnt.kalshi import KalshiClient


def wait_until(hhmm: str, max_wait_seconds: int = 2700) -> None:
    target = clock._at(clock.today_ct(), hhmm)
    remaining = clock.seconds_until(target)
    if remaining <= 0:
        print(f"{hhmm} CT has already passed; cancelling immediately.")
        return
    if remaining > max_wait_seconds:
        print(f"{remaining:.0f}s until {hhmm} CT is longer than the "
              f"{max_wait_seconds}s cap. Exiting without cancelling.")
        sys.exit(0)
    print(f"Sleeping {remaining:.0f}s until {hhmm} CT "
          f"({target:%Y-%m-%d %-I:%M:%S %p %Z})...")
    if remaining > 30:
        time.sleep(remaining - 20)
    while clock.seconds_until(target) > 0:
        time.sleep(0.5)
    print(f"Now {clock.now_ct():%-I:%M:%S %p CT}. Cancelling.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-until", metavar="HH:MM")
    parser.add_argument("--series", default=C.SERIES)
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args()

    if args.wait_until:
        wait_until(args.wait_until)

    client = KalshiClient()
    if not client.authenticated:
        print("No private key loaded -- cannot cancel. This is a hard failure.")
        notify.send("🚨 <b>Backup cancel could not run</b>\nNo Kalshi key in the "
                    "GitHub Actions environment. Check your resting orders by hand.")
        return 1

    resting = client.get_resting_orders(series_prefix=args.series)
    print(f"{len(resting)} resting order(s) in {args.series}")
    for order in resting:
        print(f"  {order.get('ticker')}  id={order.get('order_id')}  "
              f"remaining={order.get('remaining_count')}")

    if not resting:
        print("Nothing to cancel.")
        notify.send("✅ Backup cancel ran: nothing was resting.", quiet=True)
        return 0

    if args.dry:
        print("--dry set, stopping here.")
        return 0

    ids = [o["order_id"] for o in resting if o.get("order_id")]
    cancelled, failed = client.batch_cancel(ids)
    print(f"cancelled={cancelled} failed={len(failed)}")

    time.sleep(2)
    leftover = client.get_resting_orders(series_prefix=args.series)
    verified = len(leftover) == 0

    try:
        store.init_db()
        store.mark_all_resting_cancelled(clock.today_ct())
        store.log_activity(
            "backup_cancel",
            f"cancelled={cancelled} remaining={len(leftover)} verified={verified}",
            level="info" if verified else "error",
        )
    except Exception as exc:
        print(f"(could not write to the database: {exc})")

    if verified:
        notify.send(f"🛡 <b>Backup cancel fired</b>\nCancelled {cancelled} "
                    f"resting order(s) at {clock.now_ct():%-I:%M:%S %p} CT.\n"
                    f"Nothing left resting.")
        print("VERIFIED: nothing resting.")
        return 0

    notify.send(f"🚨🚨 <b>BACKUP CANCEL INCOMPLETE</b>\n{len(leftover)} order(s) "
                f"STILL RESTING at {clock.now_ct():%-I:%M:%S %p} CT.\n"
                f"Open Kalshi and cancel them by hand immediately.")
    print(f"FAILED: {len(leftover)} still resting.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
