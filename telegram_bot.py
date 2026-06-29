"""تلگرام بات با دستورات فارسی عادی، با اسلش و بدون اسلش."""
from __future__ import annotations

import re
import threading
import time
from typing import Any

import requests

from . import config
from .messages_fa import (
    balance_message,
    help_message,
    panel_message,
    positions_message,
    stats_message,
    toobit_status_message,
)
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

    @staticmethod
    def _normalize(text: str) -> str:
        text = str(text or "").strip()
        text = text.replace("ي", "ی").replace("ك", "ک")
        text = text.replace("‌", " ")
        text = text.replace("/", " ").replace("_", " ").replace("-", " ")
        text = re.sub(r"\s+", " ", text).strip().lower()
        return text

    @staticmethod
    def _last_number(text: str) -> str | None:
        m = re.findall(r"\d+(?:\.\d+)?", text)
        return m[-1] if m else None

    def _panel(self) -> str:
        balance, balance_err = self.trade_manager.get_balance_safe()
        positions, pos_err = self.trade_manager.get_positions_safe()
        toobit_status = self.trade_manager.get_toobit_status_safe(balance=balance, balance_error=balance_err)
        symbols_count = len(self.storage.get_validated_symbols() or config.WATCHLIST)
        return panel_message(
            self.storage.get_settings(),
            self.stats_manager.summary(),
            balance,
            positions,
            symbols_count,
            toobit_status,
            errors=[e for e in (balance_err, pos_err) if e],
        )

    def _enable_real_trading(self) -> str:
        ok, status = self.trade_manager.check_toobit_connection()
        if not ok:
            self.storage.update_setting("trade_enabled", False)
            return (
                "❌ ترید واقعی روشن نشد.\n"
                "اتصال Toobit یا کلید API مشکل دارد.\n\n"
                f"جزئیات: {status.get('message', 'نامشخص')}\n\n"
                "تا وقتی Toobit وصل نباشد، سیگنال‌ها فقط عادی/داخلی ثبت می‌شوند."
            )
        self.storage.update_setting("trade_enabled", True)
        return (
            "✅ ترید واقعی فعال شد.\n"
            "اتصال Toobit بررسی شد و پاسخ داد.\n"
            "اگر اسلات پوزیشن خالی باشد، سیگنال‌های بعدی به‌صورت رئال روی Toobit اجرا می‌شوند."
        )

    def _disable_real_trading(self) -> str:
        self.storage.update_setting("trade_enabled", False)
        return (
            "⛔ ترید واقعی خاموش شد.\n"
            "ربات همچنان تحلیل می‌کند و سیگنال می‌دهد، اما سیگنال‌ها عادی/داخلی ثبت می‌شوند."
        )

    def handle_command(self, text: str) -> str:
        raw = str(text or "").strip()
        cmd = self._normalize(raw)
        number = self._last_number(cmd)

        # راهنما و پنل
        if cmd in {"start", "help", "راهنما", "کمک", "دستورات", "لیست دستورات"}:
            return help_message()
        if cmd in {"پنل", "پنل ترید", "وضعیت", "وضعیت ربات", "status", "panel", "ترید"}:
            return self._panel()

        # آمار
        if cmd in {"آمار", "امار", "stats", "stat"}:
            return stats_message(self.stats_manager.summary())
        if cmd in {"حذف آمار", "حذف امار", "پاک کردن آمار", "پاک کردن امار", "ریست آمار", "ریست امار", "reset stats"}:
            self.stats_manager.reset()
            return "⚠️ آمار ربات حذف شد.\nآمار سیگنال‌ها، TP/SL عادی، TP/SL رئال و سود/ضرر کل از صفر شروع می‌شود."

        # روشن/خاموش کردن ترید واقعی و Toobit
        enable_phrases = {
            "ترید فعال", "ترید روشن", "فعال کردن ترید", "روشن کردن ترید",
            "ترید رئال فعال", "ترید واقعی فعال", "معامله فعال", "معامله واقعی فعال",
            "توبیت روشن", "توبیت فعال", "فعال کردن توبیت", "روشن کردن توبیت",
            "trade on", "real on", "toobit on",
        }
        disable_phrases = {
            "ترید خاموش", "ترید غیر فعال", "ترید غیرفعال", "خاموش کردن ترید", "غیر فعال کردن ترید", "غیرفعال کردن ترید",
            "ترید رئال خاموش", "ترید واقعی خاموش", "معامله خاموش", "معامله واقعی خاموش",
            "توبیت خاموش", "توبیت غیر فعال", "توبیت غیرفعال", "خاموش کردن توبیت",
            "trade off", "real off", "toobit off",
        }
        if cmd in enable_phrases:
            return self._enable_real_trading()
        if cmd in disable_phrases:
            return self._disable_real_trading()

        # وضعیت Toobit
        if cmd in {"چک توبیت", "تست توبیت", "وضعیت توبیت", "اتصال توبیت", "toobit", "toobit status"}:
            ok, status = self.trade_manager.check_toobit_connection()
            return toobit_status_message(status, ok)

        # تنظیمات عددی ترید
        if cmd.startswith(("دلار ترید", "مقدار ترید", "مارجین ترید", "حجم ترید", "trade amount")):
            if number is None:
                return "❌ مقدار دلار ترید را وارد کن.\nمثال: دلار ترید 10"
            value = safe_float(number, -1)
            ok, msg = validate_range(value, config.TRADE_AMOUNT_MIN, config.TRADE_AMOUNT_MAX, "دلار ترید")
            if not ok:
                return msg
            self.storage.update_setting("trade_amount_usdt", value)
            return f"✅ مقدار هر ترید تنظیم شد.\nمقدار جدید: {value:g} USDT\nمحدوده مجاز: 1 تا 10000 USDT"

        if cmd.startswith(("لوریج ترید", "لوریج", "leverage")):
            if number is None:
                return "❌ مقدار لوریج را وارد کن.\nمثال: لوریج ترید 10"
            value = safe_int(number, -1)
            ok, msg = validate_range(value, config.LEVERAGE_MIN, config.LEVERAGE_MAX, "لوریج")
            if not ok:
                return msg
            self.storage.update_setting("leverage", value)
            return f"✅ لوریج ترید تنظیم شد.\nلوریج جدید: {value}x\nمحدوده مجاز: 1x تا 100x"

        if cmd.startswith(("حداکثر پوزیشن", "حد اکثر پوزیشن", "حداکثر پوزیشن همزمان", "max positions")):
            if number is None:
                return "❌ تعداد حداکثر پوزیشن را وارد کن.\nمثال: حداکثر پوزیشن 3"
            value = safe_int(number, -1)
            ok, msg = validate_range(value, config.MAX_POSITIONS_MIN, config.MAX_POSITIONS_MAX, "حداکثر پوزیشن")
            if not ok:
                return msg
            self.storage.update_setting("max_positions", value)
            return f"✅ حداکثر پوزیشن تنظیم شد.\nحداکثر پوزیشن رئال همزمان: {value}\nمحدوده مجاز: 1 تا 100"

        # موجودی، مارجین، پوزیشن، سود امروز
        if cmd in {"موجودی", "بالانس", "مارجین", "موجودی توبیت", "مارجین توبیت", "balance"}:
            balance, err = self.trade_manager.get_balance_safe()
            if err:
                return f"⚠️ دریافت موجودی/مارجین Toobit ناموفق بود:\n{err}"
            return balance_message(balance or {})

        if cmd in {"پوزیشن", "پوزیشن ها", "پوزیشن‌ها", "پوزیشن توبیت", "positions"}:
            positions, err = self.trade_manager.get_positions_safe()
            if err:
                return f"⚠️ دریافت پوزیشن‌ها ناموفق بود:\n{err}"
            return positions_message(positions)

        if cmd in {"سود امروز", "ضرر امروز", "سود ضرر امروز", "سود و ضرر امروز", "pnl today", "today pnl"}:
            pnl, err = self.trade_manager.get_today_pnl_safe()
            if err:
                return f"⚠️ دریافت سود/ضرر امروز Toobit ناموفق بود:\n{err}"
            return f"📅 سود/ضرر امروز Toobit\n\nسود/ضرر امروز: {pnl:.4f} USDT"

        return "دستور نامشخص است. برای دیدن دستورات بنویس: راهنما"
