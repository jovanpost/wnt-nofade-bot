"""
Offline test: the bot runs TWO days in a row in ONE process (no restart in between).

Why it exists: the "already cancelled today" flag was only cleared after a restart. On the second
day of a long run the 5:29 in-app cancel was skipped and the late-market sweep was switched off.
Kalshi's own expiry and the GitHub Actions cancel still removed the orders, so no money was at
risk, but two of the bot's own jobs were silently not running. A server worker never restarts.

No network, no real orders. Run from the repo root:   python scripts/offline_two_days_test.py
Reuses the fake Kalshi and fake clock from scripts/offline_fast_open_test.py.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_spec = importlib.util.spec_from_file_location("base_t", os.path.join(ROOT, "scripts", "offline_fast_open_test.py"))
t = importlib.util.module_from_spec(_spec)
sys.modules["base_t"] = t
_spec.loader.exec_module(t)

C, clock, store, strategy, KalshiError = t.C, t.clock, t.store, t.strategy, t.KalshiError
MSGS = t.MSGS
FAILS: list[str] = []
D1, D2 = "2026-09-21", "2026-09-22"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


class DayFake(t.FakeKalshi):
    """FakeKalshi for one day, plus: orders rest until cancelled, and markets can appear late."""

    def __init__(self, *a, appear: dict | None = None, **k):
        super().__init__(*a, **k)
        self.appear = appear or {}
        self.cancelled: list[str] = []

    def _appears_at(self, tk: str) -> datetime:
        return self.open_at + timedelta(seconds=self.appear[tk])

    def get_markets(self, event_ticker):
        out = super().get_markets(event_ticker)
        if event_ticker != self.event_ticker:
            return out
        for tk in self.appear:
            if self.vc.now() >= self._appears_at(tk):
                stamp = self._appears_at(tk).strftime("%Y-%m-%dT%H:%M:%S") + ".00000+00:00"
                out.append({"ticker": tk, "status": "active", "open_time": stamp, "yes_sub_title": tk[-4:]})
        return out

    def create_no_order(self, ticker, **kw):
        if ticker in self.appear and self.vc.now() < self._appears_at(ticker):
            raise KalshiError(404, '{"error":"market not found"}', "/portfolio/events/orders")
        out = super().create_no_order(ticker, **kw)
        self.posted[-1]["oid"] = out["order_id"]
        return out

    def get_resting_orders(self, series_prefix=None):
        return [{"order_id": o["oid"], "ticker": o["ticker"]} for o in self.posted if o["oid"] not in self.cancelled]

    def batch_cancel(self, order_ids):
        self.cancelled.extend(order_ids)
        return len(order_ids), []


def run_to(r, vc, date: str, target_ct: str) -> None:
    end = t.ct(date, target_ct)
    while vc.now() < end:
        remaining = (end - vc.now()).total_seconds()
        if remaining > 120:
            vc.advance(min(remaining - 100, 300))
        r._tick()


def tick_until(r, vc, date: str, target_ct: str) -> None:
    end = t.ct(date, target_ct)
    while vc.now() < end:
        r._tick()


def statuses(date: str) -> list[str]:
    return sorted(x["status"] for x in t.rows_for(date))


def cancel_msgs() -> int:
    """The report cancel_all sends when it has run ("All orders cancelled" / "CANCEL NOT VERIFIED")."""
    return len([m for m in MSGS if "All orders cancelled" in m or "CANCEL NOT VERIFIED" in m])


def main() -> int:
    C.FAST_OPEN, C.FAST_SHADOW, C.LATE_SWEEP = False, False, True
    C.DRY_RUN, C.SMOKE_LIVE = False, False
    vc = t.VClock(t.ct(D1, "11:55:00"))
    t.fresh_day(vc)                                   # once, at "process start". Never again below.

    print("\n== day one: orders at the open, cancel at 5:29 ==")
    fake1 = DayFake(vc, D1, t.ct(D1, "11:55:00"), t.ct(D1, "12:30:00"), n=5)
    r = t.make_runner(fake1, vc)
    t.run_until_handled(r, vc)
    check("day 1: 5 orders placed", len(fake1.posted) == 5, str(len(fake1.posted)))
    run_to(r, vc, D1, "17:31:00")
    check("day 1: the 5:29 in-app cancel ran", len(fake1.cancelled) == 5, str(len(fake1.cancelled)))
    check("day 1: no row left resting", "resting" not in statuses(D1), str(statuses(D1)))
    day1 = store.get_day(D1) or {}
    check("day 1: the day row has cancelled_at", bool(day1.get("cancelled_at")))
    check("day 1: one cancel report was sent", cancel_msgs() == 1, str(cancel_msgs()))

    print("\n== day two, SAME process: a late market must still be ordered, and 5:29 must still cancel ==")
    late_tk = f"{clock.event_ticker_for_date(D2)}-LATE"
    fake2 = DayFake(vc, D2, t.ct(D2, "11:55:00"), t.ct(D2, "12:30:00"), n=5, appear={late_tk: 2.5 * 3600})
    r.client = fake2                                  # the same Runner and the same STATE: no restart
    run_to(r, vc, D2, "11:54:00")
    check("overnight: nothing was ordered or cancelled", len(fake2.posted) == 0 and len(fake1.cancelled) == 5)
    t.run_until_handled(r, vc)
    check("day 2: 5 orders placed at the open", len(fake2.posted) == 5, str(len(fake2.posted)))
    run_to(r, vc, D2, "14:58:00")
    tick_until(r, vc, D2, "15:02:00")
    late = [o for o in fake2.posted if o["ticker"] == late_tk]
    check("day 2: the late market got its order (the sweep was off before v1.2.1)", len(late) == 1, str(len(fake2.posted)))
    check("day 2: every order unique", len({o["coid"] for o in fake2.posted}) == len(fake2.posted))
    run_to(r, vc, D2, "17:31:00")
    check("day 2: the 5:29 in-app cancel ran again (it was skipped before v1.2.1)",
          len(fake2.cancelled) == len(fake2.posted) == 6, f"{len(fake2.cancelled)} of {len(fake2.posted)}")
    check("day 2: no row left resting", "resting" not in statuses(D2), str(statuses(D2)))
    day2 = store.get_day(D2) or {}
    check("day 2: the day row has cancelled_at", bool(day2.get("cancelled_at")))
    check("day 2: a second cancel report was sent", cancel_msgs() == 2, str(cancel_msgs()))
    check("day 1 rows were not touched on day 2", "resting" not in statuses(D1) and len(fake1.cancelled) == 5)

    for _ in range(120):
        r._tick()
    check("after the cancel: nothing more is ordered", len(fake2.posted) == 6, str(len(fake2.posted)))

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
