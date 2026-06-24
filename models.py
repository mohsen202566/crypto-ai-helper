"""
models.py
Level 4 / 1H Smart Scalp Bot

Shared data models for all Level 4 modules.

Architecture lock:
- This file defines stable data contracts only.
- No market fetching, AI decision logic, JSON file IO, order execution, or Telegram sending here.
- Allowed project imports: constants.py and utils.py only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Optional

from constants import (
    AI_EXIT_CONFIG,
    DIRECTION_LONG,
    DIRECTION_SHORT,
    EVENT_AI_EXIT,
    EVENT_CLOSE_CONFIRMED,
    EVENT_CLOSE_REQUESTED,
    EVENT_ERROR,
    EVENT_GHOST_OPENED,
    EVENT_MANUAL_CLOSE,
    EVENT_REAL_OPEN_CONFIRMED,
    EVENT_REAL_OPEN_FAILED,
    EVENT_REAL_OPEN_REQUESTED,
    EVENT_REJECTED,
    EVENT_SIGNAL_CREATED,
    EVENT_SL,
    EVENT_TP1,
    EVENT_TP2,
    MODE_GHOST,
    MODE_REAL,
    MODE_REJECT,
    POSITION_ACTIVE_GHOST,
    POSITION_ACTIVE_REAL,
    POSITION_CLOSED,
    POSITION_FAILED,
    POSITION_PARTIAL_TP1,
    POSITION_PENDING_REAL_CONFIRM,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    SYSTEM_VERSION,
)
from utils import (
    make_event_id,
    make_position_id,
    make_signal_id,
    normalize_direction,
    normalize_symbol,
    safe_float,
    safe_int,
    safe_str,
    to_okx_inst_id,
    to_tobit_symbol,
    utc_now_iso,
)


MODELS_VERSION: str = SYSTEM_VERSION


# =============================================================================
# Generic serialization helpers
# =============================================================================

def to_dict(obj: Any) -> Any:
    """Convert dataclass/model/nested values to plain JSON-friendly data."""
    if is_dataclass(obj):
        return {key: to_dict(value) for key, value in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(key): to_dict(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(item) for item in obj]
    return obj


def from_dict(model_cls: type, data: Optional[dict[str, Any]]) -> Any:
    """
    Construct a dataclass model from dict, ignoring unknown keys.

    This keeps old JSON records compatible when models evolve.
    """
    if data is None:
        data = {}
    allowed = getattr(model_cls, "__dataclass_fields__", {})
    clean = {key: value for key, value in dict(data).items() if key in allowed}
    return model_cls(**clean)


def model_version_payload() -> dict[str, Any]:
    return {
        "system_version": SYSTEM_VERSION,
        "created_at": utc_now_iso(),
    }


# =============================================================================
# Market / Sensor snapshots
# =============================================================================

@dataclass
class Candle:
    """One OHLCV candle."""
    timestamp: int = 0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    timeframe: str = ""

    def __post_init__(self) -> None:
        self.timestamp = safe_int(self.timestamp, 0) or 0
        self.open = safe_float(self.open, 0.0) or 0.0
        self.high = safe_float(self.high, 0.0) or 0.0
        self.low = safe_float(self.low, 0.0) or 0.0
        self.close = safe_float(self.close, 0.0) or 0.0
        self.volume = safe_float(self.volume, 0.0) or 0.0
        self.timeframe = safe_str(self.timeframe)


@dataclass
class MarketSnapshot:
    """Raw market data packet prepared for analysis."""
    symbol: str
    timeframe: str
    candles: list[Candle] = field(default_factory=list)
    current_price: float = 0.0
    ok: bool = True
    source: str = "OKX"
    error: str = ""
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.symbol = normalize_symbol(self.symbol)
        self.timeframe = safe_str(self.timeframe)
        self.current_price = safe_float(self.current_price, 0.0) or 0.0
        self.ok = bool(self.ok)
        self.source = safe_str(self.source, "OKX")
        self.error = safe_str(self.error)


@dataclass
class SensorSnapshot:
    """Raw indicator/sensor values only; no final decision here."""
    symbol: str
    timeframe: str
    price: float = 0.0
    ema20: Optional[float] = None
    ema50: Optional[float] = None
    vwap: Optional[float] = None
    rsi: Optional[float] = None
    rsi_slope: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    macd_hist_slope: Optional[float] = None
    adx: Optional[float] = None
    atr: Optional[float] = None
    atr_pct: Optional[float] = None
    buy_power: Optional[float] = None
    sell_power: Optional[float] = None
    volume_ratio: Optional[float] = None
    candle_body_pct: Optional[float] = None
    upper_wick_pct: Optional[float] = None
    lower_wick_pct: Optional[float] = None
    raw: dict[str, Any] = field(default_factory=dict)
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.symbol = normalize_symbol(self.symbol)
        self.timeframe = safe_str(self.timeframe)
        self.price = safe_float(self.price, 0.0) or 0.0


@dataclass
class StructureSnapshot:
    """1H structure context: trend, range/impulse, support/resistance."""
    symbol: str
    direction: str = ""
    trend: str = "UNKNOWN"
    structure_score: float = 0.0
    is_range: bool = False
    is_impulse: bool = False
    is_late_move: bool = False
    fresh_zone_score: float = 0.0
    nearest_support: Optional[float] = None
    nearest_resistance: Optional[float] = None
    supply_zone: Optional[dict[str, Any]] = None
    demand_zone: Optional[dict[str, Any]] = None
    swing_highs: list[float] = field(default_factory=list)
    swing_lows: list[float] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.symbol = normalize_symbol(self.symbol)
        self.direction = normalize_direction(self.direction)


@dataclass
class MomentumSnapshot:
    """Momentum and continuation/weakness context."""
    symbol: str
    direction: str = ""
    momentum_score: float = 0.0
    continuation_score: float = 0.0
    reversal_risk_score: float = 0.0
    acceleration_score: float = 0.0
    weakness_score: float = 0.0
    rsi_slope_ok: bool = False
    macd_hist_slope_ok: bool = False
    power_shift_ok: bool = False
    volume_participation_ok: bool = False
    reason_codes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.symbol = normalize_symbol(self.symbol)
        self.direction = normalize_direction(self.direction)


@dataclass
class LiquiditySnapshot:
    """Liquidity/trap assessment. It scores risk but does not decide alone."""
    symbol: str
    direction: str = ""
    trap_risk_score: float = 0.0
    liquidity_sweep_score: float = 0.0
    fake_break_risk: float = 0.0
    wick_rejection_score: float = 0.0
    breakout_survival_score: float = 0.0
    stop_hunt_detected: bool = False
    likely_trap: bool = False
    reason_codes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.symbol = normalize_symbol(self.symbol)
        self.direction = normalize_direction(self.direction)


@dataclass
class MarketContextSnapshot:
    """BTC/ETH/market regime context for Level 4."""
    market_mode: str = "UNKNOWN"
    btc_bias: str = "UNKNOWN"
    eth_bias: str = "UNKNOWN"
    context_score: float = 0.0
    market_risk_score: float = 0.0
    choppy: bool = False
    aligned_with_direction: bool = False
    reason_codes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)


# =============================================================================
# TP/SL and AI decisions
# =============================================================================

@dataclass
class TPSLPlan:
    """TP/SL candidate approved or adjusted by AI Brain."""
    symbol: str
    direction: str
    entry: float
    tp1: float
    sl: float
    tp2: Optional[float] = None
    rr: float = 0.0
    tp1_net_profit_estimate: float = 0.0
    tp1_gross_profit_estimate: float = 0.0
    fee_estimate: float = 0.0
    valid: bool = True
    reason_codes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.symbol = normalize_symbol(self.symbol)
        self.direction = normalize_direction(self.direction)
        self.entry = safe_float(self.entry, 0.0) or 0.0
        self.tp1 = safe_float(self.tp1, 0.0) or 0.0
        self.tp2 = safe_float(self.tp2, None)
        self.sl = safe_float(self.sl, 0.0) or 0.0
        self.rr = safe_float(self.rr, 0.0) or 0.0


@dataclass
class AIDecision:
    """Final AI decision for a new signal."""
    symbol: str
    direction: str
    mode: str
    score: float = 0.0
    confidence: float = 0.0
    entry: float = 0.0
    tp_sl: Optional[TPSLPlan] = None
    reason_codes: list[str] = field(default_factory=list)
    reject_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    signal_id: str = ""
    level: int = 4
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.symbol = normalize_symbol(self.symbol)
        self.direction = normalize_direction(self.direction)
        self.mode = safe_str(self.mode).upper()
        if self.mode not in {MODE_REAL, MODE_GHOST, MODE_REJECT}:
            self.mode = MODE_REJECT
        self.score = safe_float(self.score, 0.0) or 0.0
        self.confidence = safe_float(self.confidence, 0.0) or 0.0
        self.entry = safe_float(self.entry, 0.0) or 0.0
        self.level = safe_int(self.level, 4) or 4
        if not self.signal_id:
            self.signal_id = make_signal_id(self.symbol, self.direction, self.level)


@dataclass
class MonitorDecision:
    """AI decision for an already open position."""
    action: str = "HOLD"
    should_close: bool = False
    should_partial_close: bool = False
    should_protect_sl: bool = False
    close_reason: str = ""
    confidence: float = 0.0
    progress_to_tp1: float = 0.0
    weakness_confirmations: int = 0
    emergency: bool = False
    reason_codes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.action = safe_str(self.action, "HOLD").upper()
        self.confidence = safe_float(self.confidence, 0.0) or 0.0
        self.progress_to_tp1 = safe_float(self.progress_to_tp1, 0.0) or 0.0
        self.weakness_confirmations = safe_int(self.weakness_confirmations, 0) or 0


# =============================================================================
# Trade execution and position models
# =============================================================================

@dataclass
class TradeOpenResult:
    """Standard output of real_trade_manager.open_real_trade()."""
    status: str = STATUS_FAILED
    position_id: str = ""
    exchange_order_id: str = ""
    symbol: str = ""
    direction: str = ""
    entry: float = 0.0
    quantity: float = 0.0
    message: str = ""
    error: str = ""
    recovered: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.status = safe_str(self.status, STATUS_FAILED).upper()
        self.symbol = normalize_symbol(self.symbol)
        self.direction = normalize_direction(self.direction)
        self.entry = safe_float(self.entry, 0.0) or 0.0
        self.quantity = safe_float(self.quantity, 0.0) or 0.0


@dataclass
class TradeCloseResult:
    """Standard output of real_trade_manager.close_position()."""
    status: str = STATUS_FAILED
    position_id: str = ""
    exchange_order_id: str = ""
    symbol: str = ""
    direction: str = ""
    close_price: float = 0.0
    closed_quantity: float = 0.0
    pnl_usdt: Optional[float] = None
    pnl_confirmed: bool = False
    close_confirmed: bool = False
    message: str = ""
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.status = safe_str(self.status, STATUS_FAILED).upper()
        self.symbol = normalize_symbol(self.symbol)
        self.direction = normalize_direction(self.direction)
        self.close_price = safe_float(self.close_price, 0.0) or 0.0
        self.closed_quantity = safe_float(self.closed_quantity, 0.0) or 0.0
        self.pnl_usdt = safe_float(self.pnl_usdt, None)


@dataclass
class TradePosition:
    """Unified REAL/GHOST position model stored in positions.json."""
    symbol: str
    direction: str
    mode: str
    entry: float
    tp1: float
    sl: float
    position_id: str = ""
    signal_id: str = ""
    status: str = POSITION_PENDING_REAL_CONFIRM
    tp2: Optional[float] = None
    quantity: float = 0.0
    margin_usdt: float = 0.0
    leverage: int = 1
    current_price: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    exchange_symbol: str = ""
    okx_inst_id: str = ""
    exchange_order_id: str = ""
    signal_message_id: Optional[int] = None
    tp1_hit: bool = False
    tp2_hit: bool = False
    sl_hit: bool = False
    ai_exit_done: bool = False
    tp1_profit_locked: bool = False
    closed_quantity: float = 0.0
    runner_quantity: float = 0.0
    protected_sl: Optional[float] = None
    decision_metadata: dict[str, Any] = field(default_factory=dict)
    monitor_metadata: dict[str, Any] = field(default_factory=dict)
    level: int = 4
    system_version: str = SYSTEM_VERSION
    opened_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.symbol = normalize_symbol(self.symbol)
        self.direction = normalize_direction(self.direction)
        self.mode = safe_str(self.mode).upper()
        self.entry = safe_float(self.entry, 0.0) or 0.0
        self.tp1 = safe_float(self.tp1, 0.0) or 0.0
        self.tp2 = safe_float(self.tp2, None)
        self.sl = safe_float(self.sl, 0.0) or 0.0
        self.quantity = safe_float(self.quantity, 0.0) or 0.0
        self.margin_usdt = safe_float(self.margin_usdt, 0.0) or 0.0
        self.leverage = safe_int(self.leverage, 1) or 1
        self.current_price = safe_float(self.current_price, self.entry) or self.entry
        self.highest_price = safe_float(self.highest_price, self.entry) or self.entry
        self.lowest_price = safe_float(self.lowest_price, self.entry) or self.entry
        self.level = safe_int(self.level, 4) or 4
        if not self.position_id:
            self.position_id = make_position_id(self.symbol, self.direction, self.level)
        if not self.signal_id:
            self.signal_id = make_signal_id(self.symbol, self.direction, self.level)
        if not self.exchange_symbol:
            self.exchange_symbol = to_tobit_symbol(self.symbol)
        if not self.okx_inst_id:
            self.okx_inst_id = to_okx_inst_id(self.symbol)
        if self.mode == MODE_GHOST and self.status == POSITION_PENDING_REAL_CONFIRM:
            self.status = POSITION_ACTIVE_GHOST
        if self.mode == MODE_REAL and not self.status:
            self.status = POSITION_PENDING_REAL_CONFIRM


# =============================================================================
# Outcome / learning / monitor / telegram models
# =============================================================================

@dataclass
class TradeOutcome:
    """Closed or partially closed trade outcome."""
    position_id: str
    symbol: str
    direction: str
    event: str
    mode: str = MODE_GHOST
    entry: float = 0.0
    exit_price: float = 0.0
    quantity: float = 0.0
    pnl_usdt: Optional[float] = None
    pnl_pct: Optional[float] = None
    pnl_confirmed: bool = False
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None
    reason_codes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    level: int = 4
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.symbol = normalize_symbol(self.symbol)
        self.direction = normalize_direction(self.direction)
        self.event = safe_str(self.event).upper()
        self.mode = safe_str(self.mode).upper()
        self.entry = safe_float(self.entry, 0.0) or 0.0
        self.exit_price = safe_float(self.exit_price, 0.0) or 0.0
        self.quantity = safe_float(self.quantity, 0.0) or 0.0
        self.pnl_usdt = safe_float(self.pnl_usdt, None)
        self.pnl_pct = safe_float(self.pnl_pct, None)
        self.mfe_pct = safe_float(self.mfe_pct, None)
        self.mae_pct = safe_float(self.mae_pct, None)
        self.level = safe_int(self.level, 4) or 4


@dataclass
class LearningRecord:
    """One learning sample from REAL or GHOST outcome."""
    record_id: str
    symbol: str
    direction: str
    level: int
    event: str
    result: str
    indicators: dict[str, Any] = field(default_factory=dict)
    structure: dict[str, Any] = field(default_factory=dict)
    momentum: dict[str, Any] = field(default_factory=dict)
    liquidity: dict[str, Any] = field(default_factory=dict)
    market_context: dict[str, Any] = field(default_factory=dict)
    tp_sl: dict[str, Any] = field(default_factory=dict)
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None
    pnl_usdt: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if not self.record_id:
            self.record_id = make_event_id("learning")
        self.symbol = normalize_symbol(self.symbol)
        self.direction = normalize_direction(self.direction)
        self.level = safe_int(self.level, 4) or 4
        self.event = safe_str(self.event).upper()
        self.result = safe_str(self.result).upper()
        self.mfe_pct = safe_float(self.mfe_pct, None)
        self.mae_pct = safe_float(self.mae_pct, None)
        self.pnl_usdt = safe_float(self.pnl_usdt, None)


@dataclass
class MonitorEvent:
    """Position monitor output event. bot.py decides how/when to send it."""
    event: str
    position_id: str
    symbol: str
    direction: str
    mode: str = MODE_GHOST
    status: str = STATUS_OK
    message_key: str = ""
    reply_to_message_id: Optional[int] = None
    outcome: Optional[TradeOutcome] = None
    close_result: Optional[TradeCloseResult] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    event_id: str = ""
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.event = safe_str(self.event).upper()
        self.symbol = normalize_symbol(self.symbol)
        self.direction = normalize_direction(self.direction)
        self.mode = safe_str(self.mode).upper()
        self.status = safe_str(self.status, STATUS_OK).upper()
        if not self.event_id:
            self.event_id = make_event_id(self.event)


@dataclass
class TelegramMessagePlan:
    """telegram_ui.py output contract."""
    text: str
    parse_mode: Optional[str] = None
    reply_to_message_id: Optional[int] = None
    disable_web_page_preview: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)


@dataclass
class MarketDataResult:
    """Standard market_data.py function output."""
    status: str = STATUS_FAILED
    symbol: str = ""
    timeframe: str = ""
    snapshot: Optional[MarketSnapshot] = None
    message: str = ""
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.status = safe_str(self.status, STATUS_FAILED).upper()
        self.symbol = normalize_symbol(self.symbol)
        self.timeframe = safe_str(self.timeframe)


@dataclass
class RecordResult:
    """Standard learning_memory.record_* output."""
    status: str = STATUS_FAILED
    recorded: bool = False
    record_id: str = ""
    message: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    system_version: str = SYSTEM_VERSION
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.status = safe_str(self.status, STATUS_FAILED).upper()


# =============================================================================
# Lightweight constructors
# =============================================================================

def make_reject_decision(symbol: str, direction: str = "", reason: str = "", score: float = 0.0) -> AIDecision:
    """Create a standard REJECT decision."""
    return AIDecision(
        symbol=symbol,
        direction=direction,
        mode=MODE_REJECT,
        score=score,
        confidence=0.0,
        reject_reason=reason,
        reason_codes=[reason] if reason else [],
    )


def make_hold_decision(reason: str = "") -> MonitorDecision:
    """Create a standard HOLD monitor decision."""
    return MonitorDecision(
        action="HOLD",
        should_close=False,
        close_reason=reason,
        reason_codes=[reason] if reason else [],
    )


def make_error_event(position: TradePosition, error: str) -> MonitorEvent:
    """Create a monitor ERROR event for a position."""
    return MonitorEvent(
        event=EVENT_ERROR,
        position_id=position.position_id,
        symbol=position.symbol,
        direction=position.direction,
        mode=position.mode,
        status=STATUS_FAILED,
        message_key="error",
        reply_to_message_id=position.signal_message_id,
        metadata={"error": error},
    )


VALID_TRADE_EVENTS = {
    EVENT_SIGNAL_CREATED,
    EVENT_REAL_OPEN_REQUESTED,
    EVENT_REAL_OPEN_CONFIRMED,
    EVENT_REAL_OPEN_FAILED,
    EVENT_GHOST_OPENED,
    EVENT_REJECTED,
    EVENT_TP1,
    EVENT_TP2,
    EVENT_SL,
    EVENT_AI_EXIT,
    EVENT_MANUAL_CLOSE,
    EVENT_CLOSE_REQUESTED,
    EVENT_CLOSE_CONFIRMED,
    EVENT_ERROR,
}

VALID_POSITION_STATUSES = {
    POSITION_PENDING_REAL_CONFIRM,
    POSITION_ACTIVE_REAL,
    POSITION_ACTIVE_GHOST,
    POSITION_PARTIAL_TP1,
    POSITION_CLOSED,
    POSITION_FAILED,
}

VALID_OUTPUT_STATUSES = {
    STATUS_OK,
    STATUS_FAILED,
    STATUS_SKIPPED,
}


__all__ = [
    "MODELS_VERSION",
    "to_dict",
    "from_dict",
    "model_version_payload",
    "Candle",
    "MarketSnapshot",
    "SensorSnapshot",
    "StructureSnapshot",
    "MomentumSnapshot",
    "LiquiditySnapshot",
    "MarketContextSnapshot",
    "TPSLPlan",
    "AIDecision",
    "MonitorDecision",
    "TradeOpenResult",
    "TradeCloseResult",
    "TradePosition",
    "TradeOutcome",
    "LearningRecord",
    "MonitorEvent",
    "TelegramMessagePlan",
    "MarketDataResult",
    "RecordResult",
    "make_reject_decision",
    "make_hold_decision",
    "make_error_event",
    "VALID_TRADE_EVENTS",
    "VALID_POSITION_STATUSES",
    "VALID_OUTPUT_STATUSES",
]
