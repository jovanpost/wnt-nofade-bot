"""
Streamlit entry point.

Same shape as abcnws-live-bot: the page is a dashboard, and the actual work
happens in background threads started once via @st.cache_resource. The
GitHub Actions keep-alive pings the page so Streamlit Cloud does not put the
app to sleep.

Read this before trusting it: Streamlit Community Cloud can restart this
process at any time. That is survivable here only because the risky
operations are designed not to care --

  * orders carry a server-side expiry, so a dead app still ends the day flat
  * a separate GitHub Actions job cancels at 5:29 CT independently
  * client_order_ids are deterministic, so a restart cannot double an order
  * the cancel reads its list from Kalshi, never from this process's memory
"""
from __future__ import annotations

import logging
import threading

import pandas as pd
import streamlit as st

from wnt import analytics, clock, config as C, depth as depth_mod, notify, store
from wnt.kalshi import KalshiClient
from wnt.strategy import STATE, Runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)

st.set_page_config(page_title="WNT No-Fade Bot", page_icon="📉", layout="wide")


@st.cache_resource
def boot():
    """Start everything exactly once per app process."""
    store.init_db()
    client = KalshiClient()
    runner = Runner(client)
    collector = depth_mod.DepthCollector(client)

    def cmd_status(_args):
        return (
            f"<pre>{notify.esc(C.summary())}</pre>\n"
            f"running={STATE['running']} event={STATE['active_event']}\n"
            f"orders_today={STATE['orders_today']} fills_today={STATE['fills_today']}\n"
            f"last_poll={clock.fmt(STATE['last_poll'])}\n"
            f"paused={store.is_paused()} depth_snapshots={depth_mod.STATE['snapshots']}\n"
            f"storage={'postgres' if store.using_postgres() else 'SQLITE (not durable!)'}"
        )

    def cmd_today(_args):
        rows = store.orders_for_day(clock.today_ct())
        if not rows:
            return "No orders recorded today."
        live = [r for r in rows if r.get("status") != "rejected"]
        filled = [r for r in live if (r.get("filled_contracts") or 0) > 0]
        rate = len(filled) / len(live) if live else 0
        lines = [f"{len(live)} rested, {len(filled)} filled ({rate:.0%})"]
        lines += [f"• {r['title']}: {r['status']}" for r in rows[:20]]
        return "\n".join(lines)

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
        ("status", cmd_status), ("today", cmd_today), ("cancelnow", cmd_cancelnow),
        ("pause", cmd_pause), ("resume", cmd_resume), ("balance", cmd_balance),
        ("stats", cmd_stats),
    ]:
        notify.register(name, fn)
    notify.start_listener()

    threading.Thread(target=runner.run_forever, daemon=True, name="strategy").start()
    threading.Thread(target=collector.run_forever, daemon=True, name="depth").start()
    return {"runner": runner, "collector": collector, "client": client}


# The keep-alive workflow hits ?ping=true; bail out cheaply.
if st.query_params.get("ping") == "true":
    boot()
    st.write("alive")
    st.stop()

services = boot()
runner: Runner = services["runner"]

# ---------------------------------------------------------------------------
st.title("📉 WNT No-Fade Bot")

if C.DRY_RUN:
    st.info("**DRY RUN** — the bot does everything except actually submit orders.")
elif C.USE_DEMO:
    st.warning("**DEMO EXCHANGE** — real orders, fake money.")
else:
    st.error("**LIVE** — this is placing real orders with real money.")

if not store.using_postgres():
    st.error(
        "No DATABASE_URL set, so everything is going to a local sqlite file. "
        "On Streamlit Cloud that file is deleted on every restart and every "
        "push. Set DATABASE_URL before you rely on any of these numbers."
    )
if not runner.client.authenticated:
    st.error("Kalshi API key is not loaded. Market data will work; orders will not.")
if store.is_paused():
    st.warning("Bot is PAUSED. It will not place new orders.")

# ---------------------------------------------------------------------------
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Loop", "running" if STATE["running"] else "stopped")
c2.metric("Today's event", STATE["active_event"] or "—")
c3.metric("Orders today", STATE["orders_today"])
c4.metric("Fills today", STATE["fills_today"])
c5.metric("Depth snapshots", depth_mod.STATE["snapshots"])

st.caption(
    f"{'DRY RUN' if C.DRY_RUN else ('DEMO' if C.USE_DEMO else 'LIVE')} · "
    f"now {clock.now_ct():%-I:%M:%S %p} CT · last poll {clock.fmt(STATE['last_poll'])}"
)
if STATE["last_error"]:
    st.warning(f"Last error: {STATE['last_error']}")

st.caption("Commands are Telegram-only. This page cannot place, pause, or cancel.")

# ---------------------------------------------------------------------------
right = st.container()
with right:
    stats = analytics.summarise()
 st.subheader("Is the backtest holding up?")
    m1, m2, m3 = st.columns(3)

    fill_rate = stats["fill_rate"]
    m1.metric(
        "Fill rate", f"{fill_rate:.0%}" if fill_rate is not None else "—",
        delta=(f"{(fill_rate - C.BACKTEST_FILL_RATE) * 100:+.0f} pts vs backtest"
               if fill_rate is not None else None),
    )
    p_no = stats["p_no_given_filled"]
    m2.metric(
        "P(NO | filled)", f"{p_no:.0%}" if p_no is not None else "—",
        delta=(f"{(p_no - C.BACKTEST_P_NO_GIVEN_FILL) * 100:+.0f} pts vs backtest"
               if p_no is not None else None),
        help="The capacity meter. If this drifts DOWN as size grows, informed "
             "flow is finding you. Freeze size.",
    )
    m3.metric("Realised P/L", f"${stats['total_pnl']:.2f}",
              delta=f"{stats['days_settled']} settled day(s)")

    verdict, why = analytics.scaling_verdict(stats)
    {"SCALE": st.success, "FREEZE": st.error,
     "HOLD": st.warning, "WAIT": st.info}[verdict](f"**{verdict}** — {why}")

# ---------------------------------------------------------------------------
tab_today, tab_orders, tab_days, tab_depth, tab_log = st.tabs(
    ["Today", "All orders", "By day", "Depth", "Activity"]
)

with tab_today:
    rows = store.orders_for_day(clock.today_ct())
    if rows:
        df = pd.DataFrame(rows)[[
            "title", "market_ticker", "status", "no_price_cents", "contracts",
            "yes_bid_at_place", "yes_ask_at_place", "filled_contracts",
            "avg_fill_price_cents", "fees_cents", "result", "reject_reason",
        ]]
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.write("Nothing today yet.")

with tab_orders:
    rows = store.all_orders(limit=1000)
    if rows:
        df = pd.DataFrame(rows)
        df["pnl"] = df.apply(lambda r: analytics.order_pnl(dict(r)), axis=1)
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.download_button("Download CSV", df.to_csv(index=False),
                           "wnt_orders.csv", "text/csv")
    else:
        st.write("No orders recorded yet.")

with tab_days:
    if stats["by_day"]:
        st.bar_chart(pd.Series(stats["by_day"], name="P/L ($)"))
    days_rows = store.recent_days()
    if days_rows:
        st.dataframe(pd.DataFrame(days_rows), use_container_width=True,
                     hide_index=True)

with tab_depth:
    st.caption(f"{store.depth_row_count():,} snapshots stored. "
               f"Last sweep {clock.fmt(depth_mod.STATE['last_run'])}.")
    today_rows = store.orders_for_day(clock.today_ct())
    tickers = sorted({r["market_ticker"] for r in today_rows})
    if tickers:
        pick = st.selectbox("Market", tickers)
        series = store.depth_for_market(clock.today_ct(), pick)
        if series:
            df = pd.DataFrame(series).set_index("ts")
            st.line_chart(df[["best_yes_bid", "best_no_bid"]])
            st.line_chart(df[["no_size_ahead", "no_size_at_our_price",
                              "yes_size_that_would_fill_us"]])
        else:
            st.write("No snapshots for this market yet.")
    else:
        st.write("No markets tracked today yet.")

with tab_log:
    entries = store.recent_activity()
    if entries:
        st.dataframe(pd.DataFrame(entries), use_container_width=True,
                     hide_index=True)
