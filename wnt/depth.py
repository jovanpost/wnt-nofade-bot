"""Read-only orderbook snapshots. Cannot place or cancel orders."""
from __future__ import annotations

import logging
import threading
import time

from . import clock, config as C, store
from .kalshi import KalshiClient, book_metrics

log = logging.getLogger("wnt.depth")

STATE = {"running": False, "snapshots": 0, "last_error": None, "last_run": None}


class DepthCollector:
    def __init__(self, client: KalshiClient | None = None):
        self.client = client or KalshiClient()
        self._stop = threading.Event()
        self._tickers: list[str] = []
        self._event_date: str | None = None
        self._lock = threading.Lock()

    def track(self, event_date: str, tickers: list[str]) -> None:
        with self._lock:
            self._event_date = event_date
            self._tickers = list(tickers)
        log.info("depth collector now tracking %d markets for %s",
                 len(tickers), event_date)

    def _discover(self) -> None:
        today = clock.today_ct()
        with self._lock:
            if self._event_date == today and self._tickers:
                return
        try:
            for event in self.client.get_events(C.SERIES, status="open"):
                ticker = event.get("event_ticker", "")
                if clock.event_date_from_ticker(ticker) != today:
                    continue
                markets = [m["ticker"] for m in self.client.get_markets(ticker)
                           if m.get("status") in ("active", "open")]
                if markets:
                    self.track(today, markets[:C.MAX_MARKETS_PER_DAY])
                return
        except Exception as exc:
            STATE["last_error"] = str(exc)[:200]
            log.warning("depth discovery failed: %s", exc)

    def _snapshot_once(self) -> int:
        with self._lock:
            tickers = list(self._tickers)
            event_date = self._event_date
        if not tickers or not event_date:
            return 0

        taken = 0
        for ticker in tickers:
            try:
                book = self.client.get_orderbook(ticker, depth=C.DEPTH_LEVELS)
            except Exception as exc:
                log.debug("orderbook %s failed: %s", ticker, exc)
                continue
            metrics = book_metrics(book, C.NO_PRICE_CENTS)
            try:
                store.record_depth(
                    event_date=event_date,
                    market_ticker=ticker,
                    our_no_cents=C.NO_PRICE_CENTS,
                    yes_book=book["yes"],
                    no_book=book["no"],
                    **metrics,
                )
                taken += 1
            except Exception as exc:
                log.warning("could not store depth for %s: %s", ticker, exc)
            time.sleep(0.05)
        return taken

    def run_forever(self) -> None:
        STATE["running"] = True
        while not self._stop.is_set():
            try:
                now = clock.now_ct()
                today = clock.today_ct()

                if not clock.in_active_window(now) and now < clock.depth_deadline(today):
                    self._stop.wait(300)
                    continue
                if now > clock.depth_deadline(today):
                    with self._lock:
                        self._tickers = []
                    self._stop.wait(300)
                    continue

                self._discover()
                taken = self._snapshot_once()
                STATE["snapshots"] += taken
                STATE["last_run"] = now
            except Exception as exc:
                STATE["last_error"] = str(exc)[:200]
                log.exception("depth loop error")
            self._stop.wait(C.DEPTH_POLL_SECONDS)
        STATE["running"] = False

    def stop(self) -> None:
        self._stop.set()
