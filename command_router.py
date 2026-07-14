"""Router سریع دستورات فارسی؛ فقط runtime.db و Snapshot کش‌شده را می‌خواند."""
from __future__ import annotations

from typing import Any

import config
from storage import Storage
from telegram_ui import coins_panel, health_panel, help_text, positions_panel, stats_panel, trade_panel
from utils import normalize_command, parse_float_fa


class CommandRouter:
    def __init__(self, storage: Storage):
        self.storage = storage

    def handle(self, text: str) -> str:
        t = normalize_command(text)
        if not t or t in {"/start", "start", "راهنما", "کمک", "help"}:
            return help_text()
        if t in {"ترید", "پنل", "وضعیت", "پنل ترید"}:
            return trade_panel(
                self.storage.runtime.settings(),
                self.storage.runtime.account_snapshot(),
                self.storage.runtime.slot_counts(),
                self.storage.runtime.displayed_real_pnl(),
                self.storage.learning.counts_by_stage(),
            )
        if t in {"آمار", "پنل آمار"}:
            return stats_panel(self.storage.runtime.stats(), self.storage.learning.counts_by_stage())
        if t in {"پوزیشن", "پوزیشن‌ها", "پوزیشن ها"}:
            return positions_panel(self.storage.runtime.active_signals(), self.storage.runtime.slot_counts())
        if t in {"کوین‌ها", "کوین ها", "ارزها"}:
            return coins_panel(self.storage.learning.symbols())
        if t in {"سلامت", "health", "هلس"}:
            return health_panel(self.storage.runtime.health(), self.storage.runtime.settings(), self.storage.runtime.account_snapshot())
        if t in {"ترید فعال", "توبیت روشن"}:
            self.storage.runtime.set_setting("real_trade_enabled", True)
            snapshot = self.storage.runtime.account_snapshot()
            warning = "" if snapshot.get("connected") else "\n⚠️ Toobit فعلاً متصل نیست؛ تا اتصال سالم، فرصت‌های آماده رئال به‌صورت Medium ثبت می‌شوند."
            return "✅ ترید واقعی فعال شد. فقط فرصت‌های REAL_READY و دارای سود خالص مجاز وارد Toobit می‌شوند." + warning
        if t in {"ترید خاموش", "توبیت خاموش"}:
            self.storage.runtime.set_setting("real_trade_enabled", False)
            return "⛔ ترید واقعی خاموش شد. سفارش جدید باز نمی‌شود؛ پوزیشن‌های باز همچنان مانیتور می‌شوند و Initial/Medium ادامه دارند."
        if t.startswith("ترید دلار "):
            return self._set_float(t, "ترید دلار ", "trade_margin_usdt", "دلار هر پوزیشن", config.TRADE_MARGIN_MIN, config.TRADE_MARGIN_MAX, "USDT")
        if t.startswith("دلار ترید "):
            return self._set_float(t, "دلار ترید ", "trade_margin_usdt", "دلار هر پوزیشن", config.TRADE_MARGIN_MIN, config.TRADE_MARGIN_MAX, "USDT")
        if t.startswith("ترید لوریج "):
            return self._set_int(t, "ترید لوریج ", "leverage", "لوریج", config.LEVERAGE_MIN, config.LEVERAGE_MAX, "x")
        if t.startswith("لوریج ترید "):
            return self._set_int(t, "لوریج ترید ", "leverage", "لوریج", config.LEVERAGE_MIN, config.LEVERAGE_MAX, "x")
        if t.startswith("حداکثر پوزیشن "):
            return self._set_int(t, "حداکثر پوزیشن ", "max_open_positions", "حداکثر پوزیشن", config.MAX_POSITIONS_MIN, config.MAX_POSITIONS_MAX, "")
        if t == "ریست سود":
            self.storage.runtime.reset_pnl(total=False)
            return "✅ مبنای سود/ضرر امروز صفر شد. نتایج خام و یادگیری حذف نشدند."
        if t == "ریست سود کل":
            self.storage.runtime.reset_pnl(total=True)
            return "✅ مبنای سود/ضرر کل صفر شد. نتایج خام و یادگیری حذف نشدند."
        if t == "لاگ رد فعال":
            self.storage.runtime.set_setting("reject_log_enabled", True)
            return "✅ چاپ زنده دلایل رد سیگنال در لاگ VPS فعال شد."
        if t == "لاگ رد خاموش":
            self.storage.runtime.set_setting("reject_log_enabled", False)
            return "✅ چاپ دلایل رد خاموش شد؛ خطاهای حیاتی همچنان ثبت می‌شوند."
        return "دستور نامعتبر است.\n\n" + help_text()

    def _set_float(self, text: str, prefix: str, key: str, label: str, lo: float, hi: float, suffix: str) -> str:
        try:
            value = parse_float_fa(text[len(prefix):])
        except ValueError:
            return f"❌ مقدار {label} نامعتبر است. بازه: {lo:g} تا {hi:g} {suffix}"
        if value < lo or value > hi:
            return f"❌ مقدار خارج از بازه است. بازه مجاز: {lo:g} تا {hi:g} {suffix}"
        self.storage.runtime.set_setting(key, round(value, 8))
        return f"✅ {label}: {value:g} {suffix}\nتمام سیگنال‌ها و سناریوهای جدید از این مقدار استفاده می‌کنند."

    def _set_int(self, text: str, prefix: str, key: str, label: str, lo: int, hi: int, suffix: str) -> str:
        try:
            value_float = parse_float_fa(text[len(prefix):])
        except ValueError:
            return f"❌ مقدار {label} نامعتبر است. بازه: {lo} تا {hi} {suffix}"
        if not value_float.is_integer():
            return f"❌ مقدار {label} باید عدد صحیح باشد."
        value = int(value_float)
        if value < lo or value > hi:
            return f"❌ مقدار خارج از بازه است. بازه مجاز: {lo} تا {hi} {suffix}"
        self.storage.runtime.set_setting(key, value)
        return f"✅ {label}: {value}{suffix}\nتمام سیگنال‌ها و سناریوهای جدید از این مقدار استفاده می‌کنند."
