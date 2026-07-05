from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from config import SLOTS_FILE
from signal_manager import SignalStore


@dataclass
class ReservedSlot:
    symbol: str
    reserved_at: float
    expires_at: float


class SlotManager:
    def __init__(self, signal_store: SignalStore, path=SLOTS_FILE) -> None:
        self.signal_store = signal_store
        self.path = path
        self.path.parent.mkdir(exist_ok=True)
        if not self.path.exists():
            self._write([])

    def can_open_real(self, max_positions: int) -> bool:
        self.clear_expired()
        return self.signal_store.open_real_count() + len(self.reserved_slots()) < max_positions

    def reserve(self, symbol: str, seconds: int = 70) -> None:
        self.clear_expired()
        slots = self.reserved_slots()
        now = time.time()
        slots.append(ReservedSlot(symbol=symbol.upper(), reserved_at=now, expires_at=now + seconds))
        self._write([asdict(item) for item in slots])

    def release(self, symbol: str) -> None:
        symbol = symbol.upper()
        slots = [item for item in self.reserved_slots() if item.symbol.upper() != symbol]
        self._write([asdict(item) for item in slots])

    def reserved_slots(self) -> list[ReservedSlot]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = []
        return [ReservedSlot(**item) for item in data]

    def clear_expired(self) -> None:
        now = time.time()
        slots = [item for item in self.reserved_slots() if item.expires_at > now]
        self._write([asdict(item) for item in slots])

    def _write(self, data: list[dict]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
