from __future__ import annotations

"""
16 - movement_predictor.py

Simplified Pattern Start Predictor for the Level 1 / 5M crypto futures bot.

Locked goals:
- Predict pre-pump / pre-dump start conditions from raw technical sensors.
- Use Pattern Start Layer / learning summaries when available.
- Technical data is only sensor input.
- No REAL / GHOST / REJECT final decision.
- No trap/confidence/correlation/meta/state engine.
- No Toobit, no Telegram, no persistence writes.
- AI decision engine remains the only final decision maker.

This file answers:
"Does this look like the beginning of a pump/dump for this coin/direction?"
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
import math
import time

from analysis_engine import AnalysisCandidate


JsonDict = Dict[str, Any]

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"

MOVE_PUMP = "PUMP"
MOVE_DUMP = "DUMP"
MOVE_NONE = "NONE"

PREDICT_PRE_START = "PRE_START"
PREDICT_START = "START"
PREDICT_MID = "MID"
PREDICT_LATE = "LATE"
PREDICT_RANGE = "RANGE"
PREDICT_UNKNOWN = "UNKNOWN"

CONF_LOW = "LOW"
CONF_MEDIUM = "MEDIUM"
CONF_HIGH = "HIGH"
CONF_UNKNOWN = "UNKNOWN"


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
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, safe_float(value, low)))


def normalize_direction(direction: str) -> str:
    d = str(direction or "").upper().strip()
    if d in {"LONG", "BUY"}:
        return DIRECTION_LONG
    if d in {"SHORT", "SELL"}:
        return DIRECTION_SHORT
    return DIRECTION_NEUTRAL


def movement_type_from_direction(direction: str) -> str:
    d = normalize_direction(direction)
    if d == DIRECTION_LONG:
        return MOVE_PUMP
    if d == DIRECTION_SHORT:
        return MOVE_DUMP
    return MOVE_NONE


def obj_value(obj: Optional[Any], key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


@dataclass(frozen=True)
class PredictorBreakdown:
    pattern_match_score: float
    live_sensor_acceleration: float
    direction_pressure: float
    market_mode_score: float
    range_penalty: float
    late_penalty: float
    final_score: float

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class MovementPredictionResult:
    prediction_id: str
    symbol: str
    timeframe: str
    timestamp: int
    direction_hint: str
    predicted_movement_type: str
    predicted_phase: str

    pump_probability: float
    dump_probability: float
    movement_probability: float

    pattern_match_score: float
    pattern_confidence: float
    matched_pattern_id: str
    pattern_count: int
    pattern_win_rate: float

    expected_move_percent: float
    expected_pullback_percent: float
    expected_duration_seconds: float
    best_entry_zone: str

    confidence_level: str
    should_prefer_ghost_if_uncertain: bool
    breakdown: PredictorBreakdown

    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    valid: bool = True

    def to_dict(self) -> JsonDict:
        return {
            "prediction_id": self.prediction_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp,
            "direction_hint": self.direction_hint,
            "predicted_movement_type": self.predicted_movement_type,
            "predicted_phase": self.predicted_phase,
            "pump_probability": self.pump_probability,
            "dump_probability": self.dump_probability,
            "movement_probability": self.movement_probability,
            "pattern_match_score": self.pattern_match_score,
            "pattern_confidence": self.pattern_confidence,
            "matched_pattern_id": self.matched_pattern_id,
            "pattern_count": self.pattern_count,
            "pattern_win_rate": self.pattern_win_rate,
            "expected_move_percent": self.expected_move_percent,
            "expected_pullback_percent": self.expected_pullback_percent,
            "expected_duration_seconds": self.expected_duration_seconds,
            "best_entry_zone": self.best_entry_zone,
            "confidence_level": self.confidence_level,
            "should_prefer_ghost_if_uncertain": self.should_prefer_ghost_if_uncertain,
            "breakdown": self.breakdown.to_dict(),
            "reason_codes": list(self.reason_codes),
            "warnings": list(self.warnings),
            "valid": self.valid,
        }


# ---------------------------------------------------------------------------
# Pattern and sensor scoring
# ---------------------------------------------------------------------------

def extract_pattern_metrics(pattern_summary: Optional[Any]) -> Dict[str, Any]:
    """Read Pattern Start / learning output without importing that layer.

    Supported aliases are intentional because coin_learning.py and the future
    Pattern Start Layer may expose slightly different field names.
    """
    pattern_match_score = (
        obj_value(pattern_summary, "pattern_match_score", None)
        if obj_value(pattern_summary, "pattern_match_score", None) is not None
        else obj_value(pattern_summary, "fuzzy_match_score", 0.0)
    )
    pattern_confidence = (
        obj_value(pattern_summary, "pattern_confidence", None)
        if obj_value(pattern_summary, "pattern_confidence", None) is not None
        else obj_value(pattern_summary, "confidence_score", 0.0)
    )
    pattern_count = (
        obj_value(pattern_summary, "pattern_count", None)
        if obj_value(pattern_summary, "pattern_count", None) is not None
        else obj_value(pattern_summary, "sample_count", 0)
    )
    pattern_win_rate = (
        obj_value(pattern_summary, "pattern_win_rate", None)
        if obj_value(pattern_summary, "pattern_win_rate", None) is not None
        else obj_value(pattern_summary, "outcome_success_rate", obj_value(pattern_summary, "win_rate", 0.0))
    )

    expected_move = obj_value(pattern_summary, "expected_move_percent", None)
    if expected_move is None:
        expected_move = obj_value(pattern_summary, "avg_mfe_percent", 0.0)

    expected_pullback = obj_value(pattern_summary, "expected_pullback_percent", None)
    if expected_pullback is None:
        expected_pullback = obj_value(pattern_summary, "avg_mae_percent", 0.0)

    return {
        "pattern_match_score": clamp(pattern_match_score),
        "pattern_confidence": clamp(pattern_confidence),
        "matched_pattern_id": str(obj_value(pattern_summary, "matched_pattern_id", "") or ""),
        "pattern_count": safe_int(pattern_count, 0),
        "pattern_win_rate": clamp(pattern_win_rate),
        "expected_move_percent": safe_float(expected_move, 0.0),
        "expected_pullback_percent": safe_float(expected_pullback, 0.0),
        "expected_duration_seconds": safe_float(obj_value(pattern_summary, "expected_duration_seconds", 300.0), 300.0),
        "best_entry_zone": str(obj_value(pattern_summary, "best_entry_zone", "UNKNOWN") or "UNKNOWN"),
    }


def score_live_sensor_acceleration(candidate: AnalysisCandidate, direction: str) -> Tuple[float, List[str]]:
    """Score the live birth of a move from sensor slopes and accelerations."""
    m = candidate.momentum_state
    reasons: List[str] = []
    score = 0.0

    if direction == DIRECTION_LONG:
        if m.rsi_slope > 0:
            score += min(18.0, abs(m.rsi_slope) * 5.0)
            reasons.append("RSI_SLOPE_BUILDING_LONG")
        if m.rsi_acceleration > 0:
            score += min(12.0, abs(m.rsi_acceleration) * 5.0)
            reasons.append("RSI_ACCEL_BUILDING_LONG")
        if m.histogram_slope > 0:
            score += min(20.0, abs(m.histogram_slope) * 1000.0)
            reasons.append("HIST_SLOPE_BUILDING_LONG")
        if m.histogram_acceleration > 0:
            score += min(16.0, abs(m.histogram_acceleration) * 1000.0)
            reasons.append("HIST_ACCEL_BUILDING_LONG")
        if m.power_delta > 0:
            score += min(18.0, abs(m.power_delta) * 0.75)
            reasons.append("BUY_POWER_BUILDING")
        if m.plus_di > m.minus_di and m.adx_slope >= 0:
            score += 8.0
            reasons.append("ADX_DI_SUPPORTS_LONG")

    elif direction == DIRECTION_SHORT:
        if m.rsi_slope < 0:
            score += min(18.0, abs(m.rsi_slope) * 5.0)
            reasons.append("RSI_SLOPE_BUILDING_SHORT")
        if m.rsi_acceleration < 0:
            score += min(12.0, abs(m.rsi_acceleration) * 5.0)
            reasons.append("RSI_ACCEL_BUILDING_SHORT")
        if m.histogram_slope < 0:
            score += min(20.0, abs(m.histogram_slope) * 1000.0)
            reasons.append("HIST_SLOPE_BUILDING_SHORT")
        if m.histogram_acceleration < 0:
            score += min(16.0, abs(m.histogram_acceleration) * 1000.0)
            reasons.append("HIST_ACCEL_BUILDING_SHORT")
        if m.power_delta < 0:
            score += min(18.0, abs(m.power_delta) * 0.75)
            reasons.append("SELL_POWER_BUILDING")
        if m.minus_di > m.plus_di and m.adx_slope >= 0:
            score += 8.0
            reasons.append("ADX_DI_SUPPORTS_SHORT")

    if m.volume_expansion:
        score += 8.0
        reasons.append("VOLUME_EXPANSION")
    if m.volume_spike:
        score += 8.0
        reasons.append("VOLUME_SPIKE")
    if str(m.atr_expansion).upper() == "EXPANDING":
        score += 7.0
        reasons.append("ATR_EXPANDING")
    if m.atr_explosion:
        score += 8.0
        reasons.append("ATR_EXPLOSION")
    if m.compression_score >= 45 and m.range_probability < 82:
        score += 7.0
        reasons.append("COMPRESSION_BEFORE_MOVE")
    if m.compression_score >= 60 and (m.volume_expansion or m.volume_spike or abs(m.power_delta) >= 12):
        score += 8.0
        reasons.append("SQUEEZE_WITH_PARTICIPATION")

    return clamp(score), reasons


def score_market_mode(candidate: AnalysisCandidate, direction: str) -> Tuple[float, List[str]]:
    mode = str((candidate.market_mode or {}).get("mode", "NEUTRAL")).upper()
    reasons: List[str] = []

    if mode == "BULLISH":
        if direction == DIRECTION_LONG:
            reasons.append("MARKET_MODE_SUPPORTS_LONG")
            return 8.0, reasons
        if direction == DIRECTION_SHORT:
            reasons.append("MARKET_MODE_AGAINST_SHORT")
            return -6.0, reasons

    if mode == "BEARISH":
        if direction == DIRECTION_SHORT:
            reasons.append("MARKET_MODE_SUPPORTS_SHORT")
            return 8.0, reasons
        if direction == DIRECTION_LONG:
            reasons.append("MARKET_MODE_AGAINST_LONG")
            return -6.0, reasons

    if mode == "CHOPPY":
        reasons.append("MARKET_MODE_CHOPPY_CAUTION")
        return -5.0, reasons

    reasons.append("MARKET_MODE_NEUTRAL")
    return 0.0, reasons


def late_move_penalty(candidate: AnalysisCandidate, direction: str) -> Tuple[float, List[str]]:
    """Simple late killer: do not treat completed pump/dump as new start."""
    m = candidate.momentum_state
    reasons: List[str] = []
    change = safe_float(m.price_change_percent)
    atr = max(0.05, safe_float(m.atr_percent))
    extended_threshold = max(atr * 2.15, 1.65)
    very_extended_threshold = max(atr * 2.75, 2.25)

    same_extended = (
        (direction == DIRECTION_LONG and change > extended_threshold)
        or (direction == DIRECTION_SHORT and change < -extended_threshold)
    )
    same_very_extended = (
        (direction == DIRECTION_LONG and change > very_extended_threshold)
        or (direction == DIRECTION_SHORT and change < -very_extended_threshold)
    )

    if same_very_extended:
        reasons.append("VERY_LATE_SAME_DIRECTION_MOVE")
        return 42.0, reasons
    if same_extended:
        reasons.append("LATE_SAME_DIRECTION_MOVE")
        return 26.0, reasons
    return 0.0, reasons


def range_penalty(candidate: AnalysisCandidate, live_sensor_score: float, pattern_score: float) -> Tuple[float, List[str]]:
    m = candidate.momentum_state
    reasons: List[str] = []
    if m.range_probability < 75:
        return 0.0, reasons
    if live_sensor_score >= 50 or pattern_score >= 62:
        reasons.append("RANGE_BUT_START_EVIDENCE_PRESENT")
        return 5.0, reasons
    if m.range_probability >= 88:
        reasons.append("HIGH_RANGE_WITHOUT_START_EVIDENCE")
        return 18.0, reasons
    reasons.append("RANGE_CAUTION")
    return 10.0, reasons


def classify_phase(final_score: float, late_penalty_value: float, range_penalty_value: float, candidate: AnalysisCandidate) -> Tuple[str, List[str]]:
    reasons: List[str] = []

    if late_penalty_value >= 40:
        reasons.append("PHASE_LATE_VERY_EXTENDED")
        return PREDICT_LATE, reasons
    if late_penalty_value >= 26 and final_score < 74:
        reasons.append("PHASE_LATE_EXTENDED")
        return PREDICT_LATE, reasons
    if range_penalty_value >= 18 and final_score < 55:
        reasons.append("PHASE_RANGE_NO_START")
        return PREDICT_RANGE, reasons

    m = candidate.momentum_state
    live_building = (
        abs(m.rsi_slope) > 0
        and abs(m.histogram_slope) > 0
        and abs(m.power_delta) >= 3
    )

    if final_score >= 72:
        reasons.append("PHASE_START_STRONG")
        return PREDICT_START, reasons
    if final_score >= 58 and live_building:
        reasons.append("PHASE_PRE_START_LIVE_BUILDING")
        return PREDICT_PRE_START, reasons
    if final_score >= 50:
        reasons.append("PHASE_PRE_START_WATCH")
        return PREDICT_PRE_START, reasons

    reasons.append("PHASE_UNKNOWN")
    return PREDICT_UNKNOWN, reasons


def confidence_from_score(
    score: float,
    pattern_count: int,
    pattern_score: float,
    pattern_confidence: float,
    pattern_win_rate: float,
    live_sensor_score: float,
    phase: str,
) -> Tuple[str, bool, List[str]]:
    """Classify prediction confidence.

    Important Level 1 rule:
    Pattern count is useful, but it must not be the main gate.
    A few strong repeated patterns with high confidence/win-rate can be better
    than many weak patterns. Live sensor acceleration can also compensate when
    a fresh movement is forming before the pattern database is mature.
    """
    reasons: List[str] = []
    prefer_ghost = False

    strong_pattern_quality = (
        pattern_score >= 68
        and pattern_confidence >= 55
        and (pattern_win_rate >= 55 or pattern_count >= 2)
    )
    useful_pattern_quality = (
        pattern_score >= 55
        or pattern_confidence >= 50
        or pattern_win_rate >= 58
    )
    strong_live_birth = live_sensor_score >= 62

    if pattern_count <= 0:
        reasons.append("NO_PATTERN_SAMPLE_YET")
        prefer_ghost = not strong_live_birth
    elif pattern_count < 3:
        reasons.append("LOW_PATTERN_SAMPLE_BUT_QUALITY_CHECKED")
        prefer_ghost = not (strong_pattern_quality or strong_live_birth)

    if useful_pattern_quality:
        reasons.append("PATTERN_QUALITY_USEFUL")
    if strong_pattern_quality:
        reasons.append("PATTERN_QUALITY_STRONG")
    if strong_live_birth:
        reasons.append("LIVE_SENSOR_STRONG_EARLY_BIRTH")

    if phase in {PREDICT_LATE, PREDICT_RANGE, PREDICT_UNKNOWN}:
        reasons.append("PHASE_NOT_IDEAL_FOR_REAL")
        prefer_ghost = True

    if score >= 75 and phase in {PREDICT_PRE_START, PREDICT_START} and (strong_pattern_quality or strong_live_birth):
        return CONF_HIGH, prefer_ghost, reasons
    if score >= 60 and (useful_pattern_quality or strong_live_birth):
        return CONF_MEDIUM, prefer_ghost, reasons
    if score >= 42:
        return CONF_LOW, True, reasons
    return CONF_UNKNOWN, True, reasons


class MovementPredictor:
    """Pattern Start Predictor. Final trade decision is not here."""

    def predict(
        self,
        candidate: AnalysisCandidate,
        pattern_summary: Optional[Any] = None,
        learning_summary: Optional[Any] = None,
        **_: Any,
    ) -> MovementPredictionResult:
        direction = normalize_direction(candidate.direction_hint)
        movement_type = movement_type_from_direction(direction)
        reasons: List[str] = []
        warnings: List[str] = []

        if direction == DIRECTION_NEUTRAL:
            warnings.append("NO_SENSOR_DIRECTION")
            direction_pressure = 0.0
        else:
            direction_pressure = (
                candidate.sensor_direction.long_pressure
                if direction == DIRECTION_LONG
                else candidate.sensor_direction.short_pressure
            )

        pattern = extract_pattern_metrics(pattern_summary or learning_summary)
        pattern_score = pattern["pattern_match_score"]
        pattern_confidence = pattern["pattern_confidence"]
        pattern_count = pattern["pattern_count"]
        pattern_win_rate = pattern["pattern_win_rate"]

        if pattern_score >= 75:
            reasons.append("STRONG_PATTERN_START_MATCH")
        elif pattern_score >= 62:
            reasons.append("LIVE_PATTERN_START_MATCH")
        elif pattern_score >= 45:
            reasons.append("WEAK_PATTERN_MATCH")

        live_score, r = score_live_sensor_acceleration(candidate, direction)
        reasons.extend(r)

        market_score, r = score_market_mode(candidate, direction)
        reasons.extend(r)

        late_pen, r = late_move_penalty(candidate, direction)
        reasons.extend(r)

        range_pen, r = range_penalty(candidate, live_score, pattern_score)
        reasons.extend(r)

        # Pattern and live sensor acceleration are the main factors.
        final_score = clamp(
            pattern_score * 0.34
            + pattern_confidence * 0.12
            + live_score * 0.34
            + direction_pressure * 0.14
            + pattern_win_rate * 0.08
            + market_score
            - late_pen * 0.85
            - range_pen * 0.65
        )

        phase, r = classify_phase(final_score, late_pen, range_pen, candidate)
        reasons.extend(r)

        confidence, prefer_ghost, r = confidence_from_score(
            score=final_score,
            pattern_count=pattern_count,
            pattern_score=pattern_score,
            pattern_confidence=pattern_confidence,
            pattern_win_rate=pattern_win_rate,
            live_sensor_score=live_score,
            phase=phase,
        )
        reasons.extend(r)

        if prefer_ghost:
            warnings.append("PREDICTOR_PREFERS_GHOST_IF_AI_UNCERTAIN")
        if phase in {PREDICT_LATE, PREDICT_RANGE}:
            warnings.append(f"PREDICTED_PHASE_{phase}")

        pump_probability = 50.0
        dump_probability = 50.0
        if direction == DIRECTION_LONG:
            pump_probability = final_score
            dump_probability = clamp(100.0 - final_score)
        elif direction == DIRECTION_SHORT:
            dump_probability = final_score
            pump_probability = clamp(100.0 - final_score)

        expected_move = pattern["expected_move_percent"]
        if expected_move <= 0:
            expected_move = max(0.0, safe_float(candidate.momentum_state.atr_percent) * 1.15)

        expected_pullback = pattern["expected_pullback_percent"]
        if expected_pullback <= 0:
            expected_pullback = max(0.0, safe_float(candidate.momentum_state.atr_percent) * 0.45)

        expected_duration = pattern["expected_duration_seconds"] or 300.0
        best_entry_zone = str(pattern.get("best_entry_zone", "UNKNOWN") or "UNKNOWN")
        if best_entry_zone == "UNKNOWN":
            if expected_pullback <= max(0.03, expected_move * 0.20):
                best_entry_zone = "IMMEDIATE_START_ZONE"
            elif expected_pullback <= max(0.05, expected_move * 0.45):
                best_entry_zone = "SMALL_PULLBACK_ZONE"
            else:
                best_entry_zone = "WAIT_FOR_RETEST_ZONE"

        breakdown = PredictorBreakdown(
            pattern_match_score=clamp(pattern_score),
            live_sensor_acceleration=clamp(live_score),
            direction_pressure=clamp(direction_pressure),
            market_mode_score=clamp(market_score, -100.0, 100.0),
            range_penalty=clamp(range_pen),
            late_penalty=clamp(late_pen),
            final_score=clamp(final_score),
        )

        return MovementPredictionResult(
            prediction_id=f"pred_{uuid4().hex}",
            symbol=candidate.symbol,
            timeframe=candidate.timeframe,
            timestamp=candidate.timestamp or int(time.time()),
            direction_hint=direction,
            predicted_movement_type=movement_type,
            predicted_phase=phase,
            pump_probability=clamp(pump_probability),
            dump_probability=clamp(dump_probability),
            movement_probability=clamp(final_score),
            pattern_match_score=clamp(pattern_score),
            pattern_confidence=clamp(pattern_confidence),
            matched_pattern_id=pattern["matched_pattern_id"],
            pattern_count=pattern_count,
            pattern_win_rate=clamp(pattern_win_rate),
            expected_move_percent=safe_float(expected_move),
            expected_pullback_percent=safe_float(expected_pullback),
            expected_duration_seconds=safe_float(expected_duration),
            best_entry_zone=best_entry_zone,
            confidence_level=confidence,
            should_prefer_ghost_if_uncertain=prefer_ghost,
            breakdown=breakdown,
            reason_codes=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
            valid=bool(candidate.valid and direction != DIRECTION_NEUTRAL),
        )


_default_predictor: Optional[MovementPredictor] = None


def predictor() -> MovementPredictor:
    global _default_predictor
    if _default_predictor is None:
        _default_predictor = MovementPredictor()
    return _default_predictor


def predict_movement(
    candidate: AnalysisCandidate,
    pattern_summary: Optional[Any] = None,
    learning_summary: Optional[Any] = None,
    **kwargs: Any,
) -> MovementPredictionResult:
    return predictor().predict(
        candidate=candidate,
        pattern_summary=pattern_summary,
        learning_summary=learning_summary,
        **kwargs,
    )


def movement_predictor(
    candidate: AnalysisCandidate,
    pattern_summary: Optional[Any] = None,
    learning_summary: Optional[Any] = None,
    **kwargs: Any,
) -> MovementPredictionResult:
    return predict_movement(
        candidate=candidate,
        pattern_summary=pattern_summary,
        learning_summary=learning_summary,
        **kwargs,
    )
