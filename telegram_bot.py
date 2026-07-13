"""دستورات و پنل‌های تلگرام."""
from __future__ import annotations

from typing import Any
import time

import requests

import config
from health import HealthManager
from storage import Storage


class TelegramBot:
    def __init__(self, storage: Storage, health: HealthManager | None = None):
        self.storage = storage
        self.health = health
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = str(config.TELEGRAM_CHAT_ID)
        self.offset = int(self.storage.get("telegram_offset", 0) or 0)
        self.session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _api(self, method: str, payload: dict[str, Any]) -> Any:
        if not self.enabled:
            return None
        response = self.session.post(
            f"https://api.telegram.org/bot{self.token}/{method}",
            json=payload,
            timeout=8,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or not result.get("ok", False):
            raise RuntimeError(f"Telegram API error: {result}")
        return result

    def send_message(self, text: str, reply_to_message_id: int | None = None) -> int | None:
        if not self.enabled:
            return None
        data: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_to_message_id:
            data["reply_to_message_id"] = int(reply_to_message_id)
        try:
            result = self._api("sendMessage", data)
            self.storage.clear_health_component("telegram")
            return int(result.get("result", {}).get("message_id")) if isinstance(result, dict) else None
        except Exception as exc:
            self.storage.add_health_event("telegram", "warning", f"send failed: {exc}")
            return None

    def poll_once(self) -> None:
        if not self.enabled:
            return
        try:
            response = self.session.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={"offset": self.offset + 1, "timeout": 1},
                timeout=5,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not payload.get("ok", False):
                raise RuntimeError(f"Telegram poll error: {payload}")
            for update in payload.get("result", []):
                update_id = int(update.get("update_id", 0))
                self.offset = max(self.offset, update_id)
                self.storage.set("telegram_offset", self.offset)
                message = update.get("message") or {}
                source_chat_id = str((message.get("chat") or {}).get("id") or "")
                if source_chat_id != self.chat_id:
                    continue
                text = str(message.get("text") or "").strip()
                if text:
                    self.handle_command(text)
            self.storage.clear_health_component("telegram")
            if self.health:
                self.health.mark("telegram")
        except Exception as exc:
            self.storage.add_health_event("telegram", "warning", f"poll failed: {exc}")

    def handle_command(self, text: str) -> None:
        command = text.strip()
        low = command.lower()
        try:
            if command in ("پنل", "پنل ترید", "وضعیت", "ترید"):
                self.send_message(self.panel_trade())
            elif command in ("آمار", "پنل آمار"):
                self.send_message(self.panel_stats())
            elif command in ("سلامت", "هلس") or low == "health":
                self.send_message(self.health.report() if self.health else "سلامت در دسترس نیست")
            elif command == "ترید فعال":
                self.storage.set("trading_enabled", True)
                self.send_message("✅ ترید واقعی فعال شد.")
            elif command == "ترید خاموش":
                self.storage.set("trading_enabled", False)
                self.send_message("⛔ ترید واقعی خاموش شد؛ سیگنال عادی ادامه دارد.")
            elif command in ("اتو سیگنال فعال", "اتوسیگنال فعال"):
                self.storage.set("auto_signal_enabled", True)
                self.send_message("✅ اتو سیگنال واقعی فعال شد.")
            elif command in ("اتو سیگنال خاموش", "اتوسیگنال خاموش"):
                self.storage.set("auto_signal_enabled", False)
                self.send_message("⛔ اتو سیگنال واقعی خاموش شد؛ سیگنال‌ها عادی ثبت و مانیتور می‌شوند.")
            elif command.startswith("ترید دلار"):
                self._set_float(command, "trade_usdt", config.TRADE_USDT_MIN, config.TRADE_USDT_MAX, "دلار هر ترید")
            elif command.startswith("ترید لوریج"):
                self._set_int(command, "leverage", config.LEVERAGE_MIN, config.LEVERAGE_MAX, "لوریج")
            elif command.startswith("حداکثر پوزیشن"):
                self._set_int(command, "max_positions", config.MAX_POSITIONS_MIN, config.MAX_POSITIONS_MAX, "حداکثر پوزیشن")
            elif command == "حذف آمار":
                self.storage.reset_stats()
                self.send_message("✅ آمار از این لحظه صفر شد؛ پوزیشن باز حذف نشد.")
            elif command in ("ریست سود", "ریست سود کل"):
                self.storage.reset_profit()
                self.send_message("✅ سود/ضرر امروز و کل صفر شد.")
            else:
                self.send_message("⚠️ دستور شناخته نشد.")
        except Exception as exc:
            self.storage.add_health_event("telegram_command", "warning", f"command failed: {exc}")
            self.send_message(f"⚠️ خطا در اجرای دستور: {exc}")

    def _set_float(self, text: str, key: str, minimum: float, maximum: float, label: str) -> None:
        value = float(text.split()[-1])
        if not minimum <= value <= maximum:
            self.send_message(f"⚠️ {label} باید بین {minimum:g} تا {maximum:g} باشد.")
            return
        self.storage.set(key, value)
        self.send_message(f"✅ {label} روی {value:g} تنظیم شد.")

    def _set_int(self, text: str, key: str, minimum: int, maximum: int, label: str) -> None:
        value = int(float(text.split()[-1]))
        if not minimum <= value <= maximum:
            self.send_message(f"⚠️ {label} باید بین {minimum} تا {maximum} باشد.")
            return
        self.storage.set(key, value)
        self.send_message(f"✅ {label} روی {value} تنظیم شد.")

    @staticmethod
    def _age(timestamp: int) -> str:
        if timestamp <= 0:
            return "هنوز آپدیت نشده"
        seconds = max(0, int(time.time()) - timestamp)
        return f"{seconds} ثانیه قبل" if seconds < 60 else f"{seconds // 60} دقیقه قبل"

    def panel_trade(self) -> str:
        self.storage.roll_profit_day()
        trading = "✅ روشن" if self.storage.get("trading_enabled", False) else "⛔ خاموش"
        auto = "✅ فعال" if self.storage.get("auto_signal_enabled", True) else "⛔ غیرفعال"
        max_positions = int(self.storage.get("max_positions", config.MAX_POSITIONS_DEFAULT))
        open_real = self.storage.count_real_open()
        connected = bool(self.storage.get("toobit_connected", False))
        updated = self._age(int(self.storage.get("toobit_last_update", 0) or 0))
        open_signals = self.storage.get_open_signals()
        open_virtual = sum(1 for item in open_signals if not int(item.get("is_real") or 0))
        last_error = str(self.storage.get("toobit_last_error", "") or "")
        connection_line = f"{'✅ وصل' if connected else '❌ قطع/خطا'} | {updated}"
        if not connected and last_error:
            connection_line += f"\nآخرین خطا: {last_error[:120]}"
        return (
            "⚙️ پنل ترید\n\n"
            f"ترید واقعی: {trading}\n"
            f"اتو سیگنال واقعی: {auto}\n"
            f"اتصال توبیت: {connection_line}\n"
            f"مارجین: {float(self.storage.get('toobit_margin_usdt', 0)):.4f} USDT\n"
            f"موجودی آزاد: {float(self.storage.get('toobit_available_usdt', 0)):.4f} USDT\n"
            f"موجودی کل: {float(self.storage.get('toobit_total_usdt', 0)):.4f} USDT\n"
            f"دلار هر ترید: {float(self.storage.get('trade_usdt', config.TRADE_USDT_DEFAULT)):g} USDT\n"
            f"لوریج: {int(self.storage.get('leverage', config.LEVERAGE_DEFAULT))}x\n"
            f"اسلات واقعی: {open_real}/{max_positions}\n"
            f"اسلات خالی: {max(0, max_positions - open_real)}\n"
            f"سیگنال عادی باز: {open_virtual}\n"
            f"سود/ضرر خالص امروز: {float(self.storage.get('profit_today', 0)):.4f} USDT\n"
            f"سود/ضرر خالص کل: {float(self.storage.get('profit_total', 0)):.4f} USDT"
        )

    def panel_stats(self) -> str:
        stats = self.storage.stats()
        closed = stats["tp"] + stats["sl"]
        win_rate = stats["tp"] / closed * 100.0 if closed else 0.0
        return (
            "📊 پنل آمار\n\n"
            f"تعداد سیگنال: {stats['signals']}\n"
            f"باز: {stats['open']}\n"
            f"در انتظار تأیید واقعی: {stats['pending']}\n"
            f"TP: {stats['tp']}\n"
            f"SL: {stats['sl']}\n"
            f"واقعی: {stats['real']}\n"
            f"عادی: {stats['virtual']}\n"
            f"وین‌ریت: {win_rate:.2f}%\n"
            f"سود/ضرر آمار: {stats['net_pnl']:.4f} USDT\n"
            f"سود واقعی: {stats['real_net']:.4f} USDT\n"
            f"سود عادی: {stats['virtual_net']:.4f} USDT\n"
            f"سود امروز: {float(self.storage.get('profit_today', 0)):.4f} USDT\n"
            f"سود کل: {float(self.storage.get('profit_total', 0)):.4f} USDT"
        )
