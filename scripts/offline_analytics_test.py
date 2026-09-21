"""
Offline test for the new percentage numbers and the dashboard. No network, no real orders.

Run from the repo root:   python scripts/offline_analytics_test.py
It builds a throwaway sqlite file with made-up $3 and $5 days, checks every percentage
against numbers worked out by hand, then (if streamlit is installed) renders the real
dashboard against that data and checks that it shows them.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp()
os.environ["SQLITE_PATH"] = os.path.join(_tmp, "analytics_test.db")
os.environ.pop("DATABASE_URL", None)
os.environ["TELEGRAM_TOKEN"] = ""
os.environ["DOLLARS_PER_MARKET"] = "5"

from wnt import analytics, store  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


def near(a, b, tol=0.02) -> bool:
    return a is not None and abs(a - b) <= tol


def row(date, n, size, filled, result, *, mode="live", dry=False, status=None):
    contracts = 11.54 if size == 3 else 19.23
    return {
        "client_order_id": str(uuid.uuid4()), "event_date": date,
        "event_ticker": f"KXWORLDNEWSMENTION-{date}", "market_ticker": f"T-{date}-{n}",
        "title": f"word {n}", "no_price_cents": 26, "yes_price_cents": 74,
        "contracts": contracts, "collateral": round(contracts * 0.26, 4),
        "placed_at": datetime.now(timezone.utc), "dry_run": dry, "mode": mode,
        "status": status or ("filled" if filled else "cancelled"),
        "filled_contracts": contracts if filled else 0.0,
        "avg_fill_price_cents": 26.0 if filled else None, "fees_cents": 0.0,
        "result": result,
    }


ROWS = [
    # Day A: $3. one win, one loss, one unfilled.  pnl 8.5396 - 3.0004 = 5.5392
    row("2026-09-10", 1, 3, True, "no"), row("2026-09-10", 2, 3, True, "yes"),
    row("2026-09-10", 3, 3, False, "no"),
    # Day B: $5. two losses, one win, one unfilled. pnl -4.9998*2 + 14.2302 = 4.2306
    row("2026-09-11", 1, 5, True, "yes"), row("2026-09-11", 2, 5, True, "yes"),
    row("2026-09-11", 3, 5, True, "no"), row("2026-09-11", 4, 5, False, "yes"),
    # Day C: $5, NOT settled yet (no results).
    row("2026-09-12", 1, 5, True, None), row("2026-09-12", 2, 5, False, None),
    # Must be ignored in live-only: paper and smoke.
    row("2026-09-11", 9, 5, True, "no", mode="dry_run", dry=True, status="dry_run"),
    row("2026-09-11", 8, 3, True, "no", mode="smoke", status="filled"),
]


def load() -> None:
    store.init_db()
    for r in ROWS:
        store.record_order(**r)


def test_math() -> None:
    print("\n== percentage math (worked out by hand) ==")
    rows = store.all_orders()
    days = {d["event_date"]: d for d in analytics.day_breakdown(rows, live_only=True)}
    check("3 live days, paper and smoke ignored", sorted(days) == ["2026-09-10", "2026-09-11", "2026-09-12"])

    a, b, c = days["2026-09-10"], days["2026-09-11"], days["2026-09-12"]
    check("Day A ($3): P/L $5.54", near(a["pnl"], 5.5392), f"{a['pnl']:.4f}")
    check("Day A: 92.3% on filled money ($6.00 spent)", near(a["pct_on_filled"], 92.31, 0.05) and near(a["cost_filled"], 6.0008))
    check("Day A: 61.5% on resting money ($9.00 set aside)", near(a["pct_on_resting"], 61.54, 0.05), str(a["pct_on_resting"]))
    check("Day A size label is $3", a["size"] == "$3")
    check("Day B ($5): P/L $4.23, 28.2% on filled ($15.00 spent)",
          near(b["pnl"], 4.2306) and near(b["pct_on_filled"], 28.21, 0.05) and near(b["cost_filled"], 14.9994))
    check("Day B: 21.2% on resting money ($20.00 set aside)", near(b["pct_on_resting"], 21.15, 0.05), str(b["pct_on_resting"]))
    check("Day B size label is $5", b["size"] == "$5")
    check("Day C (unsettled): no % on resting, not counted as settled",
          c["pct_on_resting"] is None and c["fully_settled"] is False and c["pct_on_filled"] is None)

    s = analytics.summarise(rows, live_only=True)
    check("Total P/L $9.77", near(s["total_pnl"], 9.7698), f"{s['total_pnl']:.4f}")
    check("Total return on filled money 46.5% (capital-weighted, NOT the average of 92% and 28%)",
          near(s["pct_on_filled"], 46.52, 0.05), str(s["pct_on_filled"]))
    check("Total return on resting money 33.7% (settled days only)",
          near(s["pct_on_resting"], 33.69, 0.05), str(s["pct_on_resting"]))
    check("Best / worst day % are 92.3 and 28.2",
          near(s["best_day_pct"], 92.31, 0.05) and near(s["worst_day_pct"], 28.21, 0.05))
    check("existing numbers unchanged (fill rate, days, by_day)",
          s["orders_attempted"] == 9 and s["orders_filled"] == 6
          and s["days_settled"] == 2 and set(s["by_day"]) == {"2026-09-10", "2026-09-11"})

    sizes = {g["size"]: g for g in analytics.size_breakdown(rows, live_only=True)}
    check("By size has exactly $3 and $5", sorted(sizes) == ["$3", "$5"])
    check("$3: 92.3% on filled, P(NO|filled) 50%",
          near(sizes["$3"]["pct_on_filled"], 92.31, 0.05) and near(sizes["$3"]["p_no"], 0.5, 0.001))
    check("$5: 28.2% on filled, P(NO|filled) 33%",
          near(sizes["$5"]["pct_on_filled"], 28.21, 0.05) and near(sizes["$5"]["p_no"], 1 / 3, 0.001))

    r = [x for x in rows if x["event_date"] == "2026-09-10" and x["market_ticker"].endswith("-1")][0]
    check("one order: +$8.54 on $3.00 spent = +284.6%", near(analytics.order_pnl_pct(r), 284.6, 0.1),
          str(analytics.order_pnl_pct(r)))
    check("unsettled order has no %", analytics.order_pnl_pct(
        [x for x in rows if x["event_date"] == "2026-09-12" and x["filled_contracts"]][0]) is None)

    line = analytics.day_pnl_lines("2026-09-10").split("\n")[0]
    check("/today line shows both percentages on a settled day",
          "% on $6.00 filled" in line and "% on $9.00 resting" in line, line)
    line_c = analytics.day_pnl_lines("2026-09-12").split("\n")[0]
    check("/today line says 'not settled yet' instead of a fake +0.0%",
          "not settled yet" in line_c and "+0.0%" not in line_c, line_c)
    check("/stats report includes the return line", "Return: +46.5% on $21.00" in analytics.format_report(s))


def test_dashboard() -> None:
    print("\n== real dashboard render ==")
    try:
        from streamlit.testing.v1 import AppTest
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP  streamlit not installed ({exc})")
        return
    at = AppTest.from_file(os.path.join(ROOT, "streamlit_app.py"), default_timeout=90)
    at.run()
    check("dashboard runs with no exception", not at.exception, str([e.value for e in at.exception]))
    labels = {m.label: m.value for m in at.metric}
    check("shows 'Return on filled money' = +46.5%", labels.get("Return on filled money") == "+46.5%", str(labels.get("Return on filled money")))
    check("shows 'Return on resting money' = +33.7%", labels.get("Return on resting money") == "+33.7%", str(labels.get("Return on resting money")))
    check("shows 'Best / worst day'", labels.get("Best / worst day") == "+92.3% / +28.2%", str(labels.get("Best / worst day")))
    check("shows 'Size per name now' = $5.00", labels.get("Size per name now") == "$5.00", str(labels.get("Size per name now")))
    frames = [df.value for df in at.dataframe]
    cols = [set(f.columns) for f in frames]
    check("By-day table has the % columns", any({"% on filled", "% on resting", "size"} <= c for c in cols))
    check("By-size table lists $3 and $5",
          any("size" in f.columns and set(f["size"]) == {"$3", "$5"} and "days" in f.columns for f in frames))
    check("All-orders table has pnl_pct", any("pnl_pct" in c for c in cols))
    captions = " ".join(c.value for c in at.caption)
    check("caption shows the size per name", "size $5.00/name" in captions, captions[:200])


if __name__ == "__main__":
    load()
    test_math()
    test_dashboard()
    print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} CHECK(S) FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)
