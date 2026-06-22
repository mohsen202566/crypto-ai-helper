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


class MemorySimilarityEngine:
    """Scores current condition similarity against movement_memory.py summary."""

    def score(self, summary: MovementMemorySummary) -> Tuple[float, List[str]]:
        reasons: List[str] = []

        if summary.sample_count <= 0:
            reasons.append("NO_MOVEMENT_MEMORY")
            return 25.0, reasons

        sample_score = clamp(summary.sample_count * 5.0)
        success_score = clamp(summary.success_rate)
        move_score = clamp(abs(summary.avg_move_percent) * 35.0)

        similarity = clamp(sample_score * 0.30 + success_score * 0.45 + move_score * 0.25)

        if summary.sample_count >= 10:
            reasons.append("ENOUGH_MOVEMENT_MEMORY")
        else:
            reasons.append("LOW_MOVEMENT_MEMORY_SAMPLE")

        if summary.success_rate >= 65:
            reasons.append("SIMILAR_PREMOVE_WORKED")
        elif summary.success_rate <= 40 and summary.sample_count >= 5:
            reasons.append("SIMILAR_PREMOVE_WEAK")

        return similarity, reasons


class SensorAlignmentEngine:
    """Scores current sensor alignment for pre-pump/pre-dump movement."""

    def score(self, candidate: AnalysisCandidate, direction: str) -> Tuple[float, List[str]]:
        s = candidate.sensor_snapshot
        reasons: List[str] = []
        score = 0.0

        if direction == DIRECTION_LONG:
            if s.rsi_slope > 0:
                score += 12
                reasons.append("RSI_ALIGNED_LONG")
            if s.histogram_slope > 0:
                score += 14
                reasons.append("HIST_ALIGNED_LONG")
            if s.histogram_acceleration > 0:
                score += 10
                reasons.append("HIST_ACCEL_LONG")
            if s.power_delta > 8:
                score += 14
                reasons.append("POWER_ALIGNED_LONG")
            if s.vwap_state in {"RECLAIM", "ABOVE"}:
                score += 10
                reasons.append("VWAP_ALIGNED_LONG")
            if s.failed_breakdown or s.liquidity_grab_down:
                score += 10
                reasons.append("DOWN_SWEEP_RECOVERY_LONG")
        elif direction == DIRECTION_SHORT:
            if s.rsi_slope < 0:
                score += 12
                reasons.append("RSI_ALIGNED_SHORT")
            if s.histogram_slope < 0:
                score += 14
                reasons.append("HIST_ALIGNED_SHORT")
            if s.histogram_acceleration < 0:
                score += 10
                reasons.append("HIST_ACCEL_SHORT")
            if s.power_delta < -8:
                score += 14
                reasons.append("POWER_ALIGNED_SHORT")
            if s.vwap_state in {"LOSS", "BELOW"}:
                score += 10
                reasons.append("VWAP_ALIGNED_SHORT")
            if s.failed_breakout or s.liquidity_grab_up:
                score += 10
                reasons.append("UP_SWEEP_REVERSAL_SHORT")

        if s.atr_expansion == "EXPANDING":
            score += 8
            reasons.append("ATR_EXPANDING")
        if s.atr_explosion:
            score += 10
            reasons.append("ATR_EXPLOSION")
        if s.volume_expansion:
            score += 6
            reasons.append("VOLUME_EXPANDING")
        if s.volume_spike:
            score += 6
            reasons.append("VOLUME_SPIKE")

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
    ) -> Tuple[str, List[str]]:
        s = candidate.sensor_snapshot
        reasons: List[str] = []

        # Do not over-classify every range/late warning as a dead prediction.
        # Strong enough readiness can still be a learnable MID/GHOST candidate.
        if state.market_state == "RANGE" and movement.readiness_score < 45 and similarity < 55:
            reasons.append("PREDICT_RANGE")
            return PREDICT_RANGE, reasons

        if (
            state.market_state in {"EXHAUSTION", "LATE"}
            and movement.readiness_score < 45
            and similarity < 55
        ) or (movement.freshness == "DEAD" and movement.readiness_score < 40):
            reasons.append("PREDICT_LATE")
            return PREDICT_LATE, reasons

        if movement.freshness == "LATE" and movement.readiness_score < 45:
            reasons.append("PREDICT_LATE")
            return PREDICT_LATE, reasons

        if (
            similarity >= 65
            and movement.readiness_score < 60
            and s.compression_score >= 45
            and s.range_probability < 75
        ):
            reasons.append("PREDICT_PRE_START")
            return PREDICT_PRE_START, reasons

        if movement.freshness == "FRESH" and movement.readiness_score >= 60:
            reasons.append("PREDICT_START")
            return PREDICT_START, reasons

        if movement.readiness_score >= 45:
            reasons.append("PREDICT_MID")
            return PREDICT_MID, reasons

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
    ) -> Tuple[SimilarityBreakdown, float, float, float, List[str]]:
        direction = normalize_direction(candidate.direction_hint)
        reasons: List[str] = []

        memory_similarity, r = MemorySimilarityEngine().score(summary)
        reasons.extend(r)

        sensor_alignment, r = SensorAlignmentEngine().score(candidate, direction)
        reasons.extend(r)

        movement_alignment = clamp(
            movement.readiness_score * 0.55
            + movement.continuation_probability * 0.45
        )

        state_alignment = clamp(
            state.state_confidence
            - state.late_entry_risk * 0.35
            - state.exhaustion_risk * 0.35
        )

        trap_penalty = clamp(trap.trap_risk * 0.55 + trap.liquidity_risk * 0.25)
        range_penalty = clamp(state.range_probability * 0.45 + candidate.sensor_snapshot.compression_score * 0.12)
        exhaustion_penalty = clamp(state.exhaustion_risk * 0.55 + movement.reversal_pressure * 0.35)

        final_similarity = clamp(
            memory_similarity * 0.25
            + sensor_alignment * 0.30
            + movement_alignment * 0.27
            + state_alignment * 0.18
            - trap_penalty * 0.32
            - range_penalty * 0.18
            - exhaustion_penalty * 0.24
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
            reasons.append("PREDICTOR_LOW_DATA")
            prefer_ghost = True
        elif sample_count < 5:
            reasons.append("PREDICTOR_SMALL_SAMPLE")
            prefer_ghost = True

        if phase in {PREDICT_RANGE, PREDICT_LATE, PREDICT_UNKNOWN}:
            reasons.append("PREDICTOR_PHASE_NOT_IDEAL")
            prefer_ghost = True

        if trap.trap_risk >= 65 or state.late_entry_risk >= 65:
            reasons.append("PREDICTOR_RISK_REQUIRES_GHOST_CAUTION")
            prefer_ghost = True

        if probability >= 75 and sample_count >= 10 and not prefer_ghost:
            return CONF_HIGH, False, reasons
        if probability >= 60:
            return CONF_MEDIUM, prefer_ghost, reasons
        if probability >= 40:
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
        )

        phase, r = PhasePredictionEngine().classify(
            candidate=candidate,
            movement=movement,
            state=state,
            trap=trap,
            similarity=breakdown.final_similarity,
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
) -> MovementPredictionResult:
    return predictor().predict(
        candidate=candidate,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        movement_summary=movement_summary,
    )


def movement_predictor(
    candidate: AnalysisCandidate,
    movement: MovementHunterResult,
    trap: TrapResult,
    state: StateResult,
    confidence: Optional[ConfidenceResult] = None,
    movement_summary: Optional[MovementMemorySummary] = None,
) -> MovementPredictionResult:
    return predict_movement(
        candidate=candidate,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        movement_summary=movement_summary,
    )
