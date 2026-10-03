#!/usr/bin/env python3
"""Offline self-test for the worker lease and the two entry points. No network, no Supabase, no
Telegram, no Kalshi key.

It proves the rules the server move hangs on:
  - only one place holds the lease; a second one waits and starts nothing
  - RUN_WORKERS=false starts nothing at all (dashboard only)
  - without the lease the loops do not tick and a real order is refused before any request is made
  - a database hiccup pauses new orders but is not a loss; a real loss stops this place
  - the headless worker starts the same loops as the Streamlit app
  - the dashboard renders in both modes

    python3 scripts/offline_lease_test.py

Set TEST_DATABASE_URL to a THROWAWAY Postgres to run the lease checks on Postgres too.
"""
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("DATABASE_URL", None)
os.environ["SQLITE_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["TELEGRAM_TOKEN"] = ""
os.environ["DRY_RUN"] = "true"
for _k in ("RUN_WORKERS", "KLSH_HOST", "KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY_PEM"):
    os.environ.pop(_k, None)

from sqlalchemy import create_engine, text  # noqa: E402

from wnt import config as C, depth as depth_mod, lease, notify, runtime, store  # noqa: E402
from wnt.kalshi import KalshiClient  # noqa: E402
from wnt.strategy import Runner  # noqa: E402

if C.DATABASE_URL:
    print("STOP: DATABASE_URL is set (a local .streamlit/secrets.toml or .env?). This test only runs on a "
          "throwaway SQLite file. Nothing was touched.")
    sys.exit(2)

FAILS = []
CHECKS = [0]
NAME = runtime.NAME


def check(name, cond, detail=""):
    CHECKS[0] += 1
    print(("  ok    " if cond else "  FAIL  ") + name + ("  %s" % (detail,) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def wait_for(cond, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def names():
    return {t.name for t in threading.enumerate()}


# ------------------------------------------------------------------ 1) the lease table itself
def lease_rules(db, label):
    print("\n== lease rules on %s ==" % label)
    t = "public.worker_leases_test"
    with db.begin() as conn:
        conn.execute(text("drop table if exists %s" % lease._t(db, t)))
    lease.ensure_table(db, t)
    lease.ensure_table(db, t)                                    # safe to repeat
    check("first place takes the lease", lease.acquire(db, "bot", "streamlit", t))
    check("same place takes it again at once (restart)", lease.acquire(db, "bot", "streamlit", t))
    check("second place is refused while the first is fresh", not lease.acquire(db, "bot", "vps", t))
    check("renew works for the holder", lease.renew(db, "bot", "streamlit", t))
    check("renew never works for anyone else", not lease.renew(db, "bot", "vps", t))
    cur = lease.current(db, "bot", t)
    check("current() names the holder with a small age", cur and cur["holder"] == "streamlit" and cur["age_s"] < 5, cur)
    check("another bot's lease is separate", lease.acquire(db, "other-bot", "vps", t))
    check("a silent holder can be replaced (stale)", lease.acquire(db, "bot", "vps", t, stale_s=0))
    check("the old holder's renew now fails", not lease.renew(db, "bot", "streamlit", t))
    check("the old holder cannot take it back while the new one is fresh", not lease.acquire(db, "bot", "streamlit", t))
    lease.release(db, "bot", "streamlit", t)
    check("release by a non-holder changes nothing", (lease.current(db, "bot", t) or {}).get("holder") == "vps")
    lease.release(db, "bot", "vps", t)
    check("release by the holder frees it", lease.current(db, "bot", t) is None)
    if lease._pg(db):
        with db.connect() as conn:
            rls = conn.execute(text("select relrowsecurity from pg_class where oid = to_regclass(:t)"), {"t": t}).scalar()
        check("Row-Level Security is on for the lease table", rls is True, rls)
    with db.begin() as conn:
        conn.execute(text("drop table if exists %s" % lease._t(db, t)))


store.init_db()
lease_rules(store.engine(), "SQLite")
if os.environ.get("TEST_DATABASE_URL"):
    url = os.environ["TEST_DATABASE_URL"].replace("postgresql://", "postgresql+psycopg2://", 1)
    lease_rules(create_engine(url, future=True), "Postgres")
else:
    print("\n(TEST_DATABASE_URL not set: Postgres lease checks skipped)")


# ------------------------------------------------------------------ 2) keeper + gate
print("\n== keeper and gate ==")
db = store.engine()
lost = []
runtime.reset_for_tests()
check("no lease in use: loops may run and orders may be sent (scripts, tests)", lease.running() and lease.may_trade())

k = lease.Keeper(db, NAME, "streamlit", on_lost=lost.append, renew_s=0.05)
check("keeper takes a free lease", k.try_acquire() and lease.GATE.mode == "held")
check("held: running and may trade", lease.running() and lease.may_trade())
first = lease.GATE.last_ok
check("the renew thread keeps the lease fresh", wait_for(lambda: lease.GATE.last_ok > first))

real_renew = lease.renew
lease.renew = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("database down"))
time.sleep(0.2)
lease.GATE.last_ok = time.monotonic() - lease.SAFE_S - 1          # pretend the outage lasted SAFE_S
check("database down past SAFE_S: no new orders", not lease.may_trade())
check("...but it is NOT a loss: loops keep running here", lease.running() and lease.GATE.mode == "held" and not lost)
try:
    lease.require("send a real order")
    check("require() raises while not safe", False)
except lease.LeaseError:
    check("require() raises while not safe", True)
lease.renew = real_renew
check("database back: trading resumes by itself", wait_for(lease.may_trade))

with db.begin() as conn:                                           # the server takes over
    conn.execute(text("update worker_leases set holder = 'vps', heartbeat = :n where name = :name"),
                 {"n": time.time(), "name": NAME})
check("another holder -> on_lost is called once", wait_for(lambda: len(lost) == 1), lost)
check("on_lost is told who has it", lost and "vps" in lost[0], lost)
check("lost: not running, may not trade", lease.GATE.mode == "lost" and not lease.running() and not lease.may_trade())
time.sleep(0.2)
check("a lost keeper never takes the lease back", lease.current(db, NAME)["holder"] == "vps" and len(lost) == 1)
k.stop(give_up=True)
check("stop() after a loss leaves the new holder's row alone", lease.current(db, NAME)["holder"] == "vps")
lease.release(db, NAME, "vps")


# ------------------------------------------------------------------ 3) without the lease nothing acts
print("\n== gates in the bot ==")
sent = []
client = KalshiClient.__new__(KalshiClient)
client.request = lambda *a, **kw: (sent.append(a), {"order": {"order_id": "o1"}, "order_id": "o1"})[1]
runtime.reset_for_tests()
for mode in ("dashboard", "waiting", "lost"):
    lease.GATE.set(mode)
    try:
        client.create_no_order("T-1", 26, 1, "coid-1")
        ok = False
    except lease.LeaseError:
        ok = True
    check("%s: a real order is refused before any request" % mode, ok and not sent, sent)
lease.GATE.reset()
client.create_no_order("T-1", 26, 1, "coid-1")
check("no lease in use: the same call goes through (scripts keep working)", len(sent) == 1)

runtime.reset_for_tests()
svc = runtime.services()
real_send = notify.send
notify.send = lambda *a, **kw: True
runtime.INFO["host"] = "streamlit"
runtime._on_lost("vps (0s ago)")
check("lost: the strategy loop and the depth loop are told to stop",
      svc["runner"]._stop.is_set() and svc["collector"]._stop.is_set())
notify.send = real_send
runtime.reset_for_tests()


# ------------------------------------------------------------------ 4) the entry points
print("\n== start_workers ==")
started = []
Runner.run_forever = lambda self: started.append("strategy")
depth_mod.DepthCollector.run_forever = lambda self: started.append("depth")
listener = []
notify.start_listener = lambda: listener.append(1)
told = []
notify.send = lambda text_, quiet=False: told.append(text_) or True
runtime.WAIT_S = 0.05

runtime.reset_for_tests()
os.environ["RUN_WORKERS"] = "false"
info = runtime.start_workers(where="streamlit")
time.sleep(0.2)
check("RUN_WORKERS=false: no loops, no listener", started == [] and listener == [], (started, listener))
check("RUN_WORKERS=false: gate says dashboard, no lease row written",
      lease.GATE.mode == "dashboard" and lease.current(db, NAME) is None)
check("RUN_WORKERS=false: no Telegram commands registered, page objects still built",
      info["run_workers"] is False and "status" not in notify._handlers and "runner" in runtime.services())
check("dashboard banner says so", runtime.banner()[0] == "info" and "Dashboard only" in runtime.banner()[1], runtime.banner())
os.environ.pop("RUN_WORKERS")

runtime.reset_for_tests()
lease.acquire(db, NAME, "streamlit")                               # Streamlit is running the bot
os.environ["KLSH_HOST"] = "vps"
runtime.start_workers(where="vps")
time.sleep(0.3)
check("second place: waits, starts nothing", started == [] and listener == [] and lease.GATE.mode == "waiting",
      (started, lease.GATE.mode))
check("waiting banner names the holder", runtime.banner()[0] == "warning" and "streamlit" in runtime.banner()[1], runtime.banner())
check("the holder's row is untouched", lease.current(db, NAME)["holder"] == "streamlit")
lease.release(db, NAME, "streamlit")                               # Streamlit goes dashboard-only
check("...and takes over once the lease is free", wait_for(lambda: sorted(started) == ["depth", "strategy"]), started)
check("listener started with the loops", listener == [1])
check("lease row now says vps", lease.current(db, NAME)["holder"] == "vps")
check("Telegram told where the workers run", any("vps" in t and C.VERSION in t for t in told), told)
check("calling start_workers again starts nothing twice", runtime.start_workers(where="vps") and sorted(started) == ["depth", "strategy"])
check("/status names the host and the version",
      "host: vps" in notify._handlers["status"]([]) and C.VERSION in notify._handlers["status"]([]))
check("every Telegram command is registered from runtime (both entry points share them)",
      {"status", "today", "pnl", "cancelnow", "pause", "resume", "balance", "stats"} <= set(notify._handlers))
runtime.stop()
check("stop() gives the lease back", lease.current(db, NAME) is None)
os.environ.pop("KLSH_HOST")

started.clear()
listener.clear()
runtime.reset_for_tests()
info = runtime.start_workers(where="streamlit", block=True)
check("block=True returns with the loops running (worker.py path)", wait_for(lambda: sorted(started) == ["depth", "strategy"]), started)
check("without KLSH_HOST the Streamlit app is 'streamlit'", info["host"] == "streamlit" and lease.current(db, NAME)["holder"] == "streamlit")
runtime.stop()
runtime.reset_for_tests()


# ------------------------------------------------------------------ 5) worker.py and the dashboard, in real processes
# The real loops are swapped for sleepers inside these child processes: this checks the entry
# points (lease wait, start, signals, exit codes, page render), not the trading loops.
STUBS = """
import sys, time
from wnt import config as C, notify, runtime
assert not C.DATABASE_URL, "refusing: DATABASE_URL is set, this test only uses a throwaway SQLite file"
from wnt import depth as depth_mod
from wnt.strategy import Runner
Runner.run_forever = lambda self: time.sleep(3600)
depth_mod.DepthCollector.run_forever = lambda self: time.sleep(3600)
notify.start_listener = lambda: None
runtime.WAIT_S = 0.5
"""
WORKER = STUBS + """
sys.argv = ["worker.py"]
import worker
sys.exit(worker.main())
"""
RENDER = STUBS + """
from streamlit.testing.v1 import AppTest
at = AppTest.from_file("streamlit_app.py", default_timeout=180)
at.run()
if at.exception:
    print("EXC", [e.value for e in at.exception]); sys.exit(1)
print("INFO", [i.value for i in at.info])
"""

print("\n== worker.py ==")
env = dict(os.environ, SQLITE_PATH=tempfile.mktemp(suffix=".db"), PYTHONUNBUFFERED="1")
env.pop("DATABASE_URL", None)
env.pop("RUN_WORKERS", None)
wdb = create_engine("sqlite:///%s" % env["SQLITE_PATH"], future=True)


def holder():
    try:
        return (lease.current(wdb, NAME) or {}).get("holder")
    except Exception:
        return None


def spawn(extra):
    return subprocess.Popen([sys.executable, "-c", WORKER], cwd=ROOT, env=dict(env, **extra), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


p1 = spawn({"KLSH_HOST": "box1"})
check("worker 1 takes the lease", wait_for(lambda: holder() == "box1", 40), holder())
p2 = spawn({"KLSH_HOST": "vps"})
time.sleep(4)
check("worker 2 waits while worker 1 holds the lease", holder() == "box1" and p2.poll() is None, holder())
p1.terminate()
out1 = p1.communicate(timeout=30)[0]
check("worker 1 prints version and host", (C.VERSION + " worker starting on box1") in out1, out1[-400:])
check("worker 1 started the loops", "workers started on box1" in out1, out1[-400:])
check("worker 1 exits 0 on SIGTERM", p1.returncode == 0, p1.returncode)
check("worker 2 takes over after worker 1 stopped", wait_for(lambda: holder() == "vps", 40), holder())
p2.terminate()
out2 = p2.communicate(timeout=30)[0]
check("worker 2 logged who it waited for, then started",
      "is held by box1" in out2 and "workers started on vps" in out2
      and out2.find("is held by box1") < out2.find("workers started on vps"), out2[-600:])
check("worker 2 exits 0 on SIGTERM and gives the lease back", p2.returncode == 0 and holder() is None, (p2.returncode, holder()))
for extra, want, label in (({"RUN_WORKERS": "false"}, "RUN_WORKERS is false", "refuses to idle silently when RUN_WORKERS=false"),
                           ({"KLSH_HOST": "streamlit"}, "belongs to the Streamlit app",
                            "refuses the name 'streamlit' (two places must never share a name)")):
    p = spawn(extra)
    out = p.communicate(timeout=60)[0]
    check("worker.py " + label, p.returncode == 2 and want in out, out[-300:])
check("...and neither touched the lease", holder() is None, holder())

print("\n== dashboard render ==")
for mode in ("false", "true"):
    env2 = dict(env, RUN_WORKERS=mode, SQLITE_PATH=tempfile.mktemp(suffix=".db"))
    r = subprocess.run([sys.executable, "-c", RENDER], cwd=ROOT, env=env2, capture_output=True, text=True, timeout=600)
    check("dashboard renders with RUN_WORKERS=%s" % mode, r.returncode == 0, (r.stdout + r.stderr)[-800:])
    check("...dashboard-only notice %s" % ("shown" if mode == "false" else "not shown"),
          ("Dashboard only" in r.stdout) == (mode == "false"), r.stdout[-300:])

print()
print("%d checks, %d failed" % (CHECKS[0], len(FAILS)))
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL GOOD")
