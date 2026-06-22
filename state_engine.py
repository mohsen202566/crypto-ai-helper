from __future__ import annotations

"""
10 - state_engine.py

Market / movement state classification layer.

Responsibilities:
- Classify candidate state:
  START, MIDDLE, LATE, EXHAUSTION, REVERSAL, RANGE, UNKNOWN
- Detect trend-to-range, range-to-trend, bull-to-bear, bear-to-bull context.
- Provide StateResult for AI decision layer.

Strictly forbidden:
- No REAL/GHOST/REJECT.
- No trade execution.
- No Toobit calls.
- No Telegram.
- No persistence.
- No Paper mode.
- No Setup flow.

This file describes state only.
Final decision is only in ai_decision_engine.py.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4
import time

from analysis_layers import SensorSnapshot
from analysis_engine import AnalysisCandidate
from movement_hunter import MovementHunterResult
from trap_engine import TrapResult


JsonDict = Dict[str, Any]

STATE_START = "START"
STATE_MIDDLE = "MIDDLE"
STATE_LATE = "LATE"
STATE_EXHAUSTION = "EXHAUSTION"
STATE_REVERSAL = "REVERSAL"
STATE_RANGE = "RANGE"
STATE_UNKNOWN = "UNKNOWN"

TRANSITION_NONE = "NONE"
TRANSITION_RANGE_TO_TREND = "RANGE_TO_TREND"
TRANSITION_TREND_TO_RANGE = "TREND_TO_RANGE"
TRANSITION_BULL_TO_BEAR = "BULL_TO_BEAR"
TRANSITION_BEAR_TO_BULL = "BEAR_TO_BULL"
TRANSITION_BREAKOUT = "BREAKOUT"
TRANSITION_BREAKDOWN = "BREAKDOWN"

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"


@dataclass(frozen=True)
class StateScore:
    start_score: float
    middle_score: float
    late_score: float
    exhaustion_score: float
    reversal_score: float
    range_score: float

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class StateResult:
    state_id: str
    symbol: str
    timeframe: str
    timestamp: int
    direction_hint: str
    market_state: str
    transition_state: str
    state_confidence: float
    reversal_probability: float
    range_probability: float
    late_entry_risk: float
    exhaustion_risk: float
    score: StateScore
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    valid: bool = True

    def to_dict(self) -> JsonDict:
        return {
            "state_id": self.state_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp,
            "direction_hint": self.direction_hint,
            "market_state": self.market_state,
            "transition_state": self.transition_state,
            "state_confidence": self.state_confidence,
            "reversal_probability": self.reversal_probability,
            "range_probability": self.range_probability,
            "late_entry_risk": self.late_entry_risk,
            "exhaustion_risk": self.exhaustion_risk,
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


class StateScorer:
    """Builds state scores from sensors and movement/trap context."""

    def score(
        self,
        snapshot: SensorSnapshot,
        direction: str,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
    ) -> Tuple[StateScore, List[str]]:
        reasons: List[str] = []

        start = 0.0
        middle = 0.0
        late = 0.0
        exhaustion = 0.0
        reversal = 0.0
        range_score = clamp(snapshot.range_probability)

        if snapshot.range_probability >= 70:
            reasons.append("STATE_RANGE_HIGH_PROBABILITY")

        # Start / early state signals.
        if abs(snapshot.rsi_slope) > 0.25:
            start += 12
            reasons.append("STATE_RSI_SLOPE_START")
        if abs(snapshot.histogram_slope) > 0:
            start += 12
            reasons.append("STATE_HISTOGRAM_SLOPE_START")
        if snapshot.atr_expansion == "EXPANDING":
            start += 16
            reasons.append("STATE_ATR_EXPANDING")
        if snapshot.atr_explosion:
            start += 22
            reasons.append("STATE_ATR_EXPLOSION")
        if snapshot.volume_expansion:
            start += 10
            reasons.append("STATE_VOLUME_EXPANSION")
        if snapshot.breakout_candidate or snapshot.breakdown_candidate:
            start += 16
            reasons.append("STATE_BREAK_CANDIDATE")

        # Middle state signals.
        if snapshot.adx >= 20 and not snapshot.momentum_weakness:
            middle += 18
            reasons.append("STATE_ADX_TRENDING")
        if snapshot.relative_volume >= 1.0 and snapshot.atr_expansion in {"EXPANDING", "NORMAL"}:
            middle += 14
            reasons.append("STATE_PARTICIPATION_NORMAL")
        if abs(snapshot.power_delta) >= 12:
            middle += 12
            reasons.append("STATE_POWER_DOMINANT")

        # Late state / exhaustion.
        # Softer late detection for Movement Hunter:
        # early pump/dump hunting must not mark a fresh expanding move as LATE too soon.
        if abs(snapshot.price_change_percent) > max(snapshot.atr_percent * 2.8, 1.8):
            late += 18
            reasons.append("STATE_PRICE_EXTENDED_SOFT")
        if snapshot.momentum_weakness:
            late += 20
            exhaustion += 35
            reasons.append("STATE_MOMENTUM_WEAKNESS")
        if direction == DIRECTION_LONG and snapshot.bull_exhaustion:
            exhaustion += 45
            reasons.append("STATE_BULL_EXHAUSTION")
        if direction == DIRECTION_SHORT and snapshot.bear_exhaustion:
            exhaustion += 45
            reasons.append("STATE_BEAR_EXHAUSTION")

        # Reversal state.
        if snapshot.failed_breakout or snapshot.failed_breakdown:
            reversal += 30
            reasons.append("STATE_FAILED_BREAK_REVERSAL")
        if snapshot.liquidity_grab_up or snapshot.liquidity_grab_down:
            reversal += 24
            reasons.append("STATE_LIQUIDITY_GRAB_REVERSAL")
        # Trap pressure should warn, not dry REAL too early.
        # After trap_engine was softened, only stronger trap risk should push reversal state.
        if trap is not None and trap.trap_risk >= 70:
            reversal += 16
            reasons.append("STATE_TRAP_REVERSAL_PRESSURE_SOFT")
        if movement is not None and movement.reversal_pressure >= 60:
            reversal += 20
            reasons.append("STATE_MOVEMENT_REVERSAL_PRESSURE")

        if movement is not None:
            if movement.freshness == "FRESH":
                start += 20
                reasons.append("STATE_MOVEMENT_FRESH")
            elif movement.freshness == "MID":
                middle += 14
                reasons.append("STATE_MOVEMENT_MID")
            elif movement.freshness in {"LATE", "DEAD"}:
                late += 24
                reasons.append("STATE_MOVEMENT_LATE_DEAD")

        return StateScore(
            start_score=clamp(start),
            middle_score=clamp(middle),
            late_score=clamp(late),
            exhaustion_score=clamp(exhaustion),
            reversal_score=clamp(reversal),
            range_score=clamp(range_score),
        ), reasons


class TransitionClassifier:
    """Classifies state transition type."""

    def classify(self, snapshot: SensorSnapshot, score: StateScore, direction: str) -> Tuple[str, List[str]]:
        reasons: List[str] = []

        # Avoid calling trend-to-range too early when start evidence exists.
        if score.range_score >= 72 and score.start_score < 42 and score.middle_score < 45:
            reasons.append("TRANSITION_TREND_TO_RANGE_SOFT")
            return TRANSITION_TREND_TO_RANGE, reasons

        # Movement Hunter priority: compression + ATR expansion/explosion is often pre-start/start.
        if snapshot.compression_score >= 60 and (snapshot.atr_explosion or snapshot.atr_expansion == "EXPANDING"):
            reasons.append("TRANSITION_RANGE_TO_TREND_EARLY")
            return TRANSITION_RANGE_TO_TREND, reasons

        if snapshot.breakout_candidate:
            reasons.append("TRANSITION_BREAKOUT")
            return TRANSITION_BREAKOUT, reasons

        if snapshot.breakdown_candidate:
            reasons.append("TRANSITION_BREAKDOWN")
            return TRANSITION_BREAKDOWN, reasons

        if direction == DIRECTION_LONG and (
            snapshot.failed_breakout
            or snapshot.liquidity_grab_up
            or snapshot.histogram_slope < 0
        ):
            if score.reversal_score >= 45:
                reasons.append("TRANSITION_BULL_TO_BEAR")
                return TRANSITION_BULL_TO_BEAR, reasons

        if direction == DIRECTION_SHORT and (
            snapshot.failed_breakdown
            or snapshot.liquidity_grab_down
            or snapshot.histogram_slope > 0
        ):
            if score.reversal_score >= 45:
                reasons.append("TRANSITION_BEAR_TO_BULL")
                return TRANSITION_BEAR_TO_BULL, reasons

        return TRANSITION_NONE, reasons


class StateClassifier:
    """Chooses final descriptive state from scores."""

    def classify(self, score: StateScore) -> Tuple[str, float]:
        candidates = {
            STATE_START: score.start_score,
            STATE_MIDDLE: score.middle_score,
            STATE_LATE: score.late_score,
            STATE_EXHAUSTION: score.exhaustion_score,
            STATE_REVERSAL: score.reversal_score,
            STATE_RANGE: score.range_score,
        }

        state, value = max(candidates.items(), key=lambda kv: kv[1])

        # Guardrails: exhaustion/reversal/range override weak start.
        # Softer overrides: do not let range/exhaustion/reversal kill early movement too fast.
        if score.exhaustion_score >= 72 and score.start_score < 55:
            return STATE_EXHAUSTION, clamp(score.exhaustion_score)
        if score.reversal_score >= 76 and score.start_score < 58:
            return STATE_REVERSAL, clamp(score.reversal_score)
        if score.range_score >= 82 and score.start_score < 55 and score.middle_score < 50:
            return STATE_RANGE, clamp(score.range_score)

        # If start evidence is strong, prefer START even inside compression/range context.
        if score.start_score >= 62 and score.start_score >= score.late_score and score.start_score >= score.exhaustion_score:
            return STATE_START, clamp(score.start_score)

        if value < 25:
            return STATE_UNKNOWN, clamp(value)

        return state, clamp(value)


class StateEngine:
    """
    Main state engine.

    Input:
        AnalysisCandidate or SensorSnapshot, optional MovementHunterResult and TrapResult.

    Output:
        StateResult.

    This is descriptive context for AI, not a trade decision.
    """

    def __init__(self):
        self.scorer = StateScorer()
        self.transition = TransitionClassifier()
        self.classifier = StateClassifier()

    def analyze(
        self,
        candidate_or_snapshot: AnalysisCandidate | SensorSnapshot,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
    ) -> StateResult:
        if isinstance(candidate_or_snapshot, AnalysisCandidate):
            snapshot = candidate_or_snapshot.sensor_snapshot
            direction = candidate_or_snapshot.direction_hint
            base_warnings = list(candidate_or_snapshot.warnings)
        else:
            snapshot = candidate_or_snapshot
            direction = self._infer_direction(snapshot)
            base_warnings = list(getattr(snapshot, "warnings", ()))

        warnings: List[str] = list(base_warnings)
        reasons: List[str] = []

        score, r = self.scorer.score(snapshot, direction, movement=movement, trap=trap)
        reasons.extend(r)

        transition, r = self.transition.classify(snapshot, score, direction)
        reasons.extend(r)

        state, confidence = self.classifier.classify(score)

        reversal_probability = clamp(
            score.reversal_score * 0.55
            + score.exhaustion_score * 0.25
            + (trap.trap_risk * 0.20 if trap else 0)
        )

        # Softer late-entry risk: range alone should not heavily punish early movement hunting.
        late_entry_risk = clamp(
            score.late_score * 0.46
            + score.exhaustion_score * 0.32
            + snapshot.range_probability * 0.10
        )

        exhaustion_risk = clamp(score.exhaustion_score)

        if state in {STATE_LATE, STATE_EXHAUSTION}:
            warnings.append("LATE_OR_EXHAUSTED_STATE")
        if state == STATE_RANGE:
            warnings.append("RANGE_STATE")
        if reversal_probability >= 65:
            warnings.append("HIGH_REVERSAL_PROBABILITY")
        if not snapshot.valid:
            warnings.append("INVALID_SENSOR_SNAPSHOT")

        return StateResult(
            state_id=f"state_{uuid4().hex}",
            symbol=snapshot.symbol,
            timeframe=snapshot.timeframe,
            timestamp=snapshot.timestamp or int(time.time()),
            direction_hint=direction,
            market_state=state,
            transition_state=transition,
            state_confidence=confidence,
            reversal_probability=reversal_probability,
            range_probability=clamp(snapshot.range_probability),
            late_entry_risk=late_entry_risk,
            exhaustion_risk=exhaustion_risk,
            score=score,
            reason_codes=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
            valid=bool(snapshot.valid),
        )

    def _infer_direction(self, snapshot: SensorSnapshot) -> str:
        long_points = 0
        short_points = 0

        if snapshot.rsi_slope > 0:
            long_points += 1
        elif snapshot.rsi_slope < 0:
            short_points += 1

        if snapshot.histogram_slope > 0:
            long_points += 1
        elif snapshot.histogram_slope < 0:
            short_points += 1

        if snapshot.power_delta > 0:
            long_points += 1
        elif snapshot.power_delta < 0:
            short_points += 1

        if snapshot.vwap_state in {"ABOVE", "RECLAIM"}:
            long_points += 1
        elif snapshot.vwap_state in {"BELOW", "LOSS"}:
            short_points += 1

        if long_points > short_points:
            return DIRECTION_LONG
        if short_points > long_points:
            return DIRECTION_SHORT
        return DIRECTION_NEUTRAL


_default_engine: Optional[StateEngine] = None


def engine() -> StateEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = StateEngine()
    return _default_engine


def analyze_state(
    candidate_or_snapshot: AnalysisCandidate | SensorSnapshot,
    movement: Optional[MovementHunterResult] = None,
    trap: Optional[TrapResult] = None,
) -> StateResult:
    return engine().analyze(candidate_or_snapshot, movement=movement, trap=trap)


def state_engine(
    candidate_or_snapshot: AnalysisCandidate | SensorSnapshot,
    movement: Optional[MovementHunterResult] = None,
    trap: Optional[TrapResult] = None,
) -> StateResult:
    return analyze_state(candidate_or_snapshot, movement=movement, trap=trap)
