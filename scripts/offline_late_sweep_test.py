"""
Offline tests for the late-market sweep. No network, no real orders.

Run from the repo root:   python scripts/offline_late_sweep_test.py
Reuses the fake Kalshi and fake clock from scripts/offline_fast_open_test.py.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_spec = importlib.util.spec_from_file_location("base_t", os.path.join(ROOT, "scripts", "offline_fast_open_test.py"))
t = importlib.util.module_from_spec(_spec)
sys.modules["base_t"] = t
_spec.loader.exec_module(t)

C, clock, store, strategy, KalshiError = t.C, t.clock, t.store, t.strategy, t.KalshiError
MSGS, CALLS = t.MSGS, t.CALLS
FAILS: list[str] = []
DATE = "2026-09-21"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


class LateFake(t.FakeKalshi):
    """FakeKalshi plus markets that show up (as new tickers) some time after the open."""

    def __init__(self, *a, appear: dict | None = None, **k):
        super().__init__(*a, **k)
        self.appear = appear or {}                 # ticker -> seconds after open_time it appears
        self.balance = 1_000_000
        self.attempts: dict[str, int] = {}

    def _appears_at(self, tk: str) -> datetime:
        return self.open_at + timedelta(seconds=self.appear[tk])

    def get_markets(self, event_ticker):
        self.market_calls = getattr(self, "market_calls", 0) + 1
        out = super().get_markets(event_ticker)
        now = self.vc.now()
        for tk in self.appear:
            if now >= self._appears_at(tk):
                stamp = self._appears_at(tk).strftime("%Y-%m-%dT%H:%M:%S") + ".00000+00:00"
                out.append({"ticker": tk, "status": "active", "open_time": stamp, "yes_sub_title": tk[-4:]})
        return out

    def get_balance(self):
        return {"balance": self.balance}

    def create_no_order(self, ticker, **kw):
        self.attempts[ticker] = self.attempts.get(ticker, 0) + 1
        if ticker in self.appear and self.vc.now() < self._appears_at(ticker):
            raise KalshiError(404, '{"error":"market not found"}', "/portfolio/events/orders")
        return super().create_no_order(ticker, **kw)


def setup(*, n=15, appear=None, listed="11:55:00", opened="12:30:00", fast=False, **kw):
    C.FAST_OPEN, C.FAST_SHADOW, C.LATE_SWEEP = fast, False, True
    listed_at, open_at = t.ct(DATE, listed), t.ct(DATE, opened)
    vc = t.VClock(listed_at)
    t.fresh_day(vc)
    fake = LateFake(vc, DATE, listed_at, open_at, n=n, appear=appear, **kw)
    r = t.make_runner(fake, vc)
    t.run_until_handled(r, vc)          # the day's first orders (the old / fast path)
    return vc, fake, r, open_at


def appear_names(fake_event: str, names=("TRUM", "IRAN")) -> list[str]:
    return [f"{fake_event}-{n}" for n in names]


def run_to(r, vc, target_ct: str, date=DATE) -> None:
    """Tick until the virtual clock reaches target (coarse jumps far away, fine ticks near)."""
    end = t.ct(date, target_ct)
    while vc.now() < end:
        remaining = (end - vc.now()).total_seconds()
        if remaining > 120:
            vc.advance(min(remaining - 100, 300))
        r._tick()


def tick_until(r, vc, target_ct: str, date=DATE) -> None:
    """Plain 5-second ticks, no jumps: use this around the moment something appears."""
    end = t.ct(date, target_ct)
    while vc.now() < end:
        r._tick()


EVENT = f"KXWORLDNEWSMENTION-{datetime.strptime(DATE, '%Y-%m-%d').strftime('%y%b%d').upper()}"


# ---------------------------------------------------------------------------------------
def test_sep21_replay() -> None:
    print("\n== replay of Sep 21: 15 markets at the open, Trump and Iran appear at 3:00 PM ==")
    names = appear_names(EVENT)
    vc, fake, r, open_at = setup(appear={n: 2.5 * 3600 for n in names})
    check("first pass ordered the 15 that existed", len(fake.posted) == 15)
    run_to(r, vc, "14:58:00")
    check("before they appear, still only the 15", len(fake.posted) == 15)
    tick_until(r, vc, "15:02:00")        # fine 5-second ticks across the moment they appear (3:00 PM)
    late = [o for o in fake.posted if o["ticker"] in names]
    appear_at = open_at + timedelta(hours=2.5)
    check("both late markets got an order", len(late) == 2)
    lags = [(o["virtual_time"] - appear_at).total_seconds() for o in late]
    check("each was ordered within 10 seconds of appearing", all(0 <= x <= 10.5 for x in lags), str([round(x, 1) for x in lags]))
    check("17 orders on Kalshi, every one unique", len(fake.posted) == 17 and len({o["coid"] for o in fake.posted}) == 17)
    o = late[0]
    check("identical order settings (NO 26c, contracts, post_only, expiry)",
          o["no_price"] == C.NO_PRICE_CENTS and abs(float(o["count"]) - C.CONTRACTS) < 1e-9 and o["expiry"] is not None)
    rows = t.rows_for(DATE)
    check("17 rows saved, none rejected", len(rows) == 17 and not any(x["status"] == "rejected" for x in rows))
    day = store.get_day(DATE)
    check("days table updated: markets_seen 17, orders_placed 17", day["markets_seen"] == 17 and day["orders_placed"] == 17,
          f"{day['markets_seen']}/{day['orders_placed']}")
    joined = "\n".join(MSGS)
    check("Telegram says a late market was found and shows when it opened", "Late market found" in joined and "after open_time" in joined)
    check("the bot's own order count is updated", strategy.STATE["orders_today"] == 17)
    for _ in range(360):                # another hour: nothing more should happen
        r._tick()
    check("an hour later: still exactly 17, nothing doubled", len(fake.posted) == 17)


def test_flip_later_market() -> None:
    print("\n== a listed market that only turns active 2 hours later ==")
    vc, fake, r, open_at = setup(late={f"{EVENT}-W05": 2 * 3600.0})
    check("first pass ordered 14 and skipped the one that was not active yet",
          len(fake.posted) == 14 and not any(o["ticker"].endswith("W05") for o in fake.posted), str(len(fake.posted)))
    n0 = len(fake.posted)
    run_to(r, vc, "14:45:00")
    check("the sweep ordered it after it turned active (15 total, once)",
          len(fake.posted) == 15 and sum(o["ticker"].endswith("W05") for o in fake.posted) == 1,
          f"{n0} -> {len(fake.posted)}")


def test_cost() -> None:
    print("\n== cost while nothing new happens (one hour) ==")
    def hour(sweep: bool):
        vc, fake, r, _ = setup()
        C.LATE_SWEEP = sweep
        CALLS.clear()
        before_markets = getattr(fake, "market_calls", 0)
        for _ in range(720):
            r._tick()
        return (getattr(fake, "market_calls", 0) - before_markets, dict(CALLS))
    on_calls, on_db = hour(True)
    off_calls, off_db = hour(False)
    check(f"Kalshi: about 1 public market read per 10s with the sweep ({on_calls}/hour), none without ({off_calls})",
          340 <= on_calls <= 370 and off_calls == 0)
    check("database: the sweep adds at most 1 read (its start-up) and no pause checks",
          on_db.get("orders_for_day", 0) - off_db.get("orders_for_day", 0) <= 1 and on_db.get("is_paused", 0) == off_db.get("is_paused", 0),
          f"orders_for_day on/off {on_db.get('orders_for_day', 0)}/{off_db.get('orders_for_day', 0)}, is_paused {on_db.get('is_paused', 0)}/{off_db.get('is_paused', 0)}")
    C.LATE_SWEEP = True


def test_restart() -> None:
    print("\n== restarts ==")
    names = appear_names(EVENT, ("TRUM", "IRAN", "LATE"))
    vc, fake, r, _ = setup(appear={names[0]: 2.5 * 3600, names[1]: 2.5 * 3600, names[2]: 3.5 * 3600})
    run_to(r, vc, "15:10:00")
    n = len(fake.posted)
    check("Trump and Iran ordered before the restart", n == 17)
    r2 = t.make_runner(fake, vc)                       # fresh process, nothing in memory
    strategy.STATE.update(active_event=None, active_date=None)
    for _ in range(40):
        r2._tick()
    check("restarted bot resumes the day and adds nothing", len(fake.posted) == n)
    run_to(r2, vc, "16:10:00")
    check("a third late market after the restart is ordered exactly once", len(fake.posted) == n + 1 and
          len({o["coid"] for o in fake.posted}) == n + 1)
    r3 = t.make_runner(fake, vc)
    strategy.STATE.update(active_event=None, active_date=None)
    for _ in range(60):
        r3._tick()
    check("a second restart still adds nothing", len(fake.posted) == n + 1)


def test_cap() -> None:
    print("\n== the 25-market limit ==")
    names = appear_names(EVENT, ("A1", "A2", "A3"))
    vc, fake, r, _ = setup(n=24, appear={n: 2 * 3600 for n in names})
    run_to(r, vc, "14:30:00")
    for _ in range(180):
        r._tick()
    check("24 + 3 late: only 1 more allowed (25 total)", len(fake.posted) == 25, str(len(fake.posted)))
    notes = [m for m in MSGS if "NOT ordered" in m and "daily limit" in m]
    check("you were told once, not every 10 seconds", len(notes) == 1, str(len(notes)))


def test_pause() -> None:
    print("\n== pause and resume ==")
    names = appear_names(EVENT, ("TRUM",))
    vc, fake, r, _ = setup(appear={names[0]: 2.5 * 3600})
    store.set_state("paused", True)
    run_to(r, vc, "15:20:00")
    check("paused: the late market is NOT ordered", len(fake.posted) == 15)
    check("...and you are told once", len([m for m in MSGS if "PAUSED" in m and "Late market" in m]) == 1)
    store.set_state("paused", False)
    run_to(r, vc, "15:21:00")
    check("after /resume it is ordered", len(fake.posted) == 16)


def test_cash() -> None:
    print("\n== not enough cash ==")
    names = appear_names(EVENT, ("TRUM",))
    vc, fake, r, _ = setup(appear={names[0]: 2.5 * 3600})
    fake.balance = 100          # $1.00
    run_to(r, vc, "15:20:00")
    check("no cash: no order", len(fake.posted) == 15)
    check("you are told once", len([m for m in MSGS if "not enough cash" in m]) == 1)
    fake.balance = 1_000_000
    run_to(r, vc, "15:21:00")
    check("after topping up it is ordered", len(fake.posted) == 16)


def test_switches() -> None:
    print("\n== kill switch, /cancelnow, and a day that never started ==")
    names = appear_names(EVENT, ("TRUM",))
    vc, fake, r, _ = setup(appear={names[0]: 2.5 * 3600})
    C.LATE_SWEEP = False
    run_to(r, vc, "15:30:00")
    check("LATE_SWEEP=false: nothing is ordered late (old behavior exactly)", len(fake.posted) == 15)
    C.LATE_SWEEP = True

    vc, fake, r, _ = setup(appear={names[0]: 2.5 * 3600})
    strategy.STATE["cancelled_today"] = True          # what /cancelnow does
    run_to(r, vc, "15:30:00")
    check("after /cancelnow the sweep does not put orders back", len(fake.posted) == 15)
    strategy.STATE["cancelled_today"] = False

    C.FAST_OPEN, C.FAST_SHADOW, C.LATE_SWEEP = False, False, True
    listed_at, open_at = t.ct(DATE, "11:55:00"), t.ct(DATE, "12:30:00")
    vc = t.VClock(listed_at)
    t.fresh_day(vc)
    fake = LateFake(vc, DATE, listed_at, open_at, n=15)
    r = t.make_runner(fake, vc)
    store.set_state("paused", True)                   # paused at the open: no first orders
    t.run_until_handled(r, vc)
    store.set_state("paused", False)
    run_to(r, vc, "15:00:00")
    check("a day that got NO first orders is not started late (old rule kept)", len(fake.posted) == 0, str(len(fake.posted)))


def test_reject_and_cutoff() -> None:
    print("\n== a refused order, and the cancel cutoff ==")
    tk = f"{EVENT}-BAD1"
    vc, fake, r, _ = setup(appear={tk: 2 * 3600})
    fake.bad = {tk}
    run_to(r, vc, "14:40:00")
    for _ in range(120):
        r._tick()
    check("a refused late market is tried once and never again", fake.attempts.get(tk) == 1, str(fake.attempts.get(tk)))
    rej = [x for x in t.rows_for(DATE) if x["status"] == "rejected"]
    check("...saved as rejected with the reason", len(rej) == 1 and rej[0]["market_ticker"] == tk and rej[0]["reject_reason"])

    early, late = f"{EVENT}-E1720", f"{EVENT}-L1727"
    vc, fake, r, open_at = setup(appear={early: (17 * 3600 + 20 * 60) - 12 * 3600 - 30 * 60,
                                         late: (17 * 3600 + 27 * 60 + 45) - 12 * 3600 - 30 * 60})
    run_to(r, vc, "17:26:00")
    check("a market appearing at 5:20 PM is ordered", any(o["ticker"] == early for o in fake.posted))
    while vc.now() < t.ct(DATE, "17:28:30"):
        vc.advance(5)
        r._tick() if vc.now() < t.ct(DATE, "17:29:00") else None
    check("a market appearing at 5:27:45 PM (too close to the cancel) is NOT ordered",
          not any(o["ticker"] == late for o in fake.posted))


def test_after_fast_path() -> None:
    print("\n== works the same after the FAST path placed the day ==")
    names = appear_names(EVENT, ("TRUM", "IRAN"))
    vc, fake, r, open_at = setup(fast=True, appear={n: 2.5 * 3600 for n in names})
    check("fast path placed the first 15", len(fake.posted) == 15 and "⚡fast" in "\n".join(MSGS))
    run_to(r, vc, "15:20:00")
    check("the sweep added Trump and Iran, no duplicates", len(fake.posted) == 17 and len({o["coid"] for o in fake.posted}) == 17)


# ---------------------------------------------------------------------------------------
def test_end_to_end_http() -> None:
    print("\n== END TO END: real KalshiClient over real HTTP, a market appears 8s after the open ==")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from wnt.kalshi import KalshiClient

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    clock.now_ct = t._REAL_NOW_CT
    saved = (C.ACTIVE_WINDOW_START_CT, C.CANCEL_TIME_CT, C.EXPIRY_TIME_CT, C.FAST_MAX_ORDERS_PER_SEC,
             C.LATE_SWEEP_SECONDS)
    C.ACTIVE_WINDOW_START_CT, C.CANCEL_TIME_CT, C.EXPIRY_TIME_CT = "00:00", "23:59", "23:58"
    C.FAST_OPEN, C.FAST_SHADOW, C.LATE_SWEEP, C.LATE_SWEEP_SECONDS = False, False, True, 3.0
    today = clock.today_ct()
    event = clock.event_ticker_for_date(today)
    base = [f"{event}-T{i:02d}" for i in range(15)]
    late_tk = f"{event}-TRUM"
    open_at = datetime.now(timezone.utc) + timedelta(seconds=6)
    appear_at = open_at + timedelta(seconds=8)
    log_: dict = {"posts": [], "orders": {}}
    lock = threading.Lock()

    def status() -> str:
        return "active" if datetime.now(timezone.utc) >= open_at else "initialized"

    class H(BaseHTTPRequestHandler):
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
            stamp = open_at.strftime("%Y-%m-%dT%H:%M:%S") + f".{open_at.microsecond // 10:05d}Z"
            if path == "/trade-api/v2/markets" and q.get("event_ticker") == [event]:
                ms = [{"ticker": tk, "status": status(), "open_time": stamp, "yes_sub_title": tk[-3:]} for tk in base]
                if datetime.now(timezone.utc) >= appear_at:
                    ms.append({"ticker": late_tk, "status": "active",
                               "open_time": appear_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "yes_sub_title": "Trump"})
                return self._send(200, {"markets": ms, "cursor": ""})
            if path == "/trade-api/v2/markets":
                return self._send(200, {"markets": [], "cursor": ""})
            if path.endswith("/orderbook"):
                return self._send(200, {"orderbook": {"yes": [], "no": []}})
            if path == "/trade-api/v2/events":
                shown = status() == "active" and q.get("status") == ["open"]
                return self._send(200, {"events": [{"event_ticker": event}] if shown else [], "cursor": ""})
            if path == "/trade-api/v2/portfolio/balance":
                return self._send(200, {"balance": 500000})
            if path == "/trade-api/v2/portfolio/fills":
                return self._send(200, {"fills": [], "cursor": ""})
            return self._send(404, {"error": "no such path"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            now = datetime.now(timezone.utc)
            tk = body.get("ticker") or body.get("market_ticker") or ""
            with lock:
                log_["posts"].append({"t": now, "body": body})
                if now < open_at or (body["client_order_id"] in log_["orders"]):
                    return self._send(400 if now < open_at else 409, {"error": "refused"})
                log_["orders"][body["client_order_id"]] = {"t": now, "body": body}
            return self._send(201, {"order_id": str(uuid.uuid4()), "client_order_id": body["client_order_id"],
                                    "fill_count": "0.00", "remaining_count": body["count"]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        t.fresh_day(None)
        MSGS.clear()
        client = KalshiClient(key_id="test-key", private_key_pem=pem,
                              base_url=f"http://127.0.0.1:{server.server_port}")
        r = strategy.Runner(client=client)
        r._sleep = lambda s: time.sleep(min(s, 0.5))
        deadline = time.time() + 40
        while time.time() < deadline and len(log_["orders"]) < 16:
            r._tick()
        orders = list(log_["orders"].values())
        late_order = [o for o in orders if o["body"].get("client_order_id") == strategy.client_order_id(today, late_tk)]
        check("the 15 first markets and the late one were all ordered, once each",
              len(orders) == 16 and len(late_order) == 1, f"{len(orders)} orders")
        lag = (late_order[0]["t"] - appear_at).total_seconds() if late_order else 999
        check(f"the late market was ordered {lag:.1f}s after it appeared (real HTTP, 3s sweep)", 0 <= lag <= 5.0)
        check("nothing was sent before the open", all(p["t"] >= open_at for p in log_["posts"]))
        b = late_order[0]["body"]
        check("late order body is the exact V2 format the bot already uses",
              b["side"] == "ask" and b["price"] == f"{C.yes_price_cents() / 100:.4f}" and b["count"] == f"{C.CONTRACTS:.2f}"
              and b["post_only"] is True and isinstance(b["expiration_time"], int))
        check("Telegram announced the late market", any("Late market found" in m for m in MSGS))
        rows = t.rows_for(today)
        check("16 rows saved", len([x for x in rows if x["status"] == "resting"]) == 16, str(len(rows)))
    finally:
        server.shutdown()
        (C.ACTIVE_WINDOW_START_CT, C.CANCEL_TIME_CT, C.EXPIRY_TIME_CT, C.FAST_MAX_ORDERS_PER_SEC,
         C.LATE_SWEEP_SECONDS) = saved
        C.FAST_OPEN, C.LATE_SWEEP = True, True


if __name__ == "__main__":
    test_sep21_replay()
    test_flip_later_market()
    test_cost()
    test_restart()
    test_cap()
    test_pause()
    test_cash()
    test_switches()
    test_reject_and_cutoff()
    test_after_fast_path()
    test_end_to_end_http()
    print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} CHECK(S) FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)
