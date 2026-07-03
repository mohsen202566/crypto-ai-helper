from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import config
from ai_brain import SignalDecision
from learning_engine import LearningEngine
from storage import Storage
from symbols import MarketSymbol
from toobit_client import ToobitClient
from utils import logger


@dataclass(frozen=True)
class CreatedSignal:
    signal_id: int
    signal_type: str
    reason: str


@dataclass(frozen=True)
class PanelData:
    auto_signals_enabled: bool
    trade_enabled: bool
    trade_usdt: float
    max_positions: int
    filled_slots: int
    empty_slots: int
    today_pnl: float
    total_pnl: float
    today_stats: dict
    all_stats: dict
    wallet_usdt: float | None
    open_orders: int | None
    exchange_error: str | None
    ai_confidence: float


class TradeManager:
    def __init__(self, storage: Storage, toobit: ToobitClient) -> None:
        self.storage = storage
        self.toobit = toobit
        self.learning = LearningEngine(storage)
        self._panel_cache_time = 0.0
        self._panel_cache: tuple[float | None, int | None, str | None] | None = None

    async def create_signal(self, symbol: MarketSymbol, decision: SignalDecision) -> tuple[SignalDecision, CreatedSignal] | None:
        if not decision.accepted:
            self.storage.record_no_signal(symbol.name, decision.reason, decision.features_key)
            return None
        if self.storage.active_symbol_exists(symbol.toobit_symbol):
            self.storage.record_no_signal(symbol.name, "برای این ارز سیگنال فعال وجود دارد.", decision.features_key)
            return None
        signal_type = "normal"
        reason = "ترید خاموش یا اسلات پر است؛ سیگنال عادی برای یادگیری ثبت شد."
        if self.storage.trade_enabled() and self.storage.active_real_count() < self.storage.max_positions() and decision.real_allowed:
            signal_type = "real"
            reason = "ترید واقعی اسپات مجاز شد؛ خرید Market و فروش Limit هدف انجام می‌شود."
        signal_id = self.storage.add_signal(decision, signal_type=signal_type)
        created = CreatedSignal(signal_id=signal_id, signal_type=signal_type, reason=reason)
        if signal_type == "real":
            asyncio.create_task(self._open_real_spot(signal_id, symbol, decision))
        return decision, created

    async def _open_real_spot(self, signal_id: int, symbol: MarketSymbol, decision: SignalDecision) -> None:
        trade_usdt = self.storage.trade_usdt()
        buy_client_id = f"spot_buy_{signal_id}_{int(time.time())}"
        sell_client_id = f"spot_sell_{signal_id}_{int(time.time())}"
        try:
            buy = await asyncio.to_thread(self.toobit.place_spot_market_buy, symbol.toobit_symbol, trade_usdt, buy_client_id)
            buy_order_id = buy.get("order_id")
            self.storage.mark_buy_order(signal_id, buy_order_id, "market buy submitted")
            fill = await asyncio.to_thread(self.toobit.wait_spot_order_fill, symbol.toobit_symbol, buy_order_id, config.BUY_FILL_VERIFY_SECONDS, 5)
            if not fill:
                self.storage.fail_signal(signal_id, "خرید در زمان تعیین‌شده پر نشد.")
                return
            parsed = self.toobit.parse_order_fill(fill, fallback_fee_pct=config.SPOT_TAKER_FEE_RATE * 100)
            qty = float(parsed.get("qty") or buy.get("estimated_quantity") or 0.0)
            entry = float(parsed.get("avg_price") or buy.get("estimated_price") or decision.entry)
            fee = float(parsed.get("fee_usdt") or 0.0)
            if qty <= 0 or entry <= 0:
                self.storage.fail_signal(signal_id, "جزئیات خرید پرشده قابل خواندن نیست.")
                return
            self.storage.mark_buy_filled(signal_id, quantity=qty, entry_price=entry, fee_usdt=fee)
            sell = await asyncio.to_thread(self.toobit.place_spot_limit_sell, symbol.toobit_symbol, qty, decision.target, sell_client_id)
            self.storage.mark_sell_order(signal_id, sell.get("order_id"), float(sell.get("price") or decision.target))
        except Exception as exc:
            logger.exception("real spot open failed %s", symbol.name)
            self.storage.fail_signal(signal_id, f"خطا در اجرای اسپات Toobit: {exc}")

    async def panel_data(self) -> PanelData:
        wallet, orders, error = await self._cached_exchange_data()
        today = self.storage.today_stats()
        all_stats = self.storage.all_stats()
        ai = self.storage.ai_summary()
        max_pos = self.storage.max_positions()
        filled = self.storage.active_real_count()
        return PanelData(auto_signals_enabled=self.storage.auto_signals_enabled(), trade_enabled=self.storage.trade_enabled(), trade_usdt=self.storage.trade_usdt(), max_positions=max_pos, filled_slots=filled, empty_slots=max(0, max_pos - filled), today_pnl=float(today.get("pnl", 0.0)), total_pnl=float(all_stats.get("pnl", 0.0)), today_stats=today, all_stats=all_stats, wallet_usdt=wallet, open_orders=orders, exchange_error=error, ai_confidence=float(ai.get("confidence") or 0.0))

    async def _cached_exchange_data(self) -> tuple[float | None, int | None, str | None]:
        now = time.monotonic()
        if self._panel_cache and now - self._panel_cache_time <= config.PANEL_CACHE_SECONDS:
            return self._panel_cache
        wallet = None
        orders = None
        error = None
        try:
            bal = await asyncio.to_thread(self.toobit.get_spot_usdt_balance)
            wallet = float(bal.get("free", 0.0))
            open_orders = await asyncio.to_thread(self.toobit.get_spot_open_orders)
            orders = len(open_orders)
        except Exception as exc:
            error = str(exc)
        self._panel_cache = (wallet, orders, error)
        self._panel_cache_time = now
        return self._panel_cache
