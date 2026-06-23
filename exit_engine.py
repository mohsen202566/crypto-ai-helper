from __future__ import annotations

"""
20 - exit_engine.py

Light smart-exit engine for the simplified Level 1 / 5M crypto futures bot.

Locked rule:
- Before TP1, AI close is allowed ONLY when price has reached at least 70%
  of the path from entry to TP1 AND weakness/reversal is visible.
- Before 70% TP1 progress: no AI close, except emergency protection.
- After TP1: protect profit and optionally close runner if weakness appears.
- No movement_hunter / trap / state / confidence / meta / correlation dependency.
- No Toobit call.
- No Telegram sending.
- No persistence.
- No REAL/GHOST/REJECT entry decision.
- No paper/setup flow.

This file returns ExitDecision only.
position_monitor.py / real_trade_manager.py will execute actual closing later.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Tuple, List
from uuid import uuid4
import math
import time

from ai_decision_engine import AIDecision
from tp_sl_engine import TPSLPlan
from analysis_layers import SensorSnapshot


JsonDict = Dict[str, Any]

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"

EXIT_HOLD = "HOLD"
EXIT_PROTECT_PROFIT = "PROTECT_PROFIT"
EXIT_AI_CLOSE = "AI_CLOSE"
EXIT_EMERGENCY = "EMERGENCY"

CONFIRM_WAITING = "WAITING"
CONFIRM_CONFIRMED = "CONFIRMED"

AI_EXIT_MIN_TP1_PROGRESS = 0.70


@dataclass(frozen=True)
class PositionContext:
    position_id: str
    symbol: str
    direction: str
    entry: float
    current_price: float
    tp1: float
    tp2: float
    sl: float
    tp1_hit: bool = False
    tp2_hit: bool = False
    open_time: int = 0
    last_update: int = 0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    unrealized_pnl_percent: float = 0.0
    unrealized_pnl_usdt: float = 0.0

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class ExitScore:
    tp1_progress: float
    tp2_progress: float
    profit_score: float
    weakness_score: float
    reversal_score: float
    invalidation_score: float
    emergency_score: float
    total_exit_score: float

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class ExitDecision:
    exit_id: str
    position_id: str
    symbol: str
    direction: str
    timestamp: int
    action: str
    confirmation_status: str
    should_close: bool
    should_move_sl_to_protect: bool
    protected_sl: float
    exit_price: float
    expected_pnl_percent: float
    expected_pnl_usdt: float
    score: ExitScore
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    valid: bool = True

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class ExitConfirmationState:
    position_id: str
    action: str
    first_seen_at: int
    last_seen_at: int
    confirmation_count: int
    last_score: float


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
    return max(low, min(high, safe_float(value, low)))


def normalize_direction(direction: str) -> str:
    d = str(direction or "").upper().strip()
    if d in {"LONG", "BUY"}:
        return DIRECTION_LONG
    if d in {"SHORT", "SELL"}:
        return DIRECTION_SHORT
    return d


def pnl_percent(direction: str, entry: float, price: float) -> float:
    entry = safe_float(entry)
    price = safe_float(price)
    if entry <= 0 or price <= 0:
        return 0.0
    if normalize_direction(direction) == DIRECTION_LONG:
        return (price - entry) / entry * 100.0
    return (entry - price) / entry * 100.0


def progress_to_tp1(ctx: PositionContext) -> float:
    entry = safe_float(ctx.entry)
    tp1 = safe_float(ctx.tp1)
    price = safe_float(ctx.current_price)
    direction = normalize_direction(ctx.direction)

    if entry <= 0 or tp1 <= 0 or price <= 0 or abs(tp1 - entry) <= 0:
        return 0.0

    if direction == DIRECTION_LONG:
        return clamp((price - entry) / abs(tp1 - entry), 0.0, 1.0)
    if direction == DIRECTION_SHORT:
        return clamp((entry - price) / abs(entry - tp1), 0.0, 1.0)
    return 0.0


def progress_tp1_to_tp2(ctx: PositionContext) -> float:
    tp1 = safe_float(ctx.tp1)
    tp2 = safe_float(ctx.tp2)
    price = safe_float(ctx.current_price)
    direction = normalize_direction(ctx.direction)

    if tp1 <= 0 or tp2 <= 0 or price <= 0 or abs(tp2 - tp1) <= 0:
        return 0.0

    if direction == DIRECTION_LONG:
        return clamp((price - tp1) / abs(tp2 - tp1), 0.0, 1.0)
    if direction == DIRECTION_SHORT:
        return clamp((tp1 - price) / abs(tp1 - tp2), 0.0, 1.0)
    return 0.0


def protected_sl_price(ctx: PositionContext) -> float:
    direction = normalize_direction(ctx.direction)
    entry = safe_float(ctx.entry)
    price = safe_float(ctx.current_price)

    if entry <= 0 or price <= 0:
        return 0.0

    if ctx.tp1_hit and ctx.tp1 > 0:
        tp1 = safe_float(ctx.tp1)
        buffer_dist = abs(tp1 - entry) * 0.10
        if direction == DIRECTION_LONG:
            return max(entry, tp1 - buffer_dist)
        if direction == DIRECTION_SHORT:
            return min(entry, tp1 + buffer_dist)

    # Before TP1 only protect lightly if already in profit.
    if direction == DIRECTION_LONG:
        return max(entry, entry + max(0.0, price - entry) * 0.15)
    if direction == DIRECTION_SHORT:
        return min(entry, entry - max(0.0, entry - price) * 0.15)
    return entry


def sensor_bool(snapshot: SensorSnapshot, name: str, default: bool = False) -> bool:
    return bool(getattr(snapshot, name, default))


def sensor_float(snapshot: SensorSnapshot, name: str, default: float = 0.0) -> float:
    return safe_float(getattr(snapshot, name, default), default)


class ExitSignalScorer:
    def score(self, ctx: PositionContext, snapshot: SensorSnapshot) -> Tuple[ExitScore, List[str], List[str]]:
        reasons: List[str] = []
        warnings: List[str] = []

        direction = normalize_direction(ctx.direction)
        current_pnl = pnl_percent(direction, ctx.entry, ctx.current_price)
        tp1_progress = progress_to_tp1(ctx)
        tp2_progress = progress_tp1_to_tp2(ctx)

        profit_score = 0.0
        if ctx.tp1_hit and current_pnl > 0:
            profit_score = 75.0
            reasons.append("TP1_HIT_PROFIT_PROTECTION_ACTIVE")
        elif current_pnl > 0 and tp1_progress >= AI_EXIT_MIN_TP1_PROGRESS:
            profit_score = 55.0
            reasons.append("PROFIT_70_PERCENT_TO_TP1_REACHED")
        elif current_pnl > 0:
            profit_score = 20.0
            reasons.append("OPEN_PROFIT_BUT_BELOW_70_PERCENT_TP1")

        weakness = 0.0
        if sensor_bool(snapshot, "momentum_weakness", False):
            weakness += 30.0
            reasons.append("MOMENTUM_WEAKNESS")

        rsi_slope = sensor_float(snapshot, "rsi_slope", 0.0)
        hist_slope = sensor_float(snapshot, "histogram_slope", 0.0)
        power_delta = sensor_float(snapshot, "power_delta", 0.0)
        adx_slope = sensor_float(snapshot, "adx_slope", 0.0)

        if direction == DIRECTION_LONG:
            if rsi_slope < -0.20:
                weakness += 18.0
                reasons.append("LONG_RSI_WEAKENING")
            if hist_slope < 0:
                weakness += 18.0
                reasons.append("LONG_HISTOGRAM_WEAKENING")
            if power_delta < -8:
                weakness += 22.0
                reasons.append("LONG_POWER_FLIPPED")
            if adx_slope < -0.10:
                weakness += 10.0
                reasons.append("LONG_ADX_WEAKENING")
        elif direction == DIRECTION_SHORT:
            if rsi_slope > 0.20:
                weakness += 18.0
                reasons.append("SHORT_RSI_WEAKENING")
            if hist_slope > 0:
                weakness += 18.0
                reasons.append("SHORT_HISTOGRAM_WEAKENING")
            if power_delta > 8:
                weakness += 22.0
                reasons.append("SHORT_POWER_FLIPPED")
            if adx_slope < -0.10:
                weakness += 10.0
                reasons.append("SHORT_ADX_WEAKENING")

        reversal = 0.0
        # Optional fields from analysis_layers. Safe if missing.
        reversal += sensor_float(snapshot, "reversal_probability", 0.0) * 0.65
        reversal += sensor_float(snapshot, "range_probability", 0.0) * 0.15

        if sensor_bool(snapshot, "failed_breakout", False) and direction == DIRECTION_LONG:
            reversal += 20.0
            reasons.append("FAILED_BREAKOUT_AGAINST_LONG")
        if sensor_bool(snapshot, "failed_breakdown", False) and direction == DIRECTION_SHORT:
            reversal += 20.0
            reasons.append("FAILED_BREAKDOWN_AGAINST_SHORT")

        invalidation = 0.0
        if sensor_float(snapshot, "range_probability", 0.0) >= 88:
            invalidation += 18.0
            reasons.append("HIGH_RANGE_PROBABILITY_AFTER_ENTRY")
        if sensor_bool(snapshot, "valid", True) is False:
            invalidation += 40.0
            warnings.append("INVALID_SENSOR_SNAPSHOT")

        emergency = 0.0
        atr_percent = abs(sensor_float(snapshot, "atr_percent", 0.0))
        if current_pnl < -max(0.45, atr_percent * 1.8):
            emergency += 70.0
            reasons.append("EMERGENCY_LARGE_ADVERSE_MOVE")
        if not bool(getattr(snapshot, "valid", True)):
            emergency += 40.0
            reasons.append("EMERGENCY_INVALID_SENSOR")

        total = clamp(
            profit_score * 0.22
            + weakness * 0.35
            + reversal * 0.22
            + invalidation * 0.18
            + emergency * 0.35
        )

        return ExitScore(
            tp1_progress=round(tp1_progress, 4),
            tp2_progress=round(tp2_progress, 4),
            profit_score=clamp(profit_score),
            weakness_score=clamp(weakness),
            reversal_score=clamp(reversal),
            invalidation_score=clamp(invalidation),
            emergency_score=clamp(emergency),
            total_exit_score=clamp(total),
        ), reasons, warnings


class ExitActionClassifier:
    def classify(self, ctx: PositionContext, score: ExitScore) -> Tuple[str, bool, bool, List[str], List[str]]:
        reasons: List[str] = []
        warnings: List[str] = []

        current_pnl = pnl_percent(ctx.direction, ctx.entry, ctx.current_price)

        if score.emergency_score >= 70:
            reasons.append("EMERGENCY_EXIT")
            return EXIT_EMERGENCY, True, False, reasons, warnings

        if current_pnl <= 0:
            reasons.append("NO_PROFIT_HOLD_UNLESS_EMERGENCY")
            return EXIT_HOLD, False, False, reasons, warnings

        # LOCKED RULE:
        # Before TP1, AI close only after 70% of the path to TP1.
        if not ctx.tp1_hit:
            if score.tp1_progress < AI_EXIT_MIN_TP1_PROGRESS:
                reasons.append("BELOW_70_PERCENT_TP1_NO_AI_EXIT")
                if score.total_exit_score >= 55:
                    warnings.append("WEAKNESS_SEEN_BUT_WAITING_FOR_70_PERCENT_TP1")
                return EXIT_HOLD, False, False, reasons, warnings

            if (
                score.weakness_score >= 45
                or score.reversal_score >= 50
                or score.invalidation_score >= 42
                or score.total_exit_score >= 62
            ):
                reasons.append("AI_CLOSE_AFTER_70_PERCENT_TP1_WEAKNESS")
                return EXIT_AI_CLOSE, True, False, reasons, warnings

            if score.total_exit_score >= 50:
                reasons.append("PROTECT_PROFIT_AFTER_70_PERCENT_TP1")
                return EXIT_PROTECT_PROFIT, False, True, reasons, warnings

            return EXIT_HOLD, False, False, reasons, warnings

        # After TP1: protect the runner first. Close if weakness is meaningful.
        if ctx.tp1_hit:
            if (
                score.tp2_progress >= 0.20
                and (
                    score.weakness_score >= 35
                    or score.reversal_score >= 45
                    or score.invalidation_score >= 38
                    or score.total_exit_score >= 58
                )
            ):
                reasons.append("AI_CLOSE_RUNNER_WEAKNESS_AFTER_TP1")
                return EXIT_AI_CLOSE, True, False, reasons, warnings

            if score.total_exit_score >= 38:
                reasons.append("PROTECT_AFTER_TP1")
                return EXIT_PROTECT_PROFIT, False, True, reasons, warnings

        return EXIT_HOLD, False, False, reasons, warnings


class ExitConfirmationEngine:
    def __init__(self, required_count: int = 2, confirmation_window_seconds: int = 70):
        self.required_count = max(1, int(required_count))
        self.confirmation_window_seconds = max(10, int(confirmation_window_seconds))
        self._states: Dict[str, ExitConfirmationState] = {}

    def confirm(self, position_id: str, action: str, score: float) -> Tuple[str, bool]:
        if action not in {EXIT_AI_CLOSE, EXIT_EMERGENCY}:
            self._states.pop(position_id, None)
            return CONFIRM_CONFIRMED, action == EXIT_EMERGENCY

        if action == EXIT_EMERGENCY:
            self._states.pop(position_id, None)
            return CONFIRM_CONFIRMED, True

        ts = now_ts()
        state = self._states.get(position_id)

        if state is None or state.action != action or ts - state.first_seen_at > self.confirmation_window_seconds:
            state = ExitConfirmationState(
                position_id=position_id,
                action=action,
                first_seen_at=ts,
                last_seen_at=ts,
                confirmation_count=1,
                last_score=safe_float(score),
            )
            self._states[position_id] = state
        else:
            state.last_seen_at = ts
            state.confirmation_count += 1
            state.last_score = safe_float(score)

        if state.confirmation_count >= self.required_count:
            self._states.pop(position_id, None)
            return CONFIRM_CONFIRMED, True

        return CONFIRM_WAITING, False


class ExitEngine:
    def __init__(self):
        self.scorer = ExitSignalScorer()
        self.classifier = ExitActionClassifier()
        self.confirmation = ExitConfirmationEngine(required_count=2, confirmation_window_seconds=70)

    def evaluate(
        self,
        ctx: PositionContext,
        snapshot: SensorSnapshot,
        decision: Optional[AIDecision] = None,
        plan: Optional[TPSLPlan] = None,
        **_: Any,
    ) -> ExitDecision:
        reasons: List[str] = []
        warnings: List[str] = []

        score, r, w = self.scorer.score(ctx, snapshot)
        reasons.extend(r)
        warnings.extend(w)

        action, wants_close, wants_protect, r, w = self.classifier.classify(ctx, score)
        reasons.extend(r)
        warnings.extend(w)

        confirmation_status, confirmed_close = self.confirmation.confirm(
            position_id=ctx.position_id,
            action=action,
            score=score.total_exit_score,
        )

        if action == EXIT_AI_CLOSE and confirmation_status == CONFIRM_WAITING:
            wants_close = False
            warnings.append("AI_EXIT_WAITING_FOR_CONFIRMATION")

        protected_sl = 0.0
        if wants_protect:
            protected_sl = protected_sl_price(ctx)
            reasons.append("PROTECTED_SL_RECOMMENDED")

        current_pnl = pnl_percent(ctx.direction, ctx.entry, ctx.current_price)
        valid = bool(ctx.entry > 0 and ctx.current_price > 0 and getattr(snapshot, "valid", True))
        if not valid:
            warnings.append("INVALID_EXIT_INPUT")

        return ExitDecision(
            exit_id=f"exit_{uuid4().hex}",
            position_id=ctx.position_id,
            symbol=ctx.symbol,
            direction=normalize_direction(ctx.direction),
            timestamp=now_ts(),
            action=action,
            confirmation_status=confirmation_status,
            should_close=bool(confirmed_close and wants_close),
            should_move_sl_to_protect=bool(wants_protect),
            protected_sl=safe_float(protected_sl),
            exit_price=safe_float(ctx.current_price),
            expected_pnl_percent=safe_float(current_pnl),
            expected_pnl_usdt=safe_float(ctx.unrealized_pnl_usdt),
            score=score,
            reason_codes=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
            valid=valid,
        )


_default_engine: Optional[ExitEngine] = None


def engine() -> ExitEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = ExitEngine()
    return _default_engine


def evaluate_exit(
    ctx: PositionContext,
    snapshot: SensorSnapshot,
    decision: Optional[AIDecision] = None,
    plan: Optional[TPSLPlan] = None,
    **kwargs: Any,
) -> ExitDecision:
    return engine().evaluate(
        ctx=ctx,
        snapshot=snapshot,
        decision=decision,
        plan=plan,
        **kwargs,
    )


def exit_engine(
    ctx: PositionContext,
    snapshot: SensorSnapshot,
    decision: Optional[AIDecision] = None,
    plan: Optional[TPSLPlan] = None,
    **kwargs: Any,
) -> ExitDecision:
    return evaluate_exit(
        ctx=ctx,
        snapshot=snapshot,
        decision=decision,
        plan=plan,
        **kwargs,
    )


def position_context_from_dict(data: Dict[str, Any]) -> PositionContext:
    return PositionContext(
        position_id=str(data.get("position_id", data.get("id", ""))),
        symbol=str(data.get("symbol", "")),
        direction=normalize_direction(str(data.get("direction", ""))),
        entry=safe_float(data.get("entry", data.get("entry_price", 0.0))),
        current_price=safe_float(data.get("current_price", data.get("price", 0.0))),
        tp1=safe_float(data.get("tp1", 0.0)),
        tp2=safe_float(data.get("tp2", 0.0)),
        sl=safe_float(data.get("sl", 0.0)),
        tp1_hit=bool(data.get("tp1_hit", False)),
        tp2_hit=bool(data.get("tp2_hit", False)),
        open_time=int(data.get("open_time", 0) or 0),
        last_update=int(data.get("last_update", now_ts()) or now_ts()),
        highest_price=safe_float(data.get("highest_price", data.get("current_price", 0.0))),
        lowest_price=safe_float(data.get("lowest_price", data.get("current_price", 0.0))),
        unrealized_pnl_percent=safe_float(data.get("unrealized_pnl_percent", 0.0)),
        unrealized_pnl_usdt=safe_float(data.get("unrealized_pnl_usdt", 0.0)),
    )
