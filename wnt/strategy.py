"""Place NO orders at 26c or cheaper, cancel before the broadcast."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

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
    # Kalshi V2: client_order_id must be a UUID. Keep it deterministic
    # per day/ticker/price/size so retries do not double-rest.
    seed = f"wnt|{event_date}|{ticker}|{C.NO_PRICE_CENTS}|{C.CONTRACTS}|v2"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def smoke_client_order_id(event_date: str, ticker: str) -> str:
    digest = hashlib.md5(ticker.encode()).hexdigest()[:12]
    return f"wnt-smoke-{event_date}-{digest}-{C.NO_PRICE_CENTS}-{C.SMOKE_CONTRACTS}"[:64]


def _is_smoke_row(row: dict) -> bool:
    return (
        row.get("mode") == "smoke"
        or str(row.get("client_order_id") or "").startswith("wnt-smoke-")
    )


def _utc(value):
    """Make any datetime timezone-aware UTC (sqlite hands back naive ones)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_open_status(market: dict) -> bool:
    return str(market.get("status") or "").lower() in ("active", "open")


def _looks_like_duplicate(exc: Exception) -> bool:
    """Kalshi refusing a repeat client_order_id means the order already exists."""
    text = str(getattr(exc, "body", "") or exc).lower()
    return "already_exist" in text or "already exist" in text or "duplicate" in text


def _after(when, ref) -> str:
    if when is None or ref is None:
        return ""
    return f" ({(when - ref).total_seconds():+.1f}s)"


def timing_block(mode: str, open_at=None, first_seen=None, flip_seen=None,
                 detected=None, first_send=None, last_send=None,
                 flip_via=None, events_lag=None) -> str:
    """The timing lines shown in Telegram (HTML)."""
    lines = [f"⏱ <b>Timing</b> ({mode})"]
    lines.append(f"open_time: {clock.fmt_precise(open_at)}" if open_at else "open_time: unknown")
    if first_seen:
        lines.append(f"listing first seen: {clock.fmt_precise(first_seen)}")
    if flip_seen:
        via = f", {flip_via} check" if flip_via else ""
        lines.append(f"market active seen: {clock.fmt_precise(flip_seen)}"
                     f"{_after(flip_seen, open_at)}{via}")
    if detected:
        lines.append(f"event detected: {clock.fmt_precise(detected)}{_after(detected, open_at)}")
    if first_send:
        lines.append(f"first order sent: {clock.fmt_precise(first_send)}{_after(first_send, open_at)}")
    if last_send:
        lines.append(f"last order sent: {clock.fmt_precise(last_send)}{_after(last_send, open_at)}")
    if open_at and first_send:
        lines.append(f"<b>delay open_time → first order: "
                     f"{(first_send - open_at).total_seconds():.1f}s</b>")
    if events_lag is not None:
        lines.append("events?status=open listed the event right after the flip: "
                     + ("yes" if events_lag else "NO (that list is lagging)"))
    return "\n".join(lines)


def timing_note(mode: str, open_at=None, first_seen=None, flip_seen=None,
                detected=None, first_send=None, last_send=None) -> str:
    """One plain line saved in days.notes so you can compare days in SQL."""
    def iso(t):
        return _utc(t).strftime("%H:%M:%S.%f")[:-3] + "Z" if t else "-"
    delay = f"{(first_send - open_at).total_seconds():.2f}" if (open_at and first_send) else "-"
    return (f"timing[{mode}] open={iso(open_at)} first_seen={iso(first_seen)} "
            f"flip_seen={iso(flip_seen)} detected={iso(detected)} "
            f"first_order={iso(first_send)} last_order={iso(last_send)} "
            f"delay_open_to_first_order_s={delay}")


class Runner:
    def __init__(self, client: KalshiClient | None = None):
        self.client = client or KalshiClient()
        self._stop = threading.Event()
        # fast-open bookkeeping (only used when FAST_OPEN is on)
        self._fast: dict | None = None
        self._fast_off_date: str | None = None
        self._fast_arm_tries = 0
        self._shadow_threaded = True  # tests set False to run it inline
        self._pace_lock = threading.Lock()
        self._pace_next = 0.0

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
        t_detect = datetime.now(timezone.utc)
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

        # Timing log (never allowed to break trading).
        timing_txt, note = "", None
        try:
            opens = [clock.parse_api_time(m.get("open_time")) for m in markets]
            opens = [o for o in opens if o]
            open_at = min(opens) if opens else None
            sent = [_utc(r["placed_at"]) for r in rows
                    if r.get("placed_at") and not _is_smoke_row(r)]
            first_send = min(sent) if sent else None
            last_send = max(sent) if sent else None
            timing_txt = "\n" + timing_block(
                "standard", open_at=open_at, detected=t_detect,
                first_send=first_send, last_send=last_send)
            note = timing_note("standard", open_at=open_at, detected=t_detect,
                               first_send=first_send, last_send=last_send)
            store.log_activity("timing", note)
        except Exception as exc:  # noqa: BLE001
            log.debug("timing block failed: %s", exc)

        day_fields = dict(
            event_ticker=event_ticker,
            detected_at=datetime.now(timezone.utc), markets_seen=seen,
            orders_placed=total_live, orders_rejected=total_rejected,
            collateral=collateral, mode=self._mode(),
        )
        if note:
            day_fields["notes"] = note
        store.upsert_day(event_date, **day_fields)
        STATE["orders_today"] = total_live

        if placed == 0 and already:
            notify.send(
                f"↩️ Resumed {notify.esc(event_ticker)} after a restart. "
                f"{already} order(s) were already placed; nothing new submitted."
            )
            store.log_activity("resume_placement",
                               f"{event_ticker}: {already} pre-existing orders")
            return

        header = "🧪 DRY RUN — orders simulated" if C.DRY_RUN else "🎯 LIVE $3 — orders working"
        if C.SMOKE_LIVE and C.DRY_RUN:
            header += f" + 🔥 SMOKE {C.SMOKE_CONTRACTS}"
        trimmed = f"\n(trimmed from {seen} markets by caps)" if seen > len(markets) else ""
        extra = f", {already} already on the book" if already else ""
        take_bit = (
            f", {taken} bought immediately (book already ≤{C.NO_PRICE_CENTS}¢ NO)"
            if taken else ""
        )
        first_reject = ""
        if rejected and not placed:
            reasons = [r.get("reject_reason") for r in store.orders_for_day(event_date)
                       if r.get("reject_reason")]
            if reasons:
                first_reject = f"First reject: {notify.esc(str(reasons[0])[:180])}\n"
        notify.send(
            f"<b>{header}</b>\n"
            f"{notify.esc(event_ticker)}\n"
            f"{placed} placed{take_bit}, {rejected} rejected{extra} "
            f"of {len(markets)} attempted{trimmed}\n"
            f"{first_reject}"
            f"NO @ {C.NO_PRICE_CENTS}¢ or cheaper × {C.CONTRACTS} contracts\n"
            f"Collateral resting: ${collateral:.2f}\n"
            f"Cancel at {C.CANCEL_TIME_CT} CT"
            + (" (server-side expiry set)" if expiry else "")
            + "\n" + timing_txt
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
        for existing in store.orders_for_day(event_date):
            if existing.get("market_ticker") != ticker:
                continue
            if existing.get("status") in ("rejected",):
                continue
            if _is_smoke_row(existing):
                continue
            log.info("live row already exists for %s (%s), skipping",
                     ticker, existing.get("status"))
            return "exists", f"{title} (already placed)"

        take_now = False
        snap: dict = {}
        if C.TAKE_IF_ALREADY_CHEAP:
            try:
                book = self.client.get_orderbook(ticker, depth=10)
                snap = book_metrics(book, C.NO_PRICE_CENTS)
                take_now = float(snap.get("yes_size_that_would_fill_us") or 0) > 0
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
            "book_at_place": snap or None,
        }

        if C.DRY_RUN:
            row["order_id"] = None
            row["status"] = "dry_run"
            store.record_order(**row)
            smoke_note = ""
            if C.SMOKE_LIVE:
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

    def _balance_ok(self, needed: float, tell: bool = True) -> bool:
        try:
            balance = self.client.get_balance()
        except Exception as exc:
            if tell:
                notify.send(f"⚠️ Could not read Kalshi balance: {notify.esc(str(exc)[:200])}\n"
                            f"Not placing orders.")
            return False
        cash = (balance.get("balance") or 0) / 100.0
        if cash < needed:
            if tell:
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
            if ((not C.DRY_RUN) and not _is_smoke_row(o)) or _is_smoke_row(o)
        }
        for fill in recent:
            ticker = fill.get("ticker") or fill.get("market_ticker")
            if ticker not in known:
                continue
            count = _to_count(fill.get("count_fp") or fill.get("count"))
            price = _to_cents(
                fill.get("no_price_dollars")
                or fill.get("no_price")
                or fill.get("price")
            )
            if price is None and fill.get("yes_price_dollars") is not None:
                yes_px = _to_cents(fill.get("yes_price_dollars"))
                if yes_px is not None:
                    price = max(1, 100 - yes_px)
            if price is None:
                price = C.NO_PRICE_CENTS
            fee = _to_cents(fill.get("fee_cost") or fill.get("fee_paid")) or 0
            if count <= 0:
                count = float(C.CONTRACTS)
            fill_id = (fill.get("trade_id") or fill.get("fill_id")
                       or f"{ticker}-{fill.get('created_time')}-{count}")
            snap: dict = {}
            try:
                book = self.client.get_orderbook(ticker, depth=10)
                snap = book_metrics(book, C.NO_PRICE_CENTS)
            except Exception:
                snap = {}
            raw = dict(fill) if isinstance(fill, dict) else {"fill": fill}
            raw.update({k: snap.get(k) for k in (
                "best_yes_bid", "best_no_bid",
                "no_size_ahead", "no_size_at_our_price",
                "yes_size_that_would_fill_us",
                "yes_size_total", "no_size_total",
            )})
            is_new = store.record_fill(
                fill_id=str(fill_id)[:64],
                order_id=fill.get("order_id"),
                event_date=event_date,
                market_ticker=ticker,
                contracts=count,
                price_cents=price,
                is_taker=bool(fill.get("is_taker")),
                fee_cents=fee,
                created_at=clock.parse_api_time(fill.get("created_time")),
                raw=raw,
            )
            if not is_new:
                continue

            order = known[ticker]
            already = float(order.get("filled_contracts") or 0)
            total = already + count
            prev_px = float(order.get("avg_fill_price_cents") or price)
            avg_px = ((already * prev_px) + (count * price)) / total if total else price
            store.update_order(
                order["client_order_id"],
                status="filled" if total >= float(order.get("contracts") or C.CONTRACTS) else "resting",
                filled_contracts=total,
                first_fill_at=order.get("first_fill_at")
                or clock.parse_api_time(fill.get("created_time")),
                avg_fill_price_cents=avg_px,
                fees_cents=(order.get("fees_cents") or 0) + fee,
            )
            known[ticker]["filled_contracts"] = total
            known[ticker]["avg_fill_price_cents"] = avg_px
            STATE["fills_today"] += 1
            taker_flag = " ⚠️ TAKER FILL" if fill.get("is_taker") else ""
            tag = "🔥 smoke " if _is_smoke_row(order) else ""
            notify.send(
                f"✅ <b>{tag}Filled</b>: {notify.esc(order.get('title') or ticker)}\n"
                f"{count:g} contracts NO @ {price}¢{taker_flag}"
            )
            if C.DRY_RUN and _is_smoke_row(order):
                self._credit_paper_from_live(event_date, ticker, count, price)

    def _credit_paper_from_live(self, event_date: str, ticker: str,
                               live_count: float, price: int) -> None:
        """A live smoke fill is proof the 26/74 level traded. Credit paper."""
        paper = None
        for row in store.orders_for_day(event_date):
            if row.get("market_ticker") == ticker and not _is_smoke_row(row):
                paper = row
                break
        if not paper:
            return
        wanted = float(paper.get("contracts") or C.CONTRACTS)
        already = float(paper.get("filled_contracts") or 0)
        remaining = wanted - already
        if remaining <= 0:
            return
        available = 0.0
        try:
            book = self.client.get_orderbook(ticker, depth=10)
            available = float(
                book_metrics(book, C.NO_PRICE_CENTS).get("yes_size_that_would_fill_us") or 0
            )
        except Exception:
            pass
        filled = min(remaining, max(float(live_count or 0), available, 1.0))
        if filled <= 0:
            return
        fill_px = int(price or C.NO_PRICE_CENTS)
        prev_px = float(paper.get("avg_fill_price_cents") or fill_px)
        total = already + filled
        avg_px = ((already * prev_px) + (filled * fill_px)) / total
        store.update_order(
            paper["client_order_id"],
            status="dry_run",
            filled_contracts=total,
            first_fill_at=paper.get("first_fill_at") or clock.now_ct(),
            avg_fill_price_cents=avg_px,
        )
        digest = hashlib.md5(ticker.encode()).hexdigest()[:10]
        store.record_fill(
            fill_id=f"dl-{event_date}-{digest}-{int(time.time() * 1000)}"[:64],
            order_id=None,
            event_date=event_date,
            market_ticker=ticker,
            contracts=filled,
            price_cents=fill_px,
            is_taker=False,
            fee_cents=0,
            created_at=clock.now_ct(),
            raw={"dry_run": True, "from_live_smoke": True, "live_count": live_count},
        )
        if already <= 0:
            STATE["fills_today"] += 1
        notify.send(
            f"🧪 <b>Would have filled</b>: {notify.esc(paper.get('title') or ticker)}\n"
            f"{filled:g} more ({total:g} of {wanted:g}) NO @ {fill_px}¢ "
            f"(live smoke printed)"
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
            wait = 5
            self._sleep(min(wait, max(5, clock.seconds_until(deadline))))
            return

        if (C.FAST_OPEN or C.FAST_SHADOW) and self._fast_eligible(today, now):
            try:
                if self._fast_open_step(today):
                    return
            except Exception as exc:  # noqa: BLE001
                self._fast_failed(today, exc)

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

    # ==================================================================
    # FAST OPEN
    # Only runs when FAST_OPEN=true. Any surprise switches it off for the
    # day and the normal path (find_todays_event -> place_all) takes over.
    # The normal path skips any market that already has an order row, so
    # the two paths can never double up.
    # ==================================================================
    def _utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def _wait(self, seconds: float) -> None:
        self._stop.wait(max(0.0, float(seconds)))

    def _fast_eligible(self, today: str, now: datetime) -> bool:
        if C.DRY_RUN or C.SMOKE_LIVE:
            return False  # fast path is live-money only
        if self._fast_off_date == today:
            return False
        if not clock.in_active_window(now):
            return False
        return now < clock.cancel_deadline(today)

    def _fast_off(self, today: str, why: str) -> None:
        self._fast_off_date = today
        log.info("fast-open off for %s: %s", today, why)
        store.log_activity("fast_open_off", f"{today}: {why}")

    def _fast_failed(self, today: str, exc: Exception) -> None:
        self._fast_off_date = today
        log.exception("fast-open failed; using the normal path")
        store.log_activity("fast_open_error", str(exc)[:1000], level="error")
        notify.send("⚠️ Fast-open hit an error. Falling back to the normal path "
                    f"for today.\n{notify.esc(str(exc)[:200])}")

    def _fast_arm(self, today: str) -> dict | None:
        """Look for today's listed-but-not-open event. Never raises."""
        self._fast_arm_tries += 1
        event_ticker = clock.event_ticker_for_date(today)
        try:
            markets = self.client.get_markets(event_ticker)
            if not markets and self._fast_arm_tries % 12 == 0:
                # Backup: ask for events that are listed but not open yet.
                for ev in self.client.get_events(C.SERIES, status="unopened"):
                    ticker = ev.get("event_ticker", "")
                    if clock.event_date_from_ticker(ticker) == today:
                        event_ticker = ticker
                        markets = self.client.get_markets(ticker)
                        break
        except Exception as exc:  # noqa: BLE001
            log.debug("fast arm lookup failed: %s", exc)
            return None
        if not markets:
            return None

        dead = {"closed", "determined", "disputed", "amended", "finalized",
                "settled", "inactive"}
        usable = [m for m in markets
                  if str(m.get("status") or "").lower() not in dead and m.get("ticker")]
        if not usable:
            self._fast_off(today, "no usable markets")
            return None
        if any(_is_open_status(m) for m in usable):
            self._fast_off(today, "already trading when first seen; normal path handles it")
            return None

        timed = [(clock.parse_api_time(m.get("open_time")), m) for m in usable]
        timed = [(o, m) for o, m in timed if o]
        if not timed:
            self._fast_off(today, "no readable open_time")
            return None
        open_at, probe_market = min(timed, key=lambda pair: pair[0])
        secs = (open_at - self._utcnow()).total_seconds()
        if secs < -600 or secs > 6 * 3600:
            self._fast_off(today, f"open_time looks wrong ({secs:.0f}s away)")
            return None
        cancel_utc = clock.cancel_deadline(today).astimezone(timezone.utc)
        if open_at + timedelta(seconds=C.FAST_GIVE_UP_SECONDS) >= cancel_utc - timedelta(seconds=60):
            self._fast_off(today, "open_time is too close to the cancel time")
            return None
        if store.day_handled(today):
            self._fast_off(today, "day already handled")
            return None

        fs = {
            "event_ticker": event_ticker, "event_date": today,
            "markets": usable, "probe_ticker": probe_market["ticker"],
            "open_at": open_at, "first_seen": self._utcnow(),
        }
        store.log_activity(
            "fast_armed",
            f"{event_ticker}: {len(usable)} markets, open_time {open_at.isoformat()}")
        notify.send(
            f"⏱ Fast-open armed: {notify.esc(event_ticker)}\n"
            f"{len(usable)} markets listed, trading opens {clock.fmt_precise(open_at)}",
            quiet=True)
        return fs

    def _fast_open_step(self, today: str) -> bool:
        """One tick of the fast path. True = it handled the day."""
        fs = self._fast
        if fs is None or fs["event_date"] != today:
            fs = self._fast_arm(today)
            self._fast = fs
            if fs is None:
                return False
        secs = (fs["open_at"] - self._utcnow()).total_seconds()
        if secs > C.FAST_PREP_SECONDS:
            return False  # too early; the normal loop keeps its 5s rhythm
        return self._fast_run(fs, today)

    def _probe_status(self, ticker: str, signed: bool) -> str | None:
        resp = self.client.request("GET", f"/markets/{ticker}", auth=signed,
                                   retries=0, timeout=5) or {}
        return (resp.get("market") or {}).get("status")

    def _events_list_shows_event(self, event_date: str) -> bool | None:
        """Does GET /events?status=open list today's event yet? One try, short timeout."""
        try:
            resp = self.client.request(
                "GET", "/events",
                params={"series_ticker": C.SERIES, "status": "open", "limit": 200},
                auth=False, retries=0, timeout=3) or {}
            return any(clock.event_date_from_ticker(e.get("event_ticker", "")) == event_date
                       for e in (resp.get("events") or []))
        except Exception:  # noqa: BLE001
            return None

    def _pace(self) -> None:
        """Keep order sends under FAST_MAX_ORDERS_PER_SEC (Kalshi write limit)."""
        step = 1.0 / C.FAST_MAX_ORDERS_PER_SEC
        with self._pace_lock:
            now = time.monotonic()
            slot = max(now, self._pace_next)
            self._pace_next = slot + step
        if slot > now:
            time.sleep(slot - now)

    def _fast_wait_and_watch(self, fs: dict, today: str):
        """Wait until just before open_time, then watch one market until it is active.
        Returns (flip_seen, via) or None (already switched fast-open off for today)."""
        open_at = fs["open_at"]
        while not self._stop.is_set():
            remaining = (open_at - self._utcnow()).total_seconds() - C.FAST_LEAD_SECONDS
            if remaining <= 0:
                break
            if clock.now_ct() >= clock.cancel_deadline(today):
                self._fast_off(today, "reached cancel time while waiting")
                return None
            STATE["last_poll"] = clock.now_ct()
            self._wait(min(1.0, remaining))
        if self._stop.is_set():
            return None

        give_up_at = open_at + timedelta(seconds=C.FAST_GIVE_UP_SECONDS)
        checks = 0
        while not self._stop.is_set():
            now = self._utcnow()
            if now >= give_up_at:
                break
            signed = (checks % 2 == 0)  # alternate signed / public: logs which is fresher
            checks += 1
            try:
                status = self._probe_status(fs["probe_ticker"], signed)
            except Exception as exc:  # noqa: BLE001
                log.debug("probe failed: %s", exc)
                status = None
            if status is not None and str(status).lower() in ("active", "open"):
                return self._utcnow(), ("signed" if signed else "public")
            STATE["last_poll"] = clock.now_ct()
            self._wait(C.FAST_WATCH_SECONDS if now < open_at + timedelta(seconds=15) else 1.0)
        if not self._stop.is_set():
            notify.send("⚠️ Fast-open: the market never showed active within "
                        f"{C.FAST_GIVE_UP_SECONDS:.0f}s of open_time. Normal path continues.")
        self._fast_off(today, "market never showed active while watching")
        return None

    def _fast_shadow(self, fs: dict, today: str) -> bool:
        """Measure only. Runs in its own thread so it can never slow the main loop,
        and places NO orders. The normal path trades exactly as before."""
        if fs.get("shadow_started"):
            return False
        fs["shadow_started"] = True
        if self._shadow_threaded:
            threading.Thread(target=self._shadow_worker, args=(fs, today),
                             daemon=True, name="fast-shadow").start()
        else:
            self._shadow_worker(fs, today)
        return False  # the normal path keeps running its usual ticks

    def _shadow_worker(self, fs: dict, today: str) -> None:
        try:
            seen = self._fast_wait_and_watch(fs, today)
            if seen is None:
                return
            flip_seen, flip_via = seen
            lag = self._events_list_shows_event(fs["event_date"])
            block = timing_block("shadow: NO orders from this", open_at=fs["open_at"],
                                 first_seen=fs["first_seen"], flip_seen=flip_seen,
                                 flip_via=flip_via, events_lag=lag)
            note = timing_note("shadow", open_at=fs["open_at"], first_seen=fs["first_seen"],
                               flip_seen=flip_seen)
            store.log_activity("fast_shadow", note + f" events_list_showed_event={lag}")
            notify.send("👁 <b>Shadow check</b> (the normal path places the orders)\n" + block,
                        quiet=True)
            self._fast_off(today, "shadow measurement done")
        except Exception as exc:  # noqa: BLE001
            log.warning("shadow watcher failed: %s", exc)
            self._fast_off(today, f"shadow watcher error: {str(exc)[:120]}")

    def _fast_run(self, fs: dict, today: str) -> bool:
        if C.FAST_SHADOW and not C.FAST_OPEN:
            return self._fast_shadow(fs, today)
        event_ticker, event_date, open_at = fs["event_ticker"], fs["event_date"], fs["open_at"]

        # ---- 1) preflight: the ONLY database reads before the open --------
        if store.is_paused():
            self._fast_off(today, "bot is paused")
            return False
        known: set[str] = set()
        for row in store.orders_for_day(event_date):
            if not _is_smoke_row(row):
                known.add(row.get("market_ticker"))
        per_market = C.collateral_per_market()
        cap = max(0, min(C.MAX_MARKETS_PER_DAY, int(C.MAX_DAILY_COLLATERAL // per_market)))
        planned = list(fs["markets"])[:cap]
        if not planned or all(m["ticker"] in known for m in planned):
            self._fast_off(today, "orders already exist or nothing to place")
            return False
        if not self._balance_ok(len(planned) * per_market, tell=False):
            self._fast_off(today, "balance check failed or too low (normal path will report)")
            return False
        expiry = clock.expiry_epoch_seconds(event_date) if C.USE_SERVER_SIDE_EXPIRY else None

        # ---- 2+3) wait, then watch until the market is active ---------------
        seen = self._fast_wait_and_watch(fs, today)
        if seen is None:
            return False
        flip_seen, flip_via = seen

        # last-second safety: honour /pause
        if store.is_paused():
            notify.send("⏸ Market opened but the bot is PAUSED. Fast-open placed nothing.")
            self._fast_off(today, "paused at the open")
            return False

        # ---- 4) FIRE --------------------------------------------------------
        ctx = {
            "event_ticker": event_ticker, "event_date": event_date, "expiry": expiry,
            "lock": threading.Lock(), "dblock": threading.Lock(),
            "done": set(known), "inflight": set(),
            "first_send": None, "last_send": None, "errors": {},
        }
        summary = self._fast_fire(fs, ctx, planned, cap)
        self._fast_finish(fs, ctx, summary, flip_seen, flip_via, today)
        return True

    def _fast_fire(self, fs: dict, ctx: dict, planned: list[dict], cap: int) -> dict:
        event_ticker = ctx["event_ticker"]
        pending = {m["ticker"]: m for m in planned if m["ticker"] not in ctx["done"]}
        tried = set(pending) | set(ctx["done"])
        attempts: dict[str, int] = {}
        open_since: dict[str, datetime] = {}
        words: list[str] = []
        counts = {"placed": 0, "taken": 0, "exists": 0, "rejected": 0}
        events_lag = None
        self._pace_next = 0.0
        deadline = self._utcnow() + timedelta(seconds=C.FAST_STRAGGLER_SECONDS)
        send_now = list(pending.values())
        first_round = True

        def reject(market: dict, ticker: str) -> None:
            title = (market.get("yes_sub_title") or market.get("title") or ticker)[:120]
            self._fast_record_reject(market, ctx, ctx["errors"].get(ticker) or "unknown")
            pending.pop(ticker, None)
            counts["rejected"] += 1
            words.append(f"{title} (rejected)")

        while True:
            for res in self._fast_send(send_now, ctx):
                ticker, outcome = res["ticker"], res["outcome"]
                if outcome in ("placed", "taken", "exists"):
                    pending.pop(ticker, None)
                    counts[outcome] += 1
                    words.append(res["label"])
                elif outcome == "retry":
                    attempts[ticker] = attempts.get(ticker, 0) + 1
                    ctx["errors"][ticker] = res.get("error")

            if first_round:
                first_round = False
                events_lag = self._events_list_shows_event(ctx["event_date"])
            if self._stop.is_set():
                break

            # Look at Kalshi again: catches late markets AND markets added after the listing.
            if pending:
                self._wait(0.4)
            past_deadline = self._utcnow() >= deadline
            try:
                fresh = self.client.get_markets(event_ticker)
            except Exception as exc:  # noqa: BLE001
                log.debug("refresh failed: %s", exc)
                fresh = None
            send_now = []
            if fresh is not None:
                now = self._utcnow()
                by_ticker = {m["ticker"]: m for m in fresh if m.get("ticker")}
                for ticker in list(pending):
                    market = by_ticker.get(ticker)
                    if market is None:
                        pending.pop(ticker)
                        words.append(f"{ticker} (gone from Kalshi)")
                    elif _is_open_status(market):
                        open_since.setdefault(ticker, now)
                        n = attempts.get(ticker, 0)
                        stuck_for = (now - open_since[ticker]).total_seconds()
                        # A saved rejection blocks the market for the whole day, so be patient:
                        # reject only after 3 tries over 4+ seconds, or at the deadline.
                        if (n >= 3 and stuck_for >= 4.0) or (past_deadline and n >= 2):
                            reject(market, ticker)
                        elif not past_deadline:
                            send_now.append(market)
                for ticker, market in by_ticker.items():  # markets added after the listing
                    if (ticker in tried or not _is_open_status(market) or len(tried) >= cap
                            or past_deadline):
                        continue
                    tried.add(ticker)
                    pending[ticker] = market
                    send_now.append(market)
            if past_deadline or (not pending and not send_now):
                break

        return {"counts": counts, "words": words, "left": list(pending),
                "events_lag": events_lag, "seen": len(tried)}

    def _fast_send(self, markets: list[dict], ctx: dict) -> list[dict]:
        if not markets:
            return []
        with ThreadPoolExecutor(max_workers=C.FAST_WORKERS,
                                thread_name_prefix="fast-order") as pool:
            futures = [pool.submit(self._fast_place_one, m, ctx) for m in markets]
            return [f.result() for f in futures]

    def _fast_row(self, market: dict, ctx: dict, coid: str, title: str,
                  take_now: bool, post_only: bool, snap: dict, sent_at: datetime) -> dict:
        """Same fields as _place_one writes. No database reads."""
        yes_bid = _to_cents(market.get("yes_bid_dollars") or market.get("yes_bid"))
        yes_ask = _to_cents(market.get("yes_ask_dollars") or market.get("yes_ask"))
        no_bid = _to_cents(market.get("no_bid_dollars") or market.get("no_bid"))
        no_ask = _to_cents(market.get("no_ask_dollars") or market.get("no_ask"))
        best_yes, best_no = snap.get("best_yes_bid"), snap.get("best_no_bid")
        if yes_bid is None and best_yes is not None:
            yes_bid = best_yes
        if no_bid is None and best_no is not None:
            no_bid = best_no
        if yes_ask is None and best_no is not None:
            yes_ask = 100 - best_no
        if no_ask is None and best_yes is not None:
            no_ask = 100 - best_yes
        return {
            "client_order_id": coid,
            "event_date": ctx["event_date"],
            "event_ticker": ctx["event_ticker"],
            "market_ticker": market["ticker"],
            "title": title,
            "no_price_cents": C.NO_PRICE_CENTS,
            "yes_price_cents": C.yes_price_cents(),
            "contracts": C.CONTRACTS,
            "collateral": C.collateral_per_market(),
            "placed_at": sent_at,
            "dry_run": False,
            "mode": self._mode(),
            "yes_bid_at_place": yes_bid,
            "yes_ask_at_place": yes_ask,
            "no_bid_at_place": no_bid,
            "no_ask_at_place": no_ask,
            "took_at_open": bool(take_now),
            "post_only": bool(post_only),
            "expiration_epoch": ctx["expiry"],
            "status": "resting",
            "book_at_place": snap or None,
        }

    def _fast_record(self, row: dict, ctx: dict) -> None:
        with ctx["dblock"]:
            for attempt in (1, 2):
                try:
                    store.record_order(**row)
                    return
                except Exception as exc:  # noqa: BLE001
                    log.error("could not record order row for %s (try %d): %s",
                              row.get("market_ticker"), attempt, exc)
                    if attempt == 1:
                        time.sleep(0.3)
        notify.send(f"⚠️ {notify.esc(str(row.get('market_ticker')))}: the order is on Kalshi but "
                    "its database row could NOT be saved. The 5:29 cancel still works "
                    "(it reads from Kalshi).")

    def _fast_record_reject(self, market: dict, ctx: dict, reason: str) -> None:
        ticker = market["ticker"]
        title = (market.get("yes_sub_title") or market.get("title") or ticker)[:120]
        row = self._fast_row(market, ctx, client_order_id(ctx["event_date"], ticker), title,
                             False, bool(C.POST_ONLY), {}, self._utcnow())
        row["status"] = "rejected"
        row["reject_reason"] = str(reason)[:500]
        self._fast_record(row, ctx)
        ctx["done"].add(ticker)

    def _fast_place_one(self, market: dict, ctx: dict) -> dict:
        ticker = market["ticker"]
        title = (market.get("yes_sub_title") or market.get("title") or ticker)[:120]
        coid = client_order_id(ctx["event_date"], ticker)
        with ctx["lock"]:
            if ticker in ctx["done"] or ticker in ctx["inflight"]:
                return {"ticker": ticker, "outcome": "exists", "label": f"{title} (already placed)"}
            ctx["inflight"].add(ticker)
        try:
            take_now, snap = False, {}
            if C.TAKE_IF_ALREADY_CHEAP:
                try:
                    book = self.client.get_orderbook(ticker, depth=10)
                    snap = book_metrics(book, C.NO_PRICE_CENTS)
                    take_now = float(snap.get("yes_size_that_would_fill_us") or 0) > 0
                except Exception as exc:  # noqa: BLE001
                    log.debug("take-size book %s failed: %s", ticker, exc)
                    take_now, snap = False, {}
            post_only = bool(C.POST_ONLY) and not take_now

            self._pace()
            sent_at = self._utcnow()
            with ctx["lock"]:
                if ctx["first_send"] is None or sent_at < ctx["first_send"]:
                    ctx["first_send"] = sent_at
                if ctx["last_send"] is None or sent_at > ctx["last_send"]:
                    ctx["last_send"] = sent_at
            try:
                resp = self.client.create_no_order(
                    ticker=ticker, no_price_cents=C.NO_PRICE_CENTS, count=C.CONTRACTS,
                    client_order_id=coid, post_only=post_only,
                    expiration_epoch=ctx["expiry"],
                )
            except KalshiError as exc:
                if _looks_like_duplicate(exc):
                    row = self._fast_row(market, ctx, coid, title, take_now, post_only, snap, sent_at)
                    self._fast_record(row, ctx)
                    with ctx["lock"]:
                        ctx["done"].add(ticker)
                    return {"ticker": ticker, "outcome": "placed",
                            "label": f"{title} (already on Kalshi)"}
                # Not recorded yet: the market may simply not be open yet.
                return {"ticker": ticker, "outcome": "retry",
                        "error": f"{exc.status}: {exc.body[:300]}"}
            except Exception as exc:  # noqa: BLE001  (network trouble etc.)
                return {"ticker": ticker, "outcome": "retry", "error": str(exc)[:300]}

            row = self._fast_row(market, ctx, coid, title, take_now, post_only, snap, sent_at)
            row["order_id"] = resp.get("order_id")
            filled_now = bool(resp.get("fill_count"))
            if filled_now:
                row["status"] = "filled"
                row["filled_contracts"] = resp["fill_count"]
                row["first_fill_at"] = datetime.now(timezone.utc)
                row["avg_fill_price_cents"] = resp.get("avg_fill_price_cents")
            self._fast_record(row, ctx)
            with ctx["lock"]:
                ctx["done"].add(ticker)
            if take_now or filled_now:
                return {"ticker": ticker, "outcome": "taken", "label": f"{title} (bought immediately)"}
            return {"ticker": ticker, "outcome": "placed", "label": title}
        finally:
            with ctx["lock"]:
                ctx["inflight"].discard(ticker)

    def _fast_finish(self, fs: dict, ctx: dict, summary: dict, flip_seen: datetime,
                     flip_via: str, today: str) -> None:
        event_ticker, event_date, open_at = ctx["event_ticker"], ctx["event_date"], fs["open_at"]
        counts, words, left = summary["counts"], summary["words"], summary["left"]
        per_market = C.collateral_per_market()

        rows = store.orders_for_day(event_date)
        total_live = len([r for r in rows if r.get("status") != "rejected"])
        total_rejected = len([r for r in rows if r.get("status") == "rejected"])
        collateral = total_live * per_market

        first_send, last_send = ctx["first_send"], ctx["last_send"]
        note = timing_note("fast", open_at=open_at, first_seen=fs["first_seen"],
                           flip_seen=flip_seen, first_send=first_send, last_send=last_send)
        store.upsert_day(
            event_date, event_ticker=event_ticker, detected_at=flip_seen,
            markets_seen=summary["seen"], orders_placed=total_live,
            orders_rejected=total_rejected, collateral=collateral,
            mode=self._mode(), notes=note,
        )
        store.log_activity("timing", note)
        STATE.update(active_event=event_ticker, active_date=event_date, orders_today=total_live)
        self._fast_off_date = today  # done for today; do not re-arm

        placed = counts["placed"] + counts["taken"]
        take_bit = (f", {counts['taken']} bought immediately (book already ≤{C.NO_PRICE_CENTS}¢ NO)"
                    if counts["taken"] else "")
        extra = f", {counts['exists']} already on the book" if counts["exists"] else ""
        left_bit = ""
        if left:
            left_bit = (f"\n⚠️ {len(left)} market(s) were not tradable within "
                        f"{C.FAST_STRAGGLER_SECONDS:.0f}s and got no order: "
                        + notify.esc(", ".join(left[:6])))
        expiry = ctx["expiry"]
        block = timing_block("fast open", open_at=open_at, first_seen=fs["first_seen"],
                             flip_seen=flip_seen, flip_via=flip_via,
                             first_send=first_send, last_send=last_send,
                             events_lag=summary["events_lag"])
        notify.send(
            f"<b>🎯 LIVE $3 — orders working ⚡fast</b>\n"
            f"{notify.esc(event_ticker)}\n"
            f"{placed} placed{take_bit}, {counts['rejected']} rejected{extra} "
            f"of {summary['seen']} attempted{left_bit}\n"
            f"NO @ {C.NO_PRICE_CENTS}¢ or cheaper × {C.CONTRACTS} contracts\n"
            f"Collateral resting: ${collateral:.2f}\n"
            f"Cancel at {C.CANCEL_TIME_CT} CT"
            + (" (server-side expiry set)" if expiry else "")
            + "\n" + block
            + "\n\n" + "\n".join("• " + notify.esc(w) for w in words[:25])
        )
        store.log_activity(
            "place_all",
            f"{event_ticker}: FAST {placed} placed / {counts['taken']} taken now / "
            f"{counts['rejected']} rejected / {counts['exists']} existing",
        )

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
