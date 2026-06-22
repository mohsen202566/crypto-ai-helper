from __future__ import annotations

"""
18 - ai_decision_engine.py

Final AI decision layer for the locked Movement Hunter architecture.

Responsibilities:
- Be the ONLY component allowed to output:
  REAL / GHOST / REJECT
- Combine all previous layers:
  AnalysisCandidate
  MovementHunterResult
  TrapResult
  StateResult
  ConfidenceResult
  CorrelationResult
  LearningSummary
  MovementPredictionResult
  MetaLearningSummary / module weights
- Decide final direction, entry readiness, decision type, and decision reasons.
- Keep output simple and structured for tp_sl_engine.py and real_trade_manager.py.

Strictly forbidden in every other file:
- REAL/GHOST/REJECT final decision.

Strictly forbidden in this file:
- No Toobit order execution.
- No Telegram sending.
- No persistence side effects by default.
- No Paper mode.
- No Setup flow.

This file decides only. It does not open trades.
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
from correlation_engine import CorrelationResult
from coin_learning import LearningSummary
from movement_predictor import MovementPredictionResult
from meta_learning import MetaLearningSummary, get_meta_learning_summary
from config import SETTINGS


JsonDict = Dict[str, Any]

DECISION_REAL = "REAL"
DECISION_GHOST = "GHOST"
DECISION_REJECT = "REJECT"

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"

REJECT_REASON_HIGH_TRAP = "HIGH_TRAP_RISK"
REJECT_REASON_RANGE = "RANGE_MARKET"
REJECT_REASON_EXHAUSTED = "EXHAUSTED_OR_LATE"
REJECT_REASON_LOW_CONFIDENCE = "LOW_CONFIDENCE"
REJECT_REASON_LOW_MOVEMENT = "LOW_MOVEMENT_PROBABILITY"
REJECT_REASON_CORRELATION = "CORRELATION_LIMIT"
REJECT_REASON_INVALID = "INVALID_INPUT"


@dataclass(frozen=True)
class DecisionScore:
    analysis_score: float
    movement_score: float
    prediction_score: float
    confidence_score: float
    learning_score: float
    state_score: float
    trap_penalty: float
    correlation_penalty: float
    range_penalty: float
    late_penalty: float
    final_score: float

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class AIDecision:
    decision_id: str
    symbol: str
    timeframe: str
    timestamp: int
    direction: str
    decision_type: str

    confidence_score: float
    risk_score: float
    ai_score: float

    entry: float
    tp1: float = 0.0
    tp2: float = 0.0
    sl: float = 0.0
    tp_mode: str = "PENDING_TP_SL_ENGINE"

    movement_phase: str = "UNKNOWN"
    freshness: str = "UNKNOWN"
    market_state: str = "UNKNOWN"
    predicted_phase: str = "UNKNOWN"

    should_trade_real: bool = False
    should_create_ghost: bool = False
    should_reject: bool = False

    score: DecisionScore = field(default_factory=lambda: DecisionScore(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    reject_reasons: Tuple[str, ...] = field(default_factory=tuple)
    meta: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


def now_ts() -> int:
    return int(time.time())


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


def _meta_weight(meta: Optional[MetaLearningSummary], module_name: str, default: float = 1.0) -> float:
    if meta is None:
        return default
    try:
        return safe_float(meta.module_weights.get(module_name, default), default)
    except Exception:
        return default


class DecisionInputValidator:
    """Hard safety validation before any AI decision."""

    def validate(
        self,
        candidate: AnalysisCandidate,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
        confidence: ConfidenceResult,
        correlation: CorrelationResult,
        prediction: MovementPredictionResult,
    ) -> Tuple[bool, List[str], List[str]]:
        warnings: List[str] = []
        reject_reasons: List[str] = []

        # Only truly unusable raw input is a hard validation reject.
        # Layer-level invalid states should remain learnable and go to GHOST.
        if not candidate.valid:
            reject_reasons.append("INVALID_ANALYSIS_CANDIDATE")

        try:
            if candidate.sensor_snapshot.price <= 0:
                reject_reasons.append("INVALID_ENTRY_PRICE")
        except Exception:
            reject_reasons.append("INVALID_ENTRY_PRICE")

        if not movement.valid:
            warnings.append("INVALID_MOVEMENT_RESULT_SOFT_GHOST")
        if not trap.valid:
            warnings.append("INVALID_TRAP_RESULT_SOFT_GHOST")
        if not state.valid:
            warnings.append("INVALID_STATE_RESULT_SOFT_GHOST")
        if not confidence.valid:
            warnings.append("INVALID_CONFIDENCE_RESULT_SOFT_GHOST")
        if not correlation.valid:
            warnings.append("INVALID_CORRELATION_RESULT_SOFT_GHOST")
        if not prediction.valid:
            warnings.append("INVALID_MOVEMENT_PREDICTION_SOFT_GHOST")

        direction = normalize_direction(candidate.direction_hint)
        if direction not in {DIRECTION_LONG, DIRECTION_SHORT}:
            warnings.append("NO_VALID_CANDIDATE_DIRECTION_SOFT_GHOST")

        if candidate.symbol != movement.symbol:
            warnings.append("SYMBOL_MISMATCH_MOVEMENT")
        if candidate.symbol != trap.symbol:
            warnings.append("SYMBOL_MISMATCH_TRAP")
        if candidate.symbol != state.symbol:
            warnings.append("SYMBOL_MISMATCH_STATE")
        if candidate.symbol != correlation.symbol:
            warnings.append("SYMBOL_MISMATCH_CORRELATION")
        if candidate.symbol != prediction.symbol:
            warnings.append("SYMBOL_MISMATCH_PREDICTION")

        return len(reject_reasons) == 0, warnings, reject_reasons


class AIScoreComposer:
    """Combines all layers into one final AI score."""

    def compose(
        self,
        candidate: AnalysisCandidate,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
        confidence: ConfidenceResult,
        correlation: CorrelationResult,
        learning: Optional[LearningSummary],
        prediction: MovementPredictionResult,
        meta: Optional[MetaLearningSummary],
    ) -> DecisionScore:
        w_analysis = _meta_weight(meta, "analysis_engine")
        w_movement = _meta_weight(meta, "movement_hunter")
        w_trap = _meta_weight(meta, "trap_engine")
        w_state = _meta_weight(meta, "state_engine")
        w_conf = _meta_weight(meta, "confidence_engine")
        w_corr = _meta_weight(meta, "correlation_engine")
        w_learning = _meta_weight(meta, "coin_learning")
        w_pred = _meta_weight(meta, "movement_predictor")

        analysis_score = clamp(candidate.quality.total_quality - candidate.risk.total_risk * 0.25)
        movement_score = clamp(movement.readiness_score * 0.60 + movement.continuation_probability * 0.40)
        prediction_score = clamp(prediction.movement_probability * 0.70 + prediction.similarity_score * 0.30)
        confidence_score = clamp(confidence.confidence_score)

        learning_score = 50.0
        if learning is not None:
            learning_score = clamp(
                learning.similar_win_rate * 0.65
                + min(100.0, learning.sample_count * 5.0) * 0.20
                + (10.0 if learning.risk_label == "FAVORABLE_CONDITION" else 0.0)
                - (15.0 if learning.risk_label == "RISKY_CONDITION" else 0.0)
            )

        state_score = clamp(
            state.state_confidence
            - state.late_entry_risk * 0.35
            - state.exhaustion_risk * 0.35
            - state.range_probability * 0.20
        )

        trap_penalty = clamp(trap.trap_risk * 0.75 + trap.liquidity_risk * 0.30) * w_trap
        correlation_penalty = clamp(correlation.exposure_risk * 0.55) * w_corr
        range_penalty = clamp(state.range_probability * 0.45 + candidate.sensor_snapshot.range_probability * 0.25)
        late_penalty = clamp(state.late_entry_risk * 0.50 + movement.reversal_pressure * 0.35)

        positive = (
            analysis_score * 0.16 * w_analysis
            + movement_score * 0.20 * w_movement
            + prediction_score * 0.18 * w_pred
            + confidence_score * 0.18 * w_conf
            + learning_score * 0.14 * w_learning
            + state_score * 0.14 * w_state
        )

        # Keep penalties meaningful but not dominant.
        # The previous weights could crush otherwise valid Movement Hunter setups
        # into near-zero scores, causing continuous REJECT decisions.
        penalty = (
            trap_penalty * 0.18
            + correlation_penalty * 0.08
            + range_penalty * 0.12
            + late_penalty * 0.15
        )

        # Slightly normalize positive evidence so balanced conditions can reach
        # GHOST/REAL thresholds without removing risk protection.
        final = clamp((positive * 1.15) - penalty)

        return DecisionScore(
            analysis_score=clamp(analysis_score),
            movement_score=clamp(movement_score),
            prediction_score=clamp(prediction_score),
            confidence_score=clamp(confidence_score),
            learning_score=clamp(learning_score),
            state_score=clamp(state_score),
            trap_penalty=clamp(trap_penalty),
            correlation_penalty=clamp(correlation_penalty),
            range_penalty=clamp(range_penalty),
            late_penalty=clamp(late_penalty),
            final_score=final,
        )


class HardRejectRules:
    """
    Safety-only hard blocks.

    Important architecture rule:
    Market conditions such as RANGE / DEAD / LATE / LOW_DATA must NOT hard-reject.
    They should be routed to GHOST so the AI can learn from them.
    """

    def check(
        self,
        candidate: AnalysisCandidate,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
        confidence: ConfidenceResult,
        correlation: CorrelationResult,
        prediction: MovementPredictionResult,
        score: DecisionScore,
    ) -> Tuple[bool, List[str]]:
        reasons: List[str] = []

        # Only corrupted/unusable input remains a hard reject.
        # Risky market states are handled by DecisionTypeClassifier as GHOST,
        # not by blocking the learning pipeline.
        try:
            if not bool(candidate.valid):
                reasons.append(REJECT_REASON_INVALID)
            if getattr(candidate, "sensor_snapshot", None) is None:
                reasons.append(REJECT_REASON_INVALID)
            elif not bool(getattr(candidate.sensor_snapshot, "valid", True)):
                reasons.append(REJECT_REASON_INVALID)
            elif safe_float(getattr(candidate.sensor_snapshot, "price", 0.0), 0.0) <= 0.0:
                reasons.append(REJECT_REASON_INVALID)
        except Exception:
            reasons.append(REJECT_REASON_INVALID)

        return len(reasons) > 0, list(dict.fromkeys(reasons))


class DecisionTypeClassifier:
    """Converts score/context into REAL, GHOST or REJECT.

    Soft-learning policy:
    - REAL stays selective.
    - Borderline/weak/range/late/low-data candidates become GHOST.
    - REJECT is reserved for invalid input handled outside this classifier.
    """

    def classify(
        self,
        score: DecisionScore,
        candidate: AnalysisCandidate,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
        confidence: ConfidenceResult,
        correlation: CorrelationResult,
        learning: Optional[LearningSummary],
        prediction: MovementPredictionResult,
    ) -> Tuple[str, List[str], List[str]]:
        reasons: List[str] = []
        warnings: List[str] = []

        configured_min_real = safe_float(getattr(SETTINGS.ai, "min_real_confidence", 72.0), 72.0)
        configured_min_ghost = safe_float(getattr(SETTINGS.ai, "min_ghost_confidence", 45.0), 45.0)

        # Keep REAL strict enough for safety.
        min_real = clamp(configured_min_real, 66.0, 72.0)

        # GHOST must be softer because it is the learning path.
        min_ghost = clamp(configured_min_ghost, 22.0, 38.0)
        max_real_risk = safe_float(getattr(SETTINGS.ai, "max_real_risk", 38.0), 38.0)

        must_ghost = False

        # Downgrade conditions. These are NOT rejection reasons.
        if confidence.should_downgrade_to_ghost:
            must_ghost = True
            reasons.append("CONFIDENCE_REQUIRES_GHOST")
        if prediction.should_prefer_ghost_if_uncertain:
            must_ghost = True
            reasons.append("PREDICTOR_PREFERS_GHOST")
        if correlation.should_reduce_priority:
            must_ghost = True
            reasons.append("CORRELATION_REDUCES_PRIORITY")
        if learning is not None and learning.confidence_hint == "LOW_DATA":
            must_ghost = True
            reasons.append("LEARNING_LOW_DATA_GHOST")
        if trap.trap_risk >= 65:
            must_ghost = True
            reasons.append("TRAP_RISK_GHOST_ONLY")
        if state.market_state in {"RANGE", "EXHAUSTION", "LATE"}:
            must_ghost = True
            reasons.append(f"{state.market_state}_GHOST_ONLY")
        if movement.freshness in {"DEAD", "UNKNOWN", "LATE"}:
            must_ghost = True
            reasons.append(f"FRESHNESS_{movement.freshness}_GHOST_ONLY")
        if prediction.predicted_phase in {"RANGE", "UNKNOWN"}:
            must_ghost = True
            reasons.append(f"PREDICTED_PHASE_{prediction.predicted_phase}_GHOST_ONLY")

        real_conf_floor = max(52.0, min_real - 18.0)
        real_allowed = (
            score.final_score >= min_real
            and confidence.confidence_score >= real_conf_floor
            and (prediction.movement_probability >= 52 or movement.readiness_score >= 62)
            and movement.freshness in {"FRESH", "MID"}
            and state.market_state not in {"RANGE", "EXHAUSTION", "LATE"}
            and prediction.predicted_phase not in {"RANGE", "UNKNOWN"}
            and trap.trap_risk <= max_real_risk + 22
            and correlation.exposure_risk < 75
            and not must_ghost
        )

        # Strong Ghost -> REAL bridge.
        #
        # The main REAL gate above stays conservative, but the bot must not
        # keep profitable, fast Movement-Hunter candidates trapped as GHOST
        # forever. This bridge promotes only strong GHOST-quality candidates
        # to REAL when risk is acceptable. It intentionally does NOT touch:
        # - hard invalid-input rejects
        # - TP/SL logic
        # - Toobit execution
        # - GHOST creation/learning for weaker candidates
        #
        # This keeps the current high-WR Ghost logic intact while allowing
        # the best of those Ghost candidates to become real trades.
        learning_sample_count = 0
        learning_win_rate = 0.0
        learning_risk_label = ""
        learning_confidence_hint = ""
        if learning is not None:
            learning_sample_count = int(max(0, safe_float(getattr(learning, "sample_count", 0), 0)))
            learning_win_rate = clamp(safe_float(getattr(learning, "similar_win_rate", 0.0), 0.0))
            learning_risk_label = str(getattr(learning, "risk_label", "") or "").upper()
            learning_confidence_hint = str(getattr(learning, "confidence_hint", "") or "").upper()

        strong_learning = (
            learning_sample_count >= 2
            and learning_win_rate >= 62.0
            and learning_risk_label != "RISKY_CONDITION"
        )

        very_strong_learning = (
            learning_sample_count >= 5
            and learning_win_rate >= 68.0
            and learning_risk_label != "RISKY_CONDITION"
        )

        live_or_early_move = (
            movement.freshness in {"FRESH", "MID"}
            or prediction.predicted_phase in {"PRE_START", "START", "MID"}
            or movement.readiness_score >= 55.0
            or prediction.movement_probability >= 50.0
        )

        severe_risk_block = (
            trap.trap_risk >= 78.0
            or trap.liquidity_risk >= 85.0
            or correlation.exposure_risk >= 85.0
            or movement.freshness == "DEAD"
            or (
                state.market_state == "RANGE"
                and state.range_probability >= 82.0
                and movement.readiness_score < 65.0
                and not very_strong_learning
            )
            or (
                state.market_state == "EXHAUSTION"
                and state.exhaustion_risk >= 78.0
                and movement.continuation_probability < 55.0
                and not very_strong_learning
            )
            or (
                prediction.predicted_phase == "RANGE"
                and prediction.movement_probability < 55.0
                and not very_strong_learning
            )
        )

        strong_ghost_to_real = (
            not severe_risk_block
            and live_or_early_move
            and confidence.confidence_score >= 12.0
            and trap.trap_risk <= max_real_risk + 32.0
            and correlation.exposure_risk < 82.0
            and (
                # Good current AI evidence.
                (
                    score.final_score >= 50.0
                    and (
                        prediction.movement_probability >= 38.0
                        or movement.readiness_score >= 42.0
                        or score.analysis_score >= 52.0
                    )
                )
                # Proven similar conditions can promote earlier, so we do not
                # miss the same pump/dump pattern again.
                or (
                    strong_learning
                    and score.final_score >= 38.0
                    and (
                        prediction.movement_probability >= 28.0
                        or movement.readiness_score >= 35.0
                        or score.analysis_score >= 45.0
                    )
                )
                # Very strong immediate movement can go REAL even before
                # enough learning exists.
                or (
                    score.final_score >= 44.0
                    and movement.readiness_score >= 58.0
                    and prediction.movement_probability >= 32.0
                )
            )
        )

        if real_allowed or strong_ghost_to_real:
            reasons.append("AI_DECISION_REAL_ALLOWED")
            if strong_ghost_to_real and not real_allowed:
                reasons.append("STRONG_GHOST_TO_REAL_BRIDGE")
                if must_ghost:
                    warnings.append("GHOST_DOWNGRADE_OVERRIDDEN_BY_STRONG_EVIDENCE")
                if strong_learning:
                    reasons.append("LEARNING_SUPPORTS_REAL_PROMOTION")
                if learning_confidence_hint == "LOW_DATA":
                    warnings.append("LOW_DATA_ALLOWED_FOR_STRONG_REAL")
            return DECISION_REAL, reasons, warnings

        ghost_allowed = (
            score.final_score >= min_ghost
            or confidence.confidence_score >= 8.0
            or prediction.movement_probability >= 18.0
            or movement.readiness_score >= 12.0
            or score.analysis_score >= 45.0
            or score.state_score >= 35.0
            or (learning is not None and learning.confidence_hint == "LOW_DATA")
        )

        if ghost_allowed:
            reasons.append("AI_DECISION_GHOST_FOR_LEARNING")
            if must_ghost:
                warnings.append("DOWNGRADED_TO_GHOST")
            return DECISION_GHOST, reasons, warnings

        # Final soft fallback:
        # If data is valid but weak, still create GHOST so AI can learn why it failed.
        reasons.append("AI_DECISION_GHOST_SOFT_FALLBACK")
        warnings.append("VERY_WEAK_CANDIDATE_GHOST_LEARNING")
        return DECISION_GHOST, reasons, warnings


class AIDecisionEngine:
    """
    The only final decision-maker in the architecture.

    It decides:
        REAL
        GHOST
        REJECT

    It does not open trades.
    """

    def __init__(self):
        self.validator = DecisionInputValidator()
        self.scorer = AIScoreComposer()
        self.reject_rules = HardRejectRules()
        self.classifier = DecisionTypeClassifier()

    def decide(
        self,
        candidate: AnalysisCandidate,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
        confidence: ConfidenceResult,
        correlation: CorrelationResult,
        prediction: MovementPredictionResult,
        learning: Optional[LearningSummary] = None,
        meta: Optional[MetaLearningSummary] = None,
    ) -> AIDecision:
        if meta is None:
            try:
                meta = get_meta_learning_summary()
            except Exception:
                meta = None

        valid, validation_warnings, validation_rejects = self.validator.validate(
            candidate=candidate,
            movement=movement,
            trap=trap,
            state=state,
            confidence=confidence,
            correlation=correlation,
            prediction=prediction,
        )

        reasons: List[str] = []
        warnings: List[str] = []
        reject_reasons: List[str] = []
        warnings.extend(validation_warnings)
        reject_reasons.extend(validation_rejects)

        score = self.scorer.compose(
            candidate=candidate,
            movement=movement,
            trap=trap,
            state=state,
            confidence=confidence,
            correlation=correlation,
            learning=learning,
            prediction=prediction,
            meta=meta,
        )

        hard_reject, hard_reject_reasons = self.reject_rules.check(
            candidate=candidate,
            movement=movement,
            trap=trap,
            state=state,
            confidence=confidence,
            correlation=correlation,
            prediction=prediction,
            score=score,
        )

        if hard_reject:
            reject_reasons.extend(hard_reject_reasons)
            decision_type = DECISION_REJECT
            reasons.append("HARD_REJECT_RULE_TRIGGERED")
        elif not valid:
            decision_type = DECISION_REJECT
            reasons.append("INVALID_INPUT_REJECT")
        else:
            decision_type, r, w = self.classifier.classify(
                score=score,
                candidate=candidate,
                movement=movement,
                trap=trap,
                state=state,
                confidence=confidence,
                correlation=correlation,
                learning=learning,
                prediction=prediction,
            )
            reasons.extend(r)
            warnings.extend(w)

        # Collect important reasons from all layers without flooding output.
        layer_reasons = []
        layer_reasons.extend(list(candidate.reason_codes)[:8])
        layer_reasons.extend(list(movement.reason_codes)[:8])
        layer_reasons.extend(list(trap.reason_codes)[:8])
        layer_reasons.extend(list(state.reason_codes)[:8])
        layer_reasons.extend(list(confidence.reason_codes)[:8])
        layer_reasons.extend(list(correlation.reason_codes)[:8])
        layer_reasons.extend(list(prediction.reason_codes)[:8])
        reasons.extend(layer_reasons)

        # Prefer candidate direction, but do not hard-reject NEUTRAL immediately.
        # For learning/GHOST, fall back to movement/trap/prediction direction when available.
        direction = normalize_direction(candidate.direction_hint)
        if direction == DIRECTION_NEUTRAL:
            for fallback_direction in (
                getattr(movement, "direction_hint", None),
                getattr(trap, "direction_hint", None),
                getattr(prediction, "direction_hint", None),
            ):
                direction = normalize_direction(fallback_direction)
                if direction in {DIRECTION_LONG, DIRECTION_SHORT}:
                    warnings.append("DIRECTION_FALLBACK_USED_FOR_LEARNING")
                    break

        if direction == DIRECTION_NEUTRAL:
            decision_type = DECISION_REJECT
            reject_reasons.append("NO_USABLE_DIRECTION")

        risk_score = clamp(
            trap.trap_risk * 0.30
            + state.late_entry_risk * 0.20
            + state.range_probability * 0.18
            + correlation.exposure_risk * 0.12
            + candidate.risk.total_risk * 0.20
        )

        entry = safe_float(candidate.sensor_snapshot.price)

        return AIDecision(
            decision_id=f"dec_{uuid4().hex}",
            symbol=candidate.symbol,
            timeframe=candidate.timeframe,
            timestamp=candidate.timestamp or now_ts(),
            direction=direction,
            decision_type=decision_type,
            confidence_score=clamp(confidence.confidence_score),
            risk_score=risk_score,
            ai_score=score.final_score,
            entry=entry,
            movement_phase=movement.movement_phase,
            freshness=movement.freshness,
            market_state=state.market_state,
            predicted_phase=prediction.predicted_phase,
            should_trade_real=decision_type == DECISION_REAL,
            should_create_ghost=decision_type == DECISION_GHOST,
            should_reject=decision_type == DECISION_REJECT,
            score=score,
            reason_codes=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
            reject_reasons=tuple(dict.fromkeys(reject_reasons)),
            meta={
                "learning": learning.to_dict() if learning else {},
                "meta_learning": meta.to_dict() if meta else {},
                "prediction": prediction.to_dict(),
                "correlation": correlation.to_dict(),
                "note": "TP/SL will be calculated by tp_sl_engine.py",
            },
        )


_default_engine: Optional[AIDecisionEngine] = None


def engine() -> AIDecisionEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = AIDecisionEngine()
    return _default_engine


def decide(
    candidate: AnalysisCandidate,
    movement: MovementHunterResult,
    trap: TrapResult,
    state: StateResult,
    confidence: ConfidenceResult,
    correlation: CorrelationResult,
    prediction: MovementPredictionResult,
    learning: Optional[LearningSummary] = None,
    meta: Optional[MetaLearningSummary] = None,
) -> AIDecision:
    return engine().decide(
        candidate=candidate,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        correlation=correlation,
        prediction=prediction,
        learning=learning,
        meta=meta,
    )


def ai_decision_engine(
    candidate: AnalysisCandidate,
    movement: MovementHunterResult,
    trap: TrapResult,
    state: StateResult,
    confidence: ConfidenceResult,
    correlation: CorrelationResult,
    prediction: MovementPredictionResult,
    learning: Optional[LearningSummary] = None,
    meta: Optional[MetaLearningSummary] = None,
) -> AIDecision:
    return decide(
        candidate=candidate,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        correlation=correlation,
        prediction=prediction,
        learning=learning,
        meta=meta,
    )
