"""Place NO orders at 26c or cheaper, cancel before the broadcast."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import datetime, timezone

from . import clock, config as C, notify, settle, store
from .kalshi import KalshiClient, KalshiError, _to_cents, _to_count, book_metrics

log = logging.getLogger("wnt.strategy")

STATE: dict = {
    "running": False,
    "active_event": None,
    "active_date": None,
    "last_poll": None,
    "orders_today": 0,
    "fills_today": 0,
    "last_error": None,
    "cancelled_today": False,
    "last_settle": None,
}


def book_already_at_or_below_our_price(market: dict) -> bool:
    """Ticker snapshot only. 0 is an empty book, not a 0¢ quote."""
    yes_bid = _to_cents(market.get("yes_bid_dollars") or market.get("yes_bid"))
    no_ask = _to_cents(market.get("no_ask_dollars") or market.get("no_ask"))
    yes_cap = C.yes_price_cents()
    if yes_bid is not None and yes_bid >= yes_cap:
        return True
    if no_ask is not None and 0 < no_ask <= C.NO_PRICE_CENTS:
        return True
    return False


def take_size_on_book(book: dict) -> float:
    """Contracts of YES >= 74¢ (NO <= 26¢) sitting on the orderbook."""
    metrics = book_metrics(book, C.NO_PRICE_CENTS)
    return float(metrics.get("yes_size_that_would_fill_us") or 0)


def client_order_id(event_date: str, ticker: str) -> str:
    digest = hashlib.md5(ticker.encode()).hexdigest()[:12]
    return f"wnt-{event_date}-{digest}-{C.NO_PRICE_CENTS}-{C.CONTRACTS}"[:64]


def smoke_client_order_id(event_date: str, ticker: str) -> str:
    digest = hashlib.md5(ticker.encode()).hexdigest()[:12]
    return f"wnt-smoke-{event_date}-{digest}-{C.NO_PRICE_CENTS}-{C.SMOKE_CONTRACTS}"[:64]


def _is_smoke_row(row: dict) -> bool:
    return (
        row.get("mode") == "smoke"
        or str(row.get("client_order_id") or "").startswith("wnt-smoke-")
    )


class Runner:
    def __init__(self, client: KalshiClient | None = None):
        self.client = client or KalshiClient()
        self._stop = threading.Event()

    def find_todays_event(self) -> tuple[str, str] | None:
        today = clock.today_ct()
        try:
            events = self.client.get_events(C.SERIES, status="open")
        except Exception as exc:
            log.warning("event lookup failed: %s", exc)
            STATE["last_error"] = str(exc)[:200]
            return None
        for event in events:
            ticker = event.get("event_ticker", "")
            date_str = clock.event_date_from_ticker(ticker)
            if date_str == today and not store.day_handled(date_str):
                return ticker, date_str
        return None

    def active_markets(self, event_ticker: str) -> list[dict]:
        markets = self.client.get_markets(event_ticker)
        return [m for m in markets if m.get("status") in ("active", "open")]

    def place_all(self, event_ticker: str, event_date: str) -> None:
        if store.is_paused():
            notify.send("⏸ Event detected but the bot is PAUSED. No orders placed.")
            store.upsert_day(event_date, event_ticker=event_ticker,
                             detected_at=datetime.now(timezone.utc),
                             mode=self._mode(), notes="skipped: paused")
            return

        markets = self.active_markets(event_ticker)
        if not markets:
            notify.send(f"⚠️ {notify.esc(event_ticker)} found but it has no active markets.")
            store.upsert_day(event_date, event_ticker=event_ticker,
                             detected_at=datetime.now(timezone.utc),
                             markets_seen=0, mode=self._mode(),
                             notes="no active markets")
            return

        seen = len(markets)
        per_market = C.collateral_per_market()
        markets = markets[:C.MAX_MARKETS_PER_DAY]
        max_by_money = int(C.MAX_DAILY_COLLATERAL // per_market)
        if len(markets) > max_by_money:
            log.warning("collateral cap trims %d markets to %d",
                        len(markets), max_by_money)
            markets = markets[:max_by_money]

        needed = len(markets) * per_market
        if not C.DRY_RUN and not self._balance_ok(needed):
            return
        if C.SMOKE_LIVE:
            smoke_needed = len(markets) * C.smoke_collateral_per_market()
            if not self._balance_ok(smoke_needed):
                return

        expiry = clock.expiry_epoch_seconds(event_date) if C.USE_SERVER_SIDE_EXPIRY else None
        placed = rejected = already = taken = 0
        words: list[str] = []

        for market in markets:
            outcome, label = self._place_one(market, event_ticker, event_date, expiry)
            words.append(label)
            if outcome == "taken":
                taken += 1
                placed += 1
            elif outcome == "placed":
                placed += 1
            elif outcome == "exists":
                already += 1
            else:
                rejected += 1
            time.sleep(0.15)

        rows = store.orders_for_day(event_date)
        total_live = len([r for r in rows if r.get("status") != "rejected"])
        total_rejected = len([r for r in rows if r.get("status") == "rejected"])
        collateral = total_live * per_market

        store.upsert_day(
            event_date, event_ticker=event_ticker,
            detected_at=datetime.now(timezone.utc), markets_seen=seen,
            orders_placed=total_live, orders_rejected=total_rejected,
            collateral=collateral, mode=self._mode(),
        )
        STATE["orders_today"] = total_live

        if placed == 0 and already:
            notify.send(
                f"↩️ Resumed {notify.esc(event_ticker)} after a restart. "
                f"{already} order(s) were already placed; nothing new submitted."
            )
            store.log_activity("resume_placement",
                               f"{event_ticker}: {already} pre-existing orders")
            return

        header = "🧪 DRY RUN — orders simulated" if C.DRY_RUN else "🎯 Orders working"
        if C.SMOKE_LIVE:
            header += f" + 🔥 SMOKE {C.SMOKE_CONTRACTS} live contract/name"
        trimmed = f"\n(trimmed from {seen} markets by caps)" if seen > len(markets) else ""
        extra = f", {already} already on the book" if already else ""
        take_bit = (
            f", {taken} bought immediately (book already ≤{C.NO_PRICE_CENTS}¢ NO)"
            if taken else ""
        )
        notify.send(
            f"<b>{header}</b>\n"
            f"{notify.esc(event_ticker)}\n"
            f"{placed} placed{take_bit}, {rejected} rejected{extra} "
            f"of {len(markets)} attempted{trimmed}\n"
            f"NO @ {C.NO_PRICE_CENTS}¢ or cheaper × {C.CONTRACTS} contracts\n"
            f"Collateral resting: ${collateral:.2f}\n"
            f"Cancel at {C.CANCEL_TIME_CT} CT"
            + (" (server-side expiry set)" if expiry else "")
            + "\n\n" + "\n".join("• " + notify.esc(w) for w in words[:25])
        )
        store.log_activity(
            "place_all",
            f"{event_ticker}: {placed} placed / {taken} taken now / "
            f"{rejected} rejected / {already} existing",
        )

    def _place_one(self, market: dict, event_ticker: str, event_date: str,
                   expiry: int | None) -> tuple[str, str]:
        ticker = market["ticker"]
        title = (market.get("yes_sub_title") or market.get("title") or ticker)[:120]
        coid = client_order_id(event_date, ticker)

        if store.order_exists(coid):
            log.info("already have an order row for %s, skipping", ticker)
            return "exists", f"{title} (already placed)"

        take_now = False
        if C.TAKE_IF_ALREADY_CHEAP:
            try:
                book = self.client.get_orderbook(ticker, depth=10)
                take_now = take_size_on_book(book) > 0
            except Exception as exc:
                log.debug("take-size book %s failed: %s", ticker, exc)
                take_now = False
        post_only = bool(C.POST_ONLY) and not take_now

        row = {
            "client_order_id": coid,
            "event_date": event_date,
            "event_ticker": event_ticker,
            "market_ticker": ticker,
            "title": title,
            "no_price_cents": C.NO_PRICE_CENTS,
            "yes_price_cents": C.yes_price_cents(),
            "contracts": C.CONTRACTS,
            "collateral": C.collateral_per_market(),
            "placed_at": datetime.now(timezone.utc),
            "dry_run": C.DRY_RUN,
            "mode": self._mode(),
            "yes_bid_at_place": _to_cents(market.get("yes_bid_dollars") or market.get("yes_bid")),
            "yes_ask_at_place": _to_cents(market.get("yes_ask_dollars") or market.get("yes_ask")),
            "no_bid_at_place": _to_cents(market.get("no_bid_dollars") or market.get("no_bid")),
            "no_ask_at_place": _to_cents(market.get("no_ask_dollars") or market.get("no_ask")),
            "took_at_open": bool(take_now),
            "post_only": bool(post_only),
            "expiration_epoch": expiry,
            "status": "resting",
        }

        if C.DRY_RUN:
            row["order_id"] = None
            row["status"] = "dry_run"
            store.record_order(**row)
            smoke_note = self._place_smoke(
                market, event_ticker, event_date, expiry, take_now, post_only,
            )
            if take_now:
                log.info("[DRY] would BUY NOW NO %s @ ≤%d on %s",
                         C.CONTRACTS, C.NO_PRICE_CENTS, title)
                return "taken", f"{title} (would buy now){smoke_note}"
            log.info("[DRY] would rest NO %s@%d on %s",
                     C.CONTRACTS, C.NO_PRICE_CENTS, title)
            return "placed", f"{title}{smoke_note}"

        try:
            resp = self.client.create_no_order(
                ticker=ticker,
                no_price_cents=C.NO_PRICE_CENTS,
                count=C.CONTRACTS,
                client_order_id=coid,
                post_only=post_only,
                expiration_epoch=expiry,
            )
        except KalshiError as exc:
            reason = f"{exc.status}: {exc.body[:300]}"
            row["status"] = "rejected"
            row["reject_reason"] = reason
            store.record_order(**row)
            log.warning("order rejected for %s -- %s", ticker, reason)
            return "rejected", f"{title} (rejected)"

        row["order_id"] = resp.get("order_id")
        filled_now = bool(resp.get("fill_count"))
        if filled_now:
            row["status"] = "filled"
            row["filled_contracts"] = resp["fill_count"]
            row["first_fill_at"] = datetime.now(timezone.utc)
            row["avg_fill_price_cents"] = resp.get("avg_fill_price_cents")
        store.record_order(**row)
        if take_now or filled_now:
            return "taken", f"{title} (bought immediately)"
        return "placed", title

    def _place_smoke(self, market: dict, event_ticker: str, event_date: str,
                     expiry: int | None, take_now: bool, post_only: bool) -> str:
        """Send the 1-lot live order. Paper row is already stored."""
        if not C.SMOKE_LIVE:
            return ""
        ticker = market["ticker"]
        title = (market.get("yes_sub_title") or market.get("title") or ticker)[:120]
        coid = smoke_client_order_id(event_date, ticker)
        if store.order_exists(coid):
            return " [smoke already sent]"
        row = {
            "client_order_id": coid,
            "event_date": event_date,
            "event_ticker": event_ticker,
            "market_ticker": ticker,
            "title": title,
            "no_price_cents": C.NO_PRICE_CENTS,
            "yes_price_cents": C.yes_price_cents(),
            "contracts": float(C.SMOKE_CONTRACTS),
            "collateral": C.smoke_collateral_per_market(),
            "placed_at": datetime.now(timezone.utc),
            "dry_run": False,
            "mode": "smoke",
            "yes_bid_at_place": _to_cents(market.get("yes_bid_dollars") or market.get("yes_bid")),
            "yes_ask_at_place": _to_cents(market.get("yes_ask_dollars") or market.get("yes_ask")),
            "no_bid_at_place": _to_cents(market.get("no_bid_dollars") or market.get("no_bid")),
            "no_ask_at_place": _to_cents(market.get("no_ask_dollars") or market.get("no_ask")),
            "took_at_open": bool(take_now),
            "post_only": bool(post_only),
            "expiration_epoch": expiry,
            "status": "resting",
        }
        try:
            resp = self.client.create_no_order(
                ticker=ticker,
                no_price_cents=C.NO_PRICE_CENTS,
                count=int(C.SMOKE_CONTRACTS),
                client_order_id=coid,
                post_only=post_only,
                expiration_epoch=expiry,
            )
        except KalshiError as exc:
            reason = f"{exc.status}: {exc.body[:300]}"
            row["status"] = "rejected"
            row["reject_reason"] = reason
            store.record_order(**row)
            log.warning("smoke rejected for %s -- %s", ticker, reason)
            notify.send(
                f"🔥 Smoke rejected: {notify.esc(title)}\n{notify.esc(reason[:200])}"
            )
            return " [smoke REJECTED]"
        row["order_id"] = resp.get("order_id")
        filled_now = bool(resp.get("fill_count"))
        if filled_now:
            row["status"] = "filled"
            row["filled_contracts"] = resp["fill_count"]
            row["first_fill_at"] = datetime.now(timezone.utc)
            row["avg_fill_price_cents"] = resp.get("avg_fill_price_cents")
        store.record_order(**row)
        if filled_now:
            return " [smoke LIVE TAKEN]"
        return " [smoke LIVE resting]"

    def _balance_ok(self, needed: float) -> bool:
        try:
            balance = self.client.get_balance()
        except Exception as exc:
            notify.send(f"⚠️ Could not read Kalshi balance: {notify.esc(str(exc)[:200])}\n"
                        f"Not placing orders.")
            return False
        cash = (balance.get("balance") or 0) / 100.0
        if cash < needed:
            notify.send(
                f"🛑 <b>Not enough cash</b>\n"
                f"Need ${needed:.2f} of resting collateral, have ${cash:.2f}.\n"
                f"No orders placed. Remember a resting buy holds its full cost."
            )
            store.log_activity("insufficient_funds",
                               f"need {needed:.2f} have {cash:.2f}", level="error")
            return False
        return True

    def poll_fills(self, event_date: str) -> None:
        if C.DRY_RUN:
            self._poll_dry_fills(event_date)
        if C.DRY_RUN and not C.SMOKE_LIVE:
            return
        try:
            recent = self.client.get_fills(limit=200)
        except Exception as exc:
            log.warning("fill poll failed: %s", exc)
            return

        known = {
            o["market_ticker"]: o
            for o in store.orders_for_day(event_date)
            if (not C.DRY_RUN) or _is_smoke_row(o)
        }
        for fill in recent:
            ticker = fill.get("ticker")
            if ticker not in known:
                continue
            fill_id = (fill.get("trade_id") or fill.get("fill_id")
                       or f"{ticker}-{fill.get('created_time')}-{fill.get('count')}")
            count = _to_count(fill.get("count"))
            price = _to_cents(fill.get("no_price") or fill.get("price"))
            is_new = store.record_fill(
                fill_id=str(fill_id),
                order_id=fill.get("order_id"),
                event_date=event_date,
                market_ticker=ticker,
                contracts=count,
                price_cents=price,
                is_taker=bool(fill.get("is_taker")),
                fee_cents=_to_cents(fill.get("fee_paid")) or 0,
                created_at=clock.parse_api_time(fill.get("created_time")),
                raw=fill,
            )
            if not is_new:
                continue

            order = known[ticker]
            total = (order.get("filled_contracts") or 0) + count
            store.update_order(
                order["client_order_id"],
                status="filled",
                filled_contracts=total,
                first_fill_at=order.get("first_fill_at")
                or clock.parse_api_time(fill.get("created_time")),
                avg_fill_price_cents=price,
                fees_cents=(order.get("fees_cents") or 0)
                + (_to_cents(fill.get("fee_paid")) or 0),
            )
            STATE["fills_today"] += 1
            taker_flag = " ⚠️ TAKER FILL" if fill.get("is_taker") else ""
            tag = "🔥 smoke " if _is_smoke_row(order) else ""
            notify.send(
                f"✅ <b>{tag}Filled</b>: {notify.esc(order.get('title') or ticker)}\n"
                f"{count:g} contracts NO @ {price}¢{taker_flag}"
            )

    def _poll_dry_fills(self, event_date: str) -> None:
        rows = [
            r for r in store.orders_for_day(event_date)
            if r.get("status") in ("dry_run", "resting")
            and not _is_smoke_row(r)
        ]
        for row in rows:
            ticker = row["market_ticker"]
            wanted = float(row.get("contracts") or C.CONTRACTS)
            already = float(row.get("filled_contracts") or 0)
            remaining = wanted - already
            if remaining <= 0:
                continue
            try:
                book = self.client.get_orderbook(ticker, depth=10)
            except Exception as exc:
                log.debug("dry fill check %s failed: %s", ticker, exc)
                continue

            metrics = book_metrics(book, C.NO_PRICE_CENTS)
            available = float(metrics.get("yes_size_that_would_fill_us") or 0)
            if available <= 0:
                continue
            filled = min(remaining, available)
            if filled <= 0:
                continue

            best_yes = metrics.get("best_yes_bid")
            if best_yes is not None and int(best_yes) >= C.yes_price_cents():
                fill_px = min(C.NO_PRICE_CENTS, max(1, 100 - int(best_yes)))
            else:
                fill_px = C.NO_PRICE_CENTS
            prev_px = float(row.get("avg_fill_price_cents") or fill_px)
            total = already + filled
            avg_px = ((already * prev_px) + (filled * fill_px)) / total

            store.update_order(
                row["client_order_id"],
                status="dry_run",
                filled_contracts=total,
                first_fill_at=row.get("first_fill_at") or clock.now_ct(),
                avg_fill_price_cents=avg_px,
            )
            store.record_fill(
                fill_id=f"dry-{event_date}-{ticker}-{int(time.time() * 1000)}",
                order_id=None,
                event_date=event_date,
                market_ticker=ticker,
                contracts=filled,
                price_cents=fill_px,
                is_taker=bool(row.get("took_at_open")) or already <= 0,
                fee_cents=0,
                created_at=clock.now_ct(),
                raw={"dry_run": True, "slice": filled, "already": already, **metrics},
            )
            if already <= 0:
                STATE["fills_today"] += 1
            notify.send(
                f"🧪 <b>Would have filled</b>: {notify.esc(row.get('title') or ticker)}\n"
                f"{filled:g} more ({total:g} of {wanted:g}) NO @ {fill_px}¢"
            )

    def cancel_all(self, event_date: str | None = None, reason: str = "scheduled") -> dict:
        event_date = event_date or clock.today_ct()
        summary = {"attempted": 0, "cancelled": 0, "remaining": 0, "verified": False}

        if C.DRY_RUN and not C.SMOKE_LIVE:
            n = store.mark_all_resting_cancelled(event_date)
            summary.update(attempted=n, cancelled=n, verified=True)
            store.upsert_day(
                event_date,
                cancelled_at=datetime.now(timezone.utc),
                cancel_verified=True,
            )
            STATE["cancelled_today"] = True
            self._cancel_report(event_date, summary, reason)
            return summary

        if C.DRY_RUN and C.SMOKE_LIVE:
            store.mark_all_resting_cancelled(event_date)

        for attempt in range(3):
            try:
                resting = self.client.get_resting_orders(series_prefix=C.SERIES)
            except Exception as exc:
                log.error("could not list resting orders: %s", exc)
                notify.send(f"🚨 <b>CANCEL PROBLEM</b>\nCould not list resting orders: "
                            f"{notify.esc(str(exc)[:200])}\nRetrying.")
                time.sleep(3)
                continue

            if not resting:
                summary["verified"] = True
                break

            ids = [o["order_id"] for o in resting if o.get("order_id")]
            summary["attempted"] += len(ids)
            ok, failed = self.client.batch_cancel(ids)
            summary["cancelled"] += ok
            if failed:
                log.warning("attempt %d: %d cancels failed", attempt + 1, len(failed))
            time.sleep(2)

        try:
            leftover = self.client.get_resting_orders(series_prefix=C.SERIES)
            summary["remaining"] = len(leftover)
            summary["verified"] = len(leftover) == 0
        except Exception as exc:
            summary["verified"] = False
            log.error("cancel verification failed: %s", exc)

        store.mark_all_resting_cancelled(event_date)
        store.upsert_day(event_date, cancelled_at=datetime.now(timezone.utc),
                         cancel_verified=summary["verified"])
        STATE["cancelled_today"] = True
        self._cancel_report(event_date, summary, reason)
        return summary

    def _cancel_report(self, event_date: str, summary: dict, reason: str) -> None:
        rows = [r for r in store.orders_for_day(event_date) if not _is_smoke_row(r)]
        total = len([r for r in rows if r.get("status") != "rejected"])
        filled = len([r for r in rows if (r.get("filled_contracts") or 0) > 0])
        rate = (100.0 * filled / total) if total else 0.0
        back = 100.0 * float(C.BACKTEST_FILL_RATE)

        if summary["verified"]:
            head = "🛑 <b>All orders cancelled</b>"
        else:
            head = (f"🚨🚨 <b>CANCEL NOT VERIFIED — {summary['remaining']} STILL "
                    f"RESTING</b>\nGo to Kalshi and cancel by hand NOW.")

        notify.send(
            f"{head}\n"
            f"Trigger: {notify.esc(reason)} at {clock.now_ct():%-I:%M:%S %p} CT\n"
            f"Cancelled {summary['cancelled']} of {summary['attempted']} attempted\n\n"
            f"<b>Today's fill rate: {filled}/{total} ({rate:.0f}%)</b>\n"
            f"Backtest expected ~{back:.0f}%"
        )
        store.log_activity(
            "cancel_all",
            f"{reason}: cancelled={summary['cancelled']} "
            f"remaining={summary['remaining']} verified={summary['verified']}",
            level="info" if summary["verified"] else "error",
        )

    def _mode(self) -> str:
        if C.DRY_RUN:
            return "dry_run"
        return "demo" if C.USE_DEMO else "live"

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        STATE["running"] = True
        notify.send(f"🤖 <b>WNT no-fade bot started</b>\n<pre>{notify.esc(C.summary())}</pre>")
        store.log_activity("start", C.summary())

        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                log.exception("loop error")
                STATE["last_error"] = str(exc)[:300]
                store.log_activity("loop_error", str(exc)[:1000], level="error")
                notify.send(f"⚠️ Bot error: {notify.esc(str(exc)[:300])}")
                time.sleep(60)

        STATE["running"] = False

    def _tick(self) -> None:
        now = clock.now_ct()
        today = clock.today_ct()
        store.heartbeat()
        STATE["last_poll"] = now
        self._maybe_settle()

        if STATE["active_date"] and STATE["active_date"] != today:
            STATE.update(active_event=None, active_date=None, orders_today=0,
                         fills_today=0, cancelled_today=False)

        deadline = clock.cancel_deadline(today)

        if not STATE["active_event"]:
            day = store.get_day(today)
            if day and (day.get("orders_placed") or 0) > 0 and not day.get("cancelled_at"):
                STATE.update(active_event=day.get("event_ticker"), active_date=today,
                             orders_today=day.get("orders_placed") or 0)
                log.warning("resumed unfinished day %s (%s) after a restart",
                            today, day.get("event_ticker"))
                store.log_activity("resume", f"recovered {today} after restart")

        if STATE["active_event"]:
            if now >= deadline:
                day = store.get_day(today) or {}
                if STATE.get("cancelled_today") or day.get("cancelled_at"):
                    STATE.update(active_event=None, active_date=None)
                    self._sleep(C.POLL_SECONDS_COLD)
                    return
                self.cancel_all(STATE["active_date"], reason="scheduled 5:29 cancel")
                STATE.update(active_event=None, active_date=None)
                self._sleep(C.POLL_SECONDS_COLD)
                return
            self.poll_fills(STATE["active_date"])
            wait = 5 if C.DRY_RUN else C.FILL_POLL_SECONDS
            self._sleep(min(wait, max(5, clock.seconds_until(deadline))))
            return

        found = self.find_todays_event()
        if found:
            event_ticker, event_date = found
            if now >= clock.cancel_deadline(event_date):
                log.info("event %s appeared after the cancel time; skipping the day",
                         event_ticker)
                store.upsert_day(event_date, event_ticker=event_ticker,
                                 detected_at=datetime.now(timezone.utc),
                                 orders_placed=0, mode=self._mode(),
                                 notes="skipped: event appeared after cancel time")
                notify.send(f"⏭ Event {notify.esc(event_ticker)} appeared after "
                            f"{C.CANCEL_TIME_CT} CT. Skipping today.")
            else:
                notify.send(f"📡 <b>Event detected</b>: {notify.esc(event_ticker)}\n"
                            f"Placing orders now...")
                self.place_all(event_ticker, event_date)
                STATE.update(active_event=event_ticker, active_date=event_date)
                return

        self._sleep(C.POLL_SECONDS_DETECT if clock.in_active_window(now)
                    else C.POLL_SECONDS_COLD)

    def _maybe_settle(self) -> None:
        last = STATE.get("last_settle")
        if last is not None and clock.seconds_until(last) > -3600:
            return
        try:
            settle.sweep(self.client)
        except Exception as exc:
            log.warning("settle sweep failed: %s", exc)
        STATE["last_settle"] = clock.now_ct()

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(max(1.0, float(seconds)))
