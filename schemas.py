from __future__ import annotations

"""
00 - schemas.py

Shared schema/constants layer for the locked Movement Hunter architecture.

Responsibilities:
- Define common constants and lightweight dataclasses used across the bot.
- Provide safe conversion helpers.
- Keep shared enums/labels centralized.
- Avoid imports from other bot modules to prevent circular dependencies.

Strictly forbidden:
- No trading.
- No Toobit calls.
- No Telegram.
- No AI decision.
- No persistence side effects.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4
import math
import time


JsonDict = Dict[str, Any]

# Directions
DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"

# Final AI decisions
DECISION_REAL = "REAL"
DECISION_GHOST = "GHOST"
DECISION_REJECT = "REJECT"

# Results
RESULT_OPEN = "OPEN"
RESULT_TP1 = "TP1"
RESULT_TP2 = "TP2"
RESULT_AI_EXIT = "AI_EXIT"
RESULT_SL = "SL"
RESULT_REJECT = "REJECT"
RESULT_UNKNOWN = "UNKNOWN"

# Source types
SOURCE_REAL = "REAL"
SOURCE_GHOST = "GHOST"

# Position states
POSITION_PENDING_REAL_CONFIRM = "PENDING_REAL_CONFIRM"
POSITION_OPEN = "OPEN"
POSITION_CONFIRMED = "CONFIRMED"
POSITION_CLOSED = "CLOSED"
POSITION_FAILED = "FAILED"
POSITION_REJECTED = "REJECTED"

# Market/movement states
STATE_START = "START"
STATE_EARLY = "EARLY"
STATE_MIDDLE = "MIDDLE"
STATE_LATE = "LATE"
STATE_EXHAUSTION = "EXHAUSTION"
STATE_REVERSAL = "REVERSAL"
STATE_RANGE = "RANGE"
STATE_UNKNOWN = "UNKNOWN"

# Confidence / risk labels
LEVEL_LOW = "LOW"
LEVEL_MEDIUM = "MEDIUM"
LEVEL_HIGH = "HIGH"
LEVEL_EXTREME = "EXTREME"
LEVEL_UNKNOWN = "UNKNOWN"

# TP modes
TP_MODE_TP1_ONLY = "TP1_ONLY"
TP_MODE_TP1_TP2 = "TP1_TP2"

# Exchange constants
MARGIN_ISOLATED = "ISOLATED"
MARGIN_CROSS = "CROSS"


def now_ts() -> int:
    return int(time.time())


def new_id(prefix: str = "id") -> str:
    return f"{prefix}_{uuid4().hex}"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return low
        return max(low, min(high, v))
    except Exception:
        return low


def normalize_direction(direction: str) -> str:
    d = str(direction or "").upper().strip()
    if d in {"LONG", "BUY"}:
        return DIRECTION_LONG
    if d in {"SHORT", "SELL"}:
        return DIRECTION_SHORT
    return DIRECTION_NEUTRAL


def normalize_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().replace("-", "").replace("/", "").replace("_", "").strip()
    if s and not s.endswith("USDT") and len(s) <= 14:
        s += "USDT"
    return s


def is_win_result(result: str) -> bool:
    return str(result or "").upper() in {RESULT_TP1, RESULT_TP2, RESULT_AI_EXIT}


def is_loss_result(result: str) -> bool:
    return str(result or "").upper() == RESULT_SL


@dataclass(frozen=True)
class BaseRecord:
    id: str
    timestamp: int = field(default_factory=now_ts)
    meta: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class PriceLevel:
    symbol: str
    price: float
    label: str = ""
    timestamp: int = field(default_factory=now_ts)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class TradePlanSchema:
    symbol: str
    direction: str
    entry: float
    tp1: float
    tp2: float
    sl: float
    tp_mode: str = TP_MODE_TP1_ONLY
    leverage: int = 0
    margin_usdt: float = 0.0

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class DecisionSchema:
    decision_id: str
    symbol: str
    direction: str
    decision_type: str
    ai_score: float
    confidence_score: float
    risk_score: float
    timestamp: int = field(default_factory=now_ts)
    meta: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class PositionSchema:
    position_id: str
    symbol: str
    direction: str
    entry: float
    quantity: float
    tp1: float
    tp2: float
    sl: float
    status: str = POSITION_OPEN
    timestamp: int = field(default_factory=now_ts)
    meta: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class ResultSchema:
    result_id: str
    symbol: str
    direction: str
    result: str
    price: float
    realized_pnl_usdt: float = 0.0
    realized_pnl_percent: float = 0.0
    timestamp: int = field(default_factory=now_ts)
    meta: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class ErrorSchema:
    error_id: str
    source: str
    message: str
    category: str = LEVEL_UNKNOWN
    retryable: bool = False
    timestamp: int = field(default_factory=now_ts)
    details: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


__all__ = [
    "JsonDict",
    "DIRECTION_LONG",
    "DIRECTION_SHORT",
    "DIRECTION_NEUTRAL",
    "DECISION_REAL",
    "DECISION_GHOST",
    "DECISION_REJECT",
    "RESULT_OPEN",
    "RESULT_TP1",
    "RESULT_TP2",
    "RESULT_AI_EXIT",
    "RESULT_SL",
    "RESULT_REJECT",
    "RESULT_UNKNOWN",
    "SOURCE_REAL",
    "SOURCE_GHOST",
    "POSITION_PENDING_REAL_CONFIRM",
    "POSITION_OPEN",
    "POSITION_CONFIRMED",
    "POSITION_CLOSED",
    "POSITION_FAILED",
    "POSITION_REJECTED",
    "STATE_START",
    "STATE_EARLY",
    "STATE_MIDDLE",
    "STATE_LATE",
    "STATE_EXHAUSTION",
    "STATE_REVERSAL",
    "STATE_RANGE",
    "STATE_UNKNOWN",
    "LEVEL_LOW",
    "LEVEL_MEDIUM",
    "LEVEL_HIGH",
    "LEVEL_EXTREME",
    "LEVEL_UNKNOWN",
    "TP_MODE_TP1_ONLY",
    "TP_MODE_TP1_TP2",
    "MARGIN_ISOLATED",
    "MARGIN_CROSS",
    "now_ts",
    "new_id",
    "safe_float",
    "safe_int",
    "clamp",
    "normalize_direction",
    "normalize_symbol",
    "is_win_result",
    "is_loss_result",
    "BaseRecord",
    "PriceLevel",
    "TradePlanSchema",
    "DecisionSchema",
    "PositionSchema",
    "ResultSchema",
    "ErrorSchema",
]
