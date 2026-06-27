from __future__ import annotations

import asyncio
from dataclasses import dataclass

from config import MarketSymbol
from scorer import SignalDecision
from storage import Storage
from toobit_client import ToobitClient


@dataclass(frozen=True)
class CreatedSignal:
    signal_id: int
    signal_type: str
    reason: str


@dataclass(frozen=True)
class PanelData:
    trade_enabled: bool
    wallet_margin_usdt: float | None
    margin_usdt: float
    leverage: int
    max_positions: int
    filled_slots: int
    empty_slots: int
    today_real_pnl: float
    today_approx_pnl: float


class TradeManager:
    def __init__(self, storage: Storage, toobit: ToobitClient) -> None:
        self.storage = storage
        self.toobit = toobit

    async def create_signal(self, symbol: MarketSymbol, decision: SignalDecision) -> CreatedSignal:
        if decision.direction is None:
            raise ValueError("جهت سیگنال مشخص نیست.")
        signal_type, reason = await self._select_signal_type(symbol)
        signal_id = self.storage.add_signal(
            okx_symbol=symbol.okx_inst_id,
            toobit_symbol=symbol.toobit_symbol,
            direction=decision.direction,
            entry=decision.entry,
            tp=decision.tp,
            sl=decision.sl,
            score=decision.score,
            signal_type=signal_type,
        )
        if signal_type == "real":
            asyncio.create_task(self._open_real_position(signal_id, symbol, decision))
        return CreatedSignal(signal_id=signal_id, signal_type=signal_type, reason=reason)

    async def _select_signal_type(self, symbol: MarketSymbol) -> tuple[str, str]:
        if not self.storage.trade_enabled():
            return "normal", "ترید خاموش است."
        max_positions = self.storage.max_positions()
        if self.storage.active_real_count() >= max_positions:
            return "normal", "اسلات‌ها پر هستند."
        if self.storage.active_real_symbol_exists(symbol.toobit_symbol):
            return "normal", "برای این ارز سیگنال واقعی باز وجود دارد."
        has_exchange_position = await asyncio.to_thread(self.toobit.has_open_position, symbol.toobit_symbol)
        if has_exchange_position:
            return "normal", "برای این ارز در توبیت پوزیشن باز وجود دارد."
        return "real", "پوزیشن واقعی ارسال می‌شود."

    async def _open_real_position(self, signal_id: int, symbol: MarketSymbol, decision: SignalDecision) -> None:
        try:
            result = await asyncio.to_thread(
                self.toobit.open_position_with_tp_sl,
                symbol=symbol.toobit_symbol,
                direction=decision.direction,
                margin_usdt=self.storage.margin_usdt(),
                leverage=self.storage.leverage(),
                tp_price=decision.tp,
                sl_price=decision.sl,
                price=decision.entry,
            )
            self.storage.mark_real_open_result(signal_id, opened=result.opened, order_id=result.order_id)
        except Exception:
            self.storage.mark_real_open_result(signal_id, opened=False, order_id=None)

    async def panel_data(self) -> PanelData:
        try:
            wallet_margin = await asyncio.to_thread(self.toobit.get_wallet_margin_usdt)
        except Exception:
            wallet_margin = None
        today = self.storage.today_stats()
        try:
            today_real_pnl = await asyncio.to_thread(self.toobit.get_today_real_pnl)
        except Exception:
            today_real_pnl = float(today["real_pnl"])
        max_positions = self.storage.max_positions()
        filled = self.storage.active_real_count()
        return PanelData(
            trade_enabled=self.storage.trade_enabled(),
            wallet_margin_usdt=wallet_margin,
            margin_usdt=self.storage.margin_usdt(),
            leverage=self.storage.leverage(),
            max_positions=max_positions,
            filled_slots=filled,
            empty_slots=max(0, max_positions - filled),
            today_real_pnl=float(today_real_pnl),
            today_approx_pnl=float(today["approx_pnl"]),
        )
