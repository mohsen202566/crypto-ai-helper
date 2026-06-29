"""تلگرام بات ساده با دستورات فارسی و بدون وابستگی پیچیده."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

import requests

from . import config
from .messages_fa import balance_message, help_message, panel_message, positions_message, stats_message
from .storage import JSONStorage
from .trade_manager import TradeManager
from .utils import logger, safe_float, safe_int, validate_range


class TelegramBotService:
    def __init__(self, storage: JSONStorage, trade_manager: TradeManager, stats_manager: Any):
        self.storage = storage
        self.trade_manager = trade_manager
        self.stats_manager = stats_manager
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)
        self.base_url = f"https://api.telegram.org/bot{self.token}" if self.enabled else ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0

    def send_message(self, text: str, reply_to_message_id: int | None = None) -> int | None:
        if not self.enabled:
            logger.info("تلگرام تنظیم نیست. پیام ارسال نشد:\n%s", text)
            return None
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        try:
            response = requests.post(f"{self.base_url}/sendMessage", data=payload, timeout=config.REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            if data.get("ok"):
                return int(data["result"]["message_id"])
            logger.warning("ارسال تلگرام ناموفق بود: %s", data)
        except Exception as exc:
            logger.warning("خطا در ارسال تلگرام: %s", exc)
        return None

    def start(self) -> None:
        if not self.enabled:
            logger.warning("تلگرام تنظیم نشده است؛ فقط لاگ داخلی فعال است")
            return
        self._thread = threading.Thread(target=self._poll_loop, name="telegram-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                params = {"timeout": 25, "offset": self._offset}
                response = requests.get(f"{self.base_url}/getUpdates", params=params, timeout=35)
                response.raise_for_status()
                data = response.json()
                if not data.get("ok"):
                    time.sleep(2)
                    continue
                for update in data.get("result", []):
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    self._handle_update(update)
            except Exception as exc:
                logger.warning("خطا در دریافت آپدیت تلگرام: %s", exc)
                time.sleep(3)

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        text = str(message.get("text") or "").strip()
        if not text:
            return
        if str(chat.get("id")) != str(self.chat_id):
            return
        response = self.handle_command(text)
        if response:
            self.send_message(response, reply_to_message_id=message.get("message_id"))

    def handle_command(self, text: str) -> str:
        parts = text.split()
        command = parts[0].strip()
        arg = parts[1].strip() if len(parts) > 1 else None

        if command in ("/start", "/help", "/راهنما"):
            return help_message()
        if command in ("/آمار", "/stats"):
            return stats_message(self.stats_manager.summary())
        if command in ("/حذف_آمار", "/reset_stats"):
            self.stats_manager.reset()
            return "⚠️ آمار ربات حذف شد.\nآمار سیگنال‌ها، TP/SL عادی و TP/SL واقعی از صفر شروع می‌شود."
        if command in ("/ترید_فعال", "/trade_on"):
            self.storage.update_setting("trade_enabled", True)
            return "✅ ترید فعال شد.\nاز این به بعد در صورت صدور سیگنال معتبر، سفارش واقعی روی Toobit ارسال می‌شود."
        if command in ("/ترید_خاموش", "/trade_off"):
            self.storage.update_setting("trade_enabled", False)
            return "⛔ ترید خاموش شد.\nربات همچنان تحلیل و سیگنال صادر می‌کند، اما معامله واقعی باز نمی‌کند."
        if command in ("/دلار_ترید", "/trade_amount"):
            if arg is None:
                return "❌ مقدار دلار ترید را وارد کن. مثال: /دلار_ترید 10"
            value = safe_float(arg, -1)
            ok, msg = validate_range(value, config.TRADE_AMOUNT_MIN, config.TRADE_AMOUNT_MAX, "دلار ترید")
            if not ok:
                return msg
            self.storage.update_setting("trade_amount_usdt", value)
            return f"✅ مقدار ترید تنظیم شد.\nمقدار جدید هر معامله: {value:g} USDT"
        if command in ("/لوریج_ترید", "/leverage"):
            if arg is None:
                return "❌ مقدار لوریج را وارد کن. مثال: /لوریج_ترید 10"
            value = safe_int(arg, -1)
            ok, msg = validate_range(value, config.LEVERAGE_MIN, config.LEVERAGE_MAX, "لوریج")
            if not ok:
                return msg
            self.storage.update_setting("leverage", value)
            return f"✅ لوریج ترید تنظیم شد.\nلوریج جدید: {value}x"
        if command in ("/حداکثر_پوزیشن", "/max_positions"):
            if arg is None:
                return "❌ تعداد حداکثر پوزیشن را وارد کن. مثال: /حداکثر_پوزیشن 3"
            value = safe_int(arg, -1)
            ok, msg = validate_range(value, config.MAX_POSITIONS_MIN, config.MAX_POSITIONS_MAX, "حداکثر پوزیشن")
            if not ok:
                return msg
            self.storage.update_setting("max_positions", value)
            return f"✅ حداکثر پوزیشن تنظیم شد.\nحداکثر پوزیشن همزمان: {value}"
        if command in ("/موجودی", "/balance"):
            balance, err = self.trade_manager.get_balance_safe()
            if err:
                return f"⚠️ دریافت موجودی ناموفق بود:\n{err}"
            return balance_message(balance or {})
        if command in ("/پوزیشن", "/positions"):
            positions, err = self.trade_manager.get_positions_safe()
            if err:
                return f"⚠️ دریافت پوزیشن‌ها ناموفق بود:\n{err}"
            return positions_message(positions)
        if command in ("/پنل", "/panel", "/status"):
            balance, _ = self.trade_manager.get_balance_safe()
            positions, _ = self.trade_manager.get_positions_safe()
            symbols_count = len(self.storage.get_validated_symbols() or config.WATCHLIST)
            return panel_message(self.storage.get_settings(), self.stats_manager.summary(), balance, positions, symbols_count)
        return "دستور نامشخص است. برای دیدن دستورات بنویس: /راهنما"
