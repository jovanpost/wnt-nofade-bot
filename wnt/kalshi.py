"""Kalshi REST client."""
from __future__ import annotations

import base64
import logging
import random
import time
from decimal import Decimal
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import config as C

log = logging.getLogger("wnt.kalshi")


class KalshiError(RuntimeError):
    def __init__(self, status: int, body: str, endpoint: str):
        self.status = status
        self.body = body
        self.endpoint = endpoint
        super().__init__(f"{endpoint} -> {status}: {body[:300]}")


def _to_cents(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except Exception:
        return None
    if d != d.to_integral_value() or (0 < d < 1):
        return int((d * 100).to_integral_value())
    return int(d)


def _to_count(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(Decimal(str(value)))
    except Exception:
        return 0.0


class KalshiClient:
    def __init__(
        self,
        key_id: str | None = None,
        private_key_pem: str | None = None,
        private_key_path: str | None = None,
        base_url: str | None = None,
    ):
        self.key_id = key_id if key_id is not None else C.KALSHI_KEY_ID
        self.base_url = (base_url or C.BASE_URL).rstrip("/")
        self._key = self._load_key(
            private_key_pem if private_key_pem is not None else C.KALSHI_PRIVATE_KEY_PEM,
            private_key_path if private_key_path is not None else C.KALSHI_PRIVATE_KEY_PATH,
        )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": C.USER_AGENT})

    @staticmethod
    def _load_key(pem_text: str, pem_path: str):
        raw = None
        if pem_text and "BEGIN" in pem_text:
            raw = pem_text.replace("\\n", "\n").strip().encode()
        elif pem_path:
            try:
                with open(pem_path, "rb") as fh:
                    raw = fh.read()
            except OSError as exc:
                log.warning("could not read private key at %s: %s", pem_path, exc)
        if raw is None:
            return None
        try:
            return serialization.load_pem_private_key(raw, password=None)
        except Exception as exc:
            log.error("private key failed to parse: %s", exc)
            return None

    @property
    def authenticated(self) -> bool:
        return self._key is not None and bool(self.key_id)

    def _headers(self, method: str, sign_path: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if not self.authenticated:
            return headers
        ts = str(int(time.time() * 1000))
        message = (ts + method.upper() + sign_path.split("?")[0]).encode()
        signature = self._key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        headers.update({
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        })
        return headers

    def request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        body: dict | None = None,
        auth: bool = True,
        retries: int = 4,
        timeout: int = 20,
    ) -> dict:
        sign_path = C.API_ROOT + endpoint
        url = self.base_url + sign_path
        last: Exception | None = None

        for attempt in range(retries + 1):
            headers = self._headers(method, sign_path) if auth else {
                "Content-Type": "application/json", "User-Agent": C.USER_AGENT,
            }
            try:
                resp = self.session.request(
                    method, url, params=params, json=body,
                    headers=headers, timeout=timeout,
                )
            except requests.RequestException as exc:
                last = exc
                if attempt >= retries:
                    raise
                time.sleep(min(2 ** attempt, 8) + random.random())
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last = KalshiError(resp.status_code, resp.text, endpoint)
                if attempt >= retries:
                    raise last
                time.sleep(min(0.25 * (2 ** attempt), 5) + random.random() * 0.25)
                continue

            if resp.status_code >= 400:
                raise KalshiError(resp.status_code, resp.text, endpoint)

            if not resp.text:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}

        raise last or RuntimeError("unreachable")

    def paginate(self, endpoint: str, key: str, params: dict | None = None,
                 auth: bool = True, max_pages: int = 20) -> list[dict]:
        out: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            page_params = dict(params or {})
            if cursor:
                page_params["cursor"] = cursor
            data = self.request("GET", endpoint, params=page_params, auth=auth)
            out.extend(data.get(key) or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def get_events(self, series_ticker: str, status: str = "open") -> list[dict]:
        return self.paginate(
            "/events", "events",
            {"series_ticker": series_ticker, "status": status, "limit": 200},
            auth=False,
        )

    def get_markets(self, event_ticker: str) -> list[dict]:
        return self.paginate(
            "/markets", "markets",
            {"event_ticker": event_ticker, "limit": 200},
            auth=False,
        )

    def get_market(self, ticker: str) -> dict:
        return (self.request("GET", f"/markets/{ticker}", auth=False) or {}).get("market", {})

    def get_series(self, series_ticker: str) -> dict:
        return (self.request("GET", f"/series/{series_ticker}", auth=False) or {}).get("series", {})

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        raw = self.request(
            "GET", f"/markets/{ticker}/orderbook",
            params={"depth": depth}, auth=False,
        ) or {}
        book = raw.get("orderbook_fp") or raw.get("orderbook") or {}
        out: dict[str, list[tuple[int, float]]] = {"yes": [], "no": []}
        for side in ("yes", "no"):
            levels = book.get(f"{side}_dollars")
            if levels is None:
                levels = book.get(side)
            parsed = []
            for level in levels or []:
                try:
                    cents = _to_cents(level[0])
                    count = _to_count(level[1])
                except (IndexError, TypeError):
                    continue
                if cents is not None:
                    parsed.append((cents, count))
            parsed.sort(key=lambda x: x[0])
            out[side] = parsed
        return out

    def get_balance(self) -> dict:
        return self.request("GET", "/portfolio/balance")

    def get_account_limits(self) -> dict:
        return self.request("GET", "/account/limits")

    def get_resting_orders(self, series_prefix: str | None = None) -> list[dict]:
        orders = self.paginate(
            "/portfolio/orders", "orders", {"status": "resting", "limit": 200},
        )
        if series_prefix:
            orders = [o for o in orders
                      if str(o.get("ticker", "")).startswith(series_prefix)]
        return orders

    def get_fills(self, ticker: str | None = None, limit: int = 200) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self.paginate("/portfolio/fills", "fills", params)

    def get_positions(self) -> list[dict]:
        return self.paginate(
            "/portfolio/positions", "market_positions", {"limit": 200},
        )

    def get_settlements(self, limit: int = 200) -> list[dict]:
        return self.paginate("/portfolio/settlements", "settlements", {"limit": limit})

    def create_no_order(
        self,
        ticker: str,
        no_price_cents: int,
        count: int,
        client_order_id: str,
        post_only: bool = True,
        expiration_epoch: int | None = None,
    ) -> dict:
        if C.ORDER_API == "v1":
            body = {
                "ticker": ticker,
                "client_order_id": client_order_id,
                "action": "buy",
                "side": "no",
                "count": int(count),
                "type": "limit",
                "no_price": int(no_price_cents),
                "post_only": bool(post_only),
            }
            if expiration_epoch:
                body["expiration_ts"] = int(expiration_epoch)
            resp = self.request("POST", "/portfolio/orders", body=body)
            order = resp.get("order") or {}
            return {
                "order_id": order.get("order_id"),
                "client_order_id": order.get("client_order_id") or client_order_id,
                "fill_count": _to_count(order.get("taker_fill_count") or 0),
                "remaining_count": _to_count(order.get("remaining_count") or count),
                "avg_fill_price_cents": _to_cents(order.get("taker_fill_cost")),
                "fee_cents": None,
                "raw": resp,
            }

        yes_price = 100 - int(no_price_cents)
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": "ask",
            "count": f"{float(count):.2f}",
            "price": f"{yes_price / 100:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": bool(post_only),
            "cancel_order_on_pause": True,
            "reduce_only": False,
        }
        if expiration_epoch:
            body["expiration_time"] = int(expiration_epoch)

        resp = self.request("POST", "/portfolio/events/orders", body=body)
        return {
            "order_id": resp.get("order_id"),
            "client_order_id": resp.get("client_order_id") or client_order_id,
            "fill_count": _to_count(resp.get("fill_count")),
            "remaining_count": _to_count(resp.get("remaining_count")),
            "avg_fill_price_cents": _to_cents(resp.get("average_fill_price")),
            "fee_cents": _to_cents(resp.get("average_fee_paid")),
            "raw": resp,
        }

    def cancel_order(self, order_id: str) -> bool:
        for endpoint in (f"/portfolio/events/orders/{order_id}",
                         f"/portfolio/orders/{order_id}"):
            try:
                self.request("DELETE", endpoint, retries=3)
                return True
            except KalshiError as exc:
                if exc.status == 404:
                    return True
                log.warning("cancel via %s failed: %s", endpoint, exc)
        return False

    def batch_cancel(self, order_ids: list[str]) -> tuple[int, list[str]]:
        if not order_ids:
            return 0, []
        try:
            resp = self.request(
                "DELETE", "/portfolio/events/orders/batched",
                body={"orders": [{"order_id": oid} for oid in order_ids]},
                retries=3,
            )
            failed = []
            ok = 0
            for entry in resp.get("orders", []):
                if entry.get("error"):
                    failed.append(entry.get("order_id"))
                else:
                    ok += 1
            if ok or not failed:
                return ok, [f for f in failed if f]
        except KalshiError as exc:
            log.warning("batch cancel failed (%s), falling back to singles", exc)

        ok, failed = 0, []
        for oid in order_ids:
            if self.cancel_order(oid):
                ok += 1
            else:
                failed.append(oid)
            time.sleep(0.05)
        return ok, failed


def book_metrics(book: dict, our_no_cents: int) -> dict:
    yes = book.get("yes") or []
    no = book.get("no") or []
    yes_trigger = 100 - our_no_cents
    return {
        "best_yes_bid": yes[-1][0] if yes else None,
        "best_no_bid": no[-1][0] if no else None,
        "yes_size_total": sum(c for _, c in yes),
        "no_size_total": sum(c for _, c in no),
        "no_size_ahead": sum(c for p, c in no if p > our_no_cents),
        "no_size_at_our_price": sum(c for p, c in no if p == our_no_cents),
        "yes_size_that_would_fill_us": sum(c for p, c in yes if p >= yes_trigger),
    }
