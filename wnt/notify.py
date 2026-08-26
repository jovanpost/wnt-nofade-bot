"""Telegram alerts and phone commands."""
from __future__ import annotations

import html
import logging
import threading
import time
from typing import Callable

import requests

from . import config as C

log = logging.getLogger("wnt.notify")

API = "https://api.telegram.org/bot{token}/{method}"
_handlers: dict[str, Callable[[list[str]], str]] = {}
_listener_started = False


def send(message: str, quiet: bool = False) -> bool:
    """Send a Telegram message. Never raises -- alerting must not break trading."""
    first_line = message.splitlines()[0] if message else ""
    log.info("TG: %s", first_line[:120])
    if not (C.TELEGRAM_TOKEN and C.TELEGRAM_CHAT_ID):
        return False
    try:
        resp = requests.post(
            API.format(token=C.TELEGRAM_TOKEN, method="sendMessage"),
            json={
                "chat_id": C.TELEGRAM_CHAT_ID,
                "text": message[:4000],
                "parse_mode": "HTML",
                "disable_notification": quiet,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        return resp.status_code == 200
    except Exception as exc:
        log.error("telegram send failed: %s", exc)
        return False


def esc(text: str) -> str:
    return html.escape(str(text))


def register(command: str, handler: Callable[[list[str]], str]) -> None:
    _handlers[command.lower().lstrip("/")] = handler


def _dispatch(text: str) -> str | None:
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    name = parts[0][1:].split("@")[0].lower()
    if name in ("help", "start"):
        return "Commands: " + ", ".join("/" + k for k in sorted(_handlers))
    handler = _handlers.get(name)
    if handler is None:
        return None
    try:
        return handler(parts[1:])
    except Exception as exc:
        log.exception("command %s blew up", name)
        return f"Command failed: {esc(str(exc)[:300])}"


def _listen() -> None:
    offset = None
    try:
        seed = requests.get(
            API.format(token=C.TELEGRAM_TOKEN, method="getUpdates"),
            params={"timeout": 0}, timeout=20,
        ).json()
        results = seed.get("result") or []
        if results:
            offset = results[-1]["update_id"] + 1
    except Exception:
        pass

    while True:
        try:
            resp = requests.get(
                API.format(token=C.TELEGRAM_TOKEN, method="getUpdates"),
                params={"timeout": 50, "offset": offset}, timeout=70,
            )
            for update in (resp.json().get("result") or []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("channel_post") or {}
                text = message.get("text") or ""
                chat_id = str((message.get("chat") or {}).get("id", ""))
                if C.TELEGRAM_CHAT_ID and chat_id != str(C.TELEGRAM_CHAT_ID):
                    continue
                reply = _dispatch(text)
                if reply:
                    send(reply)
        except Exception as exc:
            log.warning("telegram listener hiccup: %s", exc)
            time.sleep(10)


def start_listener() -> None:
    global _listener_started
    if _listener_started or not C.TELEGRAM_COMMANDS:
        return
    if not (C.TELEGRAM_TOKEN and C.TELEGRAM_CHAT_ID):
        log.warning("telegram not configured; commands disabled")
        return
    _listener_started = True
    threading.Thread(target=_listen, daemon=True, name="tg-listener").start()
    log.info("telegram command listener started")
