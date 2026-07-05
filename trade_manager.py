from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from ai_brain import SignalDecision
from config import PANEL_CACHE_SECONDS
from learning_engine import LearningEngine
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
    auto_signals_enabled: bool
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
    today_real_pnl: float | None
    today_stats: dict


class TradeManager:
    def __init__(self, storage: Storage, toobit: ToobitClient) -> None:
        self.storage = storage
        self.toobit = toobit
        self.learning = LearningEngine(storage)
        self._panel_cache_time = 0.0
        self._panel_cache: tuple[float | None, str | None, int | None, int | None, str | None, float | None] | None = None

    async def create_signals_batch(self, items: list[tuple[MarketSymbol, SignalDecision]]) -> list[tuple[MarketSymbol, SignalDecision, CreatedSignal]]:
        accepted = [(symbol, decision) for symbol, decision in items if decision.accepted and decision.direction]
        if not accepted:
            return []
        accepted.sort(key=lambda item: (item[1].real_allowed, item[1].estimated_net_profit_usdt, item[1].confidence), reverse=True)
        created: list[tuple[MarketSymbol, SignalDecision, CreatedSignal]] = []
        real_slots = max(0, self.storage.max_positions() - self.storage.active_real_count()) if self.storage.trade_enabled() else 0
        for symbol, decision in accepted:
            if self.storage.active_symbol_exists(symbol.toobit_symbol):
                continue
            want_real = bool(real_slots > 0 and decision.real_allowed and self.storage.trade_enabled())
            result = await self._create_one(symbol, decision, want_real)
            if result:
                created.append((symbol, decision, result))
                self.storage.register_shadows(result.signal_id, decision.shadow_plans)
                if result.signal_type == "real":
                    real_slots -= 1
        return created

    async def _create_one(self, symbol: MarketSymbol, decision: SignalDecision, want_real: bool) -> CreatedSignal | None:
        if not decision.direction:
            return None
        signal_type, real_status, reason = await self._select_signal_type(symbol, decision, want_real)
        signal_id = self.storage.add_signal(okx_symbol=symbol.okx_inst_id, toobit_symbol=symbol.toobit_symbol, symbol_name=symbol.name, decision=decision, signal_type=signal_type, real_status=real_status)
        if signal_type == "real":
            asyncio.create_task(self._open_real_position(signal_id, symbol, decision))
        return CreatedSignal(signal_id, signal_type, real_status, reason)

    async def _select_signal_type(self, symbol: MarketSymbol, decision: SignalDecision, want_real: bool) -> tuple[str, str, str]:
        if not self.storage.trade_enabled():
            return "normal", "none", "ترید واقعی خاموش است؛ سیگنال Normal ثبت شد."
        if not decision.real_allowed:
            return "normal", "none", "AI این موقعیت را برای Real کافی نمی‌داند."
        if not want_real:
            return "normal", "none", "اسلات واقعی کافی نیست یا شرایط Real کامل نیست."
        if self.storage.active_real_count() >= self.storage.max_positions():
            return "normal", "none", "اسلات‌های واقعی پر است."
        if self.storage.active_real_symbol_exists(symbol.toobit_symbol):
            return "normal", "none", "برای این ارز Real باز وجود دارد."
        try:
            has_position, has_order = await asyncio.gather(asyncio.to_thread(self.toobit.has_open_position, symbol.toobit_symbol), asyncio.to_thread(self.toobit.has_open_order, symbol.toobit_symbol))
        except Exception as exc:
            return "normal", "none", f"خواندن وضعیت Toobit خطا داد: {exc}"
        if has_position:
            return "normal", "none", "برای این ارز در Toobit پوزیشن باز وجود دارد."
        if has_order:
            return "normal", "none", "برای این ارز در Toobit سفارش باز وجود دارد."
        return "real", "reserved", "اسلات Real رزرو شد و سفارش Toobit ارسال می‌شود."

    async def _open_real_position(self, signal_id: int, symbol: MarketSymbol, decision: SignalDecision) -> None:
        self.storage.mark_real_opening(signal_id)
        try:
            result = await asyncio.to_thread(self.toobit.open_position_with_tp_sl, symbol=symbol.toobit_symbol, direction=decision.direction, margin_usdt=self.storage.margin_usdt(), leverage=self.storage.leverage(), tp_price=decision.tp, sl_price=decision.sl, price=decision.entry)
            self.storage.mark_real_open_result(signal_id, opened=result.opened, order_id=result.order_id, reason=result.reason)
        except Exception as exc:
            self.storage.mark_real_open_result(signal_id, opened=False, order_id=None, reason=f"خطا در ارسال سفارش واقعی: {exc}")

    async def panel_data(self) -> PanelData:
        wallet, wallet_error, positions, orders, exchange_error, real_pnl = await self._cached_exchange_data()
        stats = self.storage.today_stats()
        max_positions = self.storage.max_positions()
        filled = self.storage.active_real_count()
        pending = self.storage.pending_real_count()
        return PanelData(self.storage.trade_enabled(), self.storage.auto_signals_enabled(), wallet, wallet_error, positions, orders, exchange_error, self.storage.margin_usdt(), self.storage.leverage(), max_positions, filled, max(0, max_positions - filled), pending, real_pnl, stats)

    async def _cached_exchange_data(self) -> tuple[float | None, str | None, int | None, int | None, str | None, float | None]:
        now = time.monotonic()
        if self._panel_cache and now - self._panel_cache_time <= PANEL_CACHE_SECONDS:
            return self._panel_cache
        wallet = None
        wallet_error = None
        positions_count = None
        orders_count = None
        exchange_error = None
        real_pnl = None
        try:
            wallet = await asyncio.to_thread(self.toobit.get_wallet_margin_usdt)
        except Exception as exc:
            wallet_error = str(exc)
        try:
            positions, orders = await asyncio.gather(asyncio.to_thread(self.toobit.get_open_positions), asyncio.to_thread(self.toobit.get_open_orders))
            positions_count = len(positions)
            orders_count = len(orders)
        except Exception as exc:
            exchange_error = str(exc)
        try:
            real_pnl = await asyncio.to_thread(self.toobit.get_today_real_pnl)
        except Exception:
            real_pnl = None
        self._panel_cache = (wallet, wallet_error, positions_count, orders_count, exchange_error, real_pnl)
        self._panel_cache_time = now
        return self._panel_cache
