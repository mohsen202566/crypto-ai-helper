from __future__ import annotations

from typing import Any

import requests

import config
from utils import logger


class TelegramClient:
    def __init__(self, token: str = config.TELEGRAM_BOT_TOKEN, default_chat_id: str = config.TELEGRAM_CHAT_ID) -> None:
        self.token = token
        self.default_chat_id = default_chat_id
        self.offset = 0
        self.enabled = bool(token and default_chat_id)
        self.base_url = f"https://api.telegram.org/bot{token}" if token else ""

    def send(self, text: str, chat_id: str | int | None = None, reply_to_message_id: int | None = None) -> int | None:
        if not self.token:
            logger.info("Telegram disabled. Message: %s", text[:300])
            return None
        cid = chat_id or self.default_chat_id
        if not cid:
            logger.info("Telegram chat id empty. Message: %s", text[:300])
            return None
        payload: dict[str, Any] = {"chat_id": cid, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
            payload["allow_sending_without_reply"] = True
        try:
            r = requests.post(self.base_url + "/sendMessage", json=payload, timeout=15)
            data = r.json()
            if not data.get("ok"):
                logger.warning("Telegram send failed: %s", data)
                return None
            return int(data.get("result", {}).get("message_id") or 0) or None
        except Exception as exc:
            logger.warning("Telegram send exception: %s", exc)
            return None

    def get_updates(self) -> list[dict[str, Any]]:
        if not self.token:
            return []
        try:
            r = requests.get(self.base_url + "/getUpdates", params={"offset": self.offset, "timeout": config.TELEGRAM_POLL_TIMEOUT}, timeout=int(config.TELEGRAM_POLL_TIMEOUT) + 5)
            data = r.json()
            if not data.get("ok"):
                return []
            updates = data.get("result") or []
            for u in updates:
                self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
            return updates
        except Exception:
            return []
