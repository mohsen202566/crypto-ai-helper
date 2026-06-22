from __future__ import annotations

"""
19 - tp_sl_engine.py

Smart TP/SL engine for the locked Movement Hunter architecture.

Responsibilities:
- Calculate TP1, optional TP2, and SL for AIDecision.
- Adapt TP/SL to:
  ATR / volatility
  coin learning
  movement prediction
  market state
  trap/liquidity risk
  range/compression
  breakout survival / retest tolerance
  coin noise
- Keep SL not too close, especially around breakout/retest/liquidity zones.
- Decide TP mode:
  TP1_ONLY
  TP1_TP2

Strictly forbidden:
- No trade execution.
- No Toobit calls.
- No Telegram.
- No persistence.
- No REAL/GHOST/REJECT decision.
- No Paper mode.
- No Setup flow.

This file only calculates prices.
real_trade_manager.py opens orders later.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
import math
import time

from ai_decision_engine import AIDecision
from analysis_engine import AnalysisCandidate
from movement_hunter import MovementHunterResult
from trap_engine import TrapResult
from state_engine import StateResult
from confidence_engine import ConfidenceResult
from coin_learning import LearningSummary
from movement_predictor import MovementPredictionResult
from config import SETTINGS


JsonDict = Dict[str, Any]

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"

TP_MODE_TP1_ONLY = "TP1_ONLY"
TP_MODE_TP1_TP2 = "TP1_TP2"

QUALITY_LOW = "LOW"
QUALITY_MEDIUM = "MEDIUM"
QUALITY_HIGH = "HIGH"


@dataclass(frozen=True)
class TPSLPlan:
    plan_id: str
    decision_id: str
    symbol: str
    direction: str
    entry: float
    tp1: float
    tp2: float
    sl: float
    tp_mode: str
    rr_tp1: float
    rr_tp2: float
    sl_distance_percent: float
    tp1_distance_percent: float
    tp2_distance_percent: float
    atr_percent: float
    quality_label: str
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    valid: bool = True

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class TPSLMultipliers:
    tp1_atr: float
    tp2_atr: float
    sl_atr: float
    min_sl_atr: float
    max_sl_atr: float

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


def clamp(value: float, low: float, high: float) -> float:
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
    return d


def price_from_percent(entry: float, direction: str, percent: float) -> float:
    entry = safe_float(entry)
    p = safe_float(percent) / 100.0
    if normalize_direction(direction) == DIRECTION_LONG:
        return entry * (1.0 + p)
    return entry * (1.0 - p)


def sl_from_percent(entry: float, direction: str, percent: float) -> float:
    entry = safe_float(entry)
    p = safe_float(percent) / 100.0
    if normalize_direction(direction) == DIRECTION_LONG:
        return entry * (1.0 - p)
    return entry * (1.0 + p)


def distance_percent(entry: float, price: float) -> float:
    entry = safe_float(entry)
    price = safe_float(price)
    if entry <= 0 or price <= 0:
        return 0.0
    return abs(price - entry) / entry * 100.0


def rr_ratio(entry: float, tp: float, sl: float) -> float:
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return 0.0
    return reward / risk


def round_price(price: float, symbol: str = "") -> float:
    """
    Generic safe rounding. Exchange exact tick-size validation is done later
    inside tobit_client.py / real_trade_manager.py.
    """
    price = safe_float(price)
    if price <= 0:
        return 0.0

    if price >= 1000:
        decimals = 2
    elif price >= 100:
        decimals = 3
    elif price >= 10:
        decimals = 4
    elif price >= 1:
        decimals = 5
    elif price >= 0.1:
        decimals = 6
    else:
        decimals = 8
    return round(price, decimals)


class BaseMultiplierEngine:
    """Base scalping multipliers for 5M-15M Movement Hunter."""

    def base(self) -> TPSLMultipliers:
        min_sl = safe_float(getattr(SETTINGS.tp, "min_sl_atr_multiplier", 1.0), 1.0)
        max_sl = safe_float(getattr(SETTINGS.tp, "max_sl_atr_multiplier", 2.6), 2.6)
        return TPSLMultipliers(
            tp1_atr=0.85,
            tp2_atr=1.55,
            sl_atr=1.15,
            min_sl_atr=min_sl,
            max_sl_atr=max_sl,
        )


class VolatilityAdjustmentEngine:
    """Adjusts multipliers based on ATR/volatility/range."""

    def adjust(self, m: TPSLMultipliers, candidate: AnalysisCandidate, state: StateResult) -> Tuple[TPSLMultipliers, List[str]]:
        s = candidate.sensor_snapshot
        reasons: List[str] = []

        tp1 = m.tp1_atr
        tp2 = m.tp2_atr
        sl = m.sl_atr

        if s.atr_explosion:
            tp1 += 0.10
            tp2 += 0.25
            sl += 0.20
            reasons.append("ATR_EXPLOSION_WIDER_PLAN")
        elif s.atr_expansion == "EXPANDING":
            tp1 += 0.05
            tp2 += 0.15
            sl += 0.10
            reasons.append("ATR_EXPANDING_ADJUSTMENT")

        if state.range_probability >= 65:
            tp1 -= 0.10
            tp2 -= 0.25
            sl += 0.10
            reasons.append("RANGE_TIGHTER_TP_WIDER_SL")
        elif state.range_probability <= 30:
            tp2 += 0.10
            reasons.append("LOW_RANGE_ALLOW_TP2")

        return TPSLMultipliers(
            tp1_atr=clamp(tp1, 0.45, 1.40),
            tp2_atr=clamp(tp2, 0.90, 2.50),
            sl_atr=clamp(sl, m.min_sl_atr, m.max_sl_atr),
            min_sl_atr=m.min_sl_atr,
            max_sl_atr=m.max_sl_atr,
        ), reasons


class TrapLiquidityAdjustmentEngine:
    """Avoids SL being too close to liquidity/retest noise."""

    def adjust(self, m: TPSLMultipliers, trap: TrapResult, candidate: AnalysisCandidate) -> Tuple[TPSLMultipliers, List[str]]:
        s = candidate.sensor_snapshot
        reasons: List[str] = []

        tp1 = m.tp1_atr
        tp2 = m.tp2_atr
        sl = m.sl_atr

        if trap.trap_risk >= 65:
            tp1 -= 0.10
            tp2 -= 0.30
            sl += 0.20
            reasons.append("HIGH_TRAP_CAUTION")
        elif trap.trap_risk >= 40:
            sl += 0.10
            reasons.append("MEDIUM_TRAP_SL_TOLERANCE")

        if trap.liquidity_risk >= 60:
            sl += 0.20
            reasons.append("LIQUIDITY_RISK_WIDER_SL")

        if s.breakout_candidate or s.breakdown_candidate:
            sl += 0.12
            reasons.append("BREAKOUT_RETEST_TOLERANCE")

        if s.failed_breakout or s.failed_breakdown:
            tp1 -= 0.08
            tp2 -= 0.20
            reasons.append("FAILED_BREAK_CAUTION")

        return TPSLMultipliers(
            tp1_atr=clamp(tp1, 0.40, 1.35),
            tp2_atr=clamp(tp2, 0.75, 2.40),
            sl_atr=clamp(sl, m.min_sl_atr, m.max_sl_atr),
            min_sl_atr=m.min_sl_atr,
            max_sl_atr=m.max_sl_atr,
        ), reasons


class PredictionLearningAdjustmentEngine:
    """Uses prediction and coin learning to adapt targets."""

    def adjust(
        self,
        m: TPSLMultipliers,
        prediction: MovementPredictionResult,
        learning: Optional[LearningSummary],
    ) -> Tuple[TPSLMultipliers, List[str]]:
        reasons: List[str] = []
        tp1 = m.tp1_atr
        tp2 = m.tp2_atr
        sl = m.sl_atr

        if prediction.predicted_phase == "PRE_START":
            tp2 += 0.20
            reasons.append("PRE_START_ALLOW_MORE_TP2")
        elif prediction.predicted_phase == "START":
            tp2 += 0.10
            reasons.append("START_PHASE_TP2_OK")
        elif prediction.predicted_phase == "LATE":
            tp1 -= 0.18
            tp2 -= 0.45
            sl += 0.12
            reasons.append("LATE_CONSERVATIVE_TP_STRONGER")
        elif prediction.predicted_phase == "RANGE":
            tp1 -= 0.12
            tp2 -= 0.35
            sl += 0.10
            reasons.append("RANGE_CONSERVATIVE_TP")

        if prediction.expected_move_percent > 0:
            # Convert expected percent into soft ATR estimate when enough memory exists.
            if prediction.sample_count >= 5:
                if prediction.expected_move_percent > 1.2:
                    tp2 += 0.15
                    reasons.append("MEMORY_EXPECTS_LARGER_MOVE")
                elif prediction.expected_move_percent < 0.45:
                    tp1 -= 0.10
                    tp2 -= 0.25
                    reasons.append("MEMORY_EXPECTS_SMALL_MOVE")

        if learning is not None:
            if learning.risk_label == "FAVORABLE_CONDITION" and learning.win_rate >= 65:
                tp2 += 0.15
                sl -= 0.05
                reasons.append("LEARNING_FAVORABLE_CONDITION")
            elif learning.risk_label == "RISKY_CONDITION":
                tp1 -= 0.08
                tp2 -= 0.30
                sl += 0.10
                reasons.append("LEARNING_RISKY_CONDITION")

            if learning.avg_mae_percent > learning.avg_mfe_percent and learning.sample_count >= 5:
                sl += 0.15
                tp2 -= 0.15
                reasons.append("LEARNING_HIGH_ADVERSE_NOISE")

        return TPSLMultipliers(
            tp1_atr=clamp(tp1, 0.40, 1.45),
            tp2_atr=clamp(tp2, 0.70, 2.70),
            sl_atr=clamp(sl, m.min_sl_atr, m.max_sl_atr),
            min_sl_atr=m.min_sl_atr,
            max_sl_atr=m.max_sl_atr,
        ), reasons


class TPModeEngine:
    """Decides whether TP2 should be used."""

    def decide(
        self,
        decision: AIDecision,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
        confidence: ConfidenceResult,
        prediction: MovementPredictionResult,
        learning: Optional[LearningSummary],
    ) -> Tuple[str, List[str]]:
        reasons: List[str] = []

        if not bool(getattr(SETTINGS.tp, "tp2_enabled", True)):
            reasons.append("TP2_DISABLED_BY_CONFIG")
            return TP_MODE_TP1_ONLY, reasons

        strong = (
            decision.ai_score >= 70
            and movement.continuation_probability >= 60
            and confidence.confidence_score >= 60
            and prediction.movement_probability >= 60
            and trap.trap_risk < 60
            and state.market_state not in {"RANGE", "EXHAUSTION", "LATE"}
            and prediction.predicted_phase in {"PRE_START", "START", "MID"}
        )

        if learning is not None and learning.risk_label == "RISKY_CONDITION":
            strong = False
            reasons.append("TP2_BLOCKED_BY_RISKY_LEARNING")

        if strong:
            reasons.append("TP2_ALLOWED_STRONG_SIGNAL")
            return TP_MODE_TP1_TP2, reasons

        reasons.append("TP1_ONLY_CONSERVATIVE")
        return TP_MODE_TP1_ONLY, reasons


class TPSLValidator:
    """Validates price relationships and minimum RR."""

    def validate(self, plan: TPSLPlan) -> Tuple[bool, List[str]]:
        warnings: List[str] = []

        if plan.entry <= 0 or plan.tp1 <= 0 or plan.sl <= 0:
            warnings.append("INVALID_PRICE_IN_TP_SL_PLAN")
            return False, warnings

        if plan.direction == DIRECTION_LONG:
            if not (plan.sl < plan.entry < plan.tp1):
                warnings.append("INVALID_LONG_TP_SL_RELATION")
                return False, warnings
            if plan.tp_mode == TP_MODE_TP1_TP2 and plan.tp2 > 0 and not (plan.tp2 > plan.tp1):
                warnings.append("INVALID_LONG_TP2_RELATION")
                return False, warnings
        elif plan.direction == DIRECTION_SHORT:
            if not (plan.tp1 < plan.entry < plan.sl):
                warnings.append("INVALID_SHORT_TP_SL_RELATION")
                return False, warnings
            if plan.tp_mode == TP_MODE_TP1_TP2 and plan.tp2 > 0 and not (plan.tp2 < plan.tp1):
                warnings.append("INVALID_SHORT_TP2_RELATION")
                return False, warnings
        else:
            warnings.append("INVALID_DIRECTION")
            return False, warnings

        min_rr = safe_float(getattr(SETTINGS.tp, "min_rr", 1.1), 1.1)
        if plan.rr_tp1 < min_rr * 0.55:
            warnings.append("TP1_RR_LOW_BUT_ALLOWED_FOR_SCALP")
        if plan.tp_mode == TP_MODE_TP1_TP2 and plan.rr_tp2 < min_rr:
            warnings.append("TP2_RR_BELOW_MIN")

        if plan.sl_distance_percent <= 0:
            warnings.append("SL_DISTANCE_ZERO")
            return False, warnings

        return True, warnings


class TPSLEngine:
    """Main smart TP/SL engine."""

    def __init__(self):
        self.base = BaseMultiplierEngine()
        self.volatility = VolatilityAdjustmentEngine()
        self.trap = TrapLiquidityAdjustmentEngine()
        self.prediction_learning = PredictionLearningAdjustmentEngine()
        self.tp_mode = TPModeEngine()
        self.validator = TPSLValidator()

    def build_plan(
        self,
        decision: AIDecision,
        candidate: AnalysisCandidate,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
        confidence: ConfidenceResult,
        prediction: MovementPredictionResult,
        learning: Optional[LearningSummary] = None,
    ) -> TPSLPlan:
        reasons: List[str] = []
        warnings: List[str] = []

        direction = normalize_direction(decision.direction)
        entry = safe_float(decision.entry or candidate.sensor_snapshot.price)
        atr_percent = safe_float(candidate.sensor_snapshot.atr_percent)

        if atr_percent <= 0:
            # Fallback when ATR is unavailable. Conservative scalping default.
            atr_percent = 0.55
            warnings.append("ATR_PERCENT_FALLBACK_USED")

        m = self.base.base()
        m, r = self.volatility.adjust(m, candidate, state)
        reasons.extend(r)

        m, r = self.trap.adjust(m, trap, candidate)
        reasons.extend(r)

        m, r = self.prediction_learning.adjust(m, prediction, learning)
        reasons.extend(r)

        # Convert ATR multipliers to percentage distances.
        tp1_percent = atr_percent * m.tp1_atr
        tp2_percent = atr_percent * m.tp2_atr
        sl_percent = atr_percent * m.sl_atr

        # Hard minimum SL distance so breakout/retest noise doesn't instantly stop out.
        min_sl_percent = max(atr_percent * m.min_sl_atr, 0.18)
        sl_percent = max(sl_percent, min_sl_percent)

        # Avoid absurd SL in sudden volatility.
        max_sl_percent = max(atr_percent * m.max_sl_atr, min_sl_percent)
        sl_percent = min(sl_percent, max_sl_percent)

        # TP1 should remain reachable for 5M-15M scalping.
        tp1_percent = clamp(tp1_percent, 0.18, max(1.50, atr_percent * 1.50))
        tp2_percent = clamp(tp2_percent, tp1_percent * 1.25, max(3.50, atr_percent * 2.80))

        mode, r = self.tp_mode.decide(
            decision=decision,
            movement=movement,
            trap=trap,
            state=state,
            confidence=confidence,
            prediction=prediction,
            learning=learning,
        )
        reasons.extend(r)

        tp1 = round_price(price_from_percent(entry, direction, tp1_percent), decision.symbol)
        tp2 = round_price(price_from_percent(entry, direction, tp2_percent), decision.symbol) if mode == TP_MODE_TP1_TP2 else 0.0
        sl = round_price(sl_from_percent(entry, direction, sl_percent), decision.symbol)

        rr1 = rr_ratio(entry, tp1, sl)
        rr2 = rr_ratio(entry, tp2, sl) if tp2 > 0 else 0.0

        quality_label = QUALITY_MEDIUM
        if decision.ai_score >= 75 and prediction.movement_probability >= 70 and trap.trap_risk < 45:
            quality_label = QUALITY_HIGH
        elif decision.ai_score < 55 or trap.trap_risk >= 65 or state.market_state in {"RANGE", "EXHAUSTION"}:
            quality_label = QUALITY_LOW

        plan = TPSLPlan(
            plan_id=f"tpsl_{uuid4().hex}",
            decision_id=decision.decision_id,
            symbol=decision.symbol,
            direction=direction,
            entry=round_price(entry, decision.symbol),
            tp1=tp1,
            tp2=tp2,
            sl=sl,
            tp_mode=mode,
            rr_tp1=rr1,
            rr_tp2=rr2,
            sl_distance_percent=distance_percent(entry, sl),
            tp1_distance_percent=distance_percent(entry, tp1),
            tp2_distance_percent=distance_percent(entry, tp2) if tp2 > 0 else 0.0,
            atr_percent=atr_percent,
            quality_label=quality_label,
            reason_codes=tuple(dict.fromkeys(reasons)),
            warnings=tuple(warnings),
            valid=True,
        )

        valid, validation_warnings = self.validator.validate(plan)
        all_warnings = tuple(dict.fromkeys(list(plan.warnings) + validation_warnings))
        if not valid or validation_warnings:
            plan = TPSLPlan(**{**plan.to_dict(), "valid": valid, "warnings": all_warnings})

        return plan


_default_engine: Optional[TPSLEngine] = None


def engine() -> TPSLEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = TPSLEngine()
    return _default_engine


def build_tp_sl_plan(
    decision: AIDecision,
    candidate: AnalysisCandidate,
    movement: MovementHunterResult,
    trap: TrapResult,
    state: StateResult,
    confidence: ConfidenceResult,
    prediction: MovementPredictionResult,
    learning: Optional[LearningSummary] = None,
) -> TPSLPlan:
    return engine().build_plan(
        decision=decision,
        candidate=candidate,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        prediction=prediction,
        learning=learning,
    )


def apply_tp_sl_to_decision(decision: AIDecision, plan: TPSLPlan) -> AIDecision:
    """
    Return a new AIDecision with TP/SL fields filled.
    No trade execution happens here.
    """
    data = decision.to_dict()
    data.update(
        {
            "tp1": plan.tp1,
            "tp2": plan.tp2,
            "sl": plan.sl,
            "tp_mode": plan.tp_mode,
            "warnings": tuple(dict.fromkeys(list(decision.warnings) + list(plan.warnings))),
            "reason_codes": tuple(dict.fromkeys(list(decision.reason_codes) + list(plan.reason_codes))),
            "meta": {
                **dict(decision.meta),
                "tp_sl_plan": plan.to_dict(),
            },
        }
    )
    return AIDecision(**data)


def tp_sl_engine(
    decision: AIDecision,
    candidate: AnalysisCandidate,
    movement: MovementHunterResult,
    trap: TrapResult,
    state: StateResult,
    confidence: ConfidenceResult,
    prediction: MovementPredictionResult,
    learning: Optional[LearningSummary] = None,
) -> TPSLPlan:
    return build_tp_sl_plan(
        decision=decision,
        candidate=candidate,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        prediction=prediction,
        learning=learning,
    )
