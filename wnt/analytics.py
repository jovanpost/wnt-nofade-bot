"""Fill rate, P(NO|filled), and P/L."""
from __future__ import annotations

from statistics import mean, stdev

from . import config as C, store


def order_pnl(row: dict) -> float | None:
    filled = row.get("filled_contracts") or 0
    result = row.get("result")
    if not filled or result not in ("yes", "no"):
        return None
    price = row.get("avg_fill_price_cents") or row.get("no_price_cents") or 0
    gross = filled * ((100 - price) if result == "no" else -price)
    return (gross - (row.get("fees_cents") or 0)) / 100.0


def summarise(rows: list[dict] | None = None) -> dict:
    rows = rows if rows is not None else store.all_orders()
    live = [
        r for r in rows
        if r.get("status") != "rejected"
        and r.get("mode") != "smoke"
        and not str(r.get("client_order_id") or "").startswith("wnt-smoke-")
    ]

    attempted = len(live)
    filled = [r for r in live if (r.get("filled_contracts") or 0) > 0]
    settled = [r for r in filled if r.get("result") in ("yes", "no")]
    settled_no = [r for r in settled if r["result"] == "no"]

    fill_rate = len(filled) / attempted if attempted else None
    p_no = len(settled_no) / len(settled) if settled else None

    pnls = [p for p in (order_pnl(r) for r in settled) if p is not None]

    by_day: dict[str, float] = {}
    for row in settled:
        value = order_pnl(row)
        if value is not None:
            by_day[row["event_date"]] = by_day.get(row["event_date"], 0.0) + value
    day_values = list(by_day.values())

    taker_fills = len([r for r in filled if (r.get("fees_cents") or 0) > 0])

    return {
        "orders_attempted": attempted,
        "orders_filled": len(filled),
        "fill_rate": fill_rate,
        "fill_rate_target": C.BACKTEST_FILL_RATE,
        "settled_fills": len(settled),
        "p_no_given_filled": p_no,
        "p_no_target": C.BACKTEST_P_NO_GIVEN_FILL,
        "total_pnl": sum(pnls) if pnls else 0.0,
        "days_settled": len(day_values),
        "mean_day": mean(day_values) if day_values else None,
        "sd_day": stdev(day_values) if len(day_values) > 1 else None,
        "best_day": max(day_values) if day_values else None,
        "worst_day": min(day_values) if day_values else None,
        "winning_days": len([d for d in day_values if d > 0]),
        "fills_with_fees": taker_fills,
        "by_day": dict(sorted(by_day.items())),
    }


def scaling_verdict(stats: dict) -> tuple[str, str]:
    days = stats["days_settled"]
    fill_rate = stats["fill_rate"]
    p_no = stats["p_no_given_filled"]

    if fill_rate is None or days == 0:
        return "WAIT", "Not enough settled days yet. Keep collecting."

    if p_no is not None and stats["settled_fills"] >= 20:
        if p_no < C.BACKTEST_P_NO_GIVEN_FILL - 0.08:
            return "FREEZE", (
                f"P(NO|filled) is {p_no:.0%} against a {C.BACKTEST_P_NO_GIVEN_FILL:.0%} "
                f"baseline. Do not add size."
            )

    if fill_rate < C.BACKTEST_FILL_RATE * 0.6:
        return "FREEZE", (
            f"Fill rate is {fill_rate:.0%} against a {C.BACKTEST_FILL_RATE:.0%} "
            f"baseline. Diagnose before adding size."
        )

    if days < 10:
        return "WAIT", (
            f"{days} settled day(s) of {10} needed. Fill rate {fill_rate:.0%} "
            f"vs {C.BACKTEST_FILL_RATE:.0%} target -- on track."
        )

    if fill_rate >= C.BACKTEST_FILL_RATE * 0.8:
        return "SCALE", (
            f"{days} days, fill rate {fill_rate:.0%}. Rules say step size up."
        )

    return "HOLD", f"Fill rate {fill_rate:.0%} is soft. Hold size and watch."


def format_report(stats: dict) -> str:
    def pct(value):
        return f"{value:.0%}" if value is not None else "n/a"

    verdict, why = scaling_verdict(stats)
    return (
        f"<b>WNT no-fade — running totals</b>\n"
        f"Orders rested: {stats['orders_attempted']}\n"
        f"Filled: {stats['orders_filled']} ({pct(stats['fill_rate'])}) "
        f"vs backtest {pct(stats['fill_rate_target'])}\n"
        f"P(NO|filled): {pct(stats['p_no_given_filled'])} "
        f"vs backtest {pct(stats['p_no_target'])}  [{stats['settled_fills']} settled]\n"
        f"P/L: ${stats['total_pnl']:.2f} over {stats['days_settled']} day(s)\n"
        + (f"Mean/day: ${stats['mean_day']:.2f}\n" if stats["mean_day"] is not None else "")
        + (f"Worst day: ${stats['worst_day']:.2f}\n" if stats["worst_day"] is not None else "")
        + f"Fills that paid a fee: {stats['fills_with_fees']}\n"
        f"\n<b>{verdict}</b> — {why}"
    )
