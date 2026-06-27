"""
State store for Crypto AI Helper bot.

Locked responsibility:
- Stores bot settings, slots, active signals, and stats.
- No analysis, no OKX API, no Toobit API, no Telegram rendering, no order execution.

Design lock:
- Small, simple, strong.
- Trade OFF must still allow SIGNAL monitoring/results but never REAL execution.
- One coin can have only one active signal until TP/SL/cancel.
- REAL slots are reserved immediately, then confirmed/released by the trade manager.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import time
from typing import Literal

from config import (
    DEFAULT_AUTO_SIGNAL_ENABLED,
    DEFAULT_LEVERAGE,
    DEFAULT_MAX_POSITIONS,
    DEFAULT_MIN_NET_PROFIT_USDT,
    DEFAULT_REAL_TRADE_ENABLED,
    DEFAULT_TRADE_CAPITAL,
    DEFAULT_TRADE_DOLLAR,
    ENTRY_TIMEFRAME,
    MAX_HOLD_MINUTES,
    TARGET_HOLD_MINUTES,
    TIMEFRAME,
    TREND_FILTER_TIMEFRAME,
    validate_leverage,
    validate_max_positions,
    validate_min_net_profit,
    validate_trade_capital,
    validate_trade_dollar,
    get_coin,
)

SignalMode = Literal["TOOBIT", "SIGNAL"]
Direction = Literal["LONG", "SHORT"]
SignalStatus = Literal["PENDING_OPEN", "MONITORING", "CLOSED", "CANCELLED"]
ResultKind = Literal["TP", "SL"]

DEFAULT_STATE_PATH = Path("state_store.json")


@dataclass
class BotSettings:
    auto_signal_enabled: bool = DEFAULT_AUTO_SIGNAL_ENABLED
    real_trade_enabled: bool = DEFAULT_REAL_TRADE_ENABLED
    trade_capital_usdt: float = DEFAULT_TRADE_CAPITAL
    trade_dollar_usdt: float = DEFAULT_TRADE_DOLLAR
    leverage: int = DEFAULT_LEVERAGE
    max_slots: int = DEFAULT_MAX_POSITIONS
    min_net_profit_usdt: float = DEFAULT_MIN_NET_PROFIT_USDT
    margin_mode: str = "isolated"


@dataclass
class SignalRecord:
    signal_id: str
    symbol: str
    direction: Direction
    mode: SignalMode
    entry: float
    tp: float
    sl: float
    status: SignalStatus = "MONITORING"
    created_at: float = field(default_factory=time)
    opened_at: float | None = None
    closed_at: float | None = None
    result: ResultKind | None = None
    exit_price: float | None = None
    pnl_usdt: float = 0.0
    close_reason: str = ""
    main_timeframe: str = TIMEFRAME
    entry_timeframe: str = ENTRY_TIMEFRAME
    trend_timeframe: str = TREND_FILTER_TIMEFRAME
    target_hold_min_minutes: int = TARGET_HOLD_MINUTES[0]
    target_hold_max_minutes: int = TARGET_HOLD_MINUTES[1]
    max_hold_minutes: int = MAX_HOLD_MINUTES


@dataclass
class StatsState:
    real_signals: int = 0
    real_monitoring: int = 0
    real_tp: int = 0
    real_sl: int = 0
    real_pnl_usdt: float = 0.0
    signal_only_total: int = 0
    signal_only_monitoring: int = 0
    signal_only_tp: int = 0
    signal_only_sl: int = 0

    @property
    def real_win_rate(self) -> float:
        closed = self.real_tp + self.real_sl
        return 0.0 if closed <= 0 else self.real_tp / closed * 100.0

    @property
    def signal_only_win_rate(self) -> float:
        closed = self.signal_only_tp + self.signal_only_sl
        return 0.0 if closed <= 0 else self.signal_only_tp / closed * 100.0


@dataclass
class BotState:
    settings: BotSettings = field(default_factory=BotSettings)
    active_signals: dict[str, SignalRecord] = field(default_factory=dict)
    closed_signals: dict[str, SignalRecord] = field(default_factory=dict)
    stats: StatsState = field(default_factory=StatsState)
    toobit_margin_usdt: float | None = None
    toobit_open_positions: int = 0

    @property
    def reserved_real_slots(self) -> int:
        return sum(1 for item in self.active_signals.values() if item.mode == "TOOBIT")

    @property
    def pending_real_slots(self) -> int:
        return sum(
            1
            for item in self.active_signals.values()
            if item.mode == "TOOBIT" and item.status == "PENDING_OPEN"
        )

    @property
    def confirmed_real_slots(self) -> int:
        return sum(
            1
            for item in self.active_signals.values()
            if item.mode == "TOOBIT" and item.status == "MONITORING"
        )

    @property
    def used_slots(self) -> int:
        # Toobit open positions are the source of truth when recently synced.
        # Local confirmed REAL records are also counted as a safety floor so a
        # delayed/failed exchange sync cannot briefly free a slot that is still
        # active locally. Pending REAL records always reserve a slot during the
        # delayed verification window.
        confirmed_slots = max(int(self.toobit_open_positions), self.confirmed_real_slots)
        return confirmed_slots + self.pending_real_slots

    @property
    def free_slots(self) -> int:
        return max(0, self.settings.max_slots - self.used_slots)


class StateStore:
    def __init__(self, path: str | Path = DEFAULT_STATE_PATH) -> None:
        self.path = Path(path)
        self.state = self._load()
        self._recalculate_monitoring_counts()

    def save(self) -> None:
        self._recalculate_monitoring_counts()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = _state_to_dict(self.state)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.path)

    def snapshot(self) -> BotState:
        return self.state

    def set_auto_signal_enabled(self, enabled: bool) -> None:
        self.state.settings.auto_signal_enabled = bool(enabled)
        self.save()

    def set_real_trade_enabled(self, enabled: bool) -> None:
        self.state.settings.real_trade_enabled = bool(enabled)
        self.save()

    def set_trade_dollar(self, value: float) -> None:
        self.state.settings.trade_dollar_usdt = validate_trade_dollar(value)
        self.save()

    def set_leverage(self, value: int) -> None:
        self.state.settings.leverage = validate_leverage(value)
        self.save()

    def set_trade_capital(self, value: float) -> None:
        self.state.settings.trade_capital_usdt = validate_trade_capital(value)
        self.save()

    def set_max_slots(self, value: int) -> None:
        self.state.settings.max_slots = validate_max_positions(value)
        self.save()

    def set_min_net_profit(self, value: float) -> None:
        self.state.settings.min_net_profit_usdt = validate_min_net_profit(value)
        self.save()

    def sync_toobit_status(self, *, margin_usdt: float | None, open_positions: int) -> None:
        if margin_usdt is not None and margin_usdt < 0:
            raise ValueError("مارجین توبیت نمی‌تواند منفی باشد.")
        if open_positions < 0:
            raise ValueError("تعداد پوزیشن باز نمی‌تواند منفی باشد.")
        self.state.toobit_margin_usdt = margin_usdt
        self.state.toobit_open_positions = int(open_positions)
        self.save()

    def has_active_symbol(self, symbol: str) -> bool:
        key = symbol.upper()
        get_coin(key)
        return any(item.symbol == key and item.status in ("PENDING_OPEN", "MONITORING") for item in self.state.active_signals.values())

    def can_create_signal(self, symbol: str) -> bool:
        return self.state.settings.auto_signal_enabled and not self.has_active_symbol(symbol)

    def can_open_real(self, symbol: str) -> bool:
        return self.can_create_signal(symbol) and self.state.settings.real_trade_enabled and self.state.free_slots > 0

    def register_signal(
        self,
        *,
        signal_id: str,
        symbol: str,
        direction: Direction,
        requested_mode: SignalMode,
        entry: float,
        tp: float,
        sl: float,
        main_timeframe: str = TIMEFRAME,
        entry_timeframe: str = ENTRY_TIMEFRAME,
        trend_timeframe: str = TREND_FILTER_TIMEFRAME,
        target_hold_min_minutes: int = TARGET_HOLD_MINUTES[0],
        target_hold_max_minutes: int = TARGET_HOLD_MINUTES[1],
        max_hold_minutes: int = MAX_HOLD_MINUTES,
    ) -> SignalRecord:
        key = symbol.upper()
        get_coin(key)
        if direction not in ("LONG", "SHORT"):
            raise ValueError("جهت سیگنال باید LONG یا SHORT باشد.")
        if requested_mode not in ("TOOBIT", "SIGNAL"):
            raise ValueError("حالت سیگنال باید TOOBIT یا SIGNAL باشد.")
        if not self.state.settings.auto_signal_enabled:
            raise RuntimeError("اتو سیگنال خاموش است.")
        if signal_id in self.state.active_signals or signal_id in self.state.closed_signals:
            raise RuntimeError("شناسه سیگنال تکراری است.")
        if self.has_active_symbol(key):
            raise RuntimeError("برای این کوین هنوز سیگنال فعال وجود دارد.")
        if entry <= 0 or tp <= 0 or sl <= 0:
            raise ValueError("Entry/TP/SL باید مثبت باشند.")
        if target_hold_min_minutes <= 0 or target_hold_max_minutes <= 0 or max_hold_minutes <= 0:
            raise ValueError("زمان‌های نگهداری پوزیشن باید مثبت باشند.")
        if target_hold_min_minutes > target_hold_max_minutes:
            raise ValueError("حداقل زمان هدف نمی‌تواند از حداکثر زمان هدف بیشتر باشد.")
        if max_hold_minutes < target_hold_max_minutes:
            raise ValueError("خروج اجباری باید بعد از بازه هدف نگهداری باشد.")

        mode: SignalMode = "TOOBIT" if requested_mode == "TOOBIT" and self.state.settings.real_trade_enabled and self.state.free_slots > 0 else "SIGNAL"
        status: SignalStatus = "PENDING_OPEN" if mode == "TOOBIT" else "MONITORING"
        record = SignalRecord(
            signal_id=signal_id,
            symbol=key,
            direction=direction,
            mode=mode,
            entry=float(entry),
            tp=float(tp),
            sl=float(sl),
            status=status,
            opened_at=time() if mode == "SIGNAL" else None,
            main_timeframe=str(main_timeframe),
            entry_timeframe=str(entry_timeframe),
            trend_timeframe=str(trend_timeframe),
            target_hold_min_minutes=int(target_hold_min_minutes),
            target_hold_max_minutes=int(target_hold_max_minutes),
            max_hold_minutes=int(max_hold_minutes),
        )
        self.state.active_signals[signal_id] = record
        if mode == "TOOBIT":
            self.state.stats.real_signals += 1
        else:
            self.state.stats.signal_only_total += 1
        self.save()
        return record

    def confirm_real_open(self, signal_id: str) -> SignalRecord:
        record = self._active(signal_id)
        if record.mode != "TOOBIT":
            raise ValueError("فقط سیگنال توبیت نیاز به تایید باز شدن دارد.")
        record.status = "MONITORING"
        record.opened_at = time()
        self.save()
        return record

    def cancel_unconfirmed_real(self, signal_id: str, reason: str = "پوزیشن در توبیت تایید نشد") -> SignalRecord:
        record = self._active(signal_id)
        if record.mode != "TOOBIT" or record.status != "PENDING_OPEN":
            raise ValueError("فقط پوزیشن توبیت تاییدنشده قابل آزادسازی است.")
        record.status = "CANCELLED"
        record.closed_at = time()
        record.result = None
        record.pnl_usdt = 0.0
        record.close_reason = str(reason or "")
        self.state.active_signals.pop(signal_id, None)
        self.state.closed_signals[signal_id] = record
        self.save()
        return record

    def mark_result(
        self,
        signal_id: str,
        *,
        result: ResultKind,
        exit_price: float,
        pnl_usdt: float,
        close_reason: str | None = None,
    ) -> SignalRecord:
        record = self._active(signal_id)
        if result not in ("TP", "SL"):
            raise ValueError("نتیجه باید TP یا SL باشد.")
        if exit_price <= 0:
            raise ValueError("قیمت خروج باید مثبت باشد.")

        record.status = "CLOSED"
        record.result = result
        record.exit_price = float(exit_price)
        record.pnl_usdt = float(pnl_usdt)
        record.close_reason = str(close_reason or result)
        record.closed_at = time()
        self.state.active_signals.pop(signal_id, None)
        self.state.closed_signals[signal_id] = record

        if record.mode == "TOOBIT":
            if result == "TP":
                self.state.stats.real_tp += 1
            else:
                self.state.stats.real_sl += 1
            self.state.stats.real_pnl_usdt += float(pnl_usdt)
        else:
            if result == "TP":
                self.state.stats.signal_only_tp += 1
            else:
                self.state.stats.signal_only_sl += 1
        self.save()
        return record

    def _active(self, signal_id: str) -> SignalRecord:
        if signal_id not in self.state.active_signals:
            raise KeyError(f"سیگنال فعال پیدا نشد: {signal_id}")
        return self.state.active_signals[signal_id]

    def _load(self) -> BotState:
        if not self.path.exists():
            return BotState()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return _state_from_dict(payload)

    def _recalculate_monitoring_counts(self) -> None:
        real = 0
        signal = 0
        for item in self.state.active_signals.values():
            if item.status not in ("PENDING_OPEN", "MONITORING"):
                continue
            if item.mode == "TOOBIT":
                real += 1
            else:
                signal += 1
        self.state.stats.real_monitoring = real
        self.state.stats.signal_only_monitoring = signal


def _state_to_dict(state: BotState) -> dict[str, object]:
    return {
        "settings": asdict(state.settings),
        "active_signals": {key: asdict(value) for key, value in state.active_signals.items()},
        "closed_signals": {key: asdict(value) for key, value in state.closed_signals.items()},
        "stats": asdict(state.stats),
        "toobit_margin_usdt": state.toobit_margin_usdt,
        "toobit_open_positions": state.toobit_open_positions,
    }


def _state_from_dict(payload: dict[str, object]) -> BotState:
    settings = BotSettings(**dict(payload.get("settings", {})))
    active = {key: SignalRecord(**value) for key, value in dict(payload.get("active_signals", {})).items()}
    closed = {key: SignalRecord(**value) for key, value in dict(payload.get("closed_signals", {})).items()}
    stats = StatsState(**dict(payload.get("stats", {})))
    return BotState(
        settings=settings,
        active_signals=active,
        closed_signals=closed,
        stats=stats,
        toobit_margin_usdt=payload.get("toobit_margin_usdt"),
        toobit_open_positions=int(payload.get("toobit_open_positions", 0)),
    )


__all__ = [
    "BotSettings",
    "SignalRecord",
    "StatsState",
    "BotState",
    "StateStore",
    "DEFAULT_STATE_PATH",
]
