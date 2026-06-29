"""مدیریت اجرای معامله و کنترل ریسک اجرایی."""
from __future__ import annotations

from typing import Any

import config
from stats_manager import StatsManager
from storage import JSONStorage
from toobit_client import ToobitClient
from utils import hit_tp_sl, logger, now_utc_iso, safe_float


class TradeManager:
    def __init__(self, storage: JSONStorage, stats: StatsManager, toobit: ToobitClient):
        self.storage = storage
        self.stats = stats
        self.toobit = toobit

    def can_accept_signal(self, signal: dict[str, Any]) -> tuple[bool, str]:
        if self.storage.has_active_symbol(signal["symbol"]):
            return False, "برای این نماد هنوز سیگنال باز وجود دارد"
        return True, ""

    def check_toobit_connection(self) -> tuple[bool, str, dict[str, Any] | None]:
        if not self.toobit.has_credentials:
            return False, "کلید API توبیت تنظیم نشده است یا فایل .env درست خوانده نشده است", None
        try:
            balance = self.toobit.get_usdt_balance_summary()
            return True, "اتصال Toobit برقرار است", balance
        except Exception as exc:
            return False, str(exc), None

    def get_today_pnl_safe(self) -> tuple[float | None, str | None]:
        if not self.toobit.has_credentials:
            return None, "کلید API توبیت تنظیم نشده است"
        try:
            return self.toobit.get_today_pnl(), None
        except Exception as exc:
            return None, str(exc)

    def decide_execution_mode(self, signal: dict[str, Any]) -> dict[str, Any]:
        settings = self.storage.get_settings()
        signal["trade_amount_usdt"] = float(settings.get("trade_amount_usdt", config.DEFAULT_TRADE_AMOUNT_USDT))
        signal["leverage"] = int(settings.get("leverage", config.DEFAULT_LEVERAGE))
        signal["max_positions"] = int(settings.get("max_positions", config.DEFAULT_MAX_POSITIONS))
        signal["margin_type"] = str(settings.get("margin_type", config.DEFAULT_MARGIN_TYPE))

        if not settings.get("trade_enabled"):
            signal["execution_mode"] = "NORMAL"
            signal["execution_mode_fa"] = "عادی / داخلی"
            signal["execution_reason"] = "ترید واقعی خاموش است؛ سیگنال فقط به‌صورت عادی پیگیری می‌شود."
            return signal

        if self.storage.count_open_real() >= int(settings.get("max_positions", 1)):
            signal["execution_mode"] = "NORMAL"
            signal["execution_mode_fa"] = "عادی / داخلی"
            signal["execution_reason"] = "اسلات پوزیشن رئال پر است؛ سیگنال فقط به‌صورت عادی پیگیری می‌شود."
            return signal

        ok, reason, _balance = self.check_toobit_connection()
        if not ok:
            signal["execution_mode"] = "NORMAL"
            signal["execution_mode_fa"] = "عادی / داخلی"
            signal["execution_reason"] = f"اتصال Toobit برقرار نیست؛ سیگنال فقط عادی شد. خطا: {reason}"
            return signal

        signal["execution_mode"] = "REAL"
        signal["execution_mode_fa"] = "رئال Toobit"
        signal["execution_reason"] = "ترید فعال است و اسلات پوزیشن رئال خالی است؛ سیگنال برای اجرای واقعی انتخاب شد."
        return signal

    def attach_signal_defaults(self, signal: dict[str, Any]) -> dict[str, Any]:
        signal["created_utc"] = now_utc_iso()
        signal.setdefault("normal_result", None)
        signal.setdefault("real_result", None)
        signal.setdefault("real_order", None)
        signal.setdefault("real_error", None)
        signal.setdefault("telegram_message_id", None)
        return signal

    def register_signal(self, signal: dict[str, Any]) -> dict[str, Any]:
        signal = self.attach_signal_defaults(signal)
        self.storage.save_signal(signal)
        self.stats.record_signal(signal.get("execution_mode", "NORMAL"))
        return signal

    def _downgrade_real_to_normal(self, signal: dict[str, Any], reason: str) -> None:
        self.storage.update_signal(
            signal["signal_id"],
            execution_mode="NORMAL",
            execution_mode_fa="عادی / داخلی",
            execution_reason=f"اجرای رئال انجام نشد؛ از اینجا به بعد نتیجه به‌صورت عادی پیگیری می‌شود. علت: {reason}",
            real_error=reason,
        )
        self.stats.convert_real_signal_to_normal()

    def try_execute_real(self, signal: dict[str, Any], symbol_info: dict[str, Any] | None = None) -> tuple[bool, str, Any]:
        if str(signal.get("execution_mode") or "").upper() != "REAL":
            return False, signal.get("execution_reason", "سیگنال عادی است"), None

        settings = self.storage.get_settings()
        if not settings.get("trade_enabled"):
            message = "ترید واقعی خاموش است"
            self._downgrade_real_to_normal(signal, message)
            return False, message, None

        if self.storage.count_open_real() >= int(settings.get("max_positions", 1)):
            message = "حداکثر تعداد پوزیشن باز پر شده است"
            self._downgrade_real_to_normal(signal, message)
            return False, message, None

        ok, reason, _balance = self.check_toobit_connection()
        if not ok:
            self.stats.record_real_failed()
            self._downgrade_real_to_normal(signal, reason)
            return False, reason, None

        try:
            try:
                self.toobit.set_margin_type(signal["toobit_symbol"], str(settings.get("margin_type", config.DEFAULT_MARGIN_TYPE)))
            except Exception as exc:
                logger.warning("تنظیم مارجین تایپ ناموفق بود، ادامه می‌دهیم: %s", exc)
            self.toobit.set_leverage(signal["toobit_symbol"], int(settings.get("leverage", config.DEFAULT_LEVERAGE)))
            response = self.toobit.place_market_order(
                symbol=signal["toobit_symbol"],
                side=signal["side"],
                entry_price=float(signal["entry"]),
                trade_amount_usdt=float(signal.get("trade_amount_usdt", settings.get("trade_amount_usdt", config.DEFAULT_TRADE_AMOUNT_USDT))),
                leverage=int(signal.get("leverage", settings.get("leverage", config.DEFAULT_LEVERAGE))),
                tp_price=float(signal["tp"]),
                sl_price=float(signal["sl"]),
                client_order_id=signal["signal_id"].replace("-", "")[:32],
                symbol_info=symbol_info or {},
            )
            self.storage.update_signal(signal["signal_id"], real_order=response, real_error=None)
            self.stats.record_real_open()
            return True, "سفارش واقعی در Toobit ارسال شد", response
        except Exception as exc:
            message = f"اجرای واقعی ناموفق بود: {exc}"
            logger.exception(message)
            self.stats.record_real_failed()
            self._downgrade_real_to_normal(signal, message)
            return False, message, None

    @staticmethod
    def _movement_percent(signal: dict[str, Any], exit_price: float) -> float:
        entry = safe_float(signal.get("entry"), 0.0)
        if entry <= 0:
            return 0.0
        if str(signal.get("side", "")).upper() == "BUY":
            return (float(exit_price) - entry) / entry * 100.0
        return (entry - float(exit_price)) / entry * 100.0

    def _signal_pnl(self, signal: dict[str, Any], exit_price: float) -> float:
        trade_amount = float(signal.get("trade_amount_usdt") or self.storage.get_settings().get("trade_amount_usdt", config.DEFAULT_TRADE_AMOUNT_USDT))
        leverage = int(signal.get("leverage") or self.storage.get_settings().get("leverage", config.DEFAULT_LEVERAGE))
        movement = self._movement_percent(signal, exit_price)
        notional = trade_amount * leverage
        return notional * movement / 100.0

    def check_normal_results(self, symbol_prices: dict[str, float]) -> list[tuple[dict[str, Any], str, float, float]]:
        results: list[tuple[dict[str, Any], str, float, float]] = []
        for signal in self.storage.active_signals():
            price = symbol_prices.get(signal["symbol"])
            if price is None:
                continue
            result = hit_tp_sl(signal["side"], float(price), float(signal["tp"]), float(signal["sl"]))
            if result:
                pnl = self._signal_pnl(signal, float(price))
                self.storage.update_signal(
                    signal["signal_id"],
                    normal_result=result,
                    normal_exit_price=price,
                    normal_exit_utc=now_utc_iso(),
                    normal_pnl=pnl,
                )
                self.stats.record_normal_result(result, pnl=pnl)
                updated = self.storage.get_signal(signal["signal_id"]) or signal
                results.append((updated, result, float(price), pnl))
        return results

    def check_real_results(self) -> list[tuple[dict[str, Any], str, float, float]]:
        results: list[tuple[dict[str, Any], str, float, float]] = []
        for signal in self.storage.active_real_signals():
            try:
                price = self.toobit.get_mark_price(signal["toobit_symbol"])
            except Exception as exc:
                logger.warning("بررسی نتیجه واقعی %s ناموفق بود: %s", signal.get("symbol"), exc)
                continue
            result = hit_tp_sl(signal["side"], price, float(signal["tp"]), float(signal["sl"]))
            if not result:
                continue
            pnl = self._signal_pnl(signal, float(price))
            self.storage.update_signal(
                signal["signal_id"],
                real_result=result,
                real_exit_price=price,
                real_exit_utc=now_utc_iso(),
                real_pnl=pnl,
            )
            self.stats.record_real_result(result, pnl=pnl)
            updated = self.storage.get_signal(signal["signal_id"]) or signal
            results.append((updated, result, price, pnl))
        return results

    def get_balance_safe(self) -> tuple[dict[str, float] | None, str | None]:
        if not self.toobit.has_credentials:
            return None, "کلید API توبیت تنظیم نشده است"
        try:
            return self.toobit.get_usdt_balance_summary(), None
        except Exception as exc:
            return None, str(exc)

    def get_positions_safe(self) -> tuple[list[dict[str, Any]], str | None]:
        if not self.toobit.has_credentials:
            return [], "کلید API توبیت تنظیم نشده است"
        try:
            positions = self.toobit.get_positions()
            positions = [p for p in positions if safe_float(p.get("position") or p.get("positionAmt") or p.get("size")) != 0]
            return positions, None
        except Exception as exc:
            return [], str(exc)
