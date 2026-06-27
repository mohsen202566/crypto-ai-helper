from __future__ import annotations

import asyncio
from dataclasses import dataclass

from scorer import SignalDecision
from storage import Storage
from symbols import MarketSymbol
from toobit_client import ToobitClient


@dataclass(frozen=True)
class CreatedSignal:
    signal_id: int
    signal_type: str
    real_status: str
    reason: str


@dataclass(frozen=True)
class PanelData:
    trade_enabled: bool
    wallet_margin_usdt: float | None
    wallet_error: str | None
    exchange_open_positions: int | None
    exchange_open_orders: int | None
    exchange_error: str | None
    margin_usdt: float
    leverage: int
    max_positions: int
    filled_slots: int
    empty_slots: int
    pending_slots: int
    today_real_pnl: float
    today_approx_pnl: float
    today_stats: dict


class TradeManager:
    def __init__(self, storage: Storage, toobit: ToobitClient) -> None:
        self.storage = storage
        self.toobit = toobit

    async def create_signal(self, symbol: MarketSymbol, decision: SignalDecision) -> CreatedSignal | None:
        if not decision.accepted or decision.direction is None:
            return None
        if self.storage.active_symbol_exists(symbol.toobit_symbol):
            return None

        signal_type, real_status, reason = await self._select_signal_type(symbol)
        signal_id = self.storage.add_signal(
            okx_symbol=symbol.okx_inst_id,
            toobit_symbol=symbol.toobit_symbol,
            symbol_name=symbol.name,
            decision=decision,
            signal_type=signal_type,
            real_status=real_status,
        )
        if signal_type == "real":
            asyncio.create_task(self._open_real_position(signal_id, symbol, decision))
        return CreatedSignal(signal_id=signal_id, signal_type=signal_type, real_status=real_status, reason=reason)

    async def _select_signal_type(self, symbol: MarketSymbol) -> tuple[str, str, str]:
        if not self.storage.trade_enabled():
            return "normal", "none", "ترید واقعی خاموش است؛ سیگنال عادی ثبت شد."
        max_positions = self.storage.max_positions()
        if self.storage.active_real_count() >= max_positions:
            return "normal", "none", "اسلات‌های واقعی پر هستند؛ سیگنال عادی ثبت شد."
        if self.storage.active_real_symbol_exists(symbol.toobit_symbol):
            return "normal", "none", "برای این ارز سیگنال/پوزیشن واقعی باز وجود دارد؛ سیگنال عادی شد."
        try:
            has_position, has_order = await asyncio.gather(
                asyncio.to_thread(self.toobit.has_open_position, symbol.toobit_symbol),
                asyncio.to_thread(self.toobit.has_open_order, symbol.toobit_symbol),
            )
        except Exception as exc:
            return "normal", "none", f"خواندن وضعیت Toobit خطا داد؛ سیگنال عادی شد: {exc}"
        if has_position:
            return "normal", "none", "برای این ارز در Toobit پوزیشن باز وجود دارد؛ سفارش واقعی بلاک شد."
        if has_order:
            return "normal", "none", "برای این ارز در Toobit سفارش باز وجود دارد؛ سفارش واقعی بلاک شد."
        return "real", "reserved", "اسلات واقعی رزرو شد و سفارش Toobit در حال ارسال است."

    async def _open_real_position(self, signal_id: int, symbol: MarketSymbol, decision: SignalDecision) -> None:
        self.storage.mark_real_opening(signal_id)
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
            self.storage.mark_real_open_result(
                signal_id,
                opened=result.opened,
                order_id=result.order_id,
                reason=result.reason,
                actual_margin_usdt=result.actual_margin_usdt,
                quantity=result.quantity,
            )
        except Exception as exc:
            self.storage.mark_real_open_result(
                signal_id,
                opened=False,
                order_id=None,
                reason=f"خطا در ارسال سفارش واقعی: {exc}",
            )

    async def panel_data(self) -> PanelData:
        wallet: float | None = None
        wallet_error: str | None = None
        exchange_positions: int | None = None
        exchange_orders: int | None = None
        exchange_error: str | None = None
        try:
            wallet = await asyncio.to_thread(self.toobit.get_wallet_margin_usdt)
        except Exception as exc:
            wallet_error = str(exc)
        try:
            positions, orders = await asyncio.gather(
                asyncio.to_thread(self.toobit.get_open_positions),
                asyncio.to_thread(self.toobit.get_open_orders),
            )
            exchange_positions = len(positions)
            exchange_orders = len(orders)
        except Exception as exc:
            exchange_error = str(exc)
        today = self.storage.today_stats()
        try:
            today_real_pnl = await asyncio.to_thread(self.toobit.get_today_real_pnl)
        except Exception:
            today_real_pnl = float(today.get("real_pnl", 0.0))
        max_positions = self.storage.max_positions()
        filled = self.storage.active_real_count()
        pending = self.storage.pending_real_count()
        return PanelData(
            trade_enabled=self.storage.trade_enabled(),
            wallet_margin_usdt=wallet,
            wallet_error=wallet_error,
            exchange_open_positions=exchange_positions,
            exchange_open_orders=exchange_orders,
            exchange_error=exchange_error,
            margin_usdt=self.storage.margin_usdt(),
            leverage=self.storage.leverage(),
            max_positions=max_positions,
            filled_slots=filled,
            empty_slots=max(0, max_positions - filled),
            pending_slots=pending,
            today_real_pnl=float(today_real_pnl),
            today_approx_pnl=float(today.get("approx_pnl", 0.0)),
            today_stats=today,
        )
