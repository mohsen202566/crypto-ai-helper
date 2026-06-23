from __future__ import annotations

"""
11 - confidence_engine.py

Confidence Boundary Engine for the locked Movement Hunter architecture.

Responsibilities:
- Detect KNOWN vs UNKNOWN market/coin/condition states.
- Estimate confidence level using:
  AnalysisCandidate
  MovementHunterResult
  TrapResult
  StateResult
  optional learning summary / historical sample counts
- Downgrade uncertain states toward GHOST in ai_decision_engine.py.
- Provide confidence diagnostics, not trade decisions.

Strictly forbidden:
- No REAL/GHOST/REJECT.
- No trade execution.
- No Toobit calls.
- No Telegram.
- No persistence.
- No Paper mode.
- No Setup flow.

This file does not decide signals.
It only describes confidence and uncertainty.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4
import time

from analysis_engine import AnalysisCandidate
from movement_hunter import MovementHunterResult
from trap_engine import TrapResult
from state_engine import StateResult


JsonDict = Dict[str, Any]

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"
CONFIDENCE_UNKNOWN = "UNKNOWN"

BOUNDARY_KNOWN = "KNOWN"
BOUNDARY_LOW_DATA = "LOW_DATA"
BOUNDARY_UNKNOWN = "UNKNOWN"
BOUNDARY_OUT_OF_DISTRIBUTION = "OUT_OF_DISTRIBUTION"
BOUNDARY_CONFLICTED = "CONFLICTED"

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"


@dataclass(frozen=True)
class ConfidenceScore:
    data_confidence: float
    signal_confidence: float
    state_confidence: float
    movement_confidence: float
    trap_penalty: float
    conflict_penalty: float
    unknown_penalty: float
    total_confidence: float

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class ConfidenceResult:
    confidence_id: str
    symbol: str
    timeframe: str
    timestamp: int
    direction_hint: str
    confidence_level: str
    boundary_state: str
    confidence_score: float
    known_sample_count: int
    similar_win_rate: float
    should_downgrade_to_ghost: bool
    should_reject_if_risk_high: bool
    score: ConfidenceScore
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    valid: bool = True

    def to_dict(self) -> JsonDict:
        return {
            "confidence_id": self.confidence_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp,
            "direction_hint": self.direction_hint,
            "confidence_level": self.confidence_level,
            "boundary_state": self.boundary_state,
            "confidence_score": self.confidence_score,
            "known_sample_count": self.known_sample_count,
            "similar_win_rate": self.similar_win_rate,
            "should_downgrade_to_ghost": self.should_downgrade_to_ghost,
            "should_reject_if_risk_high": self.should_reject_if_risk_high,
            "score": self.score.to_dict(),
            "reason_codes": list(self.reason_codes),
            "warnings": list(self.warnings),
            "valid": self.valid,
        }


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except Exception:
        return low


def avg(values: Sequence[float]) -> float:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _get_learning_value(learning_summary: Optional[Any], key: str, default: Any = None) -> Any:
    if learning_summary is None:
        return default
    if isinstance(learning_summary, dict):
        return learning_summary.get(key, default)
    return getattr(learning_summary, key, default)


def _safe_learning_float(learning_summary: Optional[Any], key: str, default: float = 0.0) -> float:
    try:
        value = _get_learning_value(learning_summary, key, default)
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_learning_int(learning_summary: Optional[Any], key: str, default: int = 0) -> int:
    try:
        value = _get_learning_value(learning_summary, key, default)
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _learning_notes(learning_summary: Optional[Any]) -> Tuple[str, ...]:
    notes = _get_learning_value(learning_summary, "notes", ())
    if notes is None:
        return ()
    if isinstance(notes, str):
        return (notes,)
    try:
        return tuple(str(x) for x in notes)
    except Exception:
        return ()


class DataConfidenceEngine:
    """Estimates confidence from historical sample counts and learning quality.

    Movement Hunter rule:
    - Result history matters.
    - Timing history matters too.
    - Ghost samples are useful training data, not noise.
    - Low data is caution, not blindness.
    """

    def score(self, candidate: AnalysisCandidate, learning_summary: Optional[Any] = None) -> Tuple[float, int, float, List[str]]:
        reasons: List[str] = []

        sample_count = _safe_learning_int(learning_summary, "sample_count", 0)
        similar_win_rate = _safe_learning_float(learning_summary, "similar_win_rate", 50.0)
        real_samples = _safe_learning_int(learning_summary, "real_samples", 0)
        ghost_samples = _safe_learning_int(learning_summary, "ghost_samples", 0)

        # New fields from movement_memory.py. They may not exist in older summaries,
        # so every read is backward-compatible.
        outcome_success_rate = _safe_learning_float(learning_summary, "outcome_success_rate", similar_win_rate)
        timing_score = _safe_learning_float(learning_summary, "timing_score", 50.0)
        early_success_rate = _safe_learning_float(learning_summary, "early_success_rate", 0.0)
        fuzzy_match_score = _safe_learning_float(learning_summary, "fuzzy_match_score", 0.0)
        notes = _learning_notes(learning_summary)

        if sample_count <= 0:
            reasons.append("NO_SIMILAR_HISTORY_SOFT")
            # No memory should not bury a fresh movement. It only means the AI
            # should be cautious and let ai_decision_engine decide GHOST/REAL.
            return 28.0, 0, 50.0, reasons

        # Sample confidence grows with both REAL and GHOST data. Ghost is the AI's
        # practice school, so it gets meaningful credit but still less than REAL.
        sample_score = clamp(sample_count * 5.0)
        if sample_count >= 30:
            sample_score = 92.0
            reasons.append("HIGH_SAMPLE_COUNT")
        elif sample_count >= 10:
            sample_score = 74.0
            reasons.append("MEDIUM_SAMPLE_COUNT")
        elif sample_count >= 5:
            sample_score = 56.0
            reasons.append("SMALL_BUT_USABLE_SAMPLE_COUNT")
        else:
            sample_score = 42.0
            reasons.append("LOW_SAMPLE_COUNT_SOFT")

        real_weight_bonus = min(12.0, real_samples * 1.4)
        ghost_weight_bonus = min(11.0, ghost_samples * 0.55)

        wr_score = clamp(50.0 + (similar_win_rate - 50.0) * 0.75)
        outcome_score = clamp(50.0 + (outcome_success_rate - 50.0) * 0.85)
        timing_component = clamp(timing_score)
        early_component = clamp(50.0 + early_success_rate * 0.55)
        fuzzy_component = clamp(fuzzy_match_score)

        confidence = clamp(
            sample_score * 0.26
            + wr_score * 0.20
            + outcome_score * 0.20
            + timing_component * 0.18
            + early_component * 0.10
            + fuzzy_component * 0.06
            + real_weight_bonus
            + ghost_weight_bonus
        )

        if timing_score >= 65:
            reasons.append("LEARNING_TIMING_SUPPORTS_CONFIDENCE")
        elif timing_score <= 40 and sample_count >= 5:
            reasons.append("LEARNING_TIMING_WEAK")

        if early_success_rate >= 45 and sample_count >= 5:
            reasons.append("EARLY_SUCCESS_HISTORY_SUPPORTS_CONFIDENCE")

        if fuzzy_match_score >= 70:
            reasons.append("FUZZY_MEMORY_MATCH_STRONG")
        elif fuzzy_match_score > 0 and fuzzy_match_score < 55:
            reasons.append("FUZZY_MEMORY_MATCH_WEAK")

        if "PREMOVE_PATTERN_WEAK_OR_LATE" in notes:
            confidence = clamp(confidence - 8.0)
            reasons.append("MEMORY_PATTERN_WEAK_OR_LATE_REDUCED_CONFIDENCE")
        if "PREMOVE_PATTERN_WORKED_WITH_TIMING" in notes:
            confidence = clamp(confidence + 5.0)
            reasons.append("MEMORY_PATTERN_WORKED_WITH_TIMING_BONUS")

        return confidence, sample_count, similar_win_rate, reasons

class ConflictDetector:
    """Detects disagreement between analysis, movement, trap and state layers."""

    def score(
        self,
        candidate: AnalysisCandidate,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
    ) -> Tuple[float, List[str]]:
        penalty = 0.0
        reasons: List[str] = []

        if candidate.direction_hint != movement.direction_hint:
            penalty += 18
            reasons.append("CANDIDATE_MOVEMENT_DIRECTION_CONFLICT")

        if candidate.direction_hint != trap.direction_hint:
            penalty += 12
            reasons.append("CANDIDATE_TRAP_DIRECTION_CONFLICT")

        if state.market_state in {"RANGE", "EXHAUSTION", "REVERSAL"} and movement.freshness == "FRESH":
            penalty += 20
            reasons.append("STATE_CONFLICTS_WITH_FRESH_MOVEMENT")

        if trap.trap_risk >= 65 and movement.readiness_score >= 70:
            penalty += 22
            reasons.append("HIGH_READINESS_HIGH_TRAP_CONFLICT")

        if candidate.quality.total_quality >= 70 and candidate.risk.total_risk >= 65:
            penalty += 18
            reasons.append("HIGH_QUALITY_HIGH_RISK_CONFLICT")

        if movement.continuation_probability < 35 and candidate.direction_score.gap >= 25:
            penalty += 14
            reasons.append("DIRECTION_STRONG_BUT_CONTINUATION_WEAK")

        return clamp(penalty), reasons


class UnknownStateDetector:
    """Detects out-of-distribution / unknown conditions.

    Low-data should encourage Ghost when uncertain, but it must not erase fresh
    high-quality movement births. Real invalid sensor data remains dangerous.
    """

    def score(
        self,
        candidate: AnalysisCandidate,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
        learning_summary: Optional[Any] = None,
    ) -> Tuple[float, str, List[str]]:
        penalty = 0.0
        boundary = BOUNDARY_KNOWN
        reasons: List[str] = []

        sample_count = _safe_learning_int(learning_summary, "sample_count", 0)
        timing_score = _safe_learning_float(learning_summary, "timing_score", 50.0)
        early_success_rate = _safe_learning_float(learning_summary, "early_success_rate", 0.0)
        fuzzy_match_score = _safe_learning_float(learning_summary, "fuzzy_match_score", 0.0)

        fresh_live_move = (
            movement.freshness in {"FRESH", "MID"}
            and movement.readiness_score >= 45
            and movement.continuation_probability >= 35
        )
        learning_timing_support = (
            sample_count >= 3
            and (timing_score >= 62 or early_success_rate >= 35 or fuzzy_match_score >= 68)
        )

        if sample_count == 0:
            penalty += 24 if fresh_live_move else 32
            boundary = BOUNDARY_UNKNOWN
            reasons.append("UNKNOWN_CONDITION_NO_HISTORY_SOFT")
        elif sample_count < 5:
            penalty += 12 if (fresh_live_move or learning_timing_support) else 20
            boundary = BOUNDARY_LOW_DATA
            reasons.append("LOW_DATA_CONDITION_SOFT")

        if state.market_state == "UNKNOWN":
            penalty += 14
            boundary = BOUNDARY_UNKNOWN
            reasons.append("UNKNOWN_STATE")

        if candidate.sensor_snapshot.valid is False:
            penalty += 40
            boundary = BOUNDARY_OUT_OF_DISTRIBUTION
            reasons.append("INVALID_SENSOR_DATA")

        # Extreme sensor values can be out of distribution for scalping.
        s = candidate.sensor_snapshot
        if s.atr_percent > 5.0:
            penalty += 20
            boundary = BOUNDARY_OUT_OF_DISTRIBUTION
            reasons.append("ATR_PERCENT_EXTREME")
        if s.relative_volume > 8.0:
            penalty += 15
            boundary = BOUNDARY_OUT_OF_DISTRIBUTION
            reasons.append("RELATIVE_VOLUME_EXTREME")
        if abs(s.power_delta) > 90 and trap.trap_risk >= 50:
            penalty += 18
            boundary = BOUNDARY_OUT_OF_DISTRIBUTION
            reasons.append("EXTREME_POWER_WITH_TRAP_RISK")

        if learning_timing_support and boundary in {BOUNDARY_LOW_DATA, BOUNDARY_UNKNOWN}:
            penalty = max(0.0, penalty - 6.0)
            reasons.append("TIMING_MEMORY_SOFTENS_UNKNOWN_BOUNDARY")

        return clamp(penalty), boundary, reasons

class ConfidenceLevelClassifier:
    """Classifies final confidence level and downgrade flags."""

    def classify(
        self,
        total: float,
        boundary: str,
        trap: TrapResult,
        state: StateResult,
        movement: Optional[MovementHunterResult] = None,
        learning_summary: Optional[Any] = None,
    ) -> Tuple[str, bool, bool, List[str]]:
        reasons: List[str] = []
        downgrade = False
        reject_if_risk_high = False

        timing_score = _safe_learning_float(learning_summary, "timing_score", 50.0)
        early_success_rate = _safe_learning_float(learning_summary, "early_success_rate", 0.0)
        fuzzy_match_score = _safe_learning_float(learning_summary, "fuzzy_match_score", 0.0)
        sample_count = _safe_learning_int(learning_summary, "sample_count", 0)

        fresh_supported = bool(
            movement is not None
            and movement.freshness in {"FRESH", "MID"}
            and movement.readiness_score >= 45
            and movement.continuation_probability >= 35
        )
        memory_supported = bool(
            sample_count >= 3
            and (timing_score >= 62 or early_success_rate >= 40 or fuzzy_match_score >= 70)
        )

        if boundary in {BOUNDARY_UNKNOWN, BOUNDARY_OUT_OF_DISTRIBUTION, BOUNDARY_LOW_DATA}:
            downgrade = True
            reasons.append("BOUNDARY_REQUIRES_GHOST_DOWNGRADE")

        # Allow the AI brain to consider strong fresh/memory-supported cases;
        # do not force downgrade only because data is still small.
        if boundary == BOUNDARY_LOW_DATA and fresh_supported and total >= 58 and trap.trap_risk < 65:
            downgrade = False
            reasons.append("LOW_DATA_OVERRIDDEN_BY_FRESH_CONFIDENCE")
        elif boundary == BOUNDARY_UNKNOWN and fresh_supported and memory_supported and total >= 64 and trap.trap_risk < 60:
            downgrade = False
            reasons.append("UNKNOWN_SOFTENED_BY_FRESH_MEMORY_SUPPORT")

        if trap.trap_risk >= 75:
            downgrade = True
            reject_if_risk_high = True
            reasons.append("TRAP_RISK_LIMITS_CONFIDENCE")

        if state.market_state in {"RANGE", "EXHAUSTION"} and total < 75:
            # Fresh movement can still be Ghost/learnable, but not blindly trusted.
            downgrade = True
            reasons.append("STATE_LIMITS_CONFIDENCE")

        if total >= 78 and boundary == BOUNDARY_KNOWN and trap.trap_risk < 60:
            level = CONFIDENCE_HIGH
        elif total >= 66 and fresh_supported and memory_supported and trap.trap_risk < 65:
            level = CONFIDENCE_HIGH
            reasons.append("HUNTER_MEMORY_CONFIDENCE_HIGH")
        elif total >= 55:
            level = CONFIDENCE_MEDIUM
        elif total >= 35:
            level = CONFIDENCE_LOW
            downgrade = True
        else:
            level = CONFIDENCE_UNKNOWN
            downgrade = True
            reject_if_risk_high = True

        return level, downgrade, reject_if_risk_high, reasons

class ConfidenceEngine:
    """
    Main confidence boundary engine.

    This engine does not decide REAL/GHOST/REJECT.
    ai_decision_engine.py will use its downgrade flags.
    """

    def __init__(self):
        self.data_confidence = DataConfidenceEngine()
        self.conflict = ConflictDetector()
        self.unknown = UnknownStateDetector()
        self.classifier = ConfidenceLevelClassifier()

    def analyze(
        self,
        candidate: AnalysisCandidate,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
        learning_summary: Optional[Any] = None,
    ) -> ConfidenceResult:
        reasons: List[str] = []
        warnings: List[str] = []

        data_score, sample_count, similar_wr, r = self.data_confidence.score(candidate, learning_summary)
        reasons.extend(r)

        signal_confidence = clamp(
            candidate.quality.total_quality * 0.45
            + movement.readiness_score * 0.35
            + movement.continuation_probability * 0.20
        )

        state_confidence = clamp(state.state_confidence)
        movement_confidence = clamp(avg([movement.readiness_score, movement.continuation_probability]))

        trap_penalty = clamp(trap.trap_risk * 0.55)
        conflict_penalty, r = self.conflict.score(candidate, movement, trap, state)
        reasons.extend(r)

        unknown_penalty, boundary, r = self.unknown.score(candidate, movement, trap, state, learning_summary)
        reasons.extend(r)

        timing_score = _safe_learning_float(learning_summary, "timing_score", 50.0)
        early_success_rate = _safe_learning_float(learning_summary, "early_success_rate", 0.0)
        fuzzy_match_score = _safe_learning_float(learning_summary, "fuzzy_match_score", 0.0)
        outcome_success_rate = _safe_learning_float(learning_summary, "outcome_success_rate", similar_wr)

        learning_timing_confidence = clamp(
            timing_score * 0.45
            + early_success_rate * 0.30
            + fuzzy_match_score * 0.15
            + outcome_success_rate * 0.10
        )

        # Movement Hunter confidence:
        # data/learning timing must actively help confidence, not just sit in reports.
        positive_confidence = (
            data_score * 0.24
            + signal_confidence * 0.30
            + state_confidence * 0.15
            + movement_confidence * 0.22
            + learning_timing_confidence * 0.09
        )

        risk_deduction = (
            trap_penalty * 0.40
            + conflict_penalty * 0.48
            + unknown_penalty * 0.30
        )

        # Low-data / cautious states should downgrade to GHOST, not become zero-confidence.
        # Only truly broken input data is allowed to collapse confidence toward 0.
        low_data_floor = 24.0 if sample_count <= 0 else 14.0

        sensor = getattr(candidate, "sensor_snapshot", None)
        try:
            sensor_price = float(getattr(sensor, "price", 0.0) or 0.0)
        except Exception:
            sensor_price = 0.0

        has_real_invalid_data = (
            not bool(candidate.valid)
            or sensor is None
            or getattr(sensor, "valid", True) is False
            or sensor_price <= 0.0
        )

        if has_real_invalid_data:
            reasons.append("CONFIDENCE_ZERO_ALLOWED_INVALID_DATA")
            total = clamp(positive_confidence - risk_deduction)
        else:
            total = clamp(max(low_data_floor, positive_confidence - risk_deduction))

        if conflict_penalty >= 40:
            boundary = BOUNDARY_CONFLICTED
            warnings.append("HIGH_LAYER_CONFLICT")

        level, downgrade, reject_if_risk_high, r = self.classifier.classify(
            total, boundary, trap, state, movement=movement, learning_summary=learning_summary
        )
        reasons.extend(r)

        if downgrade:
            warnings.append("CONFIDENCE_DOWNGRADE_RECOMMENDED")
        if boundary != BOUNDARY_KNOWN:
            warnings.append(f"BOUNDARY_{boundary}")

        score = ConfidenceScore(
            data_confidence=data_score,
            signal_confidence=signal_confidence,
            state_confidence=state_confidence,
            movement_confidence=movement_confidence,
            trap_penalty=trap_penalty,
            conflict_penalty=conflict_penalty,
            unknown_penalty=unknown_penalty,
            total_confidence=total,
        )

        return ConfidenceResult(
            confidence_id=f"conf_{uuid4().hex}",
            symbol=candidate.symbol,
            timeframe=candidate.timeframe,
            timestamp=candidate.timestamp or int(time.time()),
            direction_hint=candidate.direction_hint,
            confidence_level=level,
            boundary_state=boundary,
            confidence_score=total,
            known_sample_count=sample_count,
            similar_win_rate=similar_wr,
            should_downgrade_to_ghost=downgrade,
            should_reject_if_risk_high=reject_if_risk_high,
            score=score,
            reason_codes=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
            valid=bool(candidate.valid and movement.valid and trap.valid and state.valid),
        )


_default_engine: Optional[ConfidenceEngine] = None


def engine() -> ConfidenceEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = ConfidenceEngine()
    return _default_engine


def analyze_confidence(
    candidate: AnalysisCandidate,
    movement: MovementHunterResult,
    trap: TrapResult,
    state: StateResult,
    learning_summary: Optional[Any] = None,
) -> ConfidenceResult:
    return engine().analyze(
        candidate=candidate,
        movement=movement,
        trap=trap,
        state=state,
        learning_summary=learning_summary,
    )


def confidence_engine(
    candidate: AnalysisCandidate,
    movement: MovementHunterResult,
    trap: TrapResult,
    state: StateResult,
    learning_summary: Optional[Any] = None,
) -> ConfidenceResult:
    return analyze_confidence(
        candidate=candidate,
        movement=movement,
        trap=trap,
        state=state,
        learning_summary=learning_summary,
    )
