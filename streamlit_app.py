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

import pandas as pd
import streamlit as st

import mdkit

from wnt import analytics, clock, config as C, depth as depth_mod, runtime, store
from wnt.strategy import STATE, Runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)

st.set_page_config(page_title="WNT No-Fade Bot", page_icon="📉", layout="wide")


@st.cache_resource
def boot():
    """Once per app process. The loops start only if RUN_WORKERS is on AND this place holds the
    worker lease (wnt/runtime.py); otherwise this page is a dashboard. The Telegram commands live
    in wnt/runtime.py."""
    runtime.start_workers(where="streamlit")
    return runtime.services()


# The keep-alive workflow hits ?ping=true; bail out cheaply.
if st.query_params.get("ping") == "true":
    boot()
    st.write("alive")
    st.stop()

services = boot()
runner: Runner = services["runner"]

# ---------------------------------------------------------------------------
st.title("📉 WNT No-Fade Bot")
_lvl, _txt = runtime.banner()
if _lvl != "ok":
    {"info": st.info, "warning": st.warning, "error": st.error}[_lvl](_txt)

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
# ONE read of the orders table, reused by every section below (it used to be read 3 times).
all_rows = store.all_orders()
live_days = analytics.day_breakdown(all_rows, live_only=True)
size_rows = analytics.size_breakdown(all_rows, live_only=True)

today_rows = analytics.canonical_orders(
    [r for r in store.orders_for_day(clock.today_ct()) if analytics.is_live_cash(r)]
)
today_rested = len(today_rows)
today_filled = len([r for r in today_rows if (r.get("filled_contracts") or 0) > 0])
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Loop", "running" if STATE["running"] else "stopped")
c2.metric("Today's event", STATE["active_event"] or "—")
c3.metric("Orders today", today_rested or STATE["orders_today"])
c4.metric("Fills today", today_filled)
c5.metric("Depth snapshots", depth_mod.STATE["snapshots"])

st.caption(
    f"{'DRY RUN' if C.DRY_RUN else ('DEMO' if C.USE_DEMO else 'LIVE')} · "
    f"size ${C.collateral_per_market():.2f}/name ({C.CONTRACTS:g} contracts) · "
    f"now {clock.now_ct():%-I:%M:%S %p} CT · last poll {clock.fmt(STATE['last_poll'])}"
)
if STATE["last_error"]:
    st.warning(f"Last error: {STATE['last_error']}")

st.caption("Commands are Telegram-only. This page cannot place, pause, or cancel.")

_NOW_TXT = clock.fmt(clock.now_ct())


def _page(title):
    mdkit.page(title, C.VERSION, _NOW_TXT)


_page("Summary")
# ---------------------------------------------------------------------------
right = st.container()
with right:
    live_stats = analytics.summarise(all_rows, live_only=True)
    st.subheader("Live book only")
    st.caption("Kalshi cash. Paper / dry_run rows are excluded.")
    l1, l2, l3, l4 = st.columns(4)
    live_fill = live_stats["fill_rate"]
    live_pno = live_stats["p_no_given_filled"]
    l1.metric(
        "Fill rate",
        f"{live_fill:.0%}" if live_fill is not None else "—",
        delta=f"{live_stats['orders_filled']} / {live_stats['orders_attempted']} names",
    )
    l2.metric(
        "P(NO | filled)",
        f"{live_pno:.0%}" if live_pno is not None else "—",
        delta=f"{live_stats['settled_fills']} settled fills",
    )
    l3.metric(
        "Realised P/L",
        f"${live_stats['total_pnl']:.2f}",
        delta=f"{live_stats['days_settled']} live day(s)",
    )
    l4.metric(
        "Taker fills",
        str(live_stats["fills_with_fees"]),
        help="Crossed the book (fee > 0). Maker rest should stay 0.",
    )
    # --- percentages: size-neutral, so $3 days and $5 days can be compared ---
    p1, p2, p3, p4 = st.columns(4)
    pf, pr = live_stats["pct_on_filled"], live_stats["pct_on_resting"]
    p1.metric(
        "Return on filled money",
        f"{pf:+.1f}%" if pf is not None else "—",
        delta=f"${live_stats['total_cost_filled']:.0f} spent on fills",
        delta_color="off",
        help="Profit ÷ the money actually spent on filled orders (settled only). "
             "Of every $100 that went into fills, how many dollars came back as profit.",
    )
    p2.metric(
        "Return on resting money",
        f"{pr:+.1f}%" if pr is not None else "—",
        delta=f"${live_stats['resting_settled']:.0f} set aside",
        delta_color="off",
        help="Profit ÷ all the cash set aside on every name, filled or not. "
             "Counts only days where every name has settled.",
    )
    best_pct, worst_pct = live_stats["best_day_pct"], live_stats["worst_day_pct"]
    p3.metric(
        "Best / worst day",
        (f"{best_pct:+.1f}% / {worst_pct:+.1f}%"
         if best_pct is not None and worst_pct is not None else "—"),
        help="Return on filled money, by day.",
    )
    p4.metric(
        "Size per name now",
        f"${C.collateral_per_market():.2f}",
        delta=f"{C.CONTRACTS:g} contracts", delta_color="off",
        help="Set DOLLARS_PER_MARKET in the Streamlit secrets to change it.",
    )
    if live_stats["by_day"]:
        pct_by_day = {d["event_date"]: d["pct_on_filled"] for d in live_days}
        days_txt = " · ".join(
            f"{d}: ${v:+.2f}"
            + (f" ({pct_by_day[d]:+.1f}%)" if pct_by_day.get(d) is not None else "")
            for d, v in live_stats["by_day"].items()
        )
        st.caption(days_txt)
    if len(all_rows) >= 2000:
        st.caption("Totals cover only the newest 2,000 order rows.")

    stats = analytics.summarise(all_rows)
    st.subheader("Is the backtest holding up?")
    st.caption("Paper + live combined. Do not size off this after going live.")
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
              delta=(f"{stats['days_settled']} settled day(s)"
                     + (f" · {stats['pct_on_filled']:+.1f}% on filled money"
                        if stats["pct_on_filled"] is not None else "")),
              delta_color="off")

    verdict, why = analytics.scaling_verdict(stats)
    {"SCALE": st.success, "FREEZE": st.error,
     "HOLD": st.warning, "WAIT": st.info}[verdict](f"**{verdict}** — {why}")

# ---------------------------------------------------------------------------
tab_ledger, tab_today, tab_orders, tab_days, tab_depth, tab_log = st.tabs(
    ["Ledger (day / week)", "Today", "All orders", "By day", "Depth", "Activity"]
)

NAMES = {r["market_ticker"]: r.get("title") for r in all_rows if r.get("title")}


def _nm(ticker):
    return NAMES.get(ticker) or ticker


def _said(result):
    return {"yes": "SAID (YES)", "no": "not said (NO)"}.get(result or "", "waiting for result")


with tab_ledger:
    _page("Ledger")
    live_rows = analytics.canonical_orders([r for r in all_rows if analytics.is_live_cash(r)])
    per = mdkit.period_picker([r["event_date"] for r in live_rows], "ledger", clock.today_ct())
    st.caption(f"Showing: {per['label']}. Live (real money) orders only. All times are Central.")
    sel = [r for r in live_rows if per["match"](r["event_date"])]
    all_f = store.all_fills()
    by_oid: dict = {}
    for f in all_f:
        by_oid.setdefault(str(f.get("order_id")), []).append(f)
    if not sel:
        st.info("No live orders in this period.")
    else:
        rows_l = []
        net = spent = 0.0
        wins = settled = 0
        for r in sorted(sel, key=lambda x: (x["event_date"], str(x.get("placed_at")))):
            fl = by_oid.get(str(r.get("order_id")), [])
            filled = float(r.get("filled_contracts") or 0)
            avg = r.get("avg_fill_price_cents")
            cost = filled * float(avg or 0) / 100.0
            pnl = analytics.order_pnl(r)
            spent += cost
            if pnl is not None:
                net += pnl
                settled += 1
                wins += 1 if r.get("result") == "no" else 0
            n_t = sum(1 for f in fl if f.get("is_taker"))
            rows_l.append({
                "date": r["event_date"], "word": r.get("title") or r["market_ticker"],
                "placed": mdkit.ct_time(r.get("placed_at"), C.CT),
                "order": f"buy NO at {r['no_price_cents']}¢ or less x {float(r.get('contracts') or 0):g}",
                "filled": round(filled, 2),
                "first fill": mdkit.ct_time(r.get("first_fill_at"), C.CT),
                "fills": (f"{len(fl)} ({n_t} taker, {len(fl) - n_t} maker)") if fl else "0",
                "avg NO price ¢": None if avg is None else round(float(avg), 2),
                "cost $": round(cost, 2),
                "fees $": round(float(r.get("fees_cents") or 0) / 100.0, 2),
                "ended": r.get("status"),
                "cancelled": mdkit.ct_time(r.get("cancelled_at"), C.CT),
                "result": _said(r.get("result")) if filled > 0 else "not filled",
                "paid out $": (round(filled, 2) if r.get("result") == "no" else 0.0) if (r.get("result") and filled > 0) else None,
                "P&L $": None if pnl is None else round(pnl, 2),
                "return %": analytics.order_pnl_pct(r),
            })
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Words ordered", len(rows_l))
        m2.metric("Filled", sum(1 for x in rows_l if x["filled"] > 0))
        m3.metric("Won / settled", f"{wins} / {settled}")
        m4.metric("Money in fills", f"${spent:.2f}")
        m5.metric("Net P&L (after fees)", f"{net:+.2f}")
        st.dataframe(pd.DataFrame(rows_l), use_container_width=True, hide_index=True)
        oids = {str(r.get("order_id")) for r in sel if r.get("order_id")}
        fl_rows = [{
            "time": mdkit.ct_time(f.get("created_at"), C.CT), "date": f.get("event_date"),
            "word": _nm(f.get("market_ticker")), "contracts": round(float(f.get("contracts") or 0), 2),
            "NO price ¢": f.get("price_cents"),
            "type": "taker (instant)" if f.get("is_taker") else "maker (rested)",
            "fee $": round(float(f.get("fee_cents") or 0) / 100.0, 2),
            "cost $": round(float(f.get("contracts") or 0) * float(f.get("price_cents") or 0) / 100.0, 2),
        } for f in all_f if str(f.get("order_id")) in oids]
        st.subheader("Every real fill")
        st.dataframe(pd.DataFrame(fl_rows), use_container_width=True, hide_index=True)

with tab_today:
    _page("Today")
    rows = store.orders_for_day(clock.today_ct())
    if rows:
        df = pd.DataFrame(rows)[[
            "title", "status", "no_price_cents", "contracts",
            "yes_bid_at_place", "yes_ask_at_place", "filled_contracts",
            "avg_fill_price_cents", "fees_cents", "result", "reject_reason",
        ]]
        df["pnl $"] = [analytics.order_pnl(r) for r in rows]
        df["pnl %"] = [analytics.order_pnl_pct(r) for r in rows]
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.write("Nothing today yet.")

with tab_orders:
    _page("All orders")
    rows = all_rows[:1000]  # same newest-first rows, no second database read
    if rows:
        df = pd.DataFrame(rows)
        df["pnl"] = df.apply(lambda r: analytics.order_pnl(dict(r)), axis=1)
        df["pnl_pct"] = df.apply(lambda r: analytics.order_pnl_pct(dict(r)), axis=1)
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.download_button("Download CSV", df.to_csv(index=False),
                           "wnt_orders.csv", "text/csv")
    else:
        st.write("No orders recorded yet.")

with tab_days:
    _page("By day")
    if live_days:
        st.subheader("Live days (real money)")
        st.caption("Percentages ignore size, so a $3 day and a $5 day compare fairly. "
                   "'% on filled' = profit ÷ money spent on fills. "
                   "'% on resting' = profit ÷ cash set aside on every name "
                   "(shown once the whole day has settled).")
        st.dataframe(pd.DataFrame([{
            "date": d["event_date"], "size": d["size"], "orders": d["orders"],
            "filled": d["filled"],
            "fill %": None if d["fill_rate"] is None else round(100 * d["fill_rate"], 1),
            "P(NO|filled) %": None if d["p_no"] is None else round(100 * d["p_no"], 1),
            "P/L $": round(d["pnl"], 2),
            "% on filled": None if d["pct_on_filled"] is None else round(d["pct_on_filled"], 1),
            "% on resting": None if d["pct_on_resting"] is None else round(d["pct_on_resting"], 1),
            "day settled": d["fully_settled"],
        } for d in reversed(live_days)]), use_container_width=True, hide_index=True)

        day_pct = pd.Series(
            {d["event_date"]: d["pct_on_filled"] for d in live_days
             if d["pct_on_filled"] is not None}, name="Return on filled money (%)")
        if len(day_pct):
            st.caption("Daily return on filled money (%)")
            st.bar_chart(day_pct)
        run_pnl = run_cost = 0.0
        cumulative = {}
        for d in live_days:
            run_pnl += d["pnl"]
            run_cost += d["cost_filled"]
            if run_cost:
                cumulative[d["event_date"]] = 100.0 * run_pnl / run_cost
        if cumulative:
            st.caption("Running return on filled money (%), all live days so far")
            st.line_chart(pd.Series(cumulative, name="Running return (%)"))

        st.subheader("By size per name")
        st.caption("Same numbers split by how much went on each name. Watch P(NO|filled): "
                   "if it falls as size goes up, bigger orders are attracting worse fills.")
        st.dataframe(pd.DataFrame([{
            "size": g["size"], "days": g["days"], "orders": g["orders"], "filled": g["filled"],
            "fill %": None if g["fill_rate"] is None else round(100 * g["fill_rate"], 1),
            "settled fills": g["settled_fills"],
            "P(NO|filled) %": None if g["p_no"] is None else round(100 * g["p_no"], 1),
            "P/L $": round(g["pnl"], 2),
            "% on filled": None if g["pct_on_filled"] is None else round(g["pct_on_filled"], 1),
            "% on resting": None if g["pct_on_resting"] is None else round(g["pct_on_resting"], 1),
        } for g in size_rows]), use_container_width=True, hide_index=True)
        st.divider()
        st.subheader("Paper + live combined (dollars)")
    if stats["by_day"]:
        st.bar_chart(pd.Series(stats["by_day"], name="P/L ($)"))
    days_rows = store.recent_days()
    if days_rows:
        st.dataframe(pd.DataFrame(days_rows), use_container_width=True,
                     hide_index=True)

with tab_depth:
    _page("Depth")
    st.caption(f"{store.depth_row_count():,} snapshots stored. "
               f"Last sweep {clock.fmt(depth_mod.STATE['last_run'])}.")
    today_rows = store.orders_for_day(clock.today_ct())
    tickers = sorted({r["market_ticker"] for r in today_rows})
    if tickers:
        pick = st.selectbox("Market", tickers, format_func=_nm)
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
    _page("Activity")
    entries = store.recent_activity()
    if entries:
        st.dataframe(pd.DataFrame(entries), use_container_width=True,
                     hide_index=True)

mdkit.done()
