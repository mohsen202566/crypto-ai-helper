from __future__ import annotations

"""
16 - movement_predictor.py

Movement Predictor layer for the locked Movement Hunter architecture.

Responsibilities:
- Compare current candidate conditions with Movement Memory.
- Estimate pump/dump similarity.
- Estimate pre-start / start / mid / late probability.
- Provide MovementPredictionResult to ai_decision_engine.py.
- Use movement_memory.py summaries and raw current sensor context.

Strictly forbidden:
- No REAL/GHOST/REJECT final decision.
- No trade execution.
- No Toobit calls.
- No Telegram.
- No persistence writes.
- No Paper mode.
- No Setup flow.

This file predicts movement probability only.
Final decision is only in ai_decision_engine.py.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4
import math
import time

from analysis_engine import AnalysisCandidate
from movement_hunter import MovementHunterResult
from trap_engine import TrapResult
from state_engine import StateResult
from confidence_engine import ConfidenceResult
from movement_memory import (
    MovementMemorySummary,
    PreMoveSignature,
    PreMoveSignatureBuilder,
    summarize_movement_candidate,
)


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


@dataclass(frozen=True)
class SimilarityBreakdown:
    memory_similarity: float
    sensor_alignment: float
    movement_alignment: float
    state_alignment: float
    trap_penalty: float
    range_penalty: float
    exhaustion_penalty: float
    final_similarity: float

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
    similarity_score: float
    expected_move_percent: float
    expected_duration_seconds: float
    confidence_level: str
    sample_count: int
    should_prefer_ghost_if_uncertain: bool
    breakdown: SimilarityBreakdown
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
            "similarity_score": self.similarity_score,
            "expected_move_percent": self.expected_move_percent,
            "expected_duration_seconds": self.expected_duration_seconds,
            "confidence_level": self.confidence_level,
            "sample_count": self.sample_count,
            "should_prefer_ghost_if_uncertain": self.should_prefer_ghost_if_uncertain,
            "breakdown": self.breakdown.to_dict(),
            "reason_codes": list(self.reason_codes),
            "warnings": list(self.warnings),
            "valid": self.valid,
        }


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return low
        return max(low, min(high, v))
    except Exception:
        return low


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


def avg(values: Sequence[float]) -> float:
    vals = [safe_float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


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


def _obj_value(obj: Optional[Any], key: str, default: Any = None) -> Any:
    """Read a field from dict/dataclass-like summaries safely."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _summary_float(summary: Optional[Any], key: str, default: float = 0.0) -> float:
    return safe_float(_obj_value(summary, key, default), default)


def _best_memory_float(
    movement_summary: Optional[Any],
    learning_summary: Optional[Any],
    key: str,
    default: float = 0.0,
) -> float:
    """Prefer Movement Memory metric; fall back to Coin Learning metric.

    This keeps backward compatibility while allowing the predictor to consume
    the new Movement Hunter fields produced by coin_learning.py:
    timing_score, early_success_rate, premove_success_rate, late_failure_rate,
    fuzzy_match_score and outcome_success_rate.
    """
    value = _obj_value(movement_summary, key, None)
    if value is None:
        value = _obj_value(learning_summary, key, default)
    return safe_float(value, default)


class MemorySimilarityEngine:
    """Scores current condition similarity against movement_memory.py and coin_learning summaries."""

    def score(
        self,
        summary: MovementMemorySummary,
        learning_summary: Optional[Any] = None,
    ) -> Tuple[float, List[str]]:
        reasons: List[str] = []

        sample_count = int(max(0, safe_float(getattr(summary, "sample_count", 0), 0)))
        learning_samples = int(max(0, safe_float(_obj_value(learning_summary, "sample_count", 0), 0)))
        effective_samples = max(sample_count, learning_samples)

        timing_score = clamp(_best_memory_float(summary, learning_summary, "timing_score", 50.0))
        early_success_rate = clamp(_best_memory_float(summary, learning_summary, "early_success_rate", 0.0))
        premove_success_rate = clamp(_best_memory_float(summary, learning_summary, "premove_success_rate", early_success_rate))
        fuzzy_match_score = clamp(_best_memory_float(summary, learning_summary, "fuzzy_match_score", 0.0))
        outcome_success_rate = clamp(_best_memory_float(summary, learning_summary, "outcome_success_rate", getattr(summary, "success_rate", 50.0)))
        late_failure_rate = clamp(_best_memory_float(summary, learning_summary, "late_failure_rate", 0.0))

        if effective_samples <= 0:
            # Movement Hunter speed mode:
            # No memory must not make the bot a follower. In 5M scalping the
            # first useful clues often appear before enough learning samples
            # exist. Give a neutral-predictive base so raw sensors can lead.
            reasons.append("NO_MOVEMENT_MEMORY_SPEED_MODE")
            return 44.0, reasons

        # Movement Hunter memory should prioritize timing and early success over
        # raw move size. A large but late move should not teach the AI to chase.
        sample_score = clamp(effective_samples * 7.0)
        success_score = clamp(getattr(summary, "success_rate", outcome_success_rate))
        move_score = clamp(abs(getattr(summary, "avg_move_percent", 0.0)) * 38.0)

        hunter_timing_score = clamp(
            timing_score * 0.34
            + max(early_success_rate, premove_success_rate) * 0.24
            + outcome_success_rate * 0.22
            + fuzzy_match_score * 0.12
            + sample_score * 0.08
            - late_failure_rate * 0.18
        )

        if effective_samples < 3:
            similarity = clamp(
                success_score * 0.30
                + hunter_timing_score * 0.46
                + move_score * 0.16
                + sample_score * 0.08
            )
            reasons.append("VERY_LOW_MOVEMENT_MEMORY_SAMPLE")
        elif effective_samples < 8:
            similarity = clamp(
                success_score * 0.28
                + hunter_timing_score * 0.50
                + move_score * 0.12
                + sample_score * 0.10
            )
            reasons.append("LOW_MOVEMENT_MEMORY_SAMPLE")
        else:
            similarity = clamp(
                success_score * 0.24
                + hunter_timing_score * 0.54
                + move_score * 0.10
                + sample_score * 0.12
            )
            reasons.append("ENOUGH_MOVEMENT_MEMORY")

        if timing_score >= 68 and max(early_success_rate, premove_success_rate) >= 45 and outcome_success_rate >= 58:
            similarity = clamp(similarity + 8.0)
            reasons.append("PREMOVE_TIMING_MEMORY_STRONG")
        elif timing_score >= 62 and max(early_success_rate, premove_success_rate) >= 35:
            similarity = clamp(similarity + 5.0)
            reasons.append("PREMOVE_TIMING_MEMORY_GOOD")

        if fuzzy_match_score >= 70:
            similarity = clamp(similarity + 4.0)
            reasons.append("FUZZY_CONDITION_MATCH_SUPPORTS_PREDICTION")
        elif 0 < fuzzy_match_score < 50 and effective_samples >= 4:
            similarity = clamp(similarity - 3.0)
            reasons.append("FUZZY_CONDITION_MATCH_WEAK")

        if late_failure_rate >= 55 and effective_samples >= 4:
            similarity = clamp(similarity - 9.0)
            reasons.append("LATE_FAILURE_MEMORY_PENALTY")
        elif late_failure_rate >= 40 and effective_samples >= 4:
            similarity = clamp(similarity - 5.0)
            reasons.append("MODERATE_LATE_FAILURE_MEMORY_PENALTY")

        if outcome_success_rate <= 40 and timing_score <= 45 and effective_samples >= 5:
            similarity = clamp(similarity - 7.0)
            reasons.append("WEAK_OUTCOME_AND_TIMING_MEMORY")

        if success_score >= 68 and effective_samples >= 5:
            reasons.append("SIMILAR_PREMOVE_WORKED_STRONGLY")
        elif success_score >= 60:
            reasons.append("SIMILAR_PREMOVE_WORKED")
        elif success_score <= 40 and effective_samples >= 5:
            reasons.append("SIMILAR_PREMOVE_WEAK")

        return similarity, reasons


class SensorAlignmentEngine:
    """Scores current sensor alignment for pre-pump/pre-dump movement."""

    def score(self, candidate: AnalysisCandidate, direction: str) -> Tuple[float, List[str]]:
        s = candidate.sensor_snapshot
        reasons: List[str] = []
        score = 0.0

        if direction == DIRECTION_LONG:
            # Prediction-first: use slope/acceleration/power as live sensors,
            # not candle confirmation. Soft thresholds intentionally detect
            # the birth of movement before it becomes obvious.
            if s.rsi_slope > 0:
                score += 14
                reasons.append("RSI_ALIGNED_LONG")
            if s.rsi_slope > 0.08:
                score += 5
                reasons.append("ULTRA_EARLY_RSI_LONG")
            if s.histogram_slope > 0:
                score += 16
                reasons.append("HIST_ALIGNED_LONG")
            if s.histogram_acceleration > 0:
                score += 14
                reasons.append("HIST_ACCEL_LONG")
            if s.power_delta > 4:
                score += 10
                reasons.append("EARLY_POWER_ALIGNED_LONG")
            if s.power_delta > 8:
                score += 12
                reasons.append("POWER_ALIGNED_LONG")
            if s.vwap_state in {"RECLAIM", "ABOVE"}:
                score += 10
                reasons.append("VWAP_ALIGNED_LONG")
            if s.failed_breakdown or s.liquidity_grab_down:
                score += 12
                reasons.append("DOWN_SWEEP_RECOVERY_LONG")
        elif direction == DIRECTION_SHORT:
            if s.rsi_slope < 0:
                score += 14
                reasons.append("RSI_ALIGNED_SHORT")
            if s.rsi_slope < -0.08:
                score += 5
                reasons.append("ULTRA_EARLY_RSI_SHORT")
            if s.histogram_slope < 0:
                score += 16
                reasons.append("HIST_ALIGNED_SHORT")
            if s.histogram_acceleration < 0:
                score += 14
                reasons.append("HIST_ACCEL_SHORT")
            if s.power_delta < -4:
                score += 10
                reasons.append("EARLY_POWER_ALIGNED_SHORT")
            if s.power_delta < -8:
                score += 12
                reasons.append("POWER_ALIGNED_SHORT")
            if s.vwap_state in {"LOSS", "BELOW"}:
                score += 10
                reasons.append("VWAP_ALIGNED_SHORT")
            if s.failed_breakout or s.liquidity_grab_up:
                score += 12
                reasons.append("UP_SWEEP_REVERSAL_SHORT")

        if s.atr_expansion == "EXPANDING":
            score += 8
            reasons.append("ATR_EXPANDING")
        if s.atr_explosion:
            score += 10
            reasons.append("ATR_EXPLOSION")
        if s.volume_expansion:
            score += 8
            reasons.append("VOLUME_EXPANDING")
        if s.volume_spike:
            score += 8
            reasons.append("VOLUME_SPIKE")

        # Compression is not automatically range-risk. In Movement Hunter mode,
        # compression + aligned power/volume can be the exact pre-breakout state
        # we want to catch before the first large candle.
        if safe_float(getattr(s, "compression_score", 0.0)) >= 45 and safe_float(getattr(s, "range_probability", 0.0)) < 82:
            score += 8
            reasons.append("COMPRESSION_PRE_BREAKOUT_CONTEXT")
        if safe_float(getattr(s, "compression_score", 0.0)) >= 60 and (s.volume_expansion or s.volume_spike or abs(s.power_delta) >= 12):
            score += 8
            reasons.append("SQUEEZE_WITH_PARTICIPATION")

        return clamp(score), reasons


class PhasePredictionEngine:
    """Predicts whether current move is pre-start, start, mid, late or range."""

    def classify(
        self,
        candidate: AnalysisCandidate,
        movement: MovementHunterResult,
        state: StateResult,
        trap: TrapResult,
        similarity: float,
        learning_summary: Optional[Any] = None,
    ) -> Tuple[str, List[str]]:
        s = candidate.sensor_snapshot
        reasons: List[str] = []

        readiness = safe_float(movement.readiness_score)
        continuation = safe_float(movement.continuation_probability)
        compression = safe_float(getattr(s, "compression_score", 0.0))
        range_probability = safe_float(getattr(s, "range_probability", 0.0))
        state_range = safe_float(getattr(state, "range_probability", range_probability))
        late_risk = safe_float(getattr(state, "late_entry_risk", 0.0))
        exhaustion_risk = safe_float(getattr(state, "exhaustion_risk", 0.0))
        trap_risk = safe_float(getattr(trap, "trap_risk", 0.0))
        power_delta = abs(safe_float(getattr(s, "power_delta", 0.0)))
        hist_accel = abs(safe_float(getattr(s, "histogram_acceleration", 0.0)))
        hist_slope = abs(safe_float(getattr(s, "histogram_slope", 0.0)))
        volume_live = bool(getattr(s, "volume_expansion", False) or getattr(s, "volume_spike", False))
        atr_live = bool(getattr(s, "atr_expansion", "") == "EXPANDING" or getattr(s, "atr_explosion", False))
        timing_score = clamp(_summary_float(learning_summary, "timing_score", 50.0))
        early_success_rate = clamp(_summary_float(learning_summary, "early_success_rate", 0.0))
        premove_success_rate = clamp(_summary_float(learning_summary, "premove_success_rate", early_success_rate))
        late_failure_rate = clamp(_summary_float(learning_summary, "late_failure_rate", 0.0))
        learned_premove_support = (
            timing_score >= 62
            and max(early_success_rate, premove_success_rate) >= 35
            and late_failure_rate < 55
        )

        # Prediction-first birth detection:
        # Do not wait for multiple candles or a fully obvious move. For 5M
        # scalping, aligned momentum slope + small power shift is enough to mark
        # PRE_START/START when trap and late risk are not severe.
        directional_slope_birth = (
            (
                candidate.direction_hint == DIRECTION_LONG
                and safe_float(getattr(s, "rsi_slope", 0.0)) > 0
                and safe_float(getattr(s, "histogram_slope", 0.0)) > 0
                and safe_float(getattr(s, "power_delta", 0.0)) > 3
            )
            or (
                candidate.direction_hint == DIRECTION_SHORT
                and safe_float(getattr(s, "rsi_slope", 0.0)) < 0
                and safe_float(getattr(s, "histogram_slope", 0.0)) < 0
                and safe_float(getattr(s, "power_delta", 0.0)) < -3
            )
        )

        early_sensor_birth = (
            (compression >= 32 and range_probability < 88 and (power_delta >= 6 or volume_live or atr_live))
            or (hist_accel > 0 and hist_slope > 0 and power_delta >= 5)
            or directional_slope_birth
            or (similarity >= 50 and readiness < 68 and late_risk < 78)
            or (learned_premove_support and readiness < 72 and late_risk < 82)
        )

        # Only classify as RANGE when both memory/sensors and live readiness are weak.
        if state.market_state == "RANGE" and readiness < 30 and similarity < 42 and not early_sensor_birth:
            reasons.append("PREDICT_RANGE_WEAK_NO_BREAKOUT_CONTEXT")
            return PREDICT_RANGE, reasons

        # LATE must be stricter. The old logic produced too many LATE/MID labels
        # and missed pre-start opportunities. Do not call it late while fresh
        # sensors, memory similarity, or compression breakout context are alive.
        if (
            (state.market_state in {"EXHAUSTION", "LATE"} and readiness < 34 and similarity < 44 and not early_sensor_birth)
            or (movement.freshness == "DEAD" and readiness < 32 and similarity < 46)
            or (late_risk >= 88 and exhaustion_risk >= 80 and continuation < 36 and similarity < 52 and not early_sensor_birth)
        ):
            reasons.append("PREDICT_LATE_CONFIRMED_EXHAUSTION")
            return PREDICT_LATE, reasons

        if movement.freshness == "LATE" and readiness < 34 and similarity < 46 and not early_sensor_birth:
            reasons.append("PREDICT_LATE_WEAK_LATE_FRESHNESS")
            return PREDICT_LATE, reasons

        # PRE_START: strongest hunter case. It should trigger before the big move,
        # when memory or compression/participation suggests the move is forming.
        if (
            (similarity >= 50 and readiness < 74 and compression >= 28 and state_range < 88 and trap_risk < 76)
            or (early_sensor_birth and readiness < 76 and trap_risk < 78 and late_risk < 84)
            or (learned_premove_support and similarity >= 46 and readiness < 78 and trap_risk < 78)
        ):
            reasons.append("PREDICT_PRE_START_HUNTER")
            return PREDICT_PRE_START, reasons

        if movement.freshness == "FRESH" and readiness >= 44 and trap_risk < 80:
            reasons.append("PREDICT_START_FRESH_MOVE")
            return PREDICT_START, reasons

        if readiness >= 50 and continuation >= 38 and late_risk < 82:
            reasons.append("PREDICT_START_LIVE_CONTINUATION")
            return PREDICT_START, reasons

        # MID should not swallow every weak candidate. Require stronger live move.
        if readiness >= 58 and (continuation >= 48 or similarity >= 62) and late_risk < 76:
            reasons.append("PREDICT_MID_CONFIRMED")
            return PREDICT_MID, reasons

        # If the market is still building pressure, keep it as PRE_START instead
        # of UNKNOWN so the AI can watch/learn it as an early candidate.
        if early_sensor_birth and trap_risk < 80:
            reasons.append("PREDICT_PRE_START_EARLY_SENSOR_BIRTH")
            return PREDICT_PRE_START, reasons

        reasons.append("PREDICT_UNKNOWN")
        return PREDICT_UNKNOWN, reasons

class MovementProbabilityEngine:
    """Combines memory, sensors, movement, state and trap into probabilities."""

    def score(
        self,
        candidate: AnalysisCandidate,
        summary: MovementMemorySummary,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
        learning_summary: Optional[Any] = None,
    ) -> Tuple[SimilarityBreakdown, float, float, float, List[str]]:
        direction = normalize_direction(candidate.direction_hint)
        reasons: List[str] = []

        memory_similarity, r = MemorySimilarityEngine().score(summary, learning_summary=learning_summary)
        reasons.extend(r)

        sensor_alignment, r = SensorAlignmentEngine().score(candidate, direction)
        reasons.extend(r)

        movement_alignment = clamp(
            movement.readiness_score * 0.58
            + movement.continuation_probability * 0.42
        )

        state_alignment = clamp(
            state.state_confidence
            - state.late_entry_risk * 0.28
            - state.exhaustion_risk * 0.30
        )

        trap_penalty = clamp(trap.trap_risk * 0.52 + trap.liquidity_risk * 0.24)

        # Compression can be a positive squeeze before breakout. Do not punish it
        # as range unless range probability is high and participation is weak.
        s = candidate.sensor_snapshot
        compression = safe_float(getattr(s, "compression_score", 0.0))
        participation = 1.0 if (getattr(s, "volume_expansion", False) or getattr(s, "volume_spike", False) or abs(safe_float(getattr(s, "power_delta", 0.0))) >= 12) else 0.0
        compression_bonus = clamp(compression * 0.22) if participation else clamp(compression * 0.08)
        range_penalty = clamp(state.range_probability * 0.42 + max(0.0, compression - 55.0) * 0.05 - compression_bonus)
        exhaustion_penalty = clamp(state.exhaustion_risk * 0.50 + movement.reversal_pressure * 0.32)

        timing_score = clamp(_summary_float(learning_summary, "timing_score", 50.0))
        early_success_rate = clamp(_summary_float(learning_summary, "early_success_rate", 0.0))
        premove_success_rate = clamp(_summary_float(learning_summary, "premove_success_rate", early_success_rate))
        late_failure_rate = clamp(_summary_float(learning_summary, "late_failure_rate", 0.0))
        learning_premove_bonus = clamp(
            (timing_score - 50.0) * 0.16
            + max(early_success_rate, premove_success_rate) * 0.08
            - late_failure_rate * 0.07
        )
        if learning_premove_bonus > 4:
            reasons.append("LEARNING_PREMOVE_BONUS_APPLIED")
        elif learning_premove_bonus < -4:
            reasons.append("LEARNING_LATE_FAILURE_PENALTY_APPLIED")

        # Speed mode: prediction must lead, not follow. Sensors and live
        # movement receive more weight than historical memory/state. Range and
        # exhaustion still matter, but they should not bury fresh birth signals.
        final_similarity = clamp(
            memory_similarity * 0.24
            + sensor_alignment * 0.36
            + movement_alignment * 0.28
            + state_alignment * 0.10
            + compression_bonus * 0.16
            + learning_premove_bonus
            - trap_penalty * 0.26
            - range_penalty * 0.10
            - exhaustion_penalty * 0.16
        )

        base_probability = final_similarity

        pump_probability = 50.0
        dump_probability = 50.0

        if direction == DIRECTION_LONG:
            pump_probability = base_probability
            dump_probability = clamp(100.0 - base_probability + trap.short_trap_probability * 0.20)
        elif direction == DIRECTION_SHORT:
            dump_probability = base_probability
            pump_probability = clamp(100.0 - base_probability + trap.long_trap_probability * 0.20)

        breakdown = SimilarityBreakdown(
            memory_similarity=memory_similarity,
            sensor_alignment=sensor_alignment,
            movement_alignment=movement_alignment,
            state_alignment=state_alignment,
            trap_penalty=trap_penalty,
            range_penalty=range_penalty,
            exhaustion_penalty=exhaustion_penalty,
            final_similarity=final_similarity,
        )

        return breakdown, pump_probability, dump_probability, base_probability, reasons


class ConfidenceClassifier:
    """Classifies prediction confidence."""

    def classify(self, probability: float, sample_count: int, phase: str, trap: TrapResult, state: StateResult) -> Tuple[str, bool, List[str]]:
        reasons: List[str] = []
        prefer_ghost = False

        if sample_count <= 0:
            reasons.append("PREDICTOR_LOW_DATA_SPEED_MODE")
            # Low data should not force follower behavior. PRE_START/START with
            # acceptable probability is allowed to reach AI decision layer.
            prefer_ghost = phase not in {PREDICT_PRE_START, PREDICT_START} or probability < 56
        elif sample_count < 5:
            reasons.append("PREDICTOR_SMALL_SAMPLE_SPEED_MODE")
            prefer_ghost = phase not in {PREDICT_PRE_START, PREDICT_START} or probability < 54

        if phase in {PREDICT_RANGE, PREDICT_LATE, PREDICT_UNKNOWN}:
            reasons.append("PREDICTOR_PHASE_NOT_IDEAL")
            prefer_ghost = True

        if trap.trap_risk >= 76 or state.late_entry_risk >= 86:
            reasons.append("PREDICTOR_RISK_REQUIRES_GHOST_CAUTION")
            prefer_ghost = True

        if probability >= 68 and sample_count >= 5 and phase in {PREDICT_PRE_START, PREDICT_START, PREDICT_MID} and not prefer_ghost:
            return CONF_HIGH, False, reasons
        if probability >= 58:
            return CONF_MEDIUM, prefer_ghost, reasons
        if probability >= 38:
            return CONF_LOW, True, reasons
        return CONF_UNKNOWN, True, reasons


class MovementPredictor:
    """
    Main movement predictor.

    Input:
        AnalysisCandidate + movement/trap/state/confidence

    Output:
        MovementPredictionResult

    This is probability/context only, not final trade decision.
    """

    def __init__(self):
        self.signature_builder = PreMoveSignatureBuilder()

    def predict(
        self,
        candidate: AnalysisCandidate,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
        confidence: Optional[ConfidenceResult] = None,
        movement_summary: Optional[MovementMemorySummary] = None,
        learning_summary: Optional[Any] = None,
    ) -> MovementPredictionResult:
        direction = normalize_direction(candidate.direction_hint)
        movement_type = movement_type_from_direction(direction)

        if movement_summary is None:
            movement_summary = summarize_movement_candidate(
                candidate=candidate,
                movement=movement,
                trap=trap,
                state=state,
            )

        probability_engine = MovementProbabilityEngine()
        breakdown, pump_prob, dump_prob, move_prob, reasons = probability_engine.score(
            candidate=candidate,
            summary=movement_summary,
            movement=movement,
            trap=trap,
            state=state,
            learning_summary=learning_summary,
        )

        phase, r = PhasePredictionEngine().classify(
            candidate=candidate,
            movement=movement,
            state=state,
            trap=trap,
            similarity=breakdown.final_similarity,
            learning_summary=learning_summary,
        )
        reasons.extend(r)

        conf_level, prefer_ghost, r = ConfidenceClassifier().classify(
            probability=move_prob,
            sample_count=movement_summary.sample_count,
            phase=phase,
            trap=trap,
            state=state,
        )
        reasons.extend(r)

        warnings: List[str] = []
        if prefer_ghost:
            warnings.append("PREDICTOR_PREFERS_GHOST_IF_AI_UNCERTAIN")
        if phase in {PREDICT_RANGE, PREDICT_LATE}:
            warnings.append(f"PREDICTED_PHASE_{phase}")
        if movement_summary.sample_count == 0:
            warnings.append("NO_MOVEMENT_MEMORY_SAMPLE")
        if confidence is not None and confidence.should_downgrade_to_ghost:
            warnings.append("CONFIDENCE_ENGINE_DOWNGRADE_PRESENT")

        expected_move = movement_summary.avg_move_percent
        if movement_summary.sample_count <= 0:
            expected_move = max(0.0, candidate.sensor_snapshot.atr_percent * 1.2)

        expected_duration = movement_summary.avg_duration_seconds
        if expected_duration <= 0:
            expected_duration = 300.0

        return MovementPredictionResult(
            prediction_id=f"pred_{uuid4().hex}",
            symbol=candidate.symbol,
            timeframe=candidate.timeframe,
            timestamp=candidate.timestamp or int(time.time()),
            direction_hint=direction,
            predicted_movement_type=movement_type,
            predicted_phase=phase,
            pump_probability=clamp(pump_prob),
            dump_probability=clamp(dump_prob),
            movement_probability=clamp(move_prob),
            similarity_score=clamp(breakdown.final_similarity),
            expected_move_percent=safe_float(expected_move),
            expected_duration_seconds=safe_float(expected_duration),
            confidence_level=conf_level,
            sample_count=movement_summary.sample_count,
            should_prefer_ghost_if_uncertain=prefer_ghost,
            breakdown=breakdown,
            reason_codes=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
            valid=bool(candidate.valid and movement.valid and trap.valid and state.valid),
        )


_default_predictor: Optional[MovementPredictor] = None


def predictor() -> MovementPredictor:
    global _default_predictor
    if _default_predictor is None:
        _default_predictor = MovementPredictor()
    return _default_predictor


def predict_movement(
    candidate: AnalysisCandidate,
    movement: MovementHunterResult,
    trap: TrapResult,
    state: StateResult,
    confidence: Optional[ConfidenceResult] = None,
    movement_summary: Optional[MovementMemorySummary] = None,
    learning_summary: Optional[Any] = None,
) -> MovementPredictionResult:
    return predictor().predict(
        candidate=candidate,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        movement_summary=movement_summary,
        learning_summary=learning_summary,
    )


def movement_predictor(
    candidate: AnalysisCandidate,
    movement: MovementHunterResult,
    trap: TrapResult,
    state: StateResult,
    confidence: Optional[ConfidenceResult] = None,
    movement_summary: Optional[MovementMemorySummary] = None,
    learning_summary: Optional[Any] = None,
) -> MovementPredictionResult:
    return predict_movement(
        candidate=candidate,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        movement_summary=movement_summary,
        learning_summary=learning_summary,
    )
