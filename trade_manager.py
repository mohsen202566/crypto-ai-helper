from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from config import TOOBIT_PANEL_CACHE_SECONDS
from learning_engine import LearningEngine
from scorer import SignalDecision
from storage import Storage
from symbol_health import SymbolHealth
from symbols import MarketSymbol
from toobit_client import ToobitClient


@dataclass(frozen=True)
class CreatedSignal:
    signal_id: int
    signal_type: str
    real_status: str
    reason: str
    signal_label: str


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
    symbol_health: dict[str, int]


class TradeManager:
    def __init__(self, storage: Storage, toobit: ToobitClient, symbol_health: SymbolHealth) -> None:
        self.storage = storage
        self.toobit = toobit
        self.symbol_health = symbol_health
        self.learning = LearningEngine()
        self._panel_cache_time = 0.0
        self._panel_cache: tuple[float | None, str | None, int | None, int | None, str | None, float | None] | None = None

    async def create_signals_batch(self, items: list[tuple[MarketSymbol, SignalDecision]]) -> list[tuple[MarketSymbol, SignalDecision, CreatedSignal]]:
        accepted = [(s, d) for s, d in items if d.accepted and d.direction is not None]
        if not accepted:
            return []
        accepted.sort(key=lambda item: item[1].real_priority, reverse=True)
        real_slots = max(0, self.storage.max_positions() - self.storage.active_real_count()) if self.storage.trade_enabled() else 0
        created: list[tuple[MarketSymbol, SignalDecision, CreatedSignal]] = []
        for symbol, decision in accepted:
            if self.storage.active_symbol_exists(symbol.toobit_symbol):
                continue
            want_real = real_slots > 0 and decision.real_allowed and decision.session_state != "BAD_REAL_ONLY_NORMAL" and self.symbol_health.toobit_real_enabled(symbol.name)
            result = await self._create_one(symbol, decision, want_real=want_real)
            if result:
                created.append((symbol, decision, result))
                self.learning.register_shadow(self.storage, result.signal_id, direction=decision.direction, entry=decision.entry, tp=decision.tp, sl=decision.sl)
                if result.signal_type == "real":
                    real_slots -= 1
        return created

    async def _create_one(self, symbol: MarketSymbol, decision: SignalDecision, *, want_real: bool) -> CreatedSignal | None:
        if not decision.accepted or decision.direction is None:
            return None
        signal_type, real_status, reason = await self._select_signal_type(symbol, decision, want_real=want_real)
        label = self._signal_label(decision, signal_type)
        signal_id = self.storage.add_signal(okx_symbol=symbol.okx_inst_id, toobit_symbol=symbol.toobit_symbol, symbol_name=symbol.name, decision=decision, signal_type=signal_type, real_status=real_status, signal_label=label)
        if signal_type == "real":
            asyncio.create_task(self._open_real_position(signal_id, symbol, decision))
        return CreatedSignal(signal_id, signal_type, real_status, reason, label)

    async def _select_signal_type(self, symbol: MarketSymbol, decision: SignalDecision, *, want_real: bool) -> tuple[str, str, str]:
        if not self.storage.trade_enabled():
            return "normal", "none", "ترید واقعی خاموش است؛ سیگنال عادی ثبت شد."
        if not decision.real_allowed:
            return "normal", "none", decision.real_block_reason or "AI این سیگنال را فقط عادی مجاز دانست."
        if not want_real:
            return "normal", "none", "اسلات واقعی یا شرایط Real کامل نبود؛ سیگنال عادی ثبت شد."
        if self.storage.active_real_count() >= self.storage.max_positions():
            return "normal", "none", "اسلات‌های واقعی پر هستند؛ سیگنال عادی ثبت شد."
        if self.storage.active_real_symbol_exists(symbol.toobit_symbol):
            return "normal", "none", "برای این ارز سیگنال/پوزیشن واقعی باز وجود دارد؛ سیگنال عادی شد."
        try:
            has_position, has_order = await asyncio.gather(asyncio.to_thread(self.toobit.has_open_position, symbol.toobit_symbol), asyncio.to_thread(self.toobit.has_open_order, symbol.toobit_symbol))
            self.symbol_health.record_toobit_success(symbol.name)
        except Exception as exc:
            self.symbol_health.record_toobit_error(symbol.name, str(exc))
            return "normal", "none", f"خواندن وضعیت Toobit خطا داد؛ سیگنال عادی شد: {exc}"
        if has_position:
            return "normal", "none", "برای این ارز در Toobit پوزیشن باز وجود دارد؛ سفارش واقعی بلاک شد."
        if has_order:
            return "normal", "none", "برای این ارز در Toobit سفارش باز وجود دارد؛ سفارش واقعی بلاک شد."
        return "real", "reserved", "اسلات واقعی رزرو شد و سفارش Toobit در حال ارسال است."

    async def _open_real_position(self, signal_id: int, symbol: MarketSymbol, decision: SignalDecision) -> None:
        self.storage.mark_real_opening(signal_id)
        try:
            result = await asyncio.to_thread(self.toobit.open_position_with_tp_sl, symbol=symbol.toobit_symbol, direction=decision.direction, margin_usdt=self.storage.margin_usdt(), leverage=self.storage.leverage(), tp_price=decision.tp, sl_price=decision.sl, price=decision.entry)
            self.storage.mark_real_open_result(signal_id, opened=result.opened, order_id=result.order_id, reason=result.reason, actual_margin_usdt=result.actual_margin_usdt, quantity=result.quantity)
            if result.opened:
                self.symbol_health.record_toobit_success(symbol.name)
            else:
                self.symbol_health.record_toobit_error(symbol.name, result.reason)
        except Exception as exc:
            self.symbol_health.record_toobit_error(symbol.name, str(exc))
            self.storage.mark_real_open_result(signal_id, opened=False, order_id=None, reason=f"خطا در ارسال سفارش واقعی: {exc}")

    async def panel_data(self) -> PanelData:
        wallet, wallet_error, exchange_positions, exchange_orders, exchange_error, today_real_pnl = await self._cached_exchange_data()
        today = self.storage.today_stats()
        if today_real_pnl is None:
            today_real_pnl = float(today.get("real_pnl", 0.0))
        max_positions = self.storage.max_positions()
        filled = self.storage.active_real_count()
        pending = self.storage.pending_real_count()
        return PanelData(trade_enabled=self.storage.trade_enabled(), wallet_margin_usdt=wallet, wallet_error=wallet_error, exchange_open_positions=exchange_positions, exchange_open_orders=exchange_orders, exchange_error=exchange_error, margin_usdt=self.storage.margin_usdt(), leverage=self.storage.leverage(), max_positions=max_positions, filled_slots=filled, empty_slots=max(0, max_positions - filled), pending_slots=pending, today_real_pnl=float(today_real_pnl), today_approx_pnl=float(today.get("approx_pnl", 0.0)), today_stats=today, symbol_health=self.symbol_health.panel_summary())

    async def _cached_exchange_data(self) -> tuple[float | None, str | None, int | None, int | None, str | None, float | None]:
        now = time.monotonic()
        if self._panel_cache and now - self._panel_cache_time <= TOOBIT_PANEL_CACHE_SECONDS:
            return self._panel_cache
        wallet: float | None = None
        wallet_error: str | None = None
        positions_count: int | None = None
        orders_count: int | None = None
        exchange_error: str | None = None
        today_real_pnl: float | None = None
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
            today_real_pnl = await asyncio.to_thread(self.toobit.get_today_real_pnl)
        except Exception:
            today_real_pnl = None
        self._panel_cache = (wallet, wallet_error, positions_count, orders_count, exchange_error, today_real_pnl)
        self._panel_cache_time = now
        return self._panel_cache

    @staticmethod
    def _signal_label(decision: SignalDecision, signal_type: str) -> str:
        prefix = "واقعی Toobit" if signal_type == "real" else "عادی AI"
        return f"{prefix} / {decision.signal_label}"
