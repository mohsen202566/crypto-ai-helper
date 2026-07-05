from __future__ import annotations

from dataclasses import dataclass

from signal_manager import Signal
from slot_manager import SlotManager
from toobit_client import OpenOrderResult, get_client


@dataclass(frozen=True)
class ExecutionResult:
    opened: bool
    reason: str
    raw: OpenOrderResult | None = None


class TradeExecutor:
    def __init__(self, slot_manager: SlotManager) -> None:
        self.slot_manager = slot_manager

    def open_real_position(self, signal: Signal, margin_usdt: float, leverage: int) -> ExecutionResult:
        self.slot_manager.reserve(signal.toobit_symbol, seconds=70)
        try:
            client = get_client()
            result = client.open_position_with_tp_sl(
                symbol=signal.toobit_symbol,
                direction=signal.direction,
                margin_usdt=margin_usdt,
                leverage=leverage,
                tp_price=signal.tp,
                sl_price=signal.sl,
                price=signal.entry,
            )
            if result.opened:
                self.slot_manager.release(signal.toobit_symbol)
                return ExecutionResult(True, result.reason, result)
            self.slot_manager.release(signal.toobit_symbol)
            return ExecutionResult(False, result.reason, result)
        except Exception as exc:
            self.slot_manager.release(signal.toobit_symbol)
            return ExecutionResult(False, f"پوزیشن رئال در توبیت باز نشد: {exc}")
