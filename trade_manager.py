"""مدیریت اجرای معامله و کنترل ریسک اجرایی."""
from __future__ import annotations

from typing import Any

from . import config
from .stats_manager import StatsManager
from .storage import JSONStorage
from .toobit_client import ToobitClient, ToobitError
from .utils import hit_tp_sl, logger, now_utc_iso, safe_float


class TradeManager:
    def __init__(self, storage: JSONStorage, stats: StatsManager, toobit: ToobitClient):
        self.storage = storage
        self.stats = stats
        self.toobit = toobit

    def can_accept_signal(self, signal: dict[str, Any]) -> tuple[bool, str]:
        if self.storage.has_active_symbol(signal["symbol"]):
            return False, "برای این نماد هنوز سیگنال باز وجود دارد"
        return True, ""

    def attach_signal_defaults(self, signal: dict[str, Any]) -> dict[str, Any]:
        signal["created_utc"] = now_utc_iso()
        signal.setdefault("normal_result", None)
        signal.setdefault("real_result", None)
        signal.setdefault("real_order", None)
        signal.setdefault("real_error", None)
        return signal

    def register_signal(self, signal: dict[str, Any]) -> dict[str, Any]:
        signal = self.attach_signal_defaults(signal)
        self.storage.save_signal(signal)
        self.stats.record_signal()
        return signal

    def try_execute_real(self, signal: dict[str, Any], symbol_info: dict[str, Any] | None = None) -> tuple[bool, str, Any]:
        settings = self.storage.get_settings()
        if not settings.get("trade_enabled"):
            self.storage.update_signal(signal["signal_id"], real_error="ترید واقعی خاموش است")
            return False, "ترید واقعی خاموش است", None

        if self.storage.count_open_real() >= int(settings.get("max_positions", 1)):
            message = "حداکثر تعداد پوزیشن باز پر شده است"
            self.storage.update_signal(signal["signal_id"], real_error=message)
            return False, message, None

        if not self.toobit.has_credentials:
            message = "کلید API توبیت تنظیم نشده است"
            self.stats.record_real_failed()
            self.storage.update_signal(signal["signal_id"], real_error=message)
            return False, message, None

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
                trade_amount_usdt=float(settings.get("trade_amount_usdt", config.DEFAULT_TRADE_AMOUNT_USDT)),
                leverage=int(settings.get("leverage", config.DEFAULT_LEVERAGE)),
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
            self.storage.update_signal(signal["signal_id"], real_error=message)
            return False, message, None

    def check_normal_results(self, symbol_prices: dict[str, float]) -> list[tuple[dict[str, Any], str, float]]:
        results: list[tuple[dict[str, Any], str, float]] = []
        for signal in self.storage.active_signals():
            price = symbol_prices.get(signal["symbol"])
            if price is None:
                continue
            result = hit_tp_sl(signal["side"], float(price), float(signal["tp"]), float(signal["sl"]))
            if result:
                self.storage.update_signal(signal["signal_id"], normal_result=result, normal_exit_price=price, normal_exit_utc=now_utc_iso())
                self.stats.record_normal_result(result)
                updated = self.storage.get_signal(signal["signal_id"]) or signal
                results.append((updated, result, float(price)))
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
            trade_amount = float(self.storage.get_settings().get("trade_amount_usdt", config.DEFAULT_TRADE_AMOUNT_USDT))
            raw_percent = config.FIXED_TP_PERCENT if result == "TP" else -config.FIXED_SL_PERCENT
            pnl = trade_amount * raw_percent / 100.0
            self.storage.update_signal(signal["signal_id"], real_result=result, real_exit_price=price, real_exit_utc=now_utc_iso(), real_pnl=pnl)
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
            positions = [p for p in positions if safe_float(p.get("position")) != 0]
            return positions, None
        except Exception as exc:
            return [], str(exc)
