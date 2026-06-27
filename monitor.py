from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from okx_data import OkxDataClient
from storage import Storage, StoredSignal
from toobit_client import ToobitClient


class SignalMonitor:
    def __init__(self, storage: Storage, okx: OkxDataClient, toobit: ToobitClient) -> None:
        self.storage = storage
        self.okx = okx
        self.toobit = toobit

    async def check_once(self, send_result) -> None:
        signals = self.storage.open_signals()
        for signal in signals:
            try:
                price = await asyncio.to_thread(self.okx.get_last_price, signal.okx_symbol)
            except Exception:
                continue
            status = self._status_from_price(signal, price)
            if status is None:
                continue
            approx_pnl = self._approx_pnl(signal, price)
            real_pnl = await self._real_pnl(signal) if signal.signal_type == "real" else None
            result_message_id = await send_result(signal, status, approx_pnl, real_pnl)
            self.storage.finish_signal(
                signal.id,
                status=status,
                approx_pnl=approx_pnl,
                real_pnl=real_pnl,
                result_message_id=result_message_id,
            )

    def _status_from_price(self, signal: StoredSignal, price: float) -> str | None:
        if signal.direction == "LONG":
            if price >= signal.tp:
                return "TP"
            if price <= signal.sl:
                return "SL"
        if signal.direction == "SHORT":
            if price <= signal.tp:
                return "TP"
            if price >= signal.sl:
                return "SL"
        return None

    def _approx_pnl(self, signal: StoredSignal, exit_price: float) -> float:
        margin = signal.margin_usdt
        leverage = signal.leverage
        if signal.direction == "LONG":
            pct = (exit_price - signal.entry) / signal.entry
        else:
            pct = (signal.entry - exit_price) / signal.entry
        return margin * leverage * pct

    async def _real_pnl(self, signal: StoredSignal) -> float | None:
        created = datetime.fromisoformat(signal.created_at)
        start_ms = int((created - timedelta(minutes=5)).timestamp() * 1000)
        end_ms = int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp() * 1000)
        try:
            return await asyncio.to_thread(
                self.toobit.find_realized_pnl,
                symbol=signal.toobit_symbol,
                side=signal.direction,
                start_ms=start_ms,
                end_ms=end_ms,
            )
        except Exception:
            return None
