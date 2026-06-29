from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from config import AI_EXIT_RECENT_TICKS, AI_EXIT_TP_IS_TARGET_ZONE
from exit_engine import ExitEngine
from okx_data import OkxDataClient
from post_trade_analyzer import PostTradeAnalyzer
from storage import Storage, StoredSignal
from toobit_client import ToobitClient
from tp_sl_result_engine import TpSlResultEngine


class SignalMonitor:
    def __init__(self, storage: Storage, okx: OkxDataClient, toobit: ToobitClient) -> None:
        self.storage = storage
        self.okx = okx
        self.toobit = toobit
        self.result_engine = TpSlResultEngine()
        self.exit_engine = ExitEngine()
        self.post_trade = PostTradeAnalyzer()

    async def check_once(self, send_result) -> None:
        for signal in self.storage.open_signals():
            # Telegram must never receive a TP/SL result before the original signal message exists.
            # The scanner creates the DB row first, then sends Telegram and stores message_id.
            # Monitoring can hit TP/SL before message_id is written; skip until the original Telegram signal exists.
            if signal.message_id is None:
                continue
            if signal.signal_type == "real" and signal.real_status not in {"opened", "reserved", "opening"}:
                continue
            try:
                price = await asyncio.to_thread(self.okx.get_last_price, signal.okx_symbol)
            except Exception:
                continue
            mfe_pct, mae_pct = self.storage.update_signal_excursions(signal.id, price)
            status = self._status_from_price(signal, price)
            exit_price = price
            ai_exit_reason = None
            ai_exit_status = None
            ai_exit_score = 0
            target_zone_reached = False
            giveback_pct = 0.0
            if status is None:
                snapshots = self.storage.recent_second_snapshots(signal.id, AI_EXIT_RECENT_TICKS)
                recent_prices = tuple(float(row["price"]) for row in snapshots if row.get("price") is not None)
                exit_decision = self.exit_engine.analyze(signal, price, mfe_pct=mfe_pct, mae_pct=mae_pct, recent_prices=recent_prices)
                if exit_decision.should_exit:
                    status = "EXIT"
                    exit_price = float(exit_decision.exit_price or price)
                    ai_exit_reason = exit_decision.reason
                    ai_exit_status = exit_decision.status or "AI_EXIT"
                    ai_exit_score = int(exit_decision.exit_score or 0)
                    target_zone_reached = bool(exit_decision.target_zone_reached)
                    giveback_pct = float(exit_decision.giveback_pct or 0.0)
                    profit_pct = (exit_price - signal.entry) / signal.entry if signal.direction == "LONG" else (signal.entry - exit_price) / signal.entry
                    self.storage.record_ai_exit_event(signal.id, status=ai_exit_status, price=exit_price, reason=ai_exit_reason, profit_pct=profit_pct, mfe_pct=mfe_pct, mae_pct=mae_pct, giveback_pct=giveback_pct, exit_score=ai_exit_score)
                    if signal.signal_type == "real" and signal.real_status == "opened":
                        close_result = await asyncio.to_thread(self.toobit.close_position_market, symbol=signal.toobit_symbol, direction=signal.direction)
                        self.storage.mark_real_close(signal.id, order_id=close_result.order_id, reason=close_result.reason)
                        if not close_result.closed:
                            continue
                else:
                    continue
            else:
                exit_price = signal.tp if status == "TP" else signal.sl
            approx_pnl = self._approx_pnl(signal, exit_price)
            real_pnl = await self._real_pnl(signal) if signal.signal_type == "real" else None
            classified = self.result_engine.classify(status=status, signal_type=signal.signal_type, real_status=signal.real_status, real_pnl_available=real_pnl is not None)
            result_message_id = await send_result(signal, status, approx_pnl, real_pnl, classified.result_source)
            closed = self.storage.finish_signal(signal.id, status=status, approx_pnl=approx_pnl, real_pnl=real_pnl, result_message_id=result_message_id, mfe_pct=mfe_pct, mae_pct=mae_pct, result_source=classified.result_source, exit_price=exit_price, ai_exit_reason=ai_exit_reason, ai_exit_status=ai_exit_status, ai_exit_score=ai_exit_score, target_zone_reached=target_zone_reached, giveback_pct=giveback_pct)
            if closed:
                self.post_trade.record_closed_signal(self.storage, signal.id)

    def _status_from_price(self, signal: StoredSignal, price: float) -> str | None:
        if signal.direction == "LONG":
            if not AI_EXIT_TP_IS_TARGET_ZONE and price >= signal.tp:
                return "TP"
            if price <= signal.sl:
                return "SL"
        if signal.direction == "SHORT":
            if not AI_EXIT_TP_IS_TARGET_ZONE and price <= signal.tp:
                return "TP"
            if price >= signal.sl:
                return "SL"
        return None

    def _approx_pnl(self, signal: StoredSignal, exit_price: float) -> float:
        if signal.entry <= 0:
            return 0.0
        pct = (exit_price - signal.entry) / signal.entry if signal.direction == "LONG" else (signal.entry - exit_price) / signal.entry
        return signal.margin_usdt * signal.leverage * pct

    async def _real_pnl(self, signal: StoredSignal) -> float | None:
        created = datetime.fromisoformat(signal.created_at)
        start_ms = int((created - timedelta(minutes=10)).timestamp() * 1000)
        end_ms = int((datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp() * 1000)
        try:
            return await asyncio.to_thread(self.toobit.find_realized_pnl, symbol=signal.toobit_symbol, side=signal.direction, start_ms=start_ms, end_ms=end_ms)
        except Exception:
            return None
