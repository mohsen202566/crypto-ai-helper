"""تلگرام و پنل‌ها.
دستورات فقط از cache/database می‌خوانند یا تنظیم سبک انجام می‌دهند؛ مسیر تحلیل را کند نمی‌کنند.
"""
from __future__ import annotations

import time
from typing import Any

import requests

import config
from health import HealthManager
from storage import Storage

class TelegramBot:
    def __init__(self, storage: Storage, health: HealthManager | None = None):
        self.storage = storage
        self.health = health
        self.token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.offset = 0
        self.session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _api(self, method: str, payload: dict[str, Any]) -> Any:
        if not self.enabled:
            return None
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        r = self.session.post(url, json=payload, timeout=8)
        return r.json()

    def send_message(self, text: str, reply_to_message_id: int | None = None) -> int | None:
        if not self.enabled:
            return None
        data: dict[str, Any] = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if reply_to_message_id:
            data["reply_to_message_id"] = int(reply_to_message_id)
        try:
            res = self._api("sendMessage", data)
            return int(res.get("result", {}).get("message_id")) if isinstance(res, dict) else None
        except Exception as exc:
            self.storage.add_health_event("telegram", "warning", f"send failed: {exc}")
            return None

    def poll_once(self) -> None:
        if not self.enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/getUpdates"
            res = self.session.get(url, params={"offset": self.offset + 1, "timeout": 1}, timeout=5).json()
            for upd in res.get("result", []):
                self.offset = max(self.offset, int(upd.get("update_id", 0)))
                msg = upd.get("message") or {}
                text = str(msg.get("text") or "").strip()
                if text:
                    self.handle_command(text)
        except Exception as exc:
            self.storage.add_health_event("telegram", "warning", f"poll failed: {exc}")

    def handle_command(self, text: str) -> None:
        t = text.strip()
        low = t.lower()
        try:
            if t in ("پنل", "پنل ترید", "وضعیت", "ترید"):
                self.send_message(self.panel_trade())
            elif t in ("آمار", "پنل آمار"):
                self.send_message(self.panel_stats())
            elif t in ("سلامت", "هلس") or low == "health":
                self.send_message(self.panel_health())
            elif t == "ترید فعال":
                self.storage.set("trading_enabled", True)
                self.send_message("✅ ترید واقعی فعال شد.")
            elif t == "ترید خاموش":
                self.storage.set("trading_enabled", False)
                self.send_message("⛔ ترید واقعی خاموش شد. سیگنال‌ها عادی مانیتور می‌شوند.")
            elif t in ("اتو سیگنال فعال", "اتوسیگنال فعال"):
                self.storage.set("auto_signal_enabled", True)
                self.send_message("✅ اتو سیگنال فعال شد.")
            elif t in ("اتو سیگنال خاموش", "اتوسیگنال خاموش"):
                self.storage.set("auto_signal_enabled", False)
                self.send_message("⛔ اتو سیگنال خاموش شد. فقط سیگنال‌های عادی/دستی مانیتور می‌شوند.")
            elif t.startswith("ترید دلار"):
                self._set_float(t, "trade_usdt", config.TRADE_USDT_MIN, config.TRADE_USDT_MAX, "دلار هر ترید")
            elif t.startswith("ترید لوریج"):
                self._set_int(t, "leverage", config.LEVERAGE_MIN, config.LEVERAGE_MAX, "لوریج")
            elif t.startswith("حداکثر پوزیشن"):
                self._set_int(t, "max_positions", config.MAX_POSITIONS_MIN, config.MAX_POSITIONS_MAX, "حداکثر پوزیشن")
            elif t == "حذف آمار":
                self.storage.reset_stats()
                self.send_message("✅ پنل آمار صفر شد. پوزیشن‌های باز حذف نشدند.")
            elif t in ("ریست سود", "ریست سود کل"):
                self.storage.reset_profit()
                self.send_message("✅ سود/ضرر امروز و کل در پنل ترید و پنل آمار صفر شد.")
        except Exception as exc:
            self.storage.add_health_event("telegram_command", "warning", f"command failed: {exc}")
            self.send_message(f"⚠️ خطا در اجرای دستور: {exc}")

    def _set_float(self, text: str, key: str, mn: float, mx: float, label: str) -> None:
        parts = text.split()
        val = float(parts[-1])
        if not (mn <= val <= mx):
            self.send_message(f"⚠️ {label} باید بین {mn:g} تا {mx:g} باشد.")
            return
        self.storage.set(key, val)
        self.send_message(f"✅ {label} روی {val:g} تنظیم شد.")

    def _set_int(self, text: str, key: str, mn: int, mx: int, label: str) -> None:
        parts = text.split()
        val = int(float(parts[-1]))
        if not (mn <= val <= mx):
            self.send_message(f"⚠️ {label} باید بین {mn} تا {mx} باشد.")
            return
        self.storage.set(key, val)
        self.send_message(f"✅ {label} روی {val} تنظیم شد.")

    def _age_text(self, ts: int | float | None) -> str:
        try:
            ts_i = int(ts or 0)
        except Exception:
            ts_i = 0
        if ts_i <= 0:
            return "هنوز آپدیت نشده"
        sec = max(0, int(time.time()) - ts_i)
        if sec < 60:
            return f"{sec} ثانیه قبل"
        return f"{sec // 60} دقیقه قبل"

    def panel_trade(self) -> str:
        trading = "✅ روشن" if self.storage.get("trading_enabled", False) else "⛔ خاموش"
        auto = "✅ فعال" if self.storage.get("auto_signal_enabled", True) else "⛔ غیرفعال"
        max_pos = int(self.storage.get("max_positions", config.MAX_POSITIONS_DEFAULT))
        open_real = self.storage.count_real_open()
        free = max(0, max_pos - open_real)

        toobit_connected = bool(self.storage.get("toobit_connected", False))
        toobit_margin = float(self.storage.get("toobit_margin_usdt", 0.0) or 0.0)
        toobit_available = float(self.storage.get("toobit_available_usdt", 0.0) or 0.0)
        toobit_total = float(self.storage.get("toobit_total_usdt", 0.0) or 0.0)
        toobit_error = str(self.storage.get("toobit_last_error", "") or "")
        toobit_updated = self._age_text(self.storage.get("toobit_last_update", 0))
        if toobit_connected:
            toobit_line = f"✅ وصل | آپدیت: {toobit_updated}"
        else:
            short_error = (toobit_error[:90] + "...") if len(toobit_error) > 90 else toobit_error
            toobit_line = f"❌ قطع/خطا | {short_error or 'دیتای موفق ثبت نشده'}"

        return (
            "⚙️ پنل ترید\n\n"
            f"ترید واقعی: {trading}\n"
            f"اتو سیگنال: {auto}\n"
            f"اتصال توبیت: {toobit_line}\n"
            f"مارجین توبیت: {toobit_margin:.4f} USDT\n"
            f"موجودی آزاد توبیت: {toobit_available:.4f} USDT\n"
            f"موجودی کل توبیت: {toobit_total:.4f} USDT\n"
            f"دلار هر ترید: {self.storage.get('trade_usdt', config.TRADE_USDT_DEFAULT)} USDT\n"
            f"لوریج: {self.storage.get('leverage', config.LEVERAGE_DEFAULT)}x\n"
            f"اسلات واقعی: {open_real}/{max_pos}\n"
            f"اسلات خالی: {free}\n"
            f"سود/ضرر خالص امروز: {float(self.storage.get('profit_today',0.0)):.4f} USDT\n"
            f"سود/ضرر خالص کل: {float(self.storage.get('profit_total',0.0)):.4f} USDT"
        )

    def panel_stats(self) -> str:
        s = self.storage.stats()
        closed = s["tp"] + s["sl"]
        wr = (s["tp"] / closed * 100.0) if closed else 0.0
        return (
            "📊 پنل آمار\n\n"
            f"تعداد سیگنال صادر شده: {s['signals']}\n"
            f"باز: {s['open']}\n"
            f"TP خورده: {s['tp']}\n"
            f"SL خورده: {s['sl']}\n"
            f"واقعی: {s['real']}\n"
            f"عادی: {s['virtual']}\n"
            f"وین‌ریت: {wr:.2f}%\n"
            f"سود/ضرر کل آمار: {s['net_pnl']:.4f} USDT"
        )

    def panel_health(self) -> str:
        if self.health:
            return self.health.report()
        events = self.storage.active_health_events()
        if not events:
            return "🩺 سلامت ربات\n\n✅ مشکل فعالی ثبت نشده."
        lines = ["🩺 سلامت ربات", "", "🚨 مشکلات فعال:"]
        for e in events[:10]:
            lines.append(f"{e['severity']} | {e['component']} | {e['symbol_id'] or '-'} | {e['message']}")
        return "\n".join(lines)
