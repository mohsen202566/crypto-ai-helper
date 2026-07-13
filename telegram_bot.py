from __future__ import annotations

import math
import threading

import requests

import config


class TelegramBot:
    def __init__(self, storage, health=None):
        self.storage = storage
        self.health = health
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.offset = int(self.storage.get("telegram_offset", 0) or 0)
        self.session = requests.Session()
        self.session_lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_message(self, text: str, reply_to_message_id=None):
        if not self.enabled:
            return None
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
            payload["allow_sending_without_reply"] = True
        try:
            with self.session_lock:
                response = self.session.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json=payload,
                    timeout=8,
                )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok", False):
                raise RuntimeError(data.get("description") or "Telegram send failed")
            self.storage.resolve_health("telegram")
            if self.health:
                self.health.mark("telegram")
            return int(data.get("result", {}).get("message_id"))
        except Exception as exc:
            self.storage.add_health_event("telegram", "warning", str(exc))
            return None

    def poll_once(self) -> None:
        if not self.enabled:
            return
        try:
            with self.session_lock:
                response = self.session.get(
                    f"https://api.telegram.org/bot{self.token}/getUpdates",
                    params={"offset": self.offset + 1, "timeout": 1},
                    timeout=5,
                )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok", False):
                raise RuntimeError(data.get("description") or "Telegram polling failed")
            for update in data.get("result", []):
                self.offset = max(self.offset, int(update.get("update_id", 0)))
                self.storage.set("telegram_offset", self.offset)
                msg = update.get("message") or {}
                if str((msg.get("chat") or {}).get("id") or "") != str(self.chat_id):
                    continue
                text = str(msg.get("text") or "").strip()
                if text:
                    self.handle_command(text)
            self.storage.resolve_health("telegram")
            if self.health:
                self.health.mark("telegram")
        except Exception as exc:
            self.storage.add_health_event("telegram", "warning", str(exc))

    def handle_command(self, text: str) -> None:
        try:
            if text in ("پنل", "پنل ترید", "ترید", "وضعیت"):
                self.send_message(self.panel_trade())
            elif text in ("آمار", "پنل آمار"):
                self.send_message(self.panel_stats())
            elif text in ("سلامت", "هلس", "Health", "health"):
                self.send_message(self.health.report() if self.health else "سلامت در دسترس نیست")
            elif text == "ترید فعال":
                self.storage.set("trading_enabled", True)
                self.storage.audit(text)
                self.send_message("✅ ترید واقعی فعال شد. همه پوزیشن‌ها Isolated هستند.")
            elif text == "ترید خاموش":
                self.storage.set("trading_enabled", False)
                self.storage.audit(text)
                self.send_message("⛔ ترید واقعی خاموش شد؛ سیگنال و مانیتور مجازی ادامه دارد.")
            elif text in ("اتو سیگنال فعال", "سیگنال فعال"):
                self.storage.set("auto_signal_enabled", True)
                self.storage.audit(text)
                self.send_message("✅ اتو سیگنال فعال شد.")
            elif text in ("اتو سیگنال خاموش", "سیگنال خاموش"):
                self.storage.set("auto_signal_enabled", False)
                self.storage.audit(text)
                self.send_message("⛔ اتو سیگنال خاموش شد.")
            elif text.startswith("ترید دلار"):
                self._set_num(text, "trade_usdt", 1, 10000, "دلار هر ترید", False)
            elif text.startswith("ترید لوریج"):
                self._set_num(text, "leverage", 1, 100, "لوریج", True)
            elif text.startswith("حداکثر پوزیشن"):
                self._set_num(text, "max_positions", 1, 100, "حداکثر پوزیشن", True)
            elif text == "حذف آمار":
                self.storage.reset_stats()
                self.send_message("✅ آمار نمایشی صفر شد؛ تاریخچه و پوزیشن‌ها حذف نشدند.")
            elif text in ("ریست سود", "ریست سود کل"):
                self.storage.reset_profit()
                self.send_message("✅ سود/ضرر امروز و کل صفر شد.")
            elif text == "پوزیشن‌ها":
                self.send_message(self.panel_positions())
            else:
                self.send_message("⚠️ دستور شناخته نشد.")
        except (ValueError, IndexError):
            self.send_message("⚠️ مقدار دستور معتبر نیست. نمونه: ترید دلار 10")
        except Exception as exc:
            self.send_message(f"⚠️ خطا: {exc}")

    def _set_num(self, text, key, minimum, maximum, label, integer):
        value = float(text.split()[-1])
        value = int(value) if integer else value
        if not minimum <= value <= maximum:
            self.send_message(f"⚠️ {label} باید بین {minimum} تا {maximum} باشد.")
            return
        self.storage.set(key, value)
        self.storage.audit(text)
        self.send_message(f"✅ {label} روی {value} تنظیم شد.")

    def panel_trade(self) -> str:
        self.storage.ensure_daily_profit()
        max_positions = int(self.storage.get("max_positions", config.MAX_POSITIONS_DEFAULT))
        used = self.storage.count_real_open()
        opens = self.storage.get_open_signals()
        watch_count = int(self.storage.get("watch_count", 0) or 0)
        return (
            "⚙️ پنل ترید\n\n"
            f"ترید واقعی: {'✅ روشن' if self.storage.get('trading_enabled', False) else '⛔ خاموش'}\n"
            f"اتو سیگنال: {'✅ فعال' if self.storage.get('auto_signal_enabled', True) else '⛔ غیرفعال'}\n"
            "نوع مارجین: Isolated\n"
            f"اتصال توبیت: {'✅ وصل' if self.storage.get('toobit_connected', False) else '⚠️ نامشخص'}\n"
            f"واچ فعال: {watch_count}\n\n"
            f"مارجین توبیت: {float(self.storage.get('toobit_margin_usdt', 0)):.4f} USDT\n"
            f"موجودی آزاد: {float(self.storage.get('toobit_available_usdt', 0)):.4f} USDT\n"
            f"موجودی کل: {float(self.storage.get('toobit_total_usdt', 0)):.4f} USDT\n\n"
            f"دلار هر ترید: {self.storage.get('trade_usdt', 10)} USDT\n"
            f"لوریج: {self.storage.get('leverage', 10)}x\n"
            f"اسلات واقعی: {used}/{max_positions}\n"
            f"اسلات خالی: {max(0, max_positions - used)}\n"
            f"مجازی باز: {sum(not x['is_real'] for x in opens)}\n\n"
            f"سود/ضرر خالص امروز: {float(self.storage.get('profit_today', 0)):.4f} USDT\n"
            f"سود/ضرر خالص کل: {float(self.storage.get('profit_total', 0)):.4f} USDT\n"
            f"کارمزد کل: {float(self.storage.get('fees_total', 0)):.4f} USDT"
        )

    def panel_stats(self) -> str:
        stats = self.storage.stats()
        closed = stats["tp"] + stats["sl"]
        win_rate = stats["tp"] / closed * 100 if closed else 0.0
        pf = stats["profit_factor"]
        pf_text = "∞" if isinstance(pf, float) and math.isinf(pf) else f"{pf:.2f}"
        return (
            "📊 پنل آمار\n\n"
            f"تعداد سیگنال صادرشده: {stats['signals']}\n"
            f"باز: {stats['open']}\n"
            f"✅ TP خورده: {stats['tp']}\n"
            f"❌ SL خورده: {stats['sl']}\n"
            f"⌛ منقضی: {stats.get('expired', 0)}\n\n"
            f"واقعی: {stats['real']}\n"
            f"مجازی: {stats['virtual']}\n"
            f"وین‌ریت: {win_rate:.2f}%\n"
            f"Profit Factor: {pf_text}\n"
            f"سود/ضرر خالص: {stats['net_pnl']:.4f} USDT\n"
            f"کارمزد: {stats['fees']:.4f} USDT"
        )

    def panel_positions(self) -> str:
        positions = self.storage.get_open_signals()
        if not positions:
            return "📌 پوزیشن بازی وجود ندارد."
        return "📌 پوزیشن‌های باز\n\n" + "\n".join(
            f"#{x['id']} | {x['symbol_id']} | {x['side']} | {'واقعی' if x['is_real'] else 'مجازی'} | {x['status']}"
            for x in positions
        )
