"""Start the bot's background work in exactly ONE place: Streamlit Cloud or the server.

Both entry points call start_workers():
  streamlit_app.py  -> start_workers(where="streamlit")            never blocks the page
  worker.py         -> start_workers(where="vps", block=True)      headless, run by systemd

RUN_WORKERS = "false" (a setting, default true) makes a place dashboard-only: the tables are
checked and the objects the page reads are built, nothing else starts. The worker lease
(wnt/lease.py) makes sure only one place runs the loops even when both have RUN_WORKERS on: the
second one waits.

The Telegram commands live here (they used to live in streamlit_app.py) so both entry points
share them.
"""
from __future__ import annotations

import logging
import os
import threading

from . import analytics, clock, config as C, depth as depth_mod, lease, notify, store
from .kalshi import KalshiClient
from .strategy import STATE, Runner

log = logging.getLogger("wnt.runtime")

NAME = "wnt-nofade-bot"
WAIT_S = 10                      # how often a waiting place looks at the lease again

_lock = threading.Lock()
_started = False
_keeper: lease.Keeper | None = None
_exit_on_lost = False
_services: dict = {}
INFO = {"where": "", "host": "", "run_workers": True, "loops": []}


def host(where: str = "") -> str:
    """Who we are in the lease: the KLSH_HOST setting, else the entry point's own name."""
    return (C._secret("KLSH_HOST", "") or where or "unknown").strip()


def run_workers() -> bool:
    return C._flag("RUN_WORKERS", True)


def services() -> dict:
    """client, runner, collector: built once, used by the page and by the loops."""
    if not _services:
        client = KalshiClient()
        _services.update(client=client, runner=Runner(client), collector=depth_mod.DepthCollector(client))
    return _services


def register_commands() -> None:
    svc = services()
    runner, client = svc["runner"], svc["client"]

    def cmd_status(_args):
        return (
            f"<pre>{notify.esc(C.summary())}</pre>\n"
            f"running={STATE['running']} event={STATE['active_event']}\n"
            f"orders_today={STATE['orders_today']} fills_today={STATE['fills_today']}\n"
            f"last_poll={clock.fmt(STATE['last_poll'])}\n"
            f"paused={store.is_paused()} depth_snapshots={depth_mod.STATE['snapshots']}\n"
            f"storage={'postgres' if store.using_postgres() else 'SQLITE (not durable!)'}\n"
            f"host: {notify.esc(INFO['host'])} · {C.VERSION}"
        )

    def cmd_today(_args):
        return analytics.day_pnl_lines(clock.today_ct())

    def cmd_pnl(_args):
        day = _args[0] if _args else clock.today_ct()
        return analytics.day_pnl_lines(day)

    def cmd_cancelnow(_args):
        result = runner.cancel_all(reason="manual /cancelnow")
        return (f"Cancelled {result['cancelled']}, "
                f"{result['remaining']} left, verified={result['verified']}")

    def cmd_pause(_args):
        store.set_state("paused", True)
        return "⏸ Paused. No new orders will be placed. Resting orders are untouched — use /cancelnow for those."

    def cmd_resume(_args):
        store.set_state("paused", False)
        return "▶️ Resumed."

    def cmd_balance(_args):
        try:
            data = client.get_balance()
            return f"Cash: ${(data.get('balance') or 0) / 100:.2f}"
        except Exception as exc:
            return f"Balance lookup failed: {exc}"

    def cmd_stats(_args):
        return analytics.format_report(analytics.summarise())

    for name, fn in [
        ("status", cmd_status), ("today", cmd_today), ("pnl", cmd_pnl),
        ("cancelnow", cmd_cancelnow),
        ("pause", cmd_pause), ("resume", cmd_resume), ("balance", cmd_balance),
        ("stats", cmd_stats),
    ]:
        notify.register(name, fn)


def _start_loops() -> None:
    svc = services()
    notify.start_listener()
    threading.Thread(target=svc["runner"].run_forever, daemon=True, name="strategy").start()
    threading.Thread(target=svc["collector"].run_forever, daemon=True, name="depth").start()
    INFO["loops"] = ["strategy", "depth"]
    msg = "🔑 %s workers started on %s" % (C.VERSION, INFO["host"])
    log.info(msg)
    store.log_activity("workers_start", "host=%s loops=%s" % (INFO["host"], ",".join(INFO["loops"])))
    notify.send(notify.esc(msg), quiet=True)


def _on_lost(other: str) -> None:
    msg = "⚠️ %s on %s LOST the worker lease to %s. Workers stopped here." % (C.VERSION, INFO["host"], other)
    log.error(msg)
    try:
        svc = services()
        svc["runner"].stop()         # ends the strategy loop and any fast-open wait
        svc["collector"].stop()
        store.log_activity("workers_lost", "host=%s now=%s" % (INFO["host"], other), level="error")
        notify.send(notify.esc(msg))
    finally:
        if _exit_on_lost:
            os._exit(3)              # systemd starts it again; it then waits for the lease


def start_workers(where: str, block: bool = False, exit_on_lost: bool = False) -> dict:
    """Idempotent. Returns INFO. With block=True it returns once the loops are running."""
    global _started, _keeper, _exit_on_lost
    with _lock:
        if _started:
            return INFO
        _started = True
        _exit_on_lost = exit_on_lost
        INFO.update(where=where, host=host(where), run_workers=run_workers())
        store.init_db()
        services()
        if not INFO["run_workers"]:
            lease.GATE.set("dashboard", name=NAME, holder=INFO["host"])
            log.info("dashboard only (RUN_WORKERS=false): no listener, no loops")
            return INFO
        register_commands()
        _keeper = lease.Keeper(store.engine(), NAME, INFO["host"], on_lost=_on_lost)
        lease.GATE.set("waiting", name=NAME, holder=INFO["host"], other="?")

    def go() -> None:
        if _keeper.wait(every_s=WAIT_S, say=log.info):
            _start_loops()

    if block:
        go()
    else:
        threading.Thread(target=go, name="nofade-lease-wait", daemon=True).start()
    return INFO


def interrupt() -> None:
    """Safe inside a signal handler: only wakes a worker that is still waiting for the lease."""
    if _keeper is not None:
        _keeper._stop.set()


def stop() -> None:
    """worker.py on SIGTERM: give the lease back so the other place can start at once."""
    if _keeper is not None:
        _keeper.stop(give_up=True)


def banner() -> tuple:
    """(level, text) for the top of the dashboard. level: info | warning | error | ok."""
    g = lease.GATE
    held_by = ""
    if g.mode in ("dashboard", "waiting", "lost"):
        try:
            cur = lease.current(store.engine(), NAME)
            held_by = "%s, last heartbeat %.0fs ago" % (cur["holder"], cur["age_s"]) if cur else "nobody"
        except Exception:
            held_by = "unknown (the lease table could not be read)"
    if g.mode == "dashboard":
        return ("info", "Dashboard only: the workers do not run here. Worker lease: %s." % held_by)
    if g.mode == "waiting":
        return ("warning", "Workers are WAITING here: the worker lease is held by %s." % held_by)
    if g.mode == "lost":
        return ("error", "Workers STOPPED here: the worker lease was taken by %s. Reboot the app to try again." % held_by)
    if g.mode == "held" and not lease.may_trade():
        return ("error", "Lease not renewed for over %ds (database unreachable?). No new orders until it renews." % lease.SAFE_S)
    return ("ok", "Workers run here (%s)." % INFO["host"])


def reset_for_tests() -> None:
    global _started, _keeper, _exit_on_lost
    if _keeper is not None:
        _keeper.stop(give_up=False)
    _started, _keeper, _exit_on_lost = False, None, False
    _services.clear()
    INFO.update(where="", host="", run_workers=True, loops=[])
    lease.GATE.reset()
