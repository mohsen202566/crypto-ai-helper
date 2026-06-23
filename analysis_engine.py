from __future__ import annotations

"""
07 - analysis_engine.py

Simplified technical sensor wrapper for the Level 1 / 5M crypto futures bot.

Locked goals:
- Technical analysis is raw sensor data only.
- No classic signal scoring.
- No REAL / GHOST / REJECT.
- No trap/confidence/correlation/meta/state engine.
- No Telegram, no Toobit, no persistence.
- AI decision engine is the only final decision maker.
- Pattern Start Layer and AI use this file's structured sensor package.

This file only:
1) builds a SensorSnapshot from candles,
2) extracts lightweight directional pressure from raw sensors,
3) packages sensor values, slopes, acceleration, warnings and market mode.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple
from uuid import uuid4
import time

from analysis_layers import SensorSnapshot, build_sensor_snapshot
from market_data import get_market_mode


JsonDict = Dict[str, Any]

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"

BIAS_BULLISH = "BULLISH"
BIAS_BEARISH = "BEARISH"
BIAS_NEUTRAL = "NEUTRAL"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


@dataclass(frozen=True)
class SensorDirectionHint:
    """Lightweight direction pressure from raw sensors.

    This is NOT a signal and NOT final direction.
    It only tells AI which side sensors are currently strengthening toward.
    """

    long_pressure: float
    short_pressure: float
    direction_hint: str
    bias: str
    gap: float
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class SensorMomentumState:
    """Raw start-movement sensor state for Pattern Layer and AI."""

    rsi: float
    rsi_slope: float
    rsi_acceleration: float

    macd: float
    macd_signal: float
    macd_histogram: float
    histogram_slope: float
    histogram_acceleration: float

    adx: float
    adx_slope: float
    plus_di: float
    minus_di: float

    buy_power: float
    sell_power: float
    power_delta: float

    relative_volume: float
    volume_expansion: bool
    volume_spike: bool

    atr_percent: float
    atr_slope: float
    atr_expansion: str
    atr_explosion: bool

    ema_state: str
    vwap_state: str
    vwap_distance_percent: float

    price_change_percent: float
    range_probability: float
    compression_score: float

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class AnalysisCandidate:
    """Structured sensor package for downstream AI and Pattern Layer."""

    candidate_id: str
    symbol: str
    timeframe: str
    timestamp: int
    direction_hint: str
    bias: str
    sensor_direction: SensorDirectionHint
    momentum_state: SensorMomentumState
    sensor_snapshot: SensorSnapshot
    market_mode: JsonDict = field(default_factory=dict)
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    valid: bool = True

    def to_dict(self) -> JsonDict:
        return {
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp,
            "direction_hint": self.direction_hint,
            "bias": self.bias,
            "sensor_direction": self.sensor_direction.to_dict(),
            "momentum_state": self.momentum_state.to_dict(),
            "sensor_snapshot": self.sensor_snapshot.to_dict(),
            "market_mode": self.market_mode,
            "reason_codes": list(self.reason_codes),
            "warnings": list(self.warnings),
            "valid": self.valid,
        }


# ---------------------------------------------------------------------------
# Raw sensor extraction
# ---------------------------------------------------------------------------

def build_momentum_state(snapshot: SensorSnapshot) -> SensorMomentumState:
    return SensorMomentumState(
        rsi=safe_float(getattr(snapshot, "rsi", 0.0)),
        rsi_slope=safe_float(getattr(snapshot, "rsi_slope", 0.0)),
        rsi_acceleration=safe_float(getattr(snapshot, "rsi_acceleration", 0.0)),

        macd=safe_float(getattr(snapshot, "macd", 0.0)),
        macd_signal=safe_float(getattr(snapshot, "macd_signal", 0.0)),
        macd_histogram=safe_float(getattr(snapshot, "macd_histogram", 0.0)),
        histogram_slope=safe_float(getattr(snapshot, "histogram_slope", 0.0)),
        histogram_acceleration=safe_float(getattr(snapshot, "histogram_acceleration", 0.0)),

        adx=safe_float(getattr(snapshot, "adx", 0.0)),
        adx_slope=safe_float(getattr(snapshot, "adx_slope", 0.0)),
        plus_di=safe_float(getattr(snapshot, "plus_di", 0.0)),
        minus_di=safe_float(getattr(snapshot, "minus_di", 0.0)),

        buy_power=safe_float(getattr(snapshot, "buy_power", 0.0)),
        sell_power=safe_float(getattr(snapshot, "sell_power", 0.0)),
        power_delta=safe_float(getattr(snapshot, "power_delta", 0.0)),

        relative_volume=safe_float(getattr(snapshot, "relative_volume", 0.0)),
        volume_expansion=bool(getattr(snapshot, "volume_expansion", False)),
        volume_spike=bool(getattr(snapshot, "volume_spike", False)),

        atr_percent=safe_float(getattr(snapshot, "atr_percent", 0.0)),
        atr_slope=safe_float(getattr(snapshot, "atr_slope", 0.0)),
        atr_expansion=str(getattr(snapshot, "atr_expansion", "")),
        atr_explosion=bool(getattr(snapshot, "atr_explosion", False)),

        ema_state=str(getattr(snapshot, "ema_state", "")),
        vwap_state=str(getattr(snapshot, "vwap_state", "")),
        vwap_distance_percent=safe_float(getattr(snapshot, "vwap_distance_percent", 0.0)),

        price_change_percent=safe_float(getattr(snapshot, "price_change_percent", 0.0)),
        range_probability=safe_float(getattr(snapshot, "range_probability", 0.0)),
        compression_score=safe_float(getattr(snapshot, "compression_score", 0.0)),
    )


def build_sensor_direction_hint(snapshot: SensorSnapshot) -> SensorDirectionHint:
    """Create a lightweight sensor direction hint without classic scoring.

    It uses only immediate slope/acceleration/power alignment. AI can ignore or
    override it. This prevents the old classic engine from becoming decision maker.
    """
    long_pressure = 0.0
    short_pressure = 0.0
    reasons = []

    rsi_slope = safe_float(getattr(snapshot, "rsi_slope", 0.0))
    rsi_acc = safe_float(getattr(snapshot, "rsi_acceleration", 0.0))
    hist_slope = safe_float(getattr(snapshot, "histogram_slope", 0.0))
    hist_acc = safe_float(getattr(snapshot, "histogram_acceleration", 0.0))
    power_delta = safe_float(getattr(snapshot, "power_delta", 0.0))
    adx = safe_float(getattr(snapshot, "adx", 0.0))
    adx_slope = safe_float(getattr(snapshot, "adx_slope", 0.0))
    plus_di = safe_float(getattr(snapshot, "plus_di", 0.0))
    minus_di = safe_float(getattr(snapshot, "minus_di", 0.0))

    if rsi_slope > 0:
        long_pressure += min(18.0, abs(rsi_slope) * 5.0)
        reasons.append("RSI_SLOPE_UP")
    elif rsi_slope < 0:
        short_pressure += min(18.0, abs(rsi_slope) * 5.0)
        reasons.append("RSI_SLOPE_DOWN")

    if rsi_acc > 0:
        long_pressure += min(10.0, abs(rsi_acc) * 5.0)
        reasons.append("RSI_ACCEL_UP")
    elif rsi_acc < 0:
        short_pressure += min(10.0, abs(rsi_acc) * 5.0)
        reasons.append("RSI_ACCEL_DOWN")

    if hist_slope > 0:
        long_pressure += min(18.0, abs(hist_slope) * 1000.0)
        reasons.append("HIST_SLOPE_UP")
    elif hist_slope < 0:
        short_pressure += min(18.0, abs(hist_slope) * 1000.0)
        reasons.append("HIST_SLOPE_DOWN")

    if hist_acc > 0:
        long_pressure += min(12.0, abs(hist_acc) * 1000.0)
        reasons.append("HIST_ACCEL_UP")
    elif hist_acc < 0:
        short_pressure += min(12.0, abs(hist_acc) * 1000.0)
        reasons.append("HIST_ACCEL_DOWN")

    if power_delta > 0:
        long_pressure += min(20.0, abs(power_delta) * 0.8)
        reasons.append("BUY_POWER_RISING")
    elif power_delta < 0:
        short_pressure += min(20.0, abs(power_delta) * 0.8)
        reasons.append("SELL_POWER_RISING")

    if adx >= 18 and adx_slope >= 0:
        if plus_di > minus_di:
            long_pressure += 10.0
            reasons.append("DI_ADX_LONG_PRESSURE")
        elif minus_di > plus_di:
            short_pressure += 10.0
            reasons.append("DI_ADX_SHORT_PRESSURE")

    # VWAP/EMA are context sensors, not final signal.
    ema_state = str(getattr(snapshot, "ema_state", "")).upper()
    vwap_state = str(getattr(snapshot, "vwap_state", "")).upper()
    if ema_state in {"ABOVE", "CROSS_UP"}:
        long_pressure += 5.0
        reasons.append("EMA_CONTEXT_LONG")
    elif ema_state in {"BELOW", "CROSS_DOWN"}:
        short_pressure += 5.0
        reasons.append("EMA_CONTEXT_SHORT")

    if vwap_state in {"ABOVE", "RECLAIM"}:
        long_pressure += 5.0
        reasons.append("VWAP_CONTEXT_LONG")
    elif vwap_state in {"BELOW", "LOSS"}:
        short_pressure += 5.0
        reasons.append("VWAP_CONTEXT_SHORT")

    long_pressure = clamp(long_pressure)
    short_pressure = clamp(short_pressure)
    gap = abs(long_pressure - short_pressure)

    if gap < 6.0:
        direction = DIRECTION_NEUTRAL
        bias = BIAS_NEUTRAL
    elif long_pressure > short_pressure:
        direction = DIRECTION_LONG
        bias = BIAS_BULLISH
    else:
        direction = DIRECTION_SHORT
        bias = BIAS_BEARISH

    return SensorDirectionHint(
        long_pressure=long_pressure,
        short_pressure=short_pressure,
        direction_hint=direction,
        bias=bias,
        gap=gap,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class AnalysisEngine:
    """Builds sensor packages only. It never decides trades."""

    def build_candidate(
        self,
        snapshot: SensorSnapshot,
        market_mode: Optional[Any] = None,
    ) -> AnalysisCandidate:
        direction = build_sensor_direction_hint(snapshot)
        momentum = build_momentum_state(snapshot)

        warnings = list(getattr(snapshot, "warnings", ()) or [])
        valid = bool(getattr(snapshot, "valid", True))

        if direction.direction_hint == DIRECTION_NEUTRAL:
            warnings.append("SENSOR_DIRECTION_NEUTRAL")

        market_mode_dict: JsonDict = {}
        if market_mode is not None:
            if hasattr(market_mode, "to_dict") and callable(market_mode.to_dict):
                market_mode_dict = market_mode.to_dict()
            elif isinstance(market_mode, dict):
                market_mode_dict = dict(market_mode)
            else:
                market_mode_dict = dict(getattr(market_mode, "__dict__", {}))

        return AnalysisCandidate(
            candidate_id=f"cand_{uuid4().hex}",
            symbol=str(getattr(snapshot, "symbol", "")),
            timeframe=str(getattr(snapshot, "timeframe", "5m")),
            timestamp=int(getattr(snapshot, "timestamp", 0) or time.time()),
            direction_hint=direction.direction_hint,
            bias=direction.bias,
            sensor_direction=direction,
            momentum_state=momentum,
            sensor_snapshot=snapshot,
            market_mode=market_mode_dict,
            reason_codes=direction.reason_codes,
            warnings=tuple(dict.fromkeys(warnings)),
            valid=valid,
        )

    def analyze(
        self,
        symbol: str,
        timeframe: str,
        candles: Sequence[Any],
        market_mode: Optional[Any] = None,
    ) -> AnalysisCandidate:
        if market_mode is None:
            try:
                market_mode = get_market_mode()
            except Exception:
                market_mode = None

        snapshot = build_sensor_snapshot(
            symbol=symbol,
            timeframe=timeframe or "5m",
            candles=candles,
            market_context=market_mode,
        )
        return self.build_candidate(snapshot, market_mode=market_mode)


_default_engine: Optional[AnalysisEngine] = None


def engine() -> AnalysisEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = AnalysisEngine()
    return _default_engine


def build_analysis_candidate(snapshot: SensorSnapshot, market_context: Optional[Any] = None) -> AnalysisCandidate:
    # market_context name kept for backward compatibility while files are rewritten.
    return engine().build_candidate(snapshot, market_mode=market_context)


def analyze_symbol(
    symbol: str,
    timeframe: str,
    candles: Sequence[Any],
    market_context: Optional[Any] = None,
) -> AnalysisCandidate:
    return engine().analyze(symbol=symbol, timeframe=timeframe or "5m", candles=candles, market_mode=market_context)


def analyze_multi_timeframe(
    symbol: str,
    timeframe_candles: Dict[str, Sequence[Any]],
    market_context: Optional[Any] = None,
) -> Dict[str, AnalysisCandidate]:
    # Level 1 is 5m-first. This helper remains only for compatibility.
    result: Dict[str, AnalysisCandidate] = {}
    for timeframe, candles in timeframe_candles.items():
        result[timeframe] = analyze_symbol(
            symbol=symbol,
            timeframe=timeframe or "5m",
            candles=candles,
            market_context=market_context,
        )
    return result
