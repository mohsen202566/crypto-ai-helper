from __future__ import annotations

import time
from dataclasses import dataclass

from config import BotSettings
from okx_client import OKXClient
from signal_manager import Signal, SignalStore
from toobit_client import get_client


@dataclass(frozen=True)
class ResultEvent:
    signal: Signal
    is_tp: bool


class ResultMonitor:
    def __init__(self, store: SignalStore, okx_client: OKXClient, settings: BotSettings) -> None:
        self.store = store
        self.okx = okx_client
        self.settings = settings

    def check_once(self) -> list[ResultEvent]:
        events: list[ResultEvent] = []
        for signal in self.store.open_signals():
            try:
                event = self._check_signal(signal)
                if event:
                    self.store.update(event.signal)
                    events.append(event)
            except Exception:
                continue
        return events

    def _check_signal(self, signal: Signal) -> ResultEvent | None:
        if signal.signal_type == "نرمال":
            return self._check_normal(signal)
        return self._check_real(signal)

    def _check_normal(self, signal: Signal) -> ResultEvent | None:
        price = self.okx.get_last_price(signal.okx_symbol)
        if signal.direction == "LONG":
            if price >= signal.tp:
                return self._close_signal(signal, True, signal.tp, "اوکی‌اکس")
            if price <= signal.sl:
                return self._close_signal(signal, False, signal.sl, "اوکی‌اکس")
        else:
            if price <= signal.tp:
                return self._close_signal(signal, True, signal.tp, "اوکی‌اکس")
            if price >= signal.sl:
                return self._close_signal(signal, False, signal.sl, "اوکی‌اکس")
        return None

    def _check_real(self, signal: Signal) -> ResultEvent | None:
        client = get_client()
        if client.has_open_position(signal.toobit_symbol):
            return None
        realized = client.find_realized_pnl(
            symbol=signal.toobit_symbol,
            side=signal.direction,
            start_ms=int(signal.opened_at * 1000) - 60_000,
            end_ms=int(time.time() * 1000) + 60_000,
        )
        if realized is None:
            return None
        is_tp = realized >= 0
        exit_price = signal.tp if is_tp else signal.sl
        signal.status = "تیپی خورد" if is_tp else "استاپ خورد"
        signal.closed_at = time.time()
        signal.exit_price = exit_price
        signal.gross_pnl = float(realized)
        signal.net_pnl = float(realized) - signal.fee_usdt if realized >= 0 else float(realized) - signal.fee_usdt
        signal.result_source = "توبیت"
        return ResultEvent(signal, is_tp)

    def _close_signal(self, signal: Signal, is_tp: bool, exit_price: float, source: str) -> ResultEvent:
        notional = self.settings.trade_amount_usdt * self.settings.leverage
        qty = notional / signal.entry
        if signal.direction == "LONG":
            gross = (exit_price - signal.entry) * qty
        else:
            gross = (signal.entry - exit_price) * qty
        net = gross - signal.fee_usdt
        signal.status = "تیپی خورد" if is_tp else "استاپ خورد"
        signal.closed_at = time.time()
        signal.exit_price = exit_price
        signal.gross_pnl = gross
        signal.net_pnl = net
        signal.result_source = source
        return ResultEvent(signal, is_tp)
