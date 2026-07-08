from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import config
from storage import Storage, StoredSignal
from utils import safe_float


@dataclass(frozen=True)
class SignalResult:
    result: str
    price: float
    pnl_usdt: float
    reason: str
    age_seconds: int


class SignalMonitor:
    def __init__(self, storage: Storage, okx_client, toobit_client) -> None:
        self.storage = storage
        self.okx = okx_client
        self.toobit = toobit_client

    def check_once(self, callback: Callable[[StoredSignal, SignalResult], int | None]) -> None:
        settings = self.storage.settings()
        notional = max(0.0, float(settings["trade_dollar_usdt"])) * max(1, int(settings["leverage"]))
        for sig in self.storage.active_signals():
            try:
                price = self.okx.get_last_price(sig.symbol)
            except Exception:
                continue
            if price <= 0:
                continue
            result = self._evaluate(sig, price, notional)
            if result is None:
                continue
            self.storage.close_signal(sig.id, result.result, result.price, result.pnl_usdt, result.reason)
            callback(sig, result)

    def _evaluate(self, sig: StoredSignal, price: float, notional: float) -> SignalResult | None:
        now = int(time.time())
        age = max(0, now - int(sig.opened_at or sig.created_at))
        if sig.direction == "LONG":
            if price >= sig.tp_price:
                pnl = notional * abs(sig.tp_price - sig.entry_price) / sig.entry_price - float(config.ROUND_TRIP_FEE_USDT)
                return SignalResult("TP", sig.tp_price, pnl, "OKX price touched one TP", age)
            if price <= sig.sl_price:
                pnl = -notional * abs(sig.entry_price - sig.sl_price) / sig.entry_price - float(config.ROUND_TRIP_FEE_USDT)
                return SignalResult("SL", sig.sl_price, pnl, "OKX price touched SL", age)
            progress_r = (price - sig.entry_price) / sig.risk_per_unit if sig.risk_per_unit > 0 else 0.0
        else:
            if price <= sig.tp_price:
                pnl = notional * abs(sig.entry_price - sig.tp_price) / sig.entry_price - float(config.ROUND_TRIP_FEE_USDT)
                return SignalResult("TP", sig.tp_price, pnl, "OKX price touched one TP", age)
            if price >= sig.sl_price:
                pnl = -notional * abs(sig.sl_price - sig.entry_price) / sig.entry_price - float(config.ROUND_TRIP_FEE_USDT)
                return SignalResult("SL", sig.sl_price, pnl, "OKX price touched SL", age)
            progress_r = (sig.entry_price - price) / sig.risk_per_unit if sig.risk_per_unit > 0 else 0.0

        if bool(config.SOFT_EXIT_ENABLED) and age >= int(config.SOFT_EXIT_MINUTES) * 60 and progress_r < float(config.SOFT_EXIT_MIN_R):
            # Soft exit is a monitoring result. Real Toobit close is disabled by default so the unchanged TP/SL flow stays safe.
            pnl = notional * ((price - sig.entry_price) / sig.entry_price if sig.direction == "LONG" else (sig.entry_price - price) / sig.entry_price)
            pnl -= float(config.ROUND_TRIP_FEE_USDT)
            if sig.signal_type == "real" and bool(config.ENABLE_REAL_SOFT_EXIT_CLOSE):
                try:
                    self.toobit.flash_close(sig.toobit_symbol, sig.direction)
                except Exception:
                    pass
            return SignalResult("SOFT_EXIT", price, pnl, f"ICE failed to move {config.SOFT_EXIT_MIN_R}R in {config.SOFT_EXIT_MINUTES}m", age)
        return None
