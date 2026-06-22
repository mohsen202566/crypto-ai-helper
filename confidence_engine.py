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


class DataConfidenceEngine:
    """Estimates confidence from historical sample counts and learning quality."""

    def score(self, candidate: AnalysisCandidate, learning_summary: Optional[Any] = None) -> Tuple[float, int, float, List[str]]:
        reasons: List[str] = []

        sample_count = int(_get_learning_value(learning_summary, "sample_count", 0) or 0)
        similar_win_rate = float(_get_learning_value(learning_summary, "similar_win_rate", 50.0) or 50.0)
        real_samples = int(_get_learning_value(learning_summary, "real_samples", 0) or 0)
        ghost_samples = int(_get_learning_value(learning_summary, "ghost_samples", 0) or 0)

        if sample_count <= 0:
            reasons.append("NO_SIMILAR_HISTORY")
            return 20.0, 0, 50.0, reasons

        # Data confidence grows with sample size but avoids overconfidence.
        sample_score = clamp(sample_count * 4.0)
        if sample_count >= 30:
            sample_score = 90.0
            reasons.append("HIGH_SAMPLE_COUNT")
        elif sample_count >= 10:
            sample_score = 70.0
            reasons.append("MEDIUM_SAMPLE_COUNT")
        else:
            reasons.append("LOW_SAMPLE_COUNT")

        real_weight_bonus = min(10.0, real_samples * 1.2)
        ghost_weight_bonus = min(5.0, ghost_samples * 0.25)

        wr_score = clamp(50.0 + (similar_win_rate - 50.0) * 0.8)
        confidence = clamp(sample_score * 0.55 + wr_score * 0.35 + real_weight_bonus + ghost_weight_bonus)

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
    """Detects out-of-distribution / unknown conditions."""

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

        sample_count = int(_get_learning_value(learning_summary, "sample_count", 0) or 0)

        if sample_count == 0:
            penalty += 35
            boundary = BOUNDARY_UNKNOWN
            reasons.append("UNKNOWN_CONDITION_NO_HISTORY")
        elif sample_count < 5:
            penalty += 22
            boundary = BOUNDARY_LOW_DATA
            reasons.append("LOW_DATA_CONDITION")

        if state.market_state == "UNKNOWN":
            penalty += 18
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

        return clamp(penalty), boundary, reasons


class ConfidenceLevelClassifier:
    """Classifies final confidence level and downgrade flags."""

    def classify(self, total: float, boundary: str, trap: TrapResult, state: StateResult) -> Tuple[str, bool, bool, List[str]]:
        reasons: List[str] = []
        downgrade = False
        reject_if_risk_high = False

        if boundary in {BOUNDARY_UNKNOWN, BOUNDARY_OUT_OF_DISTRIBUTION, BOUNDARY_LOW_DATA}:
            downgrade = True
            reasons.append("BOUNDARY_REQUIRES_GHOST_DOWNGRADE")

        if trap.trap_risk >= 75:
            downgrade = True
            reject_if_risk_high = True
            reasons.append("TRAP_RISK_LIMITS_CONFIDENCE")

        if state.market_state in {"RANGE", "EXHAUSTION"} and total < 75:
            downgrade = True
            reasons.append("STATE_LIMITS_CONFIDENCE")

        if total >= 78 and boundary == BOUNDARY_KNOWN and trap.trap_risk < 60:
            level = CONFIDENCE_HIGH
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

        # Balanced confidence:
        # Keep uncertainty/trap/conflict as real risk controls, but do not let
        # low-data or normal caution crush confidence to 0 and silence auto signals.
        positive_confidence = (
            data_score * 0.22
            + signal_confidence * 0.34
            + state_confidence * 0.18
            + movement_confidence * 0.26
        )

        risk_deduction = (
            trap_penalty * 0.42
            + conflict_penalty * 0.50
            + unknown_penalty * 0.35
        )

        # Low-data setups should usually downgrade to GHOST, not become zero-confidence.
        low_data_floor = 18.0 if sample_count <= 0 else 0.0
        if candidate.valid and movement.valid and trap.valid and state.valid:
            total = clamp(max(low_data_floor, positive_confidence - risk_deduction))
        else:
            total = clamp(positive_confidence - risk_deduction)

        if conflict_penalty >= 40:
            boundary = BOUNDARY_CONFLICTED
            warnings.append("HIGH_LAYER_CONFLICT")

        level, downgrade, reject_if_risk_high, r = self.classifier.classify(total, boundary, trap, state)
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
