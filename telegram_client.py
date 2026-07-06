from __future__ import annotations

from typing import Any

import requests

import config
from utils import logger, safe_int


class TelegramClient:
    def __init__(self, token: str = config.TELEGRAM_BOT_TOKEN, default_chat_id: str = config.TELEGRAM_CHAT_ID) -> None:
        self.token = token
        self.default_chat_id = default_chat_id
        self.base_url = f"https://api.telegram.org/bot{token}" if token else ""
        self.session = requests.Session()
        self.offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def send(self, text: str, chat_id: str | int | None = None, reply_to_message_id: int | None = None) -> int | None:
        if not self.enabled:
            logger.info("Telegram disabled, message: %s", text)
            return None
        target = chat_id or self.default_chat_id
        if not target:
            logger.info("TELEGRAM_CHAT_ID empty, message: %s", text)
            return None
        payload: dict[str, Any] = {"chat_id": target, "text": text, "disable_web_page_preview": True}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        try:
            r = self.session.post(f"{self.base_url}/sendMessage", data=payload, timeout=15)
            r.raise_for_status()
            data = r.json()
            return safe_int(((data.get("result") or {}).get("message_id")), 0) or None
        except Exception as exc:
            logger.warning("ارسال تلگرام ناموفق بود: %s", exc)
            return None

    def get_updates(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        try:
            r = self.session.get(
                f"{self.base_url}/getUpdates",
                params={"offset": self.offset, "timeout": config.TELEGRAM_POLL_TIMEOUT, "allowed_updates": ["message"]},
                timeout=config.TELEGRAM_POLL_TIMEOUT + 10,
            )
            r.raise_for_status()
            data = r.json()
            updates = data.get("result") or []
            if updates:
                self.offset = max(int(u.get("update_id", 0)) for u in updates) + 1
            return updates
        except Exception as exc:
            logger.warning("دریافت آپدیت تلگرام ناموفق بود: %s", exc)
            return []
