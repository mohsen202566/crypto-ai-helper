from __future__ import annotations

import time
from dataclasses import dataclass

from storage import Storage, StoredSignal
from utils import logger, safe_float


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

    def try_real_closed_on_toobit(self, toobit_client, signal: StoredSignal) -> MonitorResult | None:
        if signal.signal_type != "real" or signal.real_status not in {"opened", "reserved", "opening"}:
            return None
        try:
            pos = toobit_client.get_open_position(signal.toobit_symbol, signal.direction)
            if pos is not None:
                return None
        except Exception as exc:
            logger.warning("مانیتور توبیت برای %s ناموفق بود: %s", signal.symbol, exc)
            return None

        real_pnl = None
        exit_price = signal.entry_price
        try:
            found = toobit_client.find_realized_result(
                signal.toobit_symbol,
                "BUY" if signal.direction == "LONG" else "SELL",
                int(signal.opened_at * 1000),
                int(time.time() * 1000),
            )
            if found:
                real_pnl = safe_float(found.get("pnl"), None)  # type: ignore[arg-type]
                if found.get("close_price"):
                    exit_price = safe_float(found.get("close_price"), signal.entry_price)
        except Exception as exc:
            logger.warning("خواندن PnL واقعی توبیت برای %s ناموفق بود: %s", signal.symbol, exc)

        # If Toobit closed but OKX TP/SL did not catch it yet, record as EXIT not TP/SL.
        return self.build_result(signal, "EXIT", exit_price, real_pnl=real_pnl, reason="Toobit position closed/missing during monitoring.")
