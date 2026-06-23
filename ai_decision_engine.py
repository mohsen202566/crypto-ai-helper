from __future__ import annotations

"""
18 - ai_decision_engine.py

Final AI decision layer for the simplified Level 1 / 5M crypto futures bot.

Locked goals:
- AI is the only final decision maker: REAL / GHOST / REJECT.
- Inputs are only:
  1) AnalysisCandidate = raw technical sensor package
  2) MovementPredictionResult = Pattern Start Predictor output
  3) LearningSummary / dict = coin learning output
- Technical analysis is only sensor input.
- No candle-confirmation logic.
- No trap/confidence/correlation/meta/state engine.
- No Toobit order execution.
- No Telegram sending.
- No persistence side effects.
- No paper/setup flow.

This file decides only. It does not open trades.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
import math
import time

from analysis_engine import AnalysisCandidate
from coin_learning import LearningSummary
from movement_predictor import MovementPredictionResult
from config import SETTINGS


JsonDict = Dict[str, Any]

DECISION_REAL = "REAL"
DECISION_GHOST = "GHOST"
DECISION_REJECT = "REJECT"

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"

PHASE_PRE_START = "PRE_START"
PHASE_START = "START"
PHASE_MID = "MID"
PHASE_LATE = "LATE"
PHASE_RANGE = "RANGE"
PHASE_UNKNOWN = "UNKNOWN"


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
    if d in {"LONG", "BUY", "BULL", "BULLISH", "UP"}:
        return DIRECTION_LONG
    if d in {"SHORT", "SELL", "BEAR", "BEARISH", "DOWN"}:
        return DIRECTION_SHORT
    return DIRECTION_NEUTRAL


def infer_decision_direction(candidate: AnalysisCandidate, prediction: Optional[Any] = None) -> str:
    """Prefer the live candidate direction, but allow the Pattern Start layer to provide it.

    After a full learning reset the live sensor package can be cautious and leave
    direction_hint as NEUTRAL while the movement predictor already sees a LONG/SHORT
    start pattern. For Level 1 this must still become GHOST for learning instead
    of being rejected as NO_DIRECTION.
    """
    direction = normalize_direction(getattr(candidate, "direction_hint", None))
    if direction != DIRECTION_NEUTRAL:
        return direction

    for key in (
        "direction",
        "predicted_direction",
        "movement_direction",
        "signal_direction",
        "side",
        "bias",
    ):
        direction = normalize_direction(obj_value(prediction, key, None))
        if direction != DIRECTION_NEUTRAL:
            return direction

    return DIRECTION_NEUTRAL


def obj_value(obj: Optional[Any], key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def obj_float(obj: Optional[Any], key: str, default: float = 0.0) -> float:
    return safe_float(obj_value(obj, key, default), default)


def obj_int(obj: Optional[Any], key: str, default: int = 0) -> int:
    return safe_int(obj_value(obj, key, default), default)


def obj_tuple(obj: Optional[Any], key: str) -> Tuple[str, ...]:
    value = obj_value(obj, key, ())
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    try:
        return tuple(str(x) for x in value)
    except Exception:
        return ()


def to_dict(obj: Optional[Any]) -> JsonDict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        try:
            data = obj.to_dict()
            return dict(data) if isinstance(data, dict) else {}
        except Exception:
            return {}
    try:
        return dict(getattr(obj, "__dict__", {}))
    except Exception:
        return {}


def settings_float(path: str, default: float) -> float:
    try:
        obj: Any = SETTINGS
        for part in str(path).split("."):
            obj = getattr(obj, part)
        return safe_float(obj, default)
    except Exception:
        return default


def estimated_notional_usdt() -> float:
    margin = settings_float("trading.margin_usdt", 5.0)
    leverage = max(1.0, settings_float("trading.leverage", 10.0))
    return max(0.0, margin * leverage)


def fee_rate_per_side() -> float:
    fee = settings_float("tp.fee_rate_per_side", 0.0)
    if fee <= 0:
        fee = settings_float("trading.taker_fee_rate", 0.0006)
    return max(0.0, fee)


def min_net_profit_usdt() -> float:
    # Level 1 default: do not send REAL if the expected net profit is fee-eaten.
    return max(0.0, settings_float("tp.min_net_profit_usdt", 0.20))


@dataclass(frozen=True)
class DecisionScore:
    sensor_score: float
    prediction_score: float
    learning_score: float
    market_score: float
    freshness_score: float
    range_penalty: float
    late_penalty: float
    fee_penalty: float
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

    predicted_phase: str = PHASE_UNKNOWN
    movement_probability: float = 0.0
    pattern_count: int = 0
    pattern_match_score: float = 0.0
    pattern_confidence: float = 0.0

    should_trade_real: bool = False
    should_create_ghost: bool = False
    should_reject: bool = False

    score: DecisionScore = field(default_factory=lambda: DecisionScore(0, 0, 0, 0, 0, 0, 0, 0, 50, 0))
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    reject_reasons: Tuple[str, ...] = field(default_factory=tuple)
    meta: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


class AIScoreComposer:
    def sensor_birth_score(self, candidate: AnalysisCandidate, direction: str) -> Tuple[float, List[str]]:
        m = candidate.momentum_state
        reasons: List[str] = []
        score = 0.0

        if direction == DIRECTION_LONG:
            if m.rsi_slope > 0:
                score += min(18.0, abs(m.rsi_slope) * 5.0)
                reasons.append("RSI_BUILDING_LONG")
            if m.rsi_acceleration > 0:
                score += min(12.0, abs(m.rsi_acceleration) * 5.0)
                reasons.append("RSI_ACCEL_LONG")
            if m.histogram_slope > 0:
                score += min(20.0, abs(m.histogram_slope) * 1000.0)
                reasons.append("HIST_BUILDING_LONG")
            if m.histogram_acceleration > 0:
                score += min(14.0, abs(m.histogram_acceleration) * 1000.0)
                reasons.append("HIST_ACCEL_LONG")
            if m.power_delta > 0:
                score += min(18.0, abs(m.power_delta) * 0.75)
                reasons.append("BUY_POWER_BUILDING")
            if m.plus_di > m.minus_di and m.adx_slope >= 0:
                score += 8.0
                reasons.append("ADX_DI_LONG_SUPPORT")

        elif direction == DIRECTION_SHORT:
            if m.rsi_slope < 0:
                score += min(18.0, abs(m.rsi_slope) * 5.0)
                reasons.append("RSI_BUILDING_SHORT")
            if m.rsi_acceleration < 0:
                score += min(12.0, abs(m.rsi_acceleration) * 5.0)
                reasons.append("RSI_ACCEL_SHORT")
            if m.histogram_slope < 0:
                score += min(20.0, abs(m.histogram_slope) * 1000.0)
                reasons.append("HIST_BUILDING_SHORT")
            if m.histogram_acceleration < 0:
                score += min(14.0, abs(m.histogram_acceleration) * 1000.0)
                reasons.append("HIST_ACCEL_SHORT")
            if m.power_delta < 0:
                score += min(18.0, abs(m.power_delta) * 0.75)
                reasons.append("SELL_POWER_BUILDING")
            if m.minus_di > m.plus_di and m.adx_slope >= 0:
                score += 8.0
                reasons.append("ADX_DI_SHORT_SUPPORT")

        if m.volume_expansion:
            score += 8.0
            reasons.append("VOLUME_EXPANSION")
        if m.volume_spike:
            score += 8.0
            reasons.append("VOLUME_SPIKE")
        if str(m.atr_expansion).upper() == "EXPANDING":
            score += 8.0
            reasons.append("ATR_EXPANDING")
        if m.atr_explosion:
            score += 8.0
            reasons.append("ATR_EXPLOSION")
        if m.compression_score >= 45 and m.range_probability < 85:
            score += 8.0
            reasons.append("COMPRESSION_BEFORE_MOVE")

        return clamp(score), reasons

    def market_score(self, candidate: AnalysisCandidate, direction: str) -> Tuple[float, List[str]]:
        mode = str((candidate.market_mode or {}).get("mode", "NEUTRAL")).upper()
        reasons: List[str] = []

        if mode == "BULLISH":
            if direction == DIRECTION_LONG:
                reasons.append("MARKET_MODE_SUPPORTS_LONG")
                return 8.0, reasons
            if direction == DIRECTION_SHORT:
                reasons.append("MARKET_MODE_AGAINST_SHORT")
                return -6.0, reasons
        elif mode == "BEARISH":
            if direction == DIRECTION_SHORT:
                reasons.append("MARKET_MODE_SUPPORTS_SHORT")
                return 8.0, reasons
            if direction == DIRECTION_LONG:
                reasons.append("MARKET_MODE_AGAINST_LONG")
                return -6.0, reasons
        elif mode == "CHOPPY":
            reasons.append("MARKET_MODE_CHOPPY")
            return -5.0, reasons

        reasons.append("MARKET_MODE_NEUTRAL")
        return 0.0, reasons

    def tradability_score(self, candidate: AnalysisCandidate, prediction: MovementPredictionResult, learning: Optional[Any]) -> Tuple[float, float, List[str], List[str]]:
        reasons: List[str] = []
        warnings: List[str] = []

        notional = estimated_notional_usdt()
        if notional <= 0:
            warnings.append("NO_NOTIONAL_FOR_FEE_CHECK")
            return 50.0, 0.0, reasons, warnings

        total_fee = notional * fee_rate_per_side() * 2.0
        min_net = min_net_profit_usdt()

        m = candidate.momentum_state
        atr_pct = max(0.0, safe_float(m.atr_percent))
        expected_pct = max(0.0, obj_float(prediction, "expected_move_percent", 0.0))
        phase = str(obj_value(prediction, "predicted_phase", PHASE_UNKNOWN)).upper()

        if phase in {PHASE_PRE_START, PHASE_START}:
            gross_move_pct = max(atr_pct * 0.95, expected_pct * 0.70)
        elif phase == PHASE_MID:
            gross_move_pct = max(atr_pct * 0.75, expected_pct * 0.55)
        else:
            gross_move_pct = max(atr_pct * 0.55, expected_pct * 0.35)

        if obj_float(learning, "early_success_rate", 0.0) >= 35 or obj_float(learning, "timing_score", 50.0) >= 62:
            gross_move_pct = max(gross_move_pct, atr_pct * 0.95, expected_pct * 0.72)
            reasons.append("LEARNING_SUPPORTS_EXPECTED_MOVE")

        gross_usdt = notional * gross_move_pct / 100.0
        net_usdt = gross_usdt - total_fee
        required_pct = ((total_fee + min_net) / notional) * 100.0

        fee_cover = clamp((gross_move_pct / max(required_pct, 1e-9)) * 70.0, 0.0, 75.0)
        net_quality = clamp((net_usdt / max(min_net, 0.01)) * 28.0, 0.0, 35.0)
        volume_bonus = 0.0
        if m.relative_volume >= 1.8 or m.volume_spike:
            volume_bonus = 8.0
            reasons.append("VOLUME_SUPPORTS_TRADABILITY")
        elif 0 < m.relative_volume < 0.65:
            volume_bonus = -8.0
            warnings.append("LOW_VOLUME_TRADABILITY")

        score = clamp(fee_cover + net_quality + volume_bonus)

        if net_usdt < min_net:
            warnings.append("NET_PROFIT_BELOW_MINIMUM")
        else:
            reasons.append("NET_PROFIT_USEFUL")
        if gross_move_pct < required_pct:
            warnings.append("MOVE_TOO_SMALL_FOR_FEES")

        return score, net_usdt, reasons, warnings

    def learning_score(self, learning: Optional[Any]) -> Tuple[float, List[str], List[str]]:
        reasons: List[str] = []
        warnings: List[str] = []

        if learning is None:
            warnings.append("NO_LEARNING_SUMMARY")
            return 50.0, reasons, warnings

        sample_count = obj_int(learning, "sample_count", 0)
        outcome = obj_float(learning, "outcome_success_rate", 50.0)
        timing = obj_float(learning, "timing_score", 50.0)
        early = obj_float(learning, "early_success_rate", 0.0)
        fuzzy = obj_float(learning, "fuzzy_match_score", 0.0)
        pattern_conf = obj_float(learning, "pattern_confidence", 0.0)
        risk_label = str(obj_value(learning, "risk_label", "UNKNOWN")).upper()

        sample_score = min(100.0, sample_count * 7.0)
        score = clamp(
            outcome * 0.28
            + timing * 0.24
            + early * 0.14
            + fuzzy * 0.12
            + pattern_conf * 0.12
            + sample_score * 0.10
        )

        if risk_label == "FAVORABLE_CONDITION":
            score = clamp(score + 8.0)
            reasons.append("LEARNING_FAVORABLE")
        elif risk_label == "RISKY_CONDITION":
            score = clamp(score - 14.0)
            warnings.append("LEARNING_RISKY_CONDITION")

        if sample_count <= 0:
            warnings.append("LEARNING_LOW_DATA")
        elif sample_count < 3:
            warnings.append("LEARNING_SMALL_SAMPLE")
        if early >= 35 and timing >= 58:
            reasons.append("LEARNING_EARLY_PATTERN_WORKED")

        return score, reasons, warnings

    def compose(
        self,
        candidate: AnalysisCandidate,
        prediction: MovementPredictionResult,
        learning: Optional[Any] = None,
    ) -> Tuple[DecisionScore, Tuple[str, ...], Tuple[str, ...], float]:
        reasons: List[str] = []
        warnings: List[str] = []

        direction = infer_decision_direction(candidate, prediction)
        sensor_score, r = self.sensor_birth_score(candidate, direction)
        reasons.extend(r)

        prediction_score = clamp(
            obj_float(prediction, "movement_probability", 0.0) * 0.56
            + obj_float(prediction, "pattern_match_score", 0.0) * 0.22
            + obj_float(prediction, "pattern_confidence", 0.0) * 0.14
            + obj_float(prediction, "pattern_win_rate", 0.0) * 0.08
        )

        learning_score, r, w = self.learning_score(learning)
        reasons.extend(r)
        warnings.extend(w)

        market_score, r = self.market_score(candidate, direction)
        reasons.extend(r)

        phase = str(obj_value(prediction, "predicted_phase", PHASE_UNKNOWN)).upper()
        if phase in {PHASE_PRE_START, PHASE_START}:
            freshness_score = 100.0
            reasons.append("PREDICTED_EARLY_START")
        elif phase == PHASE_MID:
            freshness_score = 62.0
            reasons.append("PREDICTED_MID_MOVE")
        elif phase == PHASE_LATE:
            freshness_score = 18.0
            warnings.append("PREDICTED_LATE_MOVE")
        elif phase == PHASE_RANGE:
            freshness_score = 20.0
            warnings.append("PREDICTED_RANGE")
        else:
            freshness_score = 35.0
            warnings.append("PREDICTED_UNKNOWN")

        m = candidate.momentum_state
        range_penalty = 0.0
        if m.range_probability >= 88:
            range_penalty = 22.0
            warnings.append("HIGH_RANGE_PROBABILITY")
        elif m.range_probability >= 75:
            range_penalty = 10.0
            warnings.append("RANGE_CAUTION")

        late_penalty = 0.0
        change = safe_float(m.price_change_percent)
        atr = max(0.05, safe_float(m.atr_percent))
        if direction == DIRECTION_LONG and change > max(atr * 2.2, 1.65):
            late_penalty = 24.0
            warnings.append("LONG_MOVE_ALREADY_EXTENDED")
        elif direction == DIRECTION_SHORT and change < -max(atr * 2.2, 1.65):
            late_penalty = 24.0
            warnings.append("SHORT_MOVE_ALREADY_EXTENDED")
        if phase == PHASE_LATE:
            late_penalty = max(late_penalty, 30.0)

        tradability, net_usdt, r, w = self.tradability_score(candidate, prediction, learning)
        reasons.extend(r)
        warnings.extend(w)

        fee_penalty = 0.0
        if tradability < 35:
            fee_penalty = 18.0
        elif tradability < 50:
            fee_penalty = 8.0

        final_score = clamp(
            sensor_score * 0.28
            + prediction_score * 0.30
            + learning_score * 0.18
            + freshness_score * 0.14
            + clamp(50.0 + market_score, 0.0, 100.0) * 0.04
            + tradability * 0.06
            - range_penalty * 0.45
            - late_penalty * 0.70
            - fee_penalty
        )

        score = DecisionScore(
            sensor_score=clamp(sensor_score),
            prediction_score=clamp(prediction_score),
            learning_score=clamp(learning_score),
            market_score=clamp(market_score, -100.0, 100.0),
            freshness_score=clamp(freshness_score),
            range_penalty=clamp(range_penalty),
            late_penalty=clamp(late_penalty),
            fee_penalty=clamp(fee_penalty),
            tradability_score=clamp(tradability),
            final_score=clamp(final_score),
        )
        return score, tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(warnings)), net_usdt


class DecisionTypeClassifier:
    def classify(
        self,
        candidate: AnalysisCandidate,
        prediction: MovementPredictionResult,
        learning: Optional[Any],
        score: DecisionScore,
    ) -> Tuple[str, Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
        reasons: List[str] = []
        warnings: List[str] = []
        rejects: List[str] = []

        if not bool(getattr(candidate, "valid", True)):
            rejects.append("INVALID_CANDIDATE")
        if safe_float(getattr(candidate.sensor_snapshot, "price", 0.0), 0.0) <= 0:
            rejects.append("INVALID_PRICE")
        direction = infer_decision_direction(candidate, prediction)
        if direction == DIRECTION_NEUTRAL:
            rejects.append("NO_DIRECTION")
        if rejects:
            return DECISION_REJECT, tuple(reasons), tuple(warnings), tuple(dict.fromkeys(rejects))

        min_real = clamp(settings_float("ai.min_real_confidence", 62.0), 52.0, 72.0)
        min_ghost = clamp(settings_float("ai.min_ghost_confidence", 28.0), 18.0, 45.0)

        phase = str(obj_value(prediction, "predicted_phase", PHASE_UNKNOWN)).upper()
        movement_probability = obj_float(prediction, "movement_probability", 0.0)
        pattern_count = obj_int(prediction, "pattern_count", 0)
        pattern_confidence = obj_float(prediction, "pattern_confidence", 0.0)

        learning_samples = obj_int(learning, "sample_count", 0)
        learning_risk = str(obj_value(learning, "risk_label", "UNKNOWN")).upper()
        learning_hint = str(obj_value(learning, "confidence_hint", "LOW_DATA")).upper()
        early_rate = obj_float(learning, "early_success_rate", 0.0)
        timing_score = obj_float(learning, "timing_score", 50.0)

        early_phase = phase in {PHASE_PRE_START, PHASE_START}
        live_enough = movement_probability >= 44 or score.sensor_score >= 42 or score.prediction_score >= 42
        useful_patterns = pattern_count >= 3 or pattern_confidence >= 45 or early_rate >= 35 or timing_score >= 60

        must_ghost = False
        if score.tradability_score < 35:
            must_ghost = True
            reasons.append("LOW_TRADABILITY_GHOST")
        if learning_risk == "RISKY_CONDITION":
            must_ghost = True
            reasons.append("RISKY_LEARNING_GHOST")
        if phase in {PHASE_LATE, PHASE_RANGE} and not (early_rate >= 45 and score.sensor_score >= 50):
            must_ghost = True
            reasons.append(f"{phase}_GHOST")
        # Fresh Level 1 reset rule:
        # LOW_DATA must be used for GHOST learning, not as a dry rejection state.
        # REAL still stays blocked by must_ghost until enough samples exist.
        if learning_hint == "LOW_DATA" or learning_samples < 3:
            must_ghost = True
            reasons.append("LOW_DATA_GHOST")

        real_allowed = (
            not must_ghost
            and score.final_score >= min_real
            and live_enough
            and early_phase
            and useful_patterns
            and score.tradability_score >= 45
            and score.late_penalty < 24
            and score.range_penalty < 22
        )

        speed_real_bridge = (
            not real_allowed
            and not must_ghost
            and early_phase
            and live_enough
            and score.final_score >= min_real - 12
            and score.sensor_score >= 48
            and movement_probability >= 48
            and score.tradability_score >= 45
        )

        learning_real_bridge = (
            not real_allowed
            and not speed_real_bridge
            and not must_ghost
            and early_phase
            and live_enough
            and learning_samples >= 5
            and early_rate >= 40
            and timing_score >= 62
            and score.final_score >= min_real - 14
            and score.tradability_score >= 42
        )

        if real_allowed or speed_real_bridge or learning_real_bridge:
            reasons.append("AI_DECISION_REAL")
            if speed_real_bridge:
                reasons.append("SPEED_HUNTER_REAL_BRIDGE")
            if learning_real_bridge:
                reasons.append("LEARNING_REAL_BRIDGE")
            return DECISION_REAL, tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(warnings)), ()

        ghost_allowed = (
            must_ghost
            or score.final_score >= min_ghost
            or movement_probability >= 12
            or score.sensor_score >= 12
            or score.prediction_score >= 12
            or pattern_count > 0
            or learning_samples < 3
        )
        if ghost_allowed:
            reasons.append("AI_DECISION_GHOST_FOR_LEARNING")
            if must_ghost:
                warnings.append("REAL_DOWNGRADED_TO_GHOST")
            return DECISION_GHOST, tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(warnings)), ()

        return DECISION_REJECT, tuple(reasons), tuple(warnings), ("TOO_WEAK_FOR_LEARNING",)


class AIDecisionEngine:
    def __init__(self):
        self.composer = AIScoreComposer()
        self.classifier = DecisionTypeClassifier()

    def decide(
        self,
        candidate: AnalysisCandidate,
        prediction: MovementPredictionResult,
        learning: Optional[LearningSummary] = None,
        **_: Any,
    ) -> AIDecision:
        score, score_reasons, score_warnings, expected_net_usdt = self.composer.compose(
            candidate=candidate,
            prediction=prediction,
            learning=learning,
        )

        decision_type, classify_reasons, classify_warnings, reject_reasons = self.classifier.classify(
            candidate=candidate,
            prediction=prediction,
            learning=learning,
            score=score,
        )

        direction = infer_decision_direction(candidate, prediction)
        entry = safe_float(getattr(candidate.sensor_snapshot, "price", 0.0), 0.0)

        confidence_score = clamp(
            score.final_score * 0.55
            + obj_float(prediction, "movement_probability", 0.0) * 0.25
            + obj_float(learning, "pattern_confidence", 0.0) * 0.20
        )

        risk_score = clamp(
            score.range_penalty * 1.6
            + score.late_penalty * 1.8
            + score.fee_penalty * 1.5
            + (20.0 if str(obj_value(learning, "risk_label", "")).upper() == "RISKY_CONDITION" else 0.0)
        )

        reasons: List[str] = []
        warnings: List[str] = []
        reasons.extend(score_reasons)
        reasons.extend(classify_reasons)
        warnings.extend(score_warnings)
        warnings.extend(classify_warnings)
        reasons.extend(list(getattr(candidate, "reason_codes", ()) or ())[:8])
        reasons.extend(list(obj_value(prediction, "reason_codes", ()) or ())[:8])

        return AIDecision(
            decision_id=f"dec_{uuid4().hex}",
            symbol=str(candidate.symbol),
            timeframe=str(candidate.timeframe or "5m"),
            timestamp=int(candidate.timestamp or now_ts()),
            direction=direction,
            decision_type=decision_type,
            confidence_score=clamp(confidence_score),
            risk_score=clamp(risk_score),
            ai_score=clamp(score.final_score),
            entry=entry,
            predicted_phase=str(obj_value(prediction, "predicted_phase", PHASE_UNKNOWN)).upper(),
            movement_probability=clamp(obj_float(prediction, "movement_probability", 0.0)),
            pattern_count=obj_int(prediction, "pattern_count", 0),
            pattern_match_score=clamp(obj_float(prediction, "pattern_match_score", 0.0)),
            pattern_confidence=clamp(obj_float(prediction, "pattern_confidence", 0.0)),
            should_trade_real=decision_type == DECISION_REAL,
            should_create_ghost=decision_type == DECISION_GHOST,
            should_reject=decision_type == DECISION_REJECT,
            score=score,
            reason_codes=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
            reject_reasons=tuple(dict.fromkeys(reject_reasons)),
            meta={
                "learning": to_dict(learning),
                "prediction": to_dict(prediction),
                "expected_net_usdt": expected_net_usdt,
                "tp_sl_note": "TP/SL will be calculated by tp_sl_engine.py",
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
    prediction: MovementPredictionResult,
    learning: Optional[LearningSummary] = None,
    **kwargs: Any,
) -> AIDecision:
    return engine().decide(
        candidate=candidate,
        prediction=prediction,
        learning=learning,
        **kwargs,
    )


def ai_decision_engine(
    candidate: AnalysisCandidate,
    prediction: MovementPredictionResult,
    learning: Optional[LearningSummary] = None,
    **kwargs: Any,
) -> AIDecision:
    return decide(candidate=candidate, prediction=prediction, learning=learning, **kwargs)
