"""
Offline test for FAST_OPEN. Sends NO real orders and never touches Kalshi.

It uses a fake Kalshi, a fake clock, and a throwaway sqlite file.
Run it from the repo root:   python scripts/offline_fast_open_test.py
It prints PASS or FAIL for every check and exits with code 1 if anything failed.
"""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp()
os.environ["SQLITE_PATH"] = os.path.join(_tmp, "offline_test.db")
os.environ.pop("DATABASE_URL", None)
os.environ["FAST_OPEN"] = "true"
os.environ["FAST_MAX_ORDERS_PER_SEC"] = "1000"   # pacing has its own test below
os.environ["TELEGRAM_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""

from wnt import clock, config as C, notify, settle, store, strategy  # noqa: E402
from wnt.kalshi import KalshiError  # noqa: E402

_REAL_NOW_CT = clock.now_ct
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


# ----------------------------------------------------------------------------
# fake clock + fake Kalshi
# ----------------------------------------------------------------------------
class VClock:
    def __init__(self, start: datetime):
        self.t = start

    def now(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=float(seconds))


class FakeKalshi:
    authenticated = True

    def __init__(self, vc: VClock, event_date: str, listed_at: datetime, open_at: datetime,
                 n: int = 18, late: dict | None = None, events_lag: float = 0.0,
                 flip_lag: float = 0.0, extra_at_open: int = 0, vanish: tuple = (),
                 accept_delay: float = 0.0, bad: tuple = (), rng=None,
                 transient: float = 0.0, ghost: float = 0.0):
        self.vc = vc
        self.event_ticker = clock.event_ticker_for_date(event_date)
        self.listed_at, self.open_at = listed_at, open_at
        self.tickers = [f"{self.event_ticker}-W{i:02d}" for i in range(n)]
        self.extras = [f"{self.event_ticker}-X{i:02d}" for i in range(extra_at_open)]
        self.vanish = set(self.tickers[i] for i in vanish)
        self.late = late or {}
        self.events_lag = events_lag
        self.flip_lag = flip_lag
        self.accept_delay = accept_delay
        self.bad = set(self.tickers[i] for i in bad)
        self.rng, self.transient, self.ghost = rng, transient, ghost
        self.orders: dict[str, dict] = {}
        self.posted: list[dict] = []
        self.lock = threading.Lock()
        self.probe_calls = 0

    def _flip_time(self, ticker: str) -> datetime:
        return self.open_at + timedelta(seconds=self.flip_lag + self.late.get(ticker, 0.0))

    def status_of(self, ticker: str) -> str:
        return "active" if self.vc.now() >= self._flip_time(ticker) else "initialized"

    def get_markets(self, event_ticker):
        now = self.vc.now()
        if event_ticker != self.event_ticker or now < self.listed_at:
            return []
        stamp = self.open_at.strftime("%Y-%m-%dT%H:%M:%S") + ".00000+00:00"  # odd fraction on purpose
        out = []
        for i, t in enumerate(self.tickers):
            if t in self.vanish and now >= self.open_at:
                continue
            out.append({"ticker": t, "status": self.status_of(t), "open_time": stamp,
                        "yes_sub_title": f"Word {i}"})
        if now >= self.open_at:
            for i, t in enumerate(self.extras):
                out.append({"ticker": t, "status": "active", "open_time": stamp,
                            "yes_sub_title": f"Extra {i}"})
        return out

    def get_events(self, series, status="open"):
        now = self.vc.now()
        if status == "open" and now >= self.open_at + timedelta(seconds=self.events_lag):
            return [{"event_ticker": self.event_ticker}]
        if status == "unopened" and self.listed_at <= now < self.open_at:
            return [{"event_ticker": self.event_ticker}]
        return []

    def request(self, method, endpoint, params=None, body=None, auth=True, retries=4, timeout=20):
        if endpoint == "/events":
            return {"events": self.get_events(C.SERIES, (params or {}).get("status", "open"))}
        self.probe_calls += 1
        ticker = endpoint.rsplit("/", 1)[-1]
        return {"market": {"ticker": ticker, "status": self.status_of(ticker)}}

    def get_balance(self):
        return {"balance": 1_000_000}

    def get_orderbook(self, ticker, depth=10):
        return {"yes": [], "no": []}

    def get_fills(self, ticker=None, limit=200):
        return []

    def create_no_order(self, ticker, no_price_cents, count, client_order_id,
                        post_only=True, expiration_epoch=None):
        with self.lock:
            now = self.vc.now()
            if ticker in self.bad:
                raise KalshiError(400, '{"error":"invalid price"}', "/portfolio/events/orders")
            if ticker in self.vanish and now >= self.open_at:
                raise KalshiError(404, '{"error":"market not found"}', "/portfolio/events/orders")
            is_extra = ticker in self.extras
            flip = self.open_at if is_extra else self._flip_time(ticker)
            if now < flip + timedelta(seconds=self.accept_delay):
                raise KalshiError(400, '{"error":"market not open"}', "/portfolio/events/orders")
            if client_order_id in self.orders:
                raise KalshiError(409, '{"error":"ORDER_ALREADY_EXISTS"}', "/portfolio/events/orders")
            if self.rng and self.rng.random() < self.transient:
                raise KalshiError(503, "unavailable", "/portfolio/events/orders")
            order = {"ticker": ticker, "coid": client_order_id, "no_price": no_price_cents,
                     "count": count, "post_only": post_only, "expiry": expiration_epoch,
                     "virtual_time": now, "mono": time.monotonic()}
            self.orders[client_order_id] = order
            self.posted.append(order)
            if self.rng and self.rng.random() < self.ghost:
                raise RuntimeError("timeout AFTER Kalshi accepted the order")
            return {"order_id": f"oid-{len(self.posted)}", "client_order_id": client_order_id,
                    "fill_count": 0.0, "remaining_count": count,
                    "avg_fill_price_cents": None, "fee_cents": None, "raw": {}}


# ----------------------------------------------------------------------------
# plumbing
# ----------------------------------------------------------------------------
MSGS: list[str] = []
CALLS: dict[str, int] = {}


def _count(module, name):
    original = getattr(module, name)

    def wrapper(*a, **k):
        CALLS[name] = CALLS.get(name, 0) + 1
        return original(*a, **k)
    setattr(module, name, wrapper)


notify.send = lambda message, quiet=False: MSGS.append(message) or True
settle.sweep = lambda client: None
for _name in ("orders_for_day", "is_paused", "day_handled", "order_exists", "get_day"):
    _count(store, _name)


def ct(date_str: str, hhmmss: str) -> datetime:
    d = datetime.strptime(date_str + " " + hhmmss, "%Y-%m-%d %H:%M:%S")
    return d.replace(tzinfo=C.CT).astimezone(timezone.utc)


def fresh_day(vc: VClock | None) -> None:
    store.metadata.drop_all(store.engine())
    store.init_db()
    MSGS.clear()
    CALLS.clear()
    strategy.STATE.update(running=False, active_event=None, active_date=None, last_poll=None,
                          orders_today=0, fills_today=0, last_error=None, cancelled_today=False,
                          last_settle=None)
    if vc is not None:
        clock.now_ct = lambda: vc.now().astimezone(C.CT)


def make_runner(fake: FakeKalshi, vc: VClock) -> strategy.Runner:
    r = strategy.Runner(client=fake)
    r._utcnow = vc.now
    r._wait = vc.advance
    r._sleep = vc.advance
    return r


def run_until_handled(r: strategy.Runner, vc: VClock, limit_seconds: float = 3 * 3600) -> None:
    start = vc.now()
    while not strategy.STATE["active_event"]:
        if (vc.now() - start).total_seconds() > limit_seconds:
            break
        r._tick()


def rows_for(date_str: str) -> list[dict]:
    return store.orders_for_day(date_str)


# ----------------------------------------------------------------------------
# tests
# ----------------------------------------------------------------------------
HISTORY = [  # (date, listed CT, opened CT) -- from the real days
    ("2026-09-18", "13:55:13", "14:00:00"),
    ("2026-09-17", "12:40:49", "12:45:00"),
    ("2026-09-16", "12:54:08", "13:00:00"),
    ("2026-09-15", "12:13:53", "12:30:00"),
    ("2026-09-14", "12:15:37", "12:30:00"),
    ("2026-09-11", "12:14:04", "12:30:00"),
    ("2026-09-10", "12:15:34", "12:30:00"),
    ("2026-09-08", "12:39:24", "12:45:00"),
    ("2026-09-04", "11:14:03", "11:30:00"),
    ("2026-09-03", "13:15:17", "13:30:00"),
    ("2026-09-02", "14:21:46", "14:30:00"),
    ("2026-09-01", "14:17:57", "14:25:00"),
]


def test_parser():
    print("\n== timestamp parser (Python 3.9 safe) ==")
    for raw in ("2026-09-18T19:00:13.83216+00:00", "2026-09-18T19:00:00Z",
                "2026-09-18T19:00:13.832Z", "2026-09-18T19:00:13.8321604567+00:00"):
        check(f"parses {raw}", clock.parse_api_time(raw) is not None)
    check("garbage returns None", clock.parse_api_time("nope") is None)


def test_history():
    print("\n== fast path on the 12 real listed->opened pairs ==")
    for date, listed, opened in HISTORY:
        listed_at, open_at = ct(date, listed), ct(date, opened)
        vc = VClock(listed_at)
        fresh_day(vc)
        fake = FakeKalshi(vc, date, listed_at, open_at)
        r = make_runner(fake, vc)
        run_until_handled(r, vc)
        rows = rows_for(date)
        coids = [o["coid"] for o in fake.posted]
        first = min(o["virtual_time"] for o in fake.posted) if fake.posted else None
        delay = (first - open_at).total_seconds() if first else 999
        ok = (len(fake.posted) == 18 and len(set(coids)) == 18 and len(rows) == 18
              and all(x["status"] == "resting" for x in rows) and 0 <= delay <= 0.4)
        check(f"{date} listed {listed} open {opened}: 18 orders, no dupes, first order +{delay:.2f}s",
              ok, f"posted={len(fake.posted)} rows={len(rows)}")
    # The last one run above is 2026-09-01; check details of it.
    o = fake.posted[0]
    check("order settings identical (NO 26c, post_only, expiry set)",
          o["no_price"] == C.NO_PRICE_CENTS and o["post_only"] is True and o["expiry"] is not None
          and abs(float(o["count"]) - C.CONTRACTS) < 1e-9)
    day = store.get_day("2026-09-01")
    check("days.notes has the timing line", "timing[fast]" in (day.get("notes") or ""))
    joined = "\n".join(MSGS)
    check("Telegram summary shows the timing block", "delay open_time" in joined and "⚡fast" in joined)
    check("database reads stay small (order_exists=0, orders_for_day<=3, is_paused<=2)",
          CALLS.get("order_exists", 0) == 0 and CALLS.get("orders_for_day", 0) <= 3
          and CALLS.get("is_paused", 0) <= 2, str(CALLS))


def _one(date="2026-09-18", listed="13:55:13", opened="14:00:00", **fake_kwargs):
    listed_at, open_at = ct(date, listed), ct(date, opened)
    vc = VClock(listed_at)
    fresh_day(vc)
    fake = FakeKalshi(vc, date, listed_at, open_at, **fake_kwargs)
    return date, listed_at, open_at, vc, fake, make_runner(fake, vc)


def test_flip_lag():
    print("\n== Kalshi flips 62s AFTER open_time (like the 2:01 you saw) ==")
    date, _, open_at, vc, fake, r = _one(flip_lag=62.0, events_lag=62.0)
    run_until_handled(r, vc)
    first = min(o["virtual_time"] for o in fake.posted)
    delay = (first - open_at).total_seconds()
    check("fires within ~1s of the real flip", 62.0 <= delay <= 63.1, f"delay {delay:.2f}s")
    check("18 orders, no dupes", len(fake.posted) == 18 and len({o['coid'] for o in fake.posted}) == 18)

    print("\n== markets flip on time but the events list lags 60s ==")
    date, _, open_at, vc, fake, r = _one(events_lag=60.0)
    run_until_handled(r, vc)
    first = min(o["virtual_time"] for o in fake.posted)
    check("fast path still fires at open_time", (first - open_at).total_seconds() <= 0.4)
    check("Telegram says the events list is lagging", "NO (that list is lagging)" in "\n".join(MSGS))


def test_never_flips_then_normal_path():
    print("\n== market never flips inside the watch window -> normal path takes over ==")
    date, _, open_at, vc, fake, r = _one(flip_lag=200.0, events_lag=200.0)
    run_until_handled(r, vc)
    joined = "\n".join(MSGS)
    check("warned that it gave up watching", "never showed active" in joined)
    check("normal path still placed all 18, once each",
          len(fake.posted) == 18 and len({o['coid'] for o in fake.posted}) == 18)
    check("standard timing block used", "(standard)" in joined)


def test_stragglers():
    print("\n== 3 markets open 2s late ==")
    date, _, open_at, vc, fake, r = _one()
    late = {fake.tickers[i]: 2.0 for i in (3, 7, 11)}
    fake.late = late
    run_until_handled(r, vc)
    rows = rows_for(date)
    check("all 18 placed", len(fake.posted) == 18)
    check("no 'rejected' rows (not-open errors are not saved as rejects)",
          not any(x["status"] == "rejected" for x in rows))
    late_times = [o["virtual_time"] for o in fake.posted if o["ticker"] in late]
    check("late ones were placed after they opened",
          all((t - open_at).total_seconds() >= 2.0 for t in late_times))


def test_restart_and_duplicates():
    print("\n== restart safety ==")
    date, _, open_at, vc, fake, r = _one()
    run_until_handled(r, vc)
    n = len(fake.posted)
    strategy.STATE.update(active_event=None, active_date=None)
    r2 = make_runner(fake, vc)
    for _ in range(40):
        r2._tick()
    check("restarted bot places nothing new", len(fake.posted) == n)

    # Crash between send and save: Kalshi has the orders, the database has nothing.
    store.metadata.drop_all(store.engine())
    store.init_db()
    strategy.STATE.update(active_event=None, active_date=None)
    r3 = make_runner(fake, vc)
    for _ in range(40):
        r3._tick()
        if strategy.STATE["active_event"]:
            break
    check("orders Kalshi already has are not sent twice (normal path, unchanged)",
          len(fake.posted) == n, f"posted={len(fake.posted)} expected={n}")


def test_fast_duplicate_on_kalshi():
    print("\n== fast path: Kalshi already has the orders (crash before they were saved) ==")
    date, _, open_at, vc, fake, r = _one()
    for ticker in fake.tickers:
        coid = strategy.client_order_id(date, ticker)
        fake.orders[coid] = {"ticker": ticker, "coid": coid}
    run_until_handled(r, vc)
    rows = rows_for(date)
    check("no new orders sent", len(fake.posted) == 0)
    check("saved as resting rows, not rejected",
          len(rows) == 18 and all(x["status"] == "resting" for x in rows))


def test_paused():
    print("\n== paused ==")
    date, _, open_at, vc, fake, r = _one()
    store.set_state("paused", True)
    run_until_handled(r, vc)
    check("no orders while paused", len(fake.posted) == 0)
    check("paused message sent", "PAUSED" in "\n".join(MSGS))


def test_flag_off():
    print("\n== FAST_OPEN=false -> exact old behavior ==")
    C.FAST_OPEN = False
    try:
        date, _, open_at, vc, fake, r = _one()

        def boom(*a, **k):
            raise AssertionError("fast path was touched while the flag is off")
        r._fast_arm = boom
        r._fast_open_step = boom
        run_until_handled(r, vc)
        check("old path placed 18 orders", len(fake.posted) == 18)
        day = store.get_day(date)
        check("old path also logs timing (for before/after)", "timing[standard]" in (day.get("notes") or ""))
    finally:
        C.FAST_OPEN = True


def test_error_fallback():
    print("\n== fast path blows up -> falls back to the normal path ==")
    date, _, open_at, vc, fake, r = _one()

    def boom(*a, **k):
        raise RuntimeError("simulated crash")
    r._fast_fire = boom
    run_until_handled(r, vc)
    check("still got all 18, once each",
          len(fake.posted) == 18 and len({o['coid'] for o in fake.posted}) == 18)
    check("told you about the fallback", "Fast-open hit an error" in "\n".join(MSGS))


def test_caps():
    print("\n== limits ==")
    old = C.MAX_MARKETS_PER_DAY
    C.MAX_MARKETS_PER_DAY = 10
    try:
        date, _, open_at, vc, fake, r = _one()
        run_until_handled(r, vc)
        check("MAX_MARKETS_PER_DAY still enforced (10)", len(fake.posted) == 10, str(len(fake.posted)))
    finally:
        C.MAX_MARKETS_PER_DAY = old


def test_pacing():
    print("\n== pacing at 8 orders/second (real time) ==")
    C.FAST_MAX_ORDERS_PER_SEC = 8.0
    try:
        date, _, open_at, vc, fake, r = _one()
        t0 = time.monotonic()
        run_until_handled(r, vc)
        elapsed = time.monotonic() - t0
        stamps = sorted(o["mono"] for o in fake.posted)
        worst = max(sum(1 for s in stamps if a <= s < a + 1.0) for a in stamps)
        check("18 orders take about 2s, not instant", 1.7 <= elapsed <= 4.0, f"{elapsed:.2f}s")
        check("never more than 9 sends in any 1s window", worst <= 9, f"worst={worst}")
    finally:
        C.FAST_MAX_ORDERS_PER_SEC = 1000.0



def test_added_and_vanished_markets():
    print("\n== markets change between listing and open ==")
    date, _, open_at, vc, fake, r = _one(n=18, extra_at_open=2)
    run_until_handled(r, vc)
    check("2 markets added after the listing still get orders (20 total, once each)",
          len(fake.posted) == 20 and len({o["coid"] for o in fake.posted}) == 20, str(len(fake.posted)))
    date, _, open_at, vc, fake, r = _one(n=18, vanish=(4, 9))
    run_until_handled(r, vc)
    rows = rows_for(date)
    check("2 markets that vanish get no order and no 'rejected' row",
          len(fake.posted) == 16 and not any(x["status"] == "rejected" for x in rows), str(len(fake.posted)))


def test_patience_and_real_rejects():
    print("\n== exchange slow to accept orders after the flip ==")
    date, _, open_at, vc, fake, r = _one(accept_delay=2.5)
    run_until_handled(r, vc)
    rows = rows_for(date)
    check("all 18 placed, none wrongly rejected", len(fake.posted) == 18
          and not any(x["status"] == "rejected" for x in rows), f"posted={len(fake.posted)}")

    print("\n== one market truly can't be ordered (real reject) ==")
    date, _, open_at, vc, fake, r = _one(bad=(5,))
    run_until_handled(r, vc)
    rows = rows_for(date)
    rejected = [x for x in rows if x["status"] == "rejected"]
    check("exactly that one market is saved as rejected", len(rejected) == 1
          and rejected[0]["market_ticker"] == fake.tickers[5], str(len(rejected)))
    check("the other 17 were placed", len(fake.posted) == 17)
    check("it settled within the straggler window (no endless loop)",
          (vc.now() - open_at).total_seconds() <= C.FAST_STRAGGLER_SECONDS + 3,
          f"{(vc.now() - open_at).total_seconds():.1f}s after open")


def test_record_failures():
    print("\n== database save fails after a live order ==")
    original = store.record_order
    state = {"fail_first": True}

    def flaky(**row):
        if state["fail_first"]:
            state["fail_first"] = False
            raise RuntimeError("db hiccup")
        return original(**row)
    store.record_order = flaky
    try:
        date, _, open_at, vc, fake, r = _one()
        run_until_handled(r, vc)
        check("one failed save is retried and saved", len(rows_for(date)) == 18)
    finally:
        store.record_order = original

    def broken(**row):
        raise RuntimeError("db down")
    store.record_order = broken
    try:
        date, _, open_at, vc, fake, r = _one()
        run_until_handled(r, vc)
        check("if saves keep failing you get a Telegram warning",
              any("could NOT be saved" in m for m in MSGS))
    finally:
        store.record_order = original


def test_shadow():
    print("\n== SHADOW mode: measures only, places nothing itself ==")
    C.FAST_OPEN, C.FAST_SHADOW = False, True
    try:
        date, _, open_at, vc, fake, r = _one(events_lag=45.0)

        def boom(*a, **k):
            raise AssertionError("shadow mode tried to fire orders")
        r._fast_fire = boom
        r._fast_place_one = boom
        r._shadow_threaded = False   # run inline so the fake clock stays deterministic
        run_until_handled(r, vc)
        joined = "\n".join(MSGS)
        check("a shadow report was sent", "Shadow check" in joined and "NO (that list is lagging)" in joined)
        check("the NORMAL path placed the 18 orders", len(fake.posted) == 18
              and "(standard)" in joined)
        first = min(o["virtual_time"] for o in fake.posted)
        check("shadow did not delay the normal path beyond the events-list lag",
              45.0 <= (first - open_at).total_seconds() <= 46.5,
              f"{(first - open_at).total_seconds():.1f}s")
    finally:
        C.FAST_OPEN, C.FAST_SHADOW = True, False


def test_near_cancel_time():
    print("\n== open time too close to the 5:29 cancel ==")
    date, _, open_at, vc, fake, r = _one(listed="17:00:00", opened="17:26:00")
    called = {"n": 0}
    orig = r._fast_run

    def spy(*a, **k):
        called["n"] += 1
        return orig(*a, **k)
    r._fast_run = spy
    run_until_handled(r, vc)
    check("fast path stayed out of it", called["n"] == 0)
    check("normal path still placed all 18", len(fake.posted) == 18)


def test_fuzz():
    print("\n== fuzz: 40 random runs with 503 errors and 'timeout after Kalshi accepted' ==")
    bad_runs = []
    for seed in range(40):
        rng = random.Random(seed)
        date, _, open_at, vc, fake, r = _one(rng=rng, transient=0.25, ghost=0.15,
                                             late={"": 0.0})
        run_until_handled(r, vc)
        rows = rows_for(date)
        live = [x for x in rows if x["status"] != "rejected"]
        ok = (len(fake.posted) == 18 and len(fake.orders) == 18
              and len({o["coid"] for o in fake.posted}) == 18
              and len(live) == 18 and len(rows) == 18
              and len({x["client_order_id"] for x in rows}) == 18)
        if not ok:
            bad_runs.append((seed, len(fake.posted), len(rows)))
    check("40/40 runs: exactly 18 orders on Kalshi, 18 saved rows, zero duplicates",
          not bad_runs, str(bad_runs[:3]))


# ---------------- end to end: REAL KalshiClient + real HTTP + real threads ----------------
def test_end_to_end_local_http(shadow: bool = False, events_lag_s: float = 0.0):
    print("\n== END TO END " + ("(SHADOW, background thread)" if shadow else "(FAST)")
          + ": real KalshiClient (signing, HTTP, threads) vs a local mock Kalshi ==")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from wnt.kalshi import KalshiClient

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()

    clock.now_ct = _REAL_NOW_CT
    MSGS.clear()
    if shadow:
        C.FAST_OPEN, C.FAST_SHADOW = False, True
    saved = (C.ACTIVE_WINDOW_START_CT, C.CANCEL_TIME_CT, C.EXPIRY_TIME_CT, C.FAST_MAX_ORDERS_PER_SEC)
    C.ACTIVE_WINDOW_START_CT, C.CANCEL_TIME_CT, C.EXPIRY_TIME_CT = "00:00", "23:59", "23:58"
    C.FAST_MAX_ORDERS_PER_SEC = 8.0
    today = clock.today_ct()
    event = clock.event_ticker_for_date(today)
    tickers = [f"{event}-T{i:02d}" for i in range(18)]
    open_at = datetime.now(timezone.utc) + timedelta(seconds=8)
    log_: dict = {"posts": [], "probes": [], "orders": {}}
    lock = threading.Lock()

    def status() -> str:
        return "active" if datetime.now(timezone.utc) >= open_at else "initialized"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            path, q = url.path, parse_qs(url.query)
            signed = "KALSHI-ACCESS-KEY" in self.headers
            stamp = open_at.strftime("%Y-%m-%dT%H:%M:%S") + f".{open_at.microsecond // 10:05d}Z"
            if path == "/trade-api/v2/markets" and q.get("event_ticker") == [event]:
                return self._send(200, {"markets": [
                    {"ticker": t, "status": status(), "open_time": stamp,
                     "yes_sub_title": t[-3:]} for t in tickers], "cursor": ""})
            if path == "/trade-api/v2/markets":
                return self._send(200, {"markets": [], "cursor": ""})
            if path.startswith("/trade-api/v2/markets/") and path.endswith("/orderbook"):
                return self._send(200, {"orderbook": {"yes": [], "no": []}})
            if path.startswith("/trade-api/v2/markets/"):
                with lock:
                    log_["probes"].append(signed)
                return self._send(200, {"market": {"status": status()}})
            if path == "/trade-api/v2/events":
                shown = (datetime.now(timezone.utc) >= open_at + timedelta(seconds=events_lag_s)
                         and q.get("status") == ["open"])
                return self._send(200, {"events": [{"event_ticker": event}] if shown else [],
                                        "cursor": ""})
            if path == "/trade-api/v2/portfolio/balance":
                return self._send(200, {"balance": 5_000_00})
            return self._send(404, {"error": "no such path"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path != "/trade-api/v2/portfolio/events/orders":
                return self._send(404, {"error": "no such path"})
            now = datetime.now(timezone.utc)
            with lock:
                log_["posts"].append({"t": now, "body": body,
                                      "signed": "KALSHI-ACCESS-SIGNATURE" in self.headers})
                if now < open_at:
                    return self._send(400, {"error": "market not open"})
                if body["client_order_id"] in log_["orders"]:
                    return self._send(409, {"error": "ORDER_ALREADY_EXISTS"})
                log_["orders"][body["client_order_id"]] = body
            return self._send(201, {"order_id": str(uuid.uuid4()), "client_order_id": body["client_order_id"],
                                    "fill_count": "0.00", "remaining_count": body["count"]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        fresh_day(None)
        client = KalshiClient(key_id="test-key", private_key_pem=pem,
                              base_url=f"http://127.0.0.1:{server.server_port}")
        r = strategy.Runner(client=client)
        r._sleep = lambda s: time.sleep(min(s, 0.5))
        if shadow:
            def boom(*a, **k):
                raise AssertionError("shadow mode tried to fire orders")
            r._fast_fire = boom
            r._fast_place_one = boom
        deadline = time.time() + 60
        while not strategy.STATE["active_event"] and time.time() < deadline:
            r._tick()
        if shadow:
            time.sleep(1.5)  # let the background watcher finish its report
        posts = [x for x in log_["posts"]]
        good = [x for x in posts if x["t"] >= open_at]
        early = [x for x in posts if x["t"] < open_at]
        first_delay = (min(x["t"] for x in good) - open_at).total_seconds() if good else 999
        last_delay = (max(x["t"] for x in good) - open_at).total_seconds() if good else 999
        rows = rows_for(today)

        if shadow:
            joined = "\n".join(MSGS)
            check("SHADOW: 18 orders placed by the normal path, once each",
                  len(log_["orders"]) == 18 and len(good) == 18 and "(standard)" in joined)
            check("SHADOW: nothing sent before open_time", len(early) == 0)
            check("SHADOW: shadow report sent and it saw the events-list lag",
                  "Shadow check" in joined and "NO (that list is lagging)" in joined)
            check(f"SHADOW: normal path was not slowed (first order {first_delay:.2f}s after open,"
                  f" list lag {events_lag_s:.0f}s)", events_lag_s <= first_delay <= events_lag_s + 1.5)
            return
        check("18 orders reached the mock Kalshi, each exactly once",
              len(log_["orders"]) == 18 and len(good) == 18, f"orders={len(log_['orders'])} posts={len(posts)}")
        check("NOTHING was sent before open_time", len(early) == 0, f"{len(early)} early")
        check(f"first order {first_delay:.2f}s after open, all 18 within {last_delay:.2f}s (real HTTP)",
              0 <= first_delay <= 1.0 and last_delay <= 4.0)
        b = good[0]["body"]
        check("request body is the exact V2 format the bot already uses",
              b["side"] == "ask" and b["price"] == f"{C.yes_price_cents() / 100:.4f}"
              and b["count"] == f"{C.CONTRACTS:.2f}" and b["post_only"] is True
              and b["time_in_force"] == "good_till_canceled" and b["cancel_order_on_pause"] is True
              and b["reduce_only"] is False and isinstance(b["expiration_time"], int)
              and str(uuid.UUID(b["client_order_id"])) == b["client_order_id"])
        check("orders were really signed", all(x["signed"] for x in posts))
        check("status checks used both signed and public requests",
              True in log_["probes"] and False in log_["probes"], str(len(log_["probes"])))
        check("18 live rows saved", len([x for x in rows if x["status"] == "resting"]) == 18)
        joined = "\n".join(MSGS)
        check("Telegram timing block has the delay line", "delay open_time → first order" in joined)
    finally:
        server.shutdown()
        C.ACTIVE_WINDOW_START_CT, C.CANCEL_TIME_CT, C.EXPIRY_TIME_CT, C.FAST_MAX_ORDERS_PER_SEC = saved
        C.FAST_OPEN, C.FAST_SHADOW = True, False


if __name__ == "__main__":
    test_parser()
    test_history()
    test_flip_lag()
    test_never_flips_then_normal_path()
    test_stragglers()
    test_restart_and_duplicates()
    test_fast_duplicate_on_kalshi()
    test_paused()
    test_flag_off()
    test_error_fallback()
    test_caps()
    test_pacing()
    test_added_and_vanished_markets()
    test_patience_and_real_rejects()
    test_record_failures()
    test_shadow()
    test_near_cancel_time()
    test_fuzz()
    test_end_to_end_local_http()
    test_end_to_end_local_http(shadow=True, events_lag_s=3.0)
    print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} CHECK(S) FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)
