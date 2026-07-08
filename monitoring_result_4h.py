from __future__ import annotations

from dataclasses import dataclass

from storage import Storage, StoredSignal


@dataclass(frozen=True)
class MonitorResult:
    status: str
    exit_price: float
    approx_pnl: float
    net_pnl: float
    real_pnl: float | None
    move_pct: float
    reason: str


class MonitoringResult4H:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def check_price_hit(self, signal: StoredSignal, price: float) -> str | None:
        if signal.direction == "LONG":
            if price >= signal.tp_price:
                return "TP"
            if price <= signal.sl_price:
                return "SL"
        else:
            if price <= signal.tp_price:
                return "TP"
            if price >= signal.sl_price:
                return "SL"
        return None

    def build_result(self, signal: StoredSignal, status: str, exit_price: float, *, real_pnl: float | None = None, reason: str = "") -> MonitorResult:
        if signal.entry_price <= 0:
            move_pct = 0.0
        elif signal.direction == "LONG":
            move_pct = (exit_price - signal.entry_price) / signal.entry_price
        else:
            move_pct = (signal.entry_price - exit_price) / signal.entry_price
        notional = signal.trade_margin_usdt * max(1, signal.leverage)
        approx = notional * move_pct
        # Fee hurts both TP and SL in net terms.
        net = approx - signal.round_trip_fee_usdt
        return MonitorResult(status, exit_price, approx, real_pnl if real_pnl is not None else net, real_pnl, move_pct, reason)
