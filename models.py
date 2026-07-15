"""مدل‌های داده مشترک بین موتور تحلیل، اجرا، مانیتور و یادگیری."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Tier(str, Enum):
    INITIAL = "INITIAL"
    MEDIUM = "MEDIUM"
    REAL = "REAL"


class SignalStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PENDING_OPEN = "PENDING_OPEN"
    OPEN = "OPEN"
    TP = "TP"
    STOP = "STOP"
    FAILED_OPEN = "FAILED_OPEN"
    CANCELLED = "CANCELLED"


class ProfileStage(str, Enum):
    INITIAL = "INITIAL"
    MEDIUM = "MEDIUM"
    REAL_READY = "REAL_READY"
    REAL_WATCH = "REAL_WATCH"
    MEDIUM_RELEARN = "MEDIUM_RELEARN"
    PAUSED = "PAUSED"


class DataSource(str, Enum):
    OKX = "OKX"
    BYBIT_FALLBACK = "BYBIT_FALLBACK"
    BINANCE_FALLBACK = "BINANCE_FALLBACK"


@dataclass(slots=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float = 0.0
    confirmed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SymbolMapping:
    canonical: str
    base: str
    okx: str
    bybit: str
    toobit: str
    binance: str = ""
    okx_aliases: tuple[str, ...] = ()
    bybit_aliases: tuple[str, ...] = ()
    binance_aliases: tuple[str, ...] = ()
    toobit_aliases: tuple[str, ...] = ()
    tick_size: float = 0.0
    quantity_step: float = 0.0
    min_qty: float = 0.0
    min_notional: float = 0.0
    contract_multiplier: float = 1.0
    liquidity_score: float = 0.0
    active: bool = False
    valid: bool = True

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["okx_aliases"] = list(self.okx_aliases)
        out["bybit_aliases"] = list(self.bybit_aliases)
        out["binance_aliases"] = list(self.binance_aliases)
        out["toobit_aliases"] = list(self.toobit_aliases)
        return out


@dataclass(slots=True)
class FeatureSnapshot:
    canonical: str
    source: str
    trade_timeframe: str
    entry_timeframe: str
    ts: int
    long_scores: dict[str, float]
    short_scores: dict[str, float]
    raw: dict[str, Any]
    data_quality: float
    estimated_hold_minutes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Decision:
    canonical: str
    side: str
    direction_score: float
    strength_score: float
    entry_quality: float
    regime_confidence: float
    noise_risk: float
    execution_quality: float
    final_score: float
    behavior: str
    behavior_probabilities: dict[str, float]
    entry_type: str
    trade_timeframe: str
    entry_timeframe: str
    estimated_hold_minutes: int
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TradePlan:
    entry: float
    tp: float
    sl: float
    rr: float
    tp_percent: float
    sl_percent: float
    expected_gross_profit: float
    expected_net_profit: float
    expected_cost: float
    margin_usdt: float
    leverage: int
    notional_usdt: float
    valid: bool
    reject_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Signal:
    id: int | None
    canonical: str
    exchange_symbol: str
    side: str
    tier: str
    status: str
    created_at: int
    entry: float
    tp: float
    sl: float
    rr: float
    margin_usdt: float
    leverage: int
    notional_usdt: float
    expected_net_profit: float
    expected_hold_minutes: int
    data_source: str
    profile_version: int
    model_version: str
    feature_version: str
    behavior_version: str
    tp_sl_version: str
    decision: dict[str, Any]
    features: dict[str, Any]
    trade_timeframe: str = "30m"
    entry_timeframe: str = "15m"
    telegram_message_id: int | None = None
    order_id: str | None = None
    opened_at: int | None = None
    closed_at: int | None = None
    result: str | None = None
    net_pnl: float | None = None
    close_price: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Scenario:
    id: int | None
    parent_signal_id: int
    canonical: str
    side: str
    created_at: int
    status: str
    entry: float
    tp: float
    sl: float
    margin_usdt: float
    leverage: int
    change_key: str
    old_value: Any
    new_value: Any
    patch: dict[str, Any]
    trade_timeframe: str = "30m"
    no_entry: bool = False
    result: str | None = None
    net_pnl: float | None = None
    closed_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
