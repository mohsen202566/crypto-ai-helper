from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from learning_engine import LearningEngine
from okx_data import OkxDataClient
from storage import Storage, StoredSignal
from toobit_client import ToobitClient
from utils import direction_profit_pct


class SignalMonitor:
    def __init__(self, storage: Storage, okx: OkxDataClient, toobit: ToobitClient) -> None:
        self.storage = storage
        self.okx = okx
        self.toobit = toobit
        self.learning = LearningEngine(storage)

    async def check_once(self, send_result) -> None:
        for signal in self.storage.open_signals():
            if signal.message_id is None:
                continue
            if signal.signal_type == "real" and signal.real_status in {"reserved", "opening"}:
                continue
            try:
                if signal.signal_type == "real" and signal.real_status == "opened":
                    closed = await self._check_real_closed(signal, send_result)
                    if closed:
                        continue
                price = await asyncio.to_thread(self.okx.get_last_price, signal.okx_symbol)
            except Exception:
                continue
            mfe_pct, mae_pct = self.storage.update_excursions(signal, price)
            status = self._status_from_price(signal, price)
            if status is None:
                continue
            exit_price = signal.tp if status == "TP" else signal.sl
            approx_pnl = self._approx_pnl(signal, exit_price)
            message_id = await send_result(signal, status, exit_price, approx_pnl, None, "normal" if signal.signal_type == "normal" else "normal_on_real")
            closed = self.storage.finish_signal(signal.id, status=status, exit_price=exit_price, approx_pnl=approx_pnl, real_pnl=None, result_message_id=message_id, result_source="normal" if signal.signal_type == "normal" else "normal_on_real", mfe_pct=mfe_pct, mae_pct=mae_pct)
            if closed:
                self.learning.learn_from_closed_signal(signal.id)

    async def _check_real_closed(self, signal: StoredSignal, send_result) -> bool:
        try:
            has_position = await asyncio.to_thread(self.toobit.has_open_position, signal.toobit_symbol)
        except Exception:
            return False
        if has_position:
            return False
        real_pnl = await self._real_pnl(signal)
        status = "TP" if real_pnl is not None and real_pnl >= 0 else "SL"
        exit_price = signal.tp if status == "TP" else signal.sl
        approx_pnl = self._approx_pnl(signal, exit_price)
        message_id = await send_result(signal, status, exit_price, approx_pnl, real_pnl, "toobit_real")
        closed = self.storage.finish_signal(signal.id, status=status, exit_price=exit_price, approx_pnl=approx_pnl, real_pnl=real_pnl, result_message_id=message_id, result_source="toobit_real", mfe_pct=signal.mfe_pct, mae_pct=signal.mae_pct)
        if closed:
            self.learning.learn_from_closed_signal(signal.id)
        return closed

    @staticmethod
    def _status_from_price(signal: StoredSignal, price: float) -> str | None:
        if signal.direction == "LONG":
            if price >= signal.tp:
                return "TP"
            if price <= signal.sl:
                return "SL"
        else:
            if price <= signal.tp:
                return "TP"
            if price >= signal.sl:
                return "SL"
        return None

    @staticmethod
    def _approx_pnl(signal: StoredSignal, exit_price: float) -> float:
        pct = direction_profit_pct(signal.direction, signal.entry, exit_price)
        return signal.margin_usdt * signal.leverage * pct

    async def _real_pnl(self, signal: StoredSignal) -> float | None:
        try:
            created = datetime.fromisoformat(signal.created_at)
            start_ms = int((created - timedelta(minutes=10)).timestamp() * 1000)
            end_ms = int((datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp() * 1000)
            return await asyncio.to_thread(self.toobit.find_realized_pnl, symbol=signal.toobit_symbol, side=signal.direction, start_ms=start_ms, end_ms=end_ms)
        except Exception:
            return None
