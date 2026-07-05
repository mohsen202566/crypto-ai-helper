from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from config import SIGNALS_FILE

SignalType = Literal["رئال", "نرمال"]
SignalStatus = Literal["باز", "تیپی خورد", "استاپ خورد"]
Direction = Literal["LONG", "SHORT"]


@dataclass
class Signal:
    signal_id: str
    symbol_name: str
    okx_symbol: str
    toobit_symbol: str
    signal_type: SignalType
    direction: Direction
    entry: float
    tp: float
    sl: float
    estimated_move_percent: float
    estimated_net_profit: float
    estimated_hold_time: str
    rr: float
    fee_usdt: float
    status: SignalStatus = "باز"
    telegram_message_id: int | None = None
    opened_at: float = 0.0
    closed_at: float | None = None
    exit_price: float | None = None
    gross_pnl: float | None = None
    net_pnl: float | None = None
    result_source: str = ""

    @classmethod
    def create(
        cls,
        *,
        symbol_name: str,
        okx_symbol: str,
        toobit_symbol: str,
        signal_type: SignalType,
        direction: Direction,
        entry: float,
        tp: float,
        sl: float,
        estimated_move_percent: float,
        estimated_net_profit: float,
        estimated_hold_time: str,
        rr: float,
        fee_usdt: float,
    ) -> "Signal":
        return cls(
            signal_id=uuid.uuid4().hex,
            symbol_name=symbol_name,
            okx_symbol=okx_symbol,
            toobit_symbol=toobit_symbol,
            signal_type=signal_type,
            direction=direction,
            entry=entry,
            tp=tp,
            sl=sl,
            estimated_move_percent=estimated_move_percent,
            estimated_net_profit=estimated_net_profit,
            estimated_hold_time=estimated_hold_time,
            rr=rr,
            fee_usdt=fee_usdt,
            opened_at=time.time(),
        )


class SignalStore:
    def __init__(self, path: Path = SIGNALS_FILE) -> None:
        self.path = path
        self.path.parent.mkdir(exist_ok=True)
        if not self.path.exists():
            self._write([])

    def all(self) -> list[Signal]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = []
        return [Signal(**item) for item in data]

    def save_all(self, signals: list[Signal]) -> None:
        self._write([asdict(item) for item in signals])

    def add(self, signal: Signal) -> None:
        signals = self.all()
        signals.append(signal)
        self.save_all(signals)

    def update(self, signal: Signal) -> None:
        signals = self.all()
        replaced = False
        for i, item in enumerate(signals):
            if item.signal_id == signal.signal_id:
                signals[i] = signal
                replaced = True
                break
        if not replaced:
            signals.append(signal)
        self.save_all(signals)

    def open_signals(self) -> list[Signal]:
        return [item for item in self.all() if item.status == "باز"]

    def has_open_symbol(self, toobit_symbol: str) -> bool:
        toobit_symbol = toobit_symbol.upper()
        return any(item.toobit_symbol.upper() == toobit_symbol and item.status == "باز" for item in self.all())

    def open_real_count(self) -> int:
        return sum(1 for item in self.all() if item.status == "باز" and item.signal_type == "رئال")

    def _write(self, data: list[dict]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
