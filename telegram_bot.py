"""تلگرام بات ساده با دستورات فارسی عادی، بدون الزام اسلش."""
from __future__ import annotations

import re
import threading
import time
from typing import Any

import requests

import config
from messages_fa import balance_message, help_message, panel_message, positions_message, stats_message
from storage import JSONStorage
from symbol_profiles import SymbolProfileManager
from trade_manager import TradeManager
from utils import logger, safe_float, safe_int, validate_range


class TelegramBotService:
    def __init__(self, storage: JSONStorage, trade_manager: TradeManager, stats_manager: Any):
        self.storage = storage
        self.trade_manager = trade_manager
        self.stats_manager = stats_manager
        self.profile_manager = SymbolProfileManager()
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
        text = text.replace("/", " ").replace("_", " ").replace("‌", " ")
        text = text.replace("ي", "ی").replace("ك", "ک")
        text = re.sub(r"\s+", " ", text)
        return text.strip().lower()

    @staticmethod
    def _last_number(parts: list[str]) -> str | None:
        if not parts:
            return None
        raw = parts[-1].replace(",", ".")
        try:
            float(raw)
            return raw
        except Exception:
            return None

    def _panel(self) -> str:
        balance, balance_err = self.trade_manager.get_balance_safe()
        positions, pos_err = self.trade_manager.get_positions_safe()
        today_pnl, today_err = self.trade_manager.get_today_pnl_safe()
        toobit_ok = balance_err is None
        symbols_count = len(self.storage.get_validated_symbols() or config.WATCHLIST)
        errors = []
        if balance_err:
            errors.append(f"موجودی/اتصال: {balance_err}")
        if pos_err:
            errors.append(f"پوزیشن‌ها: {pos_err}")
        if today_err:
            errors.append(f"سود امروز: {today_err}")
        return panel_message(
            self.storage.get_settings(),
            self.stats_manager.summary(),
            balance,
            positions,
            symbols_count,
            toobit_ok=toobit_ok,
            toobit_error="\n".join(errors) if errors else None,
            today_pnl=today_pnl,
        )

    def _monitor_message(self) -> str:
        signals = self.storage.active_all_signals()
        if not signals:
            return "✅ هیچ سیگنال باز/گیرکرده‌ای وجود ندارد."
        lines = ["📡 وضعیت مانیتورینگ سیگنال‌های باز", ""]
        for sig in signals:
            mode = str(sig.get("execution_mode_fa") or sig.get("execution_mode") or "عادی")
            side = "لانگ" if str(sig.get("side")).upper() == "BUY" else "شورت"
            lines.append(f"• {sig.get('symbol')} | {side} | {mode}")
            lines.append(f"  ورود: {sig.get('entry')} | TP: {sig.get('tp')} | SL: {sig.get('sl')}")
            if sig.get("real_order"):
                lines.append("  رئال: سفارش/پوزیشن اولیه ثبت شده")
            if sig.get("real_monitor_note"):
                lines.append(f"  مانیتور: {sig.get('real_monitor_note')}")
            if sig.get("history_missing_since_ms"):
                lines.append("  هشدار: پوزیشن بسته دیده شده ولی history هنوز PnL نداده")
            lines.append("")
        return "\n".join(lines).strip()


    def _market_message(self) -> str:
        market = self.storage.get_market_state()
        if not market:
            return "⚠️ هنوز وضعیت بازار ذخیره نشده است. چند دقیقه صبر کن یا ربات را ری‌استارت کن."

        direction = str(market.get("direction") or "RANGE").upper()
        fa = "صعودی / فقط لانگ" if direction == "BUY" else "نزولی / فقط شورت" if direction == "SELL" else "رنج / بدون سیگنال"
        lines = ["📊 وضعیت فیلتر بازار", "", f"حالت فعلی: {fa}", str(market.get("summary") or "").strip(), ""]

        details = market.get("details") or {}
        tfs = details.get("timeframes") or {}
        if tfs:
            lines.append("تایم‌فریم‌های بازار:")
            for tf, info in tfs.items():
                counts = info.get("counts") or {}
                lines.append(
                    f"• {tf}: {info.get('direction')} | BUY={counts.get('BUY', 0)} SELL={counts.get('SELL', 0)} RANGE={counts.get('RANGE', 0)}"
                )
            lines.append("")

        anchors = details.get("anchors") or {}
        if anchors:
            lines.append("تایید BTC/ETH:")
            for sym, item in anchors.items():
                label = sym.replace("USDT", "")
                parts = [f"{tf}={val}" for tf, val in item.items()]
                lines.append(f"• {label}: " + " | ".join(parts))

        lines.append("")
        lines.append("اگر حالت بازار رنج باشد، ربات عمداً سیگنال جدید نمی‌دهد.")
        return "\n".join([x for x in lines if x is not None]).strip()

    def _profile_message(self, cmd: str) -> str:
        parts = cmd.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return "❌ نماد را وارد کن. مثال: بازه SOL یا بازه XRP"
        return self.profile_manager.format_profile_message(parts[1].strip())

    def handle_command(self, text: str) -> str:
        cmd = self._normalize(text)
        parts = cmd.split()
        number = self._last_number(parts)

        # «ترید» باید پنل ترید بدهد، نه راهنما.
        if cmd in ("ترید", "پنل", "پنل ترید", "وضعیت", "وضعیت ترید", "panel", "status"):
            return self._panel()

        if cmd in ("start", "help", "راهنما", "کمک", "دستورات", "commands"):
            return help_message()

        if cmd in ("آمار", "امار", "stats", "stat"):
            return stats_message(self.stats_manager.summary())

        if cmd in ("مانیتور", "وضعیت مانیتور", "سیگنال باز", "سیگنال های باز", "سیگنال‌های باز", "open signals", "monitor"):
            return self._monitor_message()

        if cmd in ("بازار", "جهت بازار", "وضعیت بازار", "دلیل سکوت", "چرا سیگنال نمیده", "چرا سیگنال نمی‌دهد", "market"):
            return self._market_message()

        if cmd.startswith(("بازه ", "profile ")) or cmd == "بازه":
            return self._profile_message(cmd)

        if cmd in ("حذف آمار", "حذف امار", "پاک کردن آمار", "پاک کردن امار", "ریست آمار", "ریست امار", "reset stats"):
            self.stats_manager.reset()
            return "⚠️ آمار ربات حذف شد.\nآمار سیگنال‌ها، TP/SL عادی، TP/SL واقعی، سود/ضرر و سیگنال‌های باز از صفر شروع می‌شود."

        if cmd in ("موجودی", "بالانس", "مارجین", "balance", "margin"):
            balance, err = self.trade_manager.get_balance_safe()
            if err:
                return f"❌ اتصال یا دریافت موجودی Toobit ناموفق بود:\n{err}"
            return balance_message(balance or {})

        if cmd in ("پوزیشن", "پوزیشن ها", "پوزیشن‌ها", "positions"):
            positions, err = self.trade_manager.get_positions_safe()
            if err:
                return f"❌ دریافت پوزیشن‌های Toobit ناموفق بود:\n{err}"
            return positions_message(positions)

        if cmd in ("چک توبیت", "تست توبیت", "بررسی توبیت", "اتصال توبیت", "toobit check"):
            ok, msg, balance = self.trade_manager.check_toobit_connection()
            positions, pos_err = self.trade_manager.get_positions_safe()
            if not ok:
                self.storage.update_setting("trade_enabled", False)
                return f"❌ اتصال Toobit برقرار نیست. ترید واقعی خاموش شد.\n\nخطا:\n{msg}"
            out = "✅ اتصال Toobit برقرار است.\n\n" + balance_message(balance or {})
            if pos_err:
                out += f"\n⚠️ دریافت پوزیشن‌ها ناموفق بود:\n{pos_err}"
            else:
                out += f"\n📌 تعداد پوزیشن‌های باز: {len(positions)}"
            return out

        if cmd in ("ترید فعال", "ترید روشن", "فعال کردن ترید", "روشن کردن ترید", "توبیت روشن", "معامله فعال", "trade on"):
            ok, msg, _balance = self.trade_manager.check_toobit_connection()
            if not ok:
                self.storage.update_setting("trade_enabled", False)
                return f"❌ ترید واقعی فعال نشد.\nاول باید اتصال Toobit درست باشد.\n\nخطا:\n{msg}"
            self.storage.update_setting("trade_enabled", True)
            return "✅ ترید واقعی فعال شد.\nاز این به بعد اگر سیگنال معتبر باشد و اسلات پوزیشن خالی باشد، معامله واقعی روی Toobit باز می‌شود."

        if cmd in ("ترید خاموش", "ترید غیر فعال", "ترید غیرفعال", "خاموش کردن ترید", "غیر فعال کردن ترید", "توبیت خاموش", "معامله خاموش", "trade off"):
            self.storage.update_setting("trade_enabled", False)
            return "⛔ ترید واقعی خاموش شد.\nربات همچنان تحلیل می‌کند و اگر سیگنال صادر شود، آن را فقط عادی/داخلی پیگیری می‌کند."

        if cmd.startswith(("دلار ترید", "مقدار ترید", "مارجین ترید", "trade amount")):
            if number is None:
                return "❌ مقدار دلار ترید را وارد کن.\nمثال: دلار ترید 10"
            value = safe_float(number, -1)
            ok, msg = validate_range(value, config.TRADE_AMOUNT_MIN, config.TRADE_AMOUNT_MAX, "دلار ترید")
            if not ok:
                return msg
            self.storage.update_setting("trade_amount_usdt", value)
            return f"✅ مقدار ترید تنظیم شد.\nمقدار جدید هر معامله: {value:g} USDT"

        if cmd.startswith(("لوریج ترید", "لوریج", "leverage")):
            if number is None:
                return "❌ مقدار لوریج را وارد کن.\nمثال: لوریج ترید 10"
            value = safe_int(number, -1)
            ok, msg = validate_range(value, config.LEVERAGE_MIN, config.LEVERAGE_MAX, "لوریج")
            if not ok:
                return msg
            self.storage.update_setting("leverage", value)
            return f"✅ لوریج ترید تنظیم شد.\nلوریج جدید: {value}x"

        if cmd.startswith(("حداکثر پوزیشن", "حد اکثر پوزیشن", "max positions")):
            if number is None:
                return "❌ تعداد حداکثر پوزیشن را وارد کن.\nمثال: حداکثر پوزیشن 3"
            value = safe_int(number, -1)
            ok, msg = validate_range(value, config.MAX_POSITIONS_MIN, config.MAX_POSITIONS_MAX, "حداکثر پوزیشن")
            if not ok:
                return msg
            self.storage.update_setting("max_positions", value)
            return f"✅ حداکثر پوزیشن تنظیم شد.\nحداکثر پوزیشن رئال همزمان: {value}"

        if cmd in ("سود امروز", "ضرر امروز", "سود ضرر", "سود و ضرر", "pnl today", "today pnl"):
            stats = self.stats_manager.summary()
            balance, _err = self.trade_manager.get_balance_safe()
            today_pnl, today_err = self.trade_manager.get_today_pnl_safe()
            balance = balance or {}
            today_txt = f"{today_pnl:.4f} USDT" if today_pnl is not None else f"نامشخص ({today_err})"
            return f"""💰 سود و ضرر ربات

سود/ضرر عادی ثبت‌شده: {stats.get('normal_pnl', 0):.4f} USDT
سود/ضرر رئال ثبت‌شده: {stats.get('real_pnl', 0):.4f} USDT
جمع سود/ضرر ثبت‌شده: {stats.get('total_pnl', 0):.4f} USDT
سود/ضرر شناور Toobit: {balance.get('unrealized_pnl', 0):.4f} USDT
سود/ضرر امروز Toobit: {today_txt}

TP واقعی: {int(stats.get('real_tp', 0))}
SL واقعی: {int(stats.get('real_sl', 0))}
اجرای ناموفق: {int(stats.get('real_failed', 0))}
"""

        return "دستور نامشخص است. برای دیدن دستورات بنویس: راهنما"
