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
    tradability_score: float
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

    score: DecisionScore = field(default_factory=lambda: DecisionScore(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 50, 0))
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


def _learning_value(learning: Optional[Any], key: str, default: Any = None) -> Any:
    if learning is None:
        return default
    if isinstance(learning, dict):
        return learning.get(key, default)
    return getattr(learning, key, default)


def _learning_float(learning: Optional[Any], key: str, default: float = 0.0) -> float:
    try:
        value = _learning_value(learning, key, default)
        if value is None:
            return default
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def _learning_int(learning: Optional[Any], key: str, default: int = 0) -> int:
    try:
        value = _learning_value(learning, key, default)
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _learning_notes(learning: Optional[Any]) -> Tuple[str, ...]:
    notes = _learning_value(learning, "notes", ())
    if notes is None:
        return ()
    if isinstance(notes, str):
        return (notes,)
    try:
        return tuple(str(x) for x in notes)
    except Exception:
        return ()


def _learning_to_dict(learning: Optional[Any]) -> JsonDict:
    if learning is None:
        return {}
    if isinstance(learning, dict):
        return dict(learning)
    if hasattr(learning, "to_dict") and callable(learning.to_dict):
        try:
            data = learning.to_dict()
            return dict(data) if isinstance(data, dict) else {}
        except Exception:
            return {}
    try:
        return dict(getattr(learning, "__dict__", {}))
    except Exception:
        return {}




def _settings_float(path: str, default: float) -> float:
    """Read SETTINGS.foo.bar safely without adding runtime dependencies."""
    try:
        obj: Any = SETTINGS
        for part in str(path).split("."):
            obj = getattr(obj, part)
        return safe_float(obj, default)
    except Exception:
        return default


def _estimated_notional_usdt() -> float:
    margin = _settings_float("trading.margin_usdt", 5.0)
    leverage = max(1.0, _settings_float("trading.leverage", 10.0))
    return max(0.0, margin * leverage)


def _fee_rate_per_side() -> float:
    fee = _settings_float("tp.fee_rate_per_side", 0.0)
    if fee <= 0:
        fee = _settings_float("trading.taker_fee_rate", 0.0006)
    return max(0.0, fee)


def _min_net_profit_usdt() -> float:
    return max(0.0, _settings_float("tp.min_net_profit_usdt", 0.10))


def _tradability_score(
    candidate: AnalysisCandidate,
    movement: MovementHunterResult,
    prediction: MovementPredictionResult,
    learning: Optional[Any],
) -> Tuple[float, List[str], List[str]]:
    """Estimate whether a technically good move is worth REAL money.

    This is not order sizing and does not replace tp_sl_engine.py. It prevents
    AI from sending REAL on tiny/fee-eaten moves. Good but low-profit moves go
    to GHOST so the hunter still learns from them.
    """
    reasons: List[str] = []
    warnings: List[str] = []

    notional = _estimated_notional_usdt()
    fee = notional * _fee_rate_per_side() * 2.0
    min_net = _min_net_profit_usdt()

    if notional <= 0:
        warnings.append("TRADABILITY_NO_NOTIONAL_FALLBACK")
        return 50.0, reasons, warnings

    s = candidate.sensor_snapshot
    atr_pct = max(0.0, safe_float(getattr(s, "atr_percent", 0.0), 0.0))
    expected_pct = max(0.0, safe_float(getattr(prediction, "expected_move_percent", 0.0), 0.0))

    phase = str(getattr(prediction, "predicted_phase", "") or "").upper()
    if phase in {"PRE_START", "START"}:
        expected_component = expected_pct * 0.65
    elif phase == "MID":
        expected_component = expected_pct * 0.50
    else:
        expected_component = expected_pct * 0.35

    candidate_tp_pct = max(atr_pct * 0.85, expected_component)

    early = _learning_float(learning, "early_success_rate", 0.0)
    premove = _learning_float(learning, "premove_success_rate", 0.0)
    timing = _learning_float(learning, "timing_score", 50.0)
    if early >= 45.0 or premove >= 45.0 or timing >= 68.0:
        candidate_tp_pct = max(candidate_tp_pct, expected_pct * 0.70, atr_pct * 0.95)
        reasons.append("TRADABILITY_EARLY_MEMORY_SUPPORT")

    gross = notional * candidate_tp_pct / 100.0
    net = gross - fee
    required_pct = ((fee + min_net) / notional) * 100.0 if notional > 0 else 0.0

    rel_vol = safe_float(getattr(s, "relative_volume", 0.0), 0.0)
    volume_bonus = 0.0
    if rel_vol >= 1.8 or bool(getattr(s, "volume_spike", False)):
        volume_bonus += 8.0
        reasons.append("TRADABILITY_VOLUME_SUPPORT")
    elif rel_vol > 0 and rel_vol < 0.65:
        volume_bonus -= 10.0
        warnings.append("TRADABILITY_LOW_VOLUME")

    fee_cover_score = clamp((candidate_tp_pct / max(required_pct, 1e-9)) * 55.0, 0.0, 70.0)
    net_score = clamp((net / max(min_net, 0.01)) * 30.0, 0.0, 35.0)
    move_score = clamp(candidate_tp_pct * 12.0, 0.0, 18.0)
    score = clamp(fee_cover_score + net_score + move_score + volume_bonus, 0.0, 100.0)

    if net < min_net:
        warnings.append("TRADABILITY_NET_PROFIT_BELOW_MIN")
    if candidate_tp_pct < required_pct:
        warnings.append("TRADABILITY_TP_TOO_SMALL_FOR_FEES")
    if score >= 70:
        reasons.append("TRADABILITY_REAL_PROFIT_OK")
    elif score >= 45:
        reasons.append("TRADABILITY_BORDERLINE")
    else:
        reasons.append("TRADABILITY_GHOST_FEE_RISK")

    return score, reasons, warnings

def _hunter_memory_support(learning: Optional[Any]) -> Tuple[float, float, float, float, int, Tuple[str, ...]]:
    """Return timing/outcome memory fields when available.

    Backward compatible: older coin_learning summaries do not have these fields,
    so this function falls back to similar_win_rate and neutral timing.
    """
    similar_wr = _learning_float(learning, "similar_win_rate", 50.0)
    return (
        clamp(_learning_float(learning, "timing_score", 50.0)),
        clamp(_learning_float(learning, "early_success_rate", 0.0)),
        clamp(_learning_float(learning, "fuzzy_match_score", 0.0)),
        clamp(_learning_float(learning, "outcome_success_rate", similar_wr)),
        max(0, _learning_int(learning, "sample_count", 0)),
        _learning_notes(learning),
    )


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
            timing_score, early_success_rate, fuzzy_match_score, outcome_success_rate, sample_count, notes = _hunter_memory_support(learning)
            similar_wr = clamp(_learning_float(learning, "similar_win_rate", 50.0))
            risk_label = str(_learning_value(learning, "risk_label", "") or "").upper()

            # Movement Hunter learning score:
            # result matters, but timing and pre-move similarity matter too.
            # A late profitable setup must not score the same as a setup that
            # repeatedly caught the first candle before pump/dump.
            sample_score = min(100.0, sample_count * 5.0)
            memory_bonus = 0.0
            if timing_score >= 65:
                memory_bonus += 5.0
            if early_success_rate >= 45:
                memory_bonus += 6.0
            if fuzzy_match_score >= 70:
                memory_bonus += 4.0
            if "PREMOVE_PATTERN_WORKED_WITH_TIMING" in notes:
                memory_bonus += 5.0
            if "PREMOVE_PATTERN_WEAK_OR_LATE" in notes:
                memory_bonus -= 8.0
            if "TIMING_LATE_OR_WEAK_PATTERN" in notes:
                memory_bonus -= 5.0

            learning_score = clamp(
                similar_wr * 0.28
                + outcome_success_rate * 0.24
                + timing_score * 0.20
                + early_success_rate * 0.12
                + fuzzy_match_score * 0.08
                + sample_score * 0.08
                + (10.0 if risk_label == "FAVORABLE_CONDITION" else 0.0)
                - (16.0 if risk_label == "RISKY_CONDITION" else 0.0)
                + memory_bonus
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

        tradability_score, _, _ = _tradability_score(
            candidate=candidate,
            movement=movement,
            prediction=prediction,
            learning=learning,
        )

        # Movement Hunter balance:
        # classic analysis remains a sensor, while movement prediction and
        # conditional coin-learning carry more influence in the final score.
        positive = (
            analysis_score * 0.12 * w_analysis
            + movement_score * 0.22 * w_movement
            + prediction_score * 0.20 * w_pred
            + confidence_score * 0.16 * w_conf
            + learning_score * 0.20 * w_learning
            + state_score * 0.08 * w_state
            + tradability_score * 0.02
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
            tradability_score=clamp(tradability_score),
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

    Movement Hunter policy:
    - REAL must be selective and fresh/live.
    - GHOST remains the broad learning path.
    - Learning can promote strong familiar conditions, but it must not override
      explicit GHOST-only warnings such as LATE, RANGE, HIGH_TRAP, or LOW_DATA.
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

        # Keep real trading selective. GHOST remains broad for learning.
        min_real = clamp(configured_min_real, 64.0, 74.0)
        min_ghost = clamp(configured_min_ghost, 22.0, 40.0)
        max_real_risk = safe_float(getattr(SETTINGS.ai, "max_real_risk", 38.0), 38.0)

        learning_sample_count = 0
        learning_win_rate = 0.0
        learning_risk_label = ""
        learning_confidence_hint = ""
        timing_score = 50.0
        early_success_rate = 0.0
        fuzzy_match_score = 0.0
        outcome_success_rate = 50.0
        learning_notes: Tuple[str, ...] = ()
        if learning is not None:
            learning_sample_count = max(0, _learning_int(learning, "sample_count", 0))
            learning_win_rate = clamp(_learning_float(learning, "similar_win_rate", 0.0), 0.0)
            learning_risk_label = str(_learning_value(learning, "risk_label", "") or "").upper()
            learning_confidence_hint = str(_learning_value(learning, "confidence_hint", "") or "").upper()
            timing_score, early_success_rate, fuzzy_match_score, outcome_success_rate, _, learning_notes = _hunter_memory_support(learning)

        low_data = learning is not None and learning_confidence_hint == "LOW_DATA"
        # Risk is evaluated after hunter-memory quality below.
        risky_learning = False
        hunter_memory_good = (
            learning is not None
            and learning_sample_count >= 3
            and outcome_success_rate >= 52.0
            and (timing_score >= 62.0 or early_success_rate >= 35.0 or fuzzy_match_score >= 68.0)
            and "PREMOVE_PATTERN_WEAK_OR_LATE" not in learning_notes
        )
        hunter_memory_strong = (
            learning is not None
            and learning_sample_count >= 5
            and outcome_success_rate >= 58.0
            and (timing_score >= 68.0 or early_success_rate >= 45.0)
            and fuzzy_match_score >= 62.0
            and "TIMING_LATE_OR_WEAK_PATTERN" not in learning_notes
        )
        # Warm-up bridge:
        # After a clean reset the bot may have many strong GHOST wins but still
        # mark each individual coin/condition as LOW_DATA. LOW_DATA should slow
        # REAL down, not fully freeze it when movement, timing and profit quality
        # are all good.
        low_data_real_bridge = (
            low_data
            and learning is not None
            and learning_sample_count >= 3
            and outcome_success_rate >= 58.0
            and (timing_score >= 60.0 or early_success_rate >= 35.0 or fuzzy_match_score >= 64.0)
        )

        good_learning = (
            learning is not None
            and learning_sample_count >= 6
            and learning_win_rate >= 62.0
            and learning_risk_label != "RISKY_CONDITION"
        ) or hunter_memory_good
        very_strong_learning = (
            learning is not None
            and learning_sample_count >= 10
            and learning_win_rate >= 68.0
            and learning_risk_label == "FAVORABLE_CONDITION"
        ) or hunter_memory_strong

        risky_learning = (
            learning is not None
            and (
                learning_risk_label == "RISKY_CONDITION"
                or (learning_sample_count >= 4 and learning_win_rate > 0 and learning_win_rate <= 45.0 and not hunter_memory_good)
                or (learning_sample_count >= 5 and outcome_success_rate <= 38.0 and timing_score <= 42.0)
            )
        )

        s = candidate.sensor_snapshot
        compression_score = safe_float(getattr(s, "compression_score", 0.0), 0.0)
        expansion_probability = safe_float(getattr(s, "expansion_probability", 0.0), 0.0)
        range_breakout_opportunity = (
            state.market_state == "RANGE"
            and compression_score >= 58.0
            and (
                expansion_probability >= 42.0
                or movement.readiness_score >= 58.0
                or prediction.movement_probability >= 55.0
                or bool(getattr(s, "volume_expansion", False))
                or str(getattr(s, "atr_expansion", "")).upper() == "EXPANDING"
                or hunter_memory_good
            )
            and trap.trap_risk < 60.0
        )

        tradability_score = clamp(getattr(score, "tradability_score", 50.0))
        low_tradability = tradability_score < 35.0
        very_low_tradability = tradability_score < 22.0

        must_ghost = False

        # GHOST-only conditions. These must not be bypassed by a loose bridge.
        if confidence.should_downgrade_to_ghost:
            must_ghost = True
            reasons.append("CONFIDENCE_REQUIRES_GHOST")
        if prediction.should_prefer_ghost_if_uncertain:
            must_ghost = True
            reasons.append("PREDICTOR_PREFERS_GHOST")
        if correlation.should_reduce_priority:
            must_ghost = True
            reasons.append("CORRELATION_REDUCES_PRIORITY")
        if low_data:
            if low_data_real_bridge and tradability_score >= 45.0 and trap.trap_risk < 55.0:
                reasons.append("LOW_DATA_WARMUP_REAL_BRIDGE_ALLOWED")
            else:
                must_ghost = True
                reasons.append("LEARNING_LOW_DATA_GHOST")
        if risky_learning:
            must_ghost = True
            reasons.append("LEARNING_RISKY_CONDITION_GHOST_ONLY")
        if low_tradability:
            must_ghost = True
            reasons.append("TRADABILITY_LOW_PROFIT_GHOST_ONLY")
            warnings.append("REAL_BLOCKED_BY_LOW_NET_PROFIT_QUALITY")
        if trap.trap_risk >= 65:
            must_ghost = True
            reasons.append("TRAP_RISK_GHOST_ONLY")
        if state.market_state == "RANGE":
            if range_breakout_opportunity:
                reasons.append("RANGE_COMPRESSION_BREAKOUT_EXCEPTION")
            else:
                must_ghost = True
                reasons.append("RANGE_GHOST_ONLY")
        elif state.market_state in {"EXHAUSTION", "LATE"}:
            must_ghost = True
            reasons.append(f"{state.market_state}_GHOST_ONLY")
        if movement.freshness in {"DEAD", "UNKNOWN", "LATE"}:
            must_ghost = True
            reasons.append(f"FRESHNESS_{movement.freshness}_GHOST_ONLY")
        if prediction.predicted_phase == "RANGE":
            if range_breakout_opportunity:
                reasons.append("PREDICTED_RANGE_COMPRESSION_EXCEPTION")
            else:
                must_ghost = True
                reasons.append("PREDICTED_PHASE_RANGE_GHOST_ONLY")
        elif prediction.predicted_phase in {"UNKNOWN", "LATE"}:
            must_ghost = True
            reasons.append(f"PREDICTED_PHASE_{prediction.predicted_phase}_GHOST_ONLY")

        live_or_early_move = (
            movement.freshness in {"FRESH", "MID"}
            or prediction.predicted_phase in {"PRE_START", "START", "MID"}
        )
        strong_live_confirmation = (
            movement.readiness_score >= 60.0
            or prediction.movement_probability >= 58.0
            or (
                movement.readiness_score >= 52.0
                and prediction.movement_probability >= 50.0
                and confidence.confidence_score >= 38.0
            )
        )
        severe_risk_block = (
            trap.trap_risk >= 75.0
            or trap.liquidity_risk >= 82.0
            or correlation.exposure_risk >= 82.0
            or movement.freshness == "DEAD"
            or state.exhaustion_risk >= 82.0
            or state.late_entry_risk >= 82.0
            or (state.range_probability >= 85.0 and movement.readiness_score < 68.0 and not range_breakout_opportunity)
            or very_low_tradability
        )

        real_conf_floor = max(38.0, min_real - 22.0)

        # Main REAL gate: strong score + live/fresh movement + no GHOST-only flags.
        real_allowed = (
            score.final_score >= min_real
            and confidence.confidence_score >= real_conf_floor
            and strong_live_confirmation
            and live_or_early_move
            and not must_ghost
            and not severe_risk_block
            and trap.trap_risk <= max_real_risk + 18.0
            and correlation.exposure_risk < 72.0
        )

        # Learning-assisted REAL: only when familiar condition history is good.
        # This is intentionally much stricter than the old loose Ghost->REAL bridge.
        learned_hunter_real = (
            not real_allowed
            and not must_ghost
            and not severe_risk_block
            and live_or_early_move
            and good_learning
            and score.final_score >= (min_real - (13.0 if hunter_memory_good else 10.0))
            and confidence.confidence_score >= (28.0 if hunter_memory_good else 31.0)
            and (movement.readiness_score >= 50.0 or prediction.movement_probability >= 48.0 or hunter_memory_good)
            and trap.trap_risk < 58.0
            and correlation.exposure_risk < 68.0
        )

        # Very strong learned condition can accept a slightly lower score, but
        # still cannot override RANGE/LATE/HIGH_TRAP/LOW_DATA GHOST-only flags.
        very_strong_learned_hunter_real = (
            not real_allowed
            and not learned_hunter_real
            and not must_ghost
            and not severe_risk_block
            and live_or_early_move
            and very_strong_learning
            and score.final_score >= (min_real - (18.0 if hunter_memory_strong else 14.0))
            and confidence.confidence_score >= (24.0 if hunter_memory_strong else 28.0)
            and (movement.readiness_score >= 46.0 or prediction.movement_probability >= 45.0 or hunter_memory_strong)
            and trap.trap_risk < 55.0
            and correlation.exposure_risk < 65.0
        )

        # Controlled warm-up REAL:
        # Prevents the bot from becoming "dry" after a reset. It is still
        # protected by trap/correlation/tradability/freshness checks and needs
        # a strong GHOST-style learning footprint.
        controlled_warmup_real = (
            not real_allowed
            and not learned_hunter_real
            and not very_strong_learned_hunter_real
            and not must_ghost
            and not severe_risk_block
            and low_data_real_bridge
            and live_or_early_move
            and strong_live_confirmation
            and score.final_score >= (min_real - 18.0)
            and confidence.confidence_score >= 24.0
            and tradability_score >= 45.0
            and trap.trap_risk < 50.0
            and correlation.exposure_risk < 62.0
        )

        if real_allowed or learned_hunter_real or very_strong_learned_hunter_real or controlled_warmup_real:
            reasons.append("AI_DECISION_REAL_ALLOWED")
            if controlled_warmup_real:
                reasons.append("CONTROLLED_WARMUP_REAL")
            if learned_hunter_real:
                reasons.append("LEARNING_ASSISTED_HUNTER_REAL")
            if very_strong_learned_hunter_real:
                reasons.append("VERY_STRONG_LEARNING_HUNTER_REAL")
            if good_learning:
                reasons.append("CONDITIONAL_LEARNING_SUPPORTS_REAL")
            if hunter_memory_good:
                reasons.append("HUNTER_MEMORY_TIMING_SUPPORTS_REAL")
            if hunter_memory_strong:
                reasons.append("STRONG_PREMOVE_MEMORY_SUPPORTS_REAL")
            if live_or_early_move:
                reasons.append("LIVE_OR_EARLY_MOVEMENT_CONFIRMED")
            if tradability_score >= 55.0:
                reasons.append("TRADABILITY_CONFIRMS_REAL_VALUE")
            return DECISION_REAL, reasons, warnings

        ghost_allowed = (
            score.final_score >= min_ghost
            or confidence.confidence_score >= 8.0
            or prediction.movement_probability >= 18.0
            or movement.readiness_score >= 12.0
            or score.analysis_score >= 45.0
            or score.state_score >= 35.0
            or low_data
            or risky_learning
        )

        if ghost_allowed:
            reasons.append("AI_DECISION_GHOST_FOR_LEARNING")
            if must_ghost:
                warnings.append("DOWNGRADED_TO_GHOST")
            if risky_learning:
                warnings.append("LEARNING_BLOCKED_REAL_TO_GHOST")
            if low_data:
                warnings.append("LOW_DATA_COLLECT_MORE_GHOST")
            return DECISION_GHOST, reasons, warnings

        # Final soft fallback: keep weak but valid candidates as GHOST so the AI
        # keeps learning why they failed instead of losing data.
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
                "learning": _learning_to_dict(learning),
                "meta_learning": meta.to_dict() if meta else {},
                "prediction": prediction.to_dict(),
                "correlation": correlation.to_dict(),
                "tradability_score": score.tradability_score,
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
