from __future__ import annotations

"""
07 - analysis_engine.py

Candidate analysis builder for the locked Movement Hunter architecture.

Responsibilities:
- Consume raw technical SensorSnapshot objects from 06-analysis_layers.py.
- Convert sensors into an AnalysisCandidate.
- Create direction hints and quality/risk context for AI.
- Prepare structured inputs for:
  08 movement_hunter.py
  09 trap_engine.py
  10 state_engine.py
  18 ai_decision_engine.py

Strictly forbidden:
- No REAL/GHOST/REJECT.
- No trade execution.
- No Toobit API calls.
- No Telegram.
- No persistence.
- No Paper mode.
- No Setup flow.

Important:
analysis_engine.py does NOT decide signals.
It only prepares candidate analysis from sensors.
AI decision happens only in ai_decision_engine.py.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4
import time

from analysis_layers import SensorSnapshot, build_sensor_snapshot
from market_context import get_market_context


JsonDict = Dict[str, Any]


DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"

BIAS_BULLISH = "BULLISH"
BIAS_BEARISH = "BEARISH"
BIAS_NEUTRAL = "NEUTRAL"

QUALITY_LOW = "LOW"
QUALITY_MEDIUM = "MEDIUM"
QUALITY_HIGH = "HIGH"

RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"


@dataclass(frozen=True)
class DirectionScore:
    long_score: float
    short_score: float
    direction_hint: str
    bias: str
    gap: float

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class RiskProfile:
    range_risk: float
    trap_risk: float
    exhaustion_risk: float
    late_move_risk: float
    liquidity_risk: float
    total_risk: float
    risk_level: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class QualityProfile:
    trend_quality: float
    momentum_quality: float
    volatility_quality: float
    volume_quality: float
    power_quality: float
    candle_quality: float
    total_quality: float
    quality_level: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class AnalysisCandidate:
    candidate_id: str
    symbol: str
    timeframe: str
    timestamp: int
    direction_hint: str
    bias: str
    direction_score: DirectionScore
    quality: QualityProfile
    risk: RiskProfile
    sensor_snapshot: SensorSnapshot
    market_context: JsonDict = field(default_factory=dict)
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
            "direction_score": self.direction_score.to_dict(),
            "quality": self.quality.to_dict(),
            "risk": self.risk.to_dict(),
            "sensor_snapshot": self.sensor_snapshot.to_dict(),
            "market_context": self.market_context,
            "reason_codes": list(self.reason_codes),
            "warnings": list(self.warnings),
            "valid": self.valid,
        }


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def avg(values: Sequence[float]) -> float:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _score_ema(snapshot: SensorSnapshot) -> Tuple[float, float, List[str]]:
    long_score = 0.0
    short_score = 0.0
    reasons: List[str] = []

    state = str(snapshot.ema_state).upper()
    if state in {"ABOVE", "CROSS_UP"}:
        long_score += 14
        reasons.append("EMA_BULLISH")
    elif state in {"BELOW", "CROSS_DOWN"}:
        short_score += 14
        reasons.append("EMA_BEARISH")

    if snapshot.price > snapshot.ema_fast > snapshot.ema_slow:
        long_score += 8
        reasons.append("PRICE_ABOVE_EMA_STACK")
    elif snapshot.price < snapshot.ema_fast < snapshot.ema_slow:
        short_score += 8
        reasons.append("PRICE_BELOW_EMA_STACK")

    return long_score, short_score, reasons


def _score_vwap(snapshot: SensorSnapshot) -> Tuple[float, float, List[str]]:
    long_score = 0.0
    short_score = 0.0
    reasons: List[str] = []

    state = str(snapshot.vwap_state).upper()
    if state in {"ABOVE", "RECLAIM"}:
        long_score += 12
        reasons.append("VWAP_BULLISH")
    elif state in {"BELOW", "LOSS"}:
        short_score += 12
        reasons.append("VWAP_BEARISH")

    if snapshot.vwap_distance_percent > 0.05:
        long_score += 3
    elif snapshot.vwap_distance_percent < -0.05:
        short_score += 3

    return long_score, short_score, reasons


def _score_rsi(snapshot: SensorSnapshot) -> Tuple[float, float, List[str]]:
    long_score = 0.0
    short_score = 0.0
    reasons: List[str] = []

    rsi = snapshot.rsi
    if 45 <= rsi <= 68:
        long_score += 6
    if 32 <= rsi <= 55:
        short_score += 6

    if snapshot.rsi_slope > 0.3:
        long_score += 10
        reasons.append("RSI_SLOPE_UP")
    elif snapshot.rsi_slope < -0.3:
        short_score += 10
        reasons.append("RSI_SLOPE_DOWN")

    if snapshot.rsi_acceleration > 0.15:
        long_score += 4
        reasons.append("RSI_ACCEL_UP")
    elif snapshot.rsi_acceleration < -0.15:
        short_score += 4
        reasons.append("RSI_ACCEL_DOWN")

    if rsi > 76 and snapshot.rsi_slope < 0:
        short_score += 5
        reasons.append("RSI_OVERBOUGHT_WEAKENING")
    elif rsi < 24 and snapshot.rsi_slope > 0:
        long_score += 5
        reasons.append("RSI_OVERSOLD_RECOVERY")

    return long_score, short_score, reasons


def _score_macd(snapshot: SensorSnapshot) -> Tuple[float, float, List[str]]:
    long_score = 0.0
    short_score = 0.0
    reasons: List[str] = []

    if snapshot.macd_histogram > 0:
        long_score += 6
        reasons.append("MACD_HIST_POSITIVE")
    elif snapshot.macd_histogram < 0:
        short_score += 6
        reasons.append("MACD_HIST_NEGATIVE")

    if snapshot.histogram_slope > 0:
        long_score += 8
        reasons.append("MACD_HIST_SLOPE_UP")
    elif snapshot.histogram_slope < 0:
        short_score += 8
        reasons.append("MACD_HIST_SLOPE_DOWN")

    if snapshot.histogram_acceleration > 0:
        long_score += 6
        reasons.append("MACD_ACCEL_UP")
    elif snapshot.histogram_acceleration < 0:
        short_score += 6
        reasons.append("MACD_ACCEL_DOWN")

    return long_score, short_score, reasons


def _score_power(snapshot: SensorSnapshot) -> Tuple[float, float, List[str]]:
    long_score = 0.0
    short_score = 0.0
    reasons: List[str] = []

    if snapshot.power_delta > 10:
        long_score += 12
        reasons.append("BUY_POWER_DOMINANT")
    elif snapshot.power_delta < -10:
        short_score += 12
        reasons.append("SELL_POWER_DOMINANT")

    if snapshot.close_quality > 0.70:
        long_score += 4
        reasons.append("STRONG_CLOSE")
    elif snapshot.close_quality < 0.30:
        short_score += 4
        reasons.append("WEAK_CLOSE")

    return long_score, short_score, reasons


def build_direction_score(snapshot: SensorSnapshot) -> Tuple[DirectionScore, Tuple[str, ...]]:
    long_total = 0.0
    short_total = 0.0
    reasons: List[str] = []

    for fn in (_score_ema, _score_vwap, _score_rsi, _score_macd, _score_power):
        l, s, r = fn(snapshot)
        long_total += l
        short_total += s
        reasons.extend(r)

    if snapshot.plus_di > snapshot.minus_di and snapshot.adx >= 18:
        long_total += 6
        reasons.append("DI_BULLISH")
    elif snapshot.minus_di > snapshot.plus_di and snapshot.adx >= 18:
        short_total += 6
        reasons.append("DI_BEARISH")

    long_total = clamp(long_total)
    short_total = clamp(short_total)
    gap = abs(long_total - short_total)

    if gap < 4:
        direction = DIRECTION_NEUTRAL
        bias = BIAS_NEUTRAL
    elif long_total > short_total:
        direction = DIRECTION_LONG
        bias = BIAS_BULLISH
    else:
        direction = DIRECTION_SHORT
        bias = BIAS_BEARISH

    return DirectionScore(
        long_score=long_total,
        short_score=short_total,
        direction_hint=direction,
        bias=bias,
        gap=gap,
    ), tuple(reasons)


def build_quality_profile(snapshot: SensorSnapshot) -> QualityProfile:
    trend_quality = 0.0
    if snapshot.trend_strength == "STRONG":
        trend_quality = 80.0
    elif snapshot.trend_strength == "NORMAL":
        trend_quality = 55.0
    else:
        trend_quality = 25.0

    momentum_quality = clamp(
        abs(snapshot.rsi_slope) * 8
        + abs(snapshot.histogram_slope) * 1000
        + abs(snapshot.histogram_acceleration) * 1000
    )

    volatility_quality = 45.0
    if snapshot.atr_explosion:
        volatility_quality = 85.0
    elif snapshot.atr_expansion == "EXPANDING":
        volatility_quality = 70.0
    elif snapshot.atr_expansion == "SHRINKING":
        volatility_quality = 25.0

    volume_quality = clamp(snapshot.relative_volume * 35.0)
    if snapshot.volume_spike:
        volume_quality = max(volume_quality, 85.0)
    elif snapshot.volume_expansion:
        volume_quality = max(volume_quality, 65.0)

    power_quality = clamp(abs(snapshot.power_delta) * 1.5)
    candle_quality = clamp(abs(snapshot.close_quality - 0.5) * 200)

    total = avg([
        trend_quality,
        momentum_quality,
        volatility_quality,
        volume_quality,
        power_quality,
        candle_quality,
    ])

    if total >= 70:
        level = QUALITY_HIGH
    elif total >= 45:
        level = QUALITY_MEDIUM
    else:
        level = QUALITY_LOW

    return QualityProfile(
        trend_quality=clamp(trend_quality),
        momentum_quality=clamp(momentum_quality),
        volatility_quality=clamp(volatility_quality),
        volume_quality=clamp(volume_quality),
        power_quality=clamp(power_quality),
        candle_quality=clamp(candle_quality),
        total_quality=clamp(total),
        quality_level=level,
    )


def build_risk_profile(snapshot: SensorSnapshot) -> RiskProfile:
    range_risk = clamp(snapshot.range_probability)
    trap_risk = 0.0
    if snapshot.failed_breakout or snapshot.failed_breakdown:
        trap_risk += 45
    trap_risk += snapshot.stop_hunt_probability * 0.55
    trap_risk = clamp(trap_risk)

    exhaustion_risk = 0.0
    if snapshot.bull_exhaustion or snapshot.bear_exhaustion:
        exhaustion_risk += 70
    if snapshot.momentum_weakness:
        exhaustion_risk += 20
    exhaustion_risk = clamp(exhaustion_risk)

    late_move_risk = 0.0
    if abs(snapshot.price_change_percent) > max(snapshot.atr_percent * 2.0, 1.2):
        late_move_risk += 40
    if snapshot.atr_explosion and snapshot.volume_spike and snapshot.momentum_weakness:
        late_move_risk += 25
    late_move_risk = clamp(late_move_risk)

    liquidity_risk = clamp(snapshot.stop_hunt_probability)

    total = avg([
        range_risk,
        trap_risk,
        exhaustion_risk,
        late_move_risk,
        liquidity_risk,
    ])

    if total >= 65:
        level = RISK_HIGH
    elif total >= 35:
        level = RISK_MEDIUM
    else:
        level = RISK_LOW

    return RiskProfile(
        range_risk=range_risk,
        trap_risk=trap_risk,
        exhaustion_risk=exhaustion_risk,
        late_move_risk=late_move_risk,
        liquidity_risk=liquidity_risk,
        total_risk=clamp(total),
        risk_level=level,
    )


class AnalysisEngine:
    """
    Converts SensorSnapshot into AnalysisCandidate.

    This is still not a signal. It is a structured candidate for AI.
    """

    def build_candidate(
        self,
        snapshot: SensorSnapshot,
        market_context: Optional[Any] = None,
    ) -> AnalysisCandidate:
        direction_score, direction_reasons = build_direction_score(snapshot)
        quality = build_quality_profile(snapshot)
        risk = build_risk_profile(snapshot)

        warnings: List[str] = list(snapshot.warnings)
        valid = bool(snapshot.valid)

        if direction_score.direction_hint == DIRECTION_NEUTRAL:
            warnings.append("NO_CLEAR_DIRECTION")

        if risk.risk_level == RISK_HIGH:
            warnings.append("HIGH_RISK_CANDIDATE")

        ctx_dict: JsonDict = {}
        if market_context is not None:
            if hasattr(market_context, "to_dict") and callable(market_context.to_dict):
                ctx_dict = market_context.to_dict()
            elif isinstance(market_context, dict):
                ctx_dict = dict(market_context)
            else:
                ctx_dict = dict(getattr(market_context, "__dict__", {}))

        return AnalysisCandidate(
            candidate_id=f"cand_{uuid4().hex}",
            symbol=snapshot.symbol,
            timeframe=snapshot.timeframe,
            timestamp=snapshot.timestamp or int(time.time()),
            direction_hint=direction_score.direction_hint,
            bias=direction_score.bias,
            direction_score=direction_score,
            quality=quality,
            risk=risk,
            sensor_snapshot=snapshot,
            market_context=ctx_dict,
            reason_codes=direction_reasons,
            warnings=tuple(warnings),
            valid=valid,
        )

    def analyze(
        self,
        symbol: str,
        timeframe: str,
        candles: Sequence[Any],
        market_context: Optional[Any] = None,
    ) -> AnalysisCandidate:
        if market_context is None:
            try:
                market_context = get_market_context()
            except Exception:
                market_context = None

        snapshot = build_sensor_snapshot(
            symbol=symbol,
            timeframe=timeframe,
            candles=candles,
            market_context=market_context,
        )
        return self.build_candidate(snapshot, market_context=market_context)


_default_engine: Optional[AnalysisEngine] = None


def engine() -> AnalysisEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = AnalysisEngine()
    return _default_engine


def build_analysis_candidate(snapshot: SensorSnapshot, market_context: Optional[Any] = None) -> AnalysisCandidate:
    return engine().build_candidate(snapshot, market_context=market_context)


def analyze_symbol(
    symbol: str,
    timeframe: str,
    candles: Sequence[Any],
    market_context: Optional[Any] = None,
) -> AnalysisCandidate:
    return engine().analyze(symbol=symbol, timeframe=timeframe, candles=candles, market_context=market_context)


def analyze_multi_timeframe(
    symbol: str,
    timeframe_candles: Dict[str, Sequence[Any]],
    market_context: Optional[Any] = None,
) -> Dict[str, AnalysisCandidate]:
    result: Dict[str, AnalysisCandidate] = {}
    for timeframe, candles in timeframe_candles.items():
        result[timeframe] = analyze_symbol(
            symbol=symbol,
            timeframe=timeframe,
            candles=candles,
            market_context=market_context,
        )
    return result
