"""ذخیره‌سازی دائمی تنظیمات، سیگنال‌ها و آمار."""
from __future__ import annotations

from typing import Any

import config
from models import BotSettings, Signal, TradeStats
from stats_manager import StatsManager
from utils import json_load, json_save_atomic, now_ms


class JsonStorage:
    def __init__(self, path=config.RUNTIME_STATE_FILE):
        self.path = path
        self.settings = BotSettings()
        self.signals: dict[str, Signal] = {}
        self.stats = TradeStats()
        self.load()

    def load(self) -> None:
        data = json_load(self.path, {})
        self.settings = BotSettings.from_dict(data.get("settings"))
        self.stats = TradeStats.from_dict(data.get("stats"))
        self.signals = {}
        for sid, raw in (data.get("signals") or {}).items():
            try:
                self.signals[sid] = Signal.from_dict(raw)
            except Exception:
                continue

    def save(self) -> None:
        json_save_atomic(self.path, {
            "settings": self.settings.to_dict(),
            "stats": self.stats.to_dict(),
            "signals": {sid: sig.to_dict() for sid, sig in self.signals.items()},
        })

    # -----------------------------
    # تنظیمات
    # -----------------------------
    def update_settings(self, **kwargs: Any) -> BotSettings:
        for key, value in kwargs.items():
            if hasattr(self.settings, key):
                setattr(self.settings, key, value)
        self.save()
        return self.settings

    # -----------------------------
    # سیگنال‌ها
    # -----------------------------
    def add_signal(self, signal: Signal) -> Signal:
        self.signals[signal.id] = signal
        self.stats = StatsManager.register_signal(self.stats, signal)
        self.save()
        return signal

    def update_signal(self, signal: Signal) -> Signal:
        self.signals[signal.id] = signal
        self.save()
        return signal

    def close_signal(
        self,
        signal_id: str,
        *,
        close_price: float,
        move_percent: float,
        gross_profit_usdt: float,
        fee_usdt: float,
        net_profit_usdt: float,
        close_reason: str,
        raw: dict[str, Any] | None = None,
    ) -> Signal | None:
        sig = self.signals.get(signal_id)
        if not sig:
            return None
        sig.status = config.STATUS_CLOSED
        sig.closed_at_ms = now_ms()
        sig.close_price = close_price
        sig.move_percent = move_percent
        sig.gross_profit_usdt = gross_profit_usdt
        sig.fee_usdt = fee_usdt
        sig.net_profit_usdt = net_profit_usdt
        sig.close_reason = close_reason
        if raw:
            sig.raw.update(raw)
        self.stats = StatsManager.register_close(self.stats, sig)
        self.save()
        return sig

    def mark_failed(self, signal_id: str, reason: str) -> Signal | None:
        sig = self.signals.get(signal_id)
        if not sig:
            return None
        sig.status = config.STATUS_FAILED
        sig.close_reason = reason
        sig.closed_at_ms = now_ms()
        self.save()
        return sig

    def get_signal(self, signal_id: str) -> Signal | None:
        return self.signals.get(signal_id)

    def open_signals(self) -> list[Signal]:
        return [s for s in self.signals.values() if s.status in {config.STATUS_OPEN, config.STATUS_PENDING_BUY, config.STATUS_REAL_OPEN, config.STATUS_NORMAL_OPEN}]

    def normal_open_signals(self) -> list[Signal]:
        return [s for s in self.open_signals() if s.execution_mode == config.MODE_NORMAL]

    def real_open_signals(self) -> list[Signal]:
        return [s for s in self.open_signals() if s.execution_mode == config.MODE_REAL and s.status == config.STATUS_REAL_OPEN]

    def real_reserved_signals(self) -> list[Signal]:
        return [s for s in self.open_signals() if s.execution_mode == config.MODE_REAL and s.status in {config.STATUS_PENDING_BUY, config.STATUS_REAL_OPEN}]

    def closed_signals(self) -> list[Signal]:
        return [s for s in self.signals.values() if s.status == config.STATUS_CLOSED]

    def has_active_symbol(self, base_symbol: str) -> bool:
        base = base_symbol.upper()
        return any(s.base_symbol == base for s in self.open_signals())

    def free_real_slots(self) -> int:
        used = len(self.real_reserved_signals())
        return max(0, int(self.settings.max_real_positions) - used)

    def reset_stats(self) -> None:
        self.stats = StatsManager.reset()
        self.save()

    def delete_history(self) -> None:
        self.signals = {}
        self.stats = StatsManager.reset()
        self.save()
