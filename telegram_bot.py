"""Telegram adapter با مسیر فرمان مستقل و صف خروجی."""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import requests

import config
from command_router import CommandRouter
from storage import Storage
from telegram_ui import failed_open_message, position_open_message, result_message, signal_message

logger = logging.getLogger("adaptive_bot")


class TelegramBot:
    def __init__(self, storage: Storage, router: CommandRouter, notification_queue: queue.Queue[dict[str, Any]]):
        self.storage = storage
        self.router = router
        self.notifications = notification_queue
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        # Polling is a long request while notifications send concurrently; separate
        # Sessions keep the two I/O paths isolated.
        self.poll_session = requests.Session()
        self.send_session = requests.Session()
        self.stop_event = threading.Event()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_message(self, text: str, reply_to_message_id: int | None = None) -> int | None:
        if not self.enabled:
            logger.info("TELEGRAM_DISABLED | %s", text.replace("\n", " ")[:180])
            return None
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        try:
            res = self.send_session.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=payload,
                timeout=10,
            )
            data = res.json()
            if not data.get("ok"):
                raise RuntimeError(str(data)[:500])
            return int((data.get("result") or {}).get("message_id") or 0) or None
        except Exception as exc:
            self.storage.runtime.set_health("telegram", "warning", f"send failed: {exc}")
            logger.warning("Telegram send failed: %s", exc)
            return None

    def poll_loop(self) -> None:
        if not self.enabled:
            self.storage.runtime.set_health("telegram", "warning", "Token/Chat ID تنظیم نشده")
            while not self.stop_event.wait(5):
                pass
            return
        offset = self.storage.runtime.telegram_offset()
        self.storage.runtime.set_health("telegram", "ok", "polling")
        while not self.stop_event.is_set():
            try:
                res = self.poll_session.get(
                    f"https://api.telegram.org/bot{self.token}/getUpdates",
                    params={"offset": offset + 1, "timeout": config.TELEGRAM_POLL_TIMEOUT},
                    timeout=config.TELEGRAM_POLL_TIMEOUT + 5,
                )
                data = res.json()
                for upd in data.get("result", []):
                    offset = max(offset, int(upd.get("update_id", 0)))
                    self.storage.runtime.set_telegram_offset(offset)
                    msg = upd.get("message") or {}
                    incoming_chat = str((msg.get("chat") or {}).get("id") or "")
                    if incoming_chat != str(self.chat_id):
                        self.storage.runtime.add_event("TELEGRAM_SECURITY", "دستور chat_id غیرمجاز نادیده گرفته شد")
                        continue
                    text = str(msg.get("text") or "").strip()
                    if text:
                        answer = self.router.handle(text)
                        self.send_message(answer, reply_to_message_id=int(msg.get("message_id") or 0) or None)
            except Exception as exc:
                self.storage.runtime.set_health("telegram", "warning", f"poll failed: {exc}")
                time.sleep(2)

    def notification_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                item = self.notifications.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._handle_notification(item)
            finally:
                self.notifications.task_done()

    def _handle_notification(self, item: dict[str, Any]) -> None:
        kind = item.get("type")
        if kind == "plain":
            self.send_message(str(item.get("text") or ""))
            return
        signal_id = int(item.get("signal_id") or 0)
        signal = self.storage.runtime.get_signal(signal_id)
        if not signal:
            return
        if kind == "signal":
            message_id = self.send_message(signal_message(signal))
            if message_id:
                self.storage.runtime.update_signal(signal_id, telegram_message_id=message_id)
        elif kind == "position_open":
            self.send_message(position_open_message(signal), reply_to_message_id=signal.get("telegram_message_id"))
        elif kind == "failed_open":
            self.send_message(failed_open_message(signal), reply_to_message_id=signal.get("telegram_message_id"))
        elif kind == "result":
            self.send_message(result_message(signal), reply_to_message_id=signal.get("telegram_message_id"))

    def stop(self) -> None:
        self.stop_event.set()
        for session in (self.poll_session, self.send_session):
            try:
                session.close()
            except Exception:
                pass
