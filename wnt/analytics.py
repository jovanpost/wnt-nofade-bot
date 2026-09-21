"""Fill rate, P(NO|filled), and P/L."""
from __future__ import annotations

from collections import Counter
from statistics import mean, stdev

from . import config as C, store


def is_smoke(row: dict) -> bool:
    return (
        row.get("mode") == "smoke"
        or str(row.get("client_order_id") or "").startswith("wnt-smoke-")
    )


def canonical_orders(rows: list[dict]) -> list[dict]:
    """One row per ticker. Skip rejects/smoke. Keep the lot that filled."""
    best: dict[str, dict] = {}
    for row in rows:
        if row.get("status") == "rejected" or is_smoke(row):
            continue
        ticker = row.get("market_ticker") or row.get("title") or ""
        prev = best.get(ticker)
        fill = float(row.get("filled_contracts") or 0)
        prev_fill = float(prev.get("filled_contracts") or 0) if prev else -1.0
        if prev is None or fill > prev_fill:
            best[ticker] = row
    return list(best.values())


def day_pnl_lines(event_date: str, rows: list[dict] | None = None) -> str:
    rows = rows if rows is not None else store.orders_for_day(event_date)
    live = canonical_orders(rows)
    filled = [r for r in live if (r.get("filled_contracts") or 0) > 0]
    pnl_total = 0.0
    risked = 0.0
    lines = []
    for row in filled:
        pnl = order_pnl(row)
        price = float(row.get("avg_fill_price_cents") or row.get("no_price_cents") or 0)
        size = float(row.get("filled_contracts") or 0)
        fee = float(row.get("fees_cents") or 0)
        taker = " taker" if fee > 0 else ""
        result = (row.get("result") or "?").upper()
        if pnl is not None:
            pnl_total += pnl
            risked += size * price / 100.0 + fee / 100.0
        lines.append(
            f"• {row.get('title')}: {result} {size:g}@{price:.0f}¢"
            f"{taker} {'' if pnl is None else f'${pnl:+.2f}'}"
        )
    if not filled:
        return f"{event_date}: no fills"
    if not risked:
        return f"{event_date}: {len(filled)} name(s) filled, not settled yet (no profit % until they settle)"
    pct = (100.0 * pnl_total / risked) if risked else 0.0
    resting = sum(resting_dollars(r) for r in live)
    all_settled = all(r.get("result") in ("yes", "no") for r in live)
    extra = (f" · {100.0 * pnl_total / resting:+.1f}% on ${resting:.2f} resting"
             if all_settled and resting else "")
    head = (
        f"{event_date}: {pnl_total:+.2f} dollars "
        f"({pct:+.1f}% on ${risked:.2f} filled, {len(filled)} name(s))" + extra
    )
    return "\n".join([head] + lines)


def order_pnl(row: dict) -> float | None:
    filled = row.get("filled_contracts") or 0
    result = row.get("result")
    if not filled or result not in ("yes", "no"):
        return None
    price = row.get("avg_fill_price_cents") or row.get("no_price_cents") or 0
    gross = filled * ((100 - price) if result == "no" else -price)
    return (gross - (row.get("fees_cents") or 0)) / 100.0


def fill_cost(row: dict) -> float:
    """Dollars actually spent on the contracts that filled (price x size + fees)."""
    size = float(row.get("filled_contracts") or 0)
    price = float(row.get("avg_fill_price_cents") or row.get("no_price_cents") or 0)
    fee = float(row.get("fees_cents") or 0)
    return size * price / 100.0 + fee / 100.0


def order_pnl_pct(row: dict) -> float | None:
    """Profit as a % of the money spent on this order's fill."""
    pnl = order_pnl(row)
    if pnl is None:
        return None
    cost = fill_cost(row)
    return 100.0 * pnl / cost if cost > 0 else None


def resting_dollars(row: dict) -> float:
    """Cash set aside for this order while it rests (filled or not)."""
    value = row.get("collateral")
    if value:
        return float(value)
    return float(row.get("contracts") or 0) * float(row.get("no_price_cents") or 0) / 100.0


def size_label(row: dict) -> str:
    """'$3', '$5', '$4.5' ... the size per name this order was placed at."""
    return f"${round(resting_dollars(row), 2):g}"


def _pct(numerator: float, denominator: float) -> float | None:
    return 100.0 * numerator / denominator if denominator else None


def _settled(row: dict) -> bool:
    return row.get("result") in ("yes", "no")


def _live_days(rows: list[dict] | None, live_only: bool) -> dict[str, list[dict]]:
    rows = rows if rows is not None else store.all_orders()
    if live_only:
        rows = [r for r in rows if is_live_cash(r)]
    by_date: dict[str, list[dict]] = {}
    for row in canonical_orders(rows):
        by_date.setdefault(row.get("event_date") or "?", []).append(row)
    return by_date


def day_breakdown(rows: list[dict] | None = None, live_only: bool = True) -> list[dict]:
    """One dict per day, oldest first. Percentages ignore size, so $3 and $5 days compare.

    pct_on_filled  = profit / money spent on the fills that have settled
    pct_on_resting = profit / cash set aside on every name -- only shown once the
                     WHOLE day has settled (otherwise it would look too small)
    """
    out = []
    for date, day in sorted(_live_days(rows, live_only).items()):
        filled = [r for r in day if (r.get("filled_contracts") or 0) > 0]
        settled_fills = [r for r in filled if _settled(r)]
        pnl = sum(p for p in (order_pnl(r) for r in settled_fills) if p is not None)
        cost = sum(fill_cost(r) for r in settled_fills)
        resting = sum(resting_dollars(r) for r in day)
        fully = all(_settled(r) for r in day)
        wins = len([r for r in settled_fills if r["result"] == "no"])
        out.append({
            "event_date": date,
            "size": Counter(size_label(r) for r in day).most_common(1)[0][0],
            "orders": len(day),
            "filled": len(filled),
            "fill_rate": len(filled) / len(day) if day else None,
            "settled_fills": len(settled_fills),
            "p_no": wins / len(settled_fills) if settled_fills else None,
            "pnl": pnl,
            "cost_filled": cost,
            "pct_on_filled": _pct(pnl, cost),
            "resting": resting,
            "fully_settled": fully,
            "pct_on_resting": _pct(pnl, resting) if fully else None,
        })
    return out


def size_breakdown(rows: list[dict] | None = None, live_only: bool = True) -> list[dict]:
    """Same percentages, grouped by size per name ($3 vs $5 ...)."""
    groups: dict[str, dict] = {}
    for date, day in _live_days(rows, live_only).items():
        fully = all(_settled(r) for r in day)
        for row in day:
            g = groups.setdefault(size_label(row), {
                "size": size_label(row), "orders": 0, "filled": 0, "settled_fills": 0,
                "wins": 0, "pnl": 0.0, "cost_filled": 0.0, "resting": 0.0, "dates": set()})
            g["orders"] += 1
            g["dates"].add(date)
            if (row.get("filled_contracts") or 0) > 0:
                g["filled"] += 1
                if _settled(row):
                    g["settled_fills"] += 1
                    g["wins"] += 1 if row["result"] == "no" else 0
                    g["pnl"] += order_pnl(row) or 0.0
                    g["cost_filled"] += fill_cost(row)
            if fully:
                g["resting"] += resting_dollars(row)
                g["_resting_pnl"] = g.get("_resting_pnl", 0.0) + (
                    (order_pnl(row) or 0.0) if (row.get("filled_contracts") or 0) > 0 else 0.0)
    out = []
    for g in groups.values():
        out.append({
            "size": g["size"], "days": len(g["dates"]), "orders": g["orders"],
            "filled": g["filled"],
            "fill_rate": g["filled"] / g["orders"] if g["orders"] else None,
            "settled_fills": g["settled_fills"],
            "p_no": g["wins"] / g["settled_fills"] if g["settled_fills"] else None,
            "pnl": g["pnl"], "cost_filled": g["cost_filled"],
            "pct_on_filled": _pct(g["pnl"], g["cost_filled"]),
            "resting": g["resting"],
            "pct_on_resting": _pct(g.get("_resting_pnl", 0.0), g["resting"]),
        })
    out.sort(key=lambda d: float(d["size"].lstrip("$")))
    return out


def is_live_cash(row: dict) -> bool:
    """Real Kalshi money. Not paper, not smoke."""
    if is_smoke(row):
        return False
    flag = row.get("dry_run")
    if flag is True:
        return False
    if isinstance(flag, str) and flag.strip().lower() in ("true", "1", "yes"):
        return False
    return True


def summarise(rows: list[dict] | None = None, live_only: bool = False) -> dict:
    rows = rows if rows is not None else store.all_orders()
    if live_only:
        rows = [r for r in rows if is_live_cash(r)]
    live = canonical_orders(rows)

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

    detail = day_breakdown(rows, live_only=False)
    cost_total = sum(d["cost_filled"] for d in detail)
    full_days = [d for d in detail if d["fully_settled"]]
    resting_total = sum(d["resting"] for d in full_days)
    day_pcts = [d["pct_on_filled"] for d in detail if d["pct_on_filled"] is not None]

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
        # percentages (size-neutral)
        "total_cost_filled": cost_total,
        "pct_on_filled": _pct(sum(pnls) if pnls else 0.0, cost_total),
        "resting_settled": resting_total,
        "pct_on_resting": _pct(sum(d["pnl"] for d in full_days), resting_total),
        "best_day_pct": max(day_pcts) if day_pcts else None,
        "worst_day_pct": min(day_pcts) if day_pcts else None,
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
        + (f"Return: {stats['pct_on_filled']:+.1f}% on ${stats['total_cost_filled']:.2f} "
           f"of filled money" if stats.get("pct_on_filled") is not None else "")
        + (f", {stats['pct_on_resting']:+.1f}% on resting money"
           if stats.get("pct_on_resting") is not None else "")
        + ("\n" if stats.get("pct_on_filled") is not None else "")
        + (f"Mean/day: ${stats['mean_day']:.2f}\n" if stats["mean_day"] is not None else "")
        + (f"Worst day: ${stats['worst_day']:.2f}\n" if stats["worst_day"] is not None else "")
        + f"Fills that paid a fee: {stats['fills_with_fees']}\n"
        f"\n<b>{verdict}</b> — {why}"
    )
