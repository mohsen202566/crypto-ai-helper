"""مدیریت اجرای معامله، تشخیص عادی/رئال و کنترل ریسک اجرایی."""
from __future__ import annotations

from typing import Any

from . import config
from .stats_manager import StatsManager
from .storage import JSONStorage
from .toobit_client import ToobitClient
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

    def _settings_snapshot(self) -> dict[str, Any]:
        settings = self.storage.get_settings()
        return {
            "trade_amount_usdt": float(settings.get("trade_amount_usdt", config.DEFAULT_TRADE_AMOUNT_USDT)),
            "leverage": int(settings.get("leverage", config.DEFAULT_LEVERAGE)),
            "max_positions": int(settings.get("max_positions", config.DEFAULT_MAX_POSITIONS)),
            "trade_enabled": bool(settings.get("trade_enabled")),
            "margin_type": str(settings.get("margin_type", config.DEFAULT_MARGIN_TYPE)),
        }

    def choose_signal_mode(self, signal: dict[str, Any]) -> dict[str, Any]:
        """مشخص می‌کند سیگنال رئال Toobit شود یا فقط عادی/داخلی ثبت شود."""
        settings = self._settings_snapshot()
        signal.update(settings)
        signal.setdefault("normal_result", None)
        signal.setdefault("real_result", None)
        signal.setdefault("real_order", None)
        signal.setdefault("real_error", None)
        signal.setdefault("real_pnl", 0.0)
        signal.setdefault("normal_pnl", 0.0)

        if not settings["trade_enabled"]:
            signal["signal_mode"] = "NORMAL"
            signal["execution_label"] = "عادی / داخلی"
            signal["execution_reason"] = "ترید واقعی خاموش است"
            return signal

        open_real = self.storage.count_open_real()
        if open_real >= settings["max_positions"]:
            signal["signal_mode"] = "NORMAL"
            signal["execution_label"] = "عادی / داخلی"
            signal["execution_reason"] = "اسلات پوزیشن رئال پر است"
            return signal

        if not self.toobit.has_credentials:
            signal["signal_mode"] = "NORMAL"
            signal["execution_label"] = "عادی / داخلی"
            signal["execution_reason"] = "کلید API توبیت تنظیم نشده است"
            return signal

        signal["signal_mode"] = "REAL"
        signal["execution_label"] = "رئال Toobit"
        signal["execution_reason"] = "ترید فعال است و اسلات پوزیشن رئال خالی است"
        return signal

    def attach_signal_defaults(self, signal: dict[str, Any]) -> dict[str, Any]:
        signal["created_utc"] = now_utc_iso()
        return self.choose_signal_mode(signal)

    def register_signal(self, signal: dict[str, Any]) -> dict[str, Any]:
        signal = self.attach_signal_defaults(signal)
        self.storage.save_signal(signal)
        self.stats.record_signal(signal.get("signal_mode", "NORMAL"))
        return signal

    def try_execute_real(self, signal: dict[str, Any], symbol_info: dict[str, Any] | None = None) -> tuple[bool, str, Any]:
        if signal.get("signal_mode") != "REAL":
            message = str(signal.get("execution_reason") or "سیگنال برای اجرای رئال انتخاب نشده است")
            self.storage.update_signal(signal["signal_id"], real_error=message)
            return False, message, None

        if not self.toobit.has_credentials:
            message = "کلید API توبیت تنظیم نشده است"
            self.stats.record_real_failed()
            self.storage.update_signal(signal["signal_id"], real_error=message, signal_mode="NORMAL", execution_label="عادی / داخلی")
            return False, message, None

        try:
            try:
                self.toobit.set_margin_type(signal["toobit_symbol"], str(signal.get("margin_type", config.DEFAULT_MARGIN_TYPE)))
            except Exception as exc:
                logger.warning("تنظیم مارجین تایپ ناموفق بود، ادامه می‌دهیم: %s", exc)

            leverage = int(signal.get("leverage", config.DEFAULT_LEVERAGE))
            trade_amount = float(signal.get("trade_amount_usdt", config.DEFAULT_TRADE_AMOUNT_USDT))
            self.toobit.set_leverage(signal["toobit_symbol"], leverage)
            response = self.toobit.place_market_order(
                symbol=signal["toobit_symbol"],
                side=signal["side"],
                entry_price=float(signal["entry"]),
                trade_amount_usdt=trade_amount,
                leverage=leverage,
                tp_price=float(signal["tp"]),
                sl_price=float(signal["sl"]),
                client_order_id=signal["signal_id"].replace("-", "")[:32],
                symbol_info=symbol_info or {},
            )
            self.storage.update_signal(signal["signal_id"], real_order=response, real_error=None, real_open_utc=now_utc_iso())
            self.stats.record_real_open()
            return True, "سفارش واقعی در Toobit ارسال شد", response
        except Exception as exc:
            message = f"اجرای واقعی ناموفق بود: {exc}"
            logger.exception(message)
            self.stats.record_real_failed()
            self.storage.update_signal(signal["signal_id"], real_error=message)
            return False, message, None

    def calculate_pnl(self, signal: dict[str, Any], exit_price: float) -> tuple[float, float]:
        entry = safe_float(signal.get("entry"), 0.0)
        if entry <= 0:
            return 0.0, 0.0
        trade_amount = safe_float(signal.get("trade_amount_usdt"), config.DEFAULT_TRADE_AMOUNT_USDT)
        leverage = int(safe_float(signal.get("leverage"), config.DEFAULT_LEVERAGE))
        notional = trade_amount * max(1, leverage)
        side = str(signal.get("side", "BUY")).upper()
        if side == "BUY":
            pct = (exit_price - entry) / entry * 100.0
        else:
            pct = (entry - exit_price) / entry * 100.0
        pnl = notional * pct / 100.0
        return pnl, pct

    def check_normal_results(self, symbol_prices: dict[str, float]) -> list[tuple[dict[str, Any], str, float, float, float]]:
        results: list[tuple[dict[str, Any], str, float, float, float]] = []
        for signal in self.storage.active_signals():
            price = symbol_prices.get(signal["symbol"])
            if price is None:
                continue
            result = hit_tp_sl(signal["side"], float(price), float(signal["tp"]), float(signal["sl"]))
            if result:
                pnl, pct = self.calculate_pnl(signal, float(price))
                self.storage.update_signal(
                    signal["signal_id"],
                    normal_result=result,
                    normal_exit_price=price,
                    normal_exit_utc=now_utc_iso(),
                    normal_pnl=pnl,
                    normal_pnl_percent=pct,
                )
                self.stats.record_normal_result(result, pnl=pnl)
                updated = self.storage.get_signal(signal["signal_id"]) or signal
                results.append((updated, result, float(price), pnl, pct))
        return results

    def check_real_results(self) -> list[tuple[dict[str, Any], str, float, float, float]]:
        results: list[tuple[dict[str, Any], str, float, float, float]] = []
        for signal in self.storage.active_real_signals():
            try:
                price = self.toobit.get_mark_price(signal["toobit_symbol"])
            except Exception as exc:
                logger.warning("بررسی نتیجه واقعی %s ناموفق بود: %s", signal.get("symbol"), exc)
                continue
            result = hit_tp_sl(signal["side"], price, float(signal["tp"]), float(signal["sl"]))
            if not result:
                continue
            pnl, pct = self.calculate_pnl(signal, float(price))
            self.storage.update_signal(
                signal["signal_id"],
                real_result=result,
                real_exit_price=price,
                real_exit_utc=now_utc_iso(),
                real_pnl=pnl,
                real_pnl_percent=pct,
            )
            self.stats.record_real_result(result, pnl=pnl)
            updated = self.storage.get_signal(signal["signal_id"]) or signal
            results.append((updated, result, price, pnl, pct))
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
            positions = [p for p in positions if self._position_size(p) != 0]
            return positions, None
        except Exception as exc:
            return [], str(exc)

    @staticmethod
    def _position_size(position: dict[str, Any]) -> float:
        for key in ("position", "positionAmt", "size", "qty", "quantity", "available"):
            val = safe_float(position.get(key), 0.0)
            if val != 0:
                return val
        return 0.0

    def get_today_pnl_safe(self) -> tuple[float, str | None]:
        if not self.toobit.has_credentials:
            return 0.0, "کلید API توبیت تنظیم نشده است"
        try:
            return self.toobit.get_today_pnl(), None
        except Exception as exc:
            return 0.0, str(exc)

    def check_toobit_connection(self) -> tuple[bool, dict[str, Any]]:
        if not self.toobit.has_credentials:
            return False, {"connected": False, "message": "کلید API توبیت تنظیم نشده است"}
        try:
            balance = self.toobit.get_usdt_balance_summary()
            return True, {"connected": True, "message": "Toobit وصل است", "balance": balance}
        except Exception as exc:
            return False, {"connected": False, "message": str(exc)}

    def get_toobit_status_safe(self, balance: dict[str, Any] | None = None, balance_error: str | None = None) -> dict[str, Any]:
        if not self.toobit.has_credentials:
            return {"connected": False, "message": "کلید API توبیت تنظیم نشده است", "today_pnl": 0.0}
        if balance_error:
            return {"connected": False, "message": balance_error, "today_pnl": 0.0}
        status = {"connected": True, "message": "Toobit وصل است", "balance": balance or {}}
        pnl, err = self.get_today_pnl_safe()
        status["today_pnl"] = pnl
        status["today_pnl_error"] = err
        return status
