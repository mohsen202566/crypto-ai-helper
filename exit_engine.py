from __future__ import annotations

"""
20 - exit_engine.py

AI profit-protection / exit confirmation engine for the locked Movement Hunter architecture.

Responsibilities:
- Monitor an open position context and decide whether AI exit is recommended.
- Require confirmation before closing profit, to avoid single-tick false exits.
- Protect profit after TP1 and before TP2 when momentum weakens.
- Detect invalidated movement, reversal pressure, trap after entry, range re-entry.
- Return ExitDecision only; actual close is handled by position_monitor.py / real_trade_manager.py.

Strictly forbidden:
- No Toobit close order call.
- No Telegram sending.
- No persistence.
- No REAL/GHOST/REJECT entry decision.
- No Paper mode.
- No Setup flow.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4
import math
import time

from ai_decision_engine import AIDecision
from tp_sl_engine import TPSLPlan
from analysis_layers import SensorSnapshot
from movement_hunter import MovementHunterResult
from trap_engine import TrapResult
from state_engine import StateResult


JsonDict = Dict[str, Any]

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"

EXIT_NONE = "NONE"
EXIT_HOLD = "HOLD"
EXIT_PROTECT_PROFIT = "PROTECT_PROFIT"
EXIT_AI_CLOSE = "AI_CLOSE"
EXIT_EMERGENCY = "EMERGENCY"

CONFIRM_WAITING = "WAITING"
CONFIRM_CONFIRMED = "CONFIRMED"
CONFIRM_REJECTED = "REJECTED"


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
    profit_protection_score: float
    momentum_weakness_score: float
    reversal_score: float
    trap_score: float
    range_score: float
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
    pending_action: str = EXIT_NONE
    first_seen_at: int = 0
    last_seen_at: int = 0
    confirmation_count: int = 0
    last_score: float = 0.0


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
    """0..1 progress from entry toward TP1."""
    entry = safe_float(ctx.entry)
    tp1 = safe_float(ctx.tp1)
    price = safe_float(ctx.current_price)
    direction = normalize_direction(ctx.direction)
    if entry <= 0 or tp1 <= 0 or price <= 0 or abs(tp1 - entry) <= 0:
        return 0.0
    if direction == DIRECTION_LONG:
        return clamp((price - entry) / abs(tp1 - entry) * 100.0, 0.0, 100.0) / 100.0
    return clamp((entry - price) / abs(entry - tp1) * 100.0, 0.0, 100.0) / 100.0


def progress_tp1_to_tp2(ctx: PositionContext) -> float:
    """0..1 progress from TP1 toward TP2 after TP1 hit."""
    tp1 = safe_float(ctx.tp1)
    tp2 = safe_float(ctx.tp2)
    price = safe_float(ctx.current_price)
    direction = normalize_direction(ctx.direction)
    if tp1 <= 0 or tp2 <= 0 or price <= 0 or abs(tp2 - tp1) <= 0:
        return 0.0
    if direction == DIRECTION_LONG:
        return clamp((price - tp1) / abs(tp2 - tp1) * 100.0, 0.0, 100.0) / 100.0
    return clamp((tp1 - price) / abs(tp1 - tp2) * 100.0, 0.0, 100.0) / 100.0


def protected_sl_price(ctx: PositionContext) -> float:
    """
    After TP1, protect at least around break-even/slightly positive.
    Exchange tick-size rounding is handled later by real_trade_manager/tobit_client.
    """
    direction = normalize_direction(ctx.direction)
    entry = safe_float(ctx.entry)
    price = safe_float(ctx.current_price)
    if entry <= 0 or price <= 0:
        return 0.0

    # After TP1, save profit around TP1 for the remaining runner.
    if ctx.tp1_hit and ctx.tp1 > 0:
        if direction == DIRECTION_LONG:
            return max(entry, safe_float(ctx.tp1))
        return min(entry, safe_float(ctx.tp1))

    if direction == DIRECTION_LONG:
        # Before TP1, protect at entry + 15% of current open profit.
        return max(entry, entry + max(0.0, price - entry) * 0.15)
    return min(entry, entry - max(0.0, entry - price) * 0.15)


class ExitSignalScorer:
    """Scores whether the current position should be protected or AI-closed."""

    def score(
        self,
        ctx: PositionContext,
        snapshot: SensorSnapshot,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
    ) -> Tuple[ExitScore, List[str]]:
        direction = normalize_direction(ctx.direction)
        reasons: List[str] = []

        current_pnl = pnl_percent(direction, ctx.entry, ctx.current_price)

        profit_protection = 0.0
        if ctx.tp1_hit and current_pnl > 0:
            profit_protection += 45
            reasons.append("TP1_HIT_PROFIT_PROTECTION_ACTIVE")
        elif current_pnl >= max(0.25, snapshot.atr_percent * 0.55):
            profit_protection += 25
            reasons.append("OPEN_PROFIT_PROTECTION_CANDIDATE")

        momentum_weakness = 0.0
        if snapshot.momentum_weakness:
            momentum_weakness += 35
            reasons.append("MOMENTUM_WEAKNESS")
        if direction == DIRECTION_LONG:
            if snapshot.rsi_slope < -0.25:
                momentum_weakness += 18
                reasons.append("LONG_RSI_SLOPE_WEAKENING")
            if snapshot.histogram_slope < 0:
                momentum_weakness += 18
                reasons.append("LONG_HISTOGRAM_WEAKENING")
            if snapshot.power_delta < -8:
                momentum_weakness += 22
                reasons.append("LONG_POWER_FLIPPED")
        else:
            if snapshot.rsi_slope > 0.25:
                momentum_weakness += 18
                reasons.append("SHORT_RSI_SLOPE_WEAKENING")
            if snapshot.histogram_slope > 0:
                momentum_weakness += 18
                reasons.append("SHORT_HISTOGRAM_WEAKENING")
            if snapshot.power_delta > 8:
                momentum_weakness += 22
                reasons.append("SHORT_POWER_FLIPPED")

        reversal = clamp(state.reversal_probability * 0.75 + movement.reversal_pressure * 0.45)
        if reversal >= 55:
            reasons.append("REVERSAL_PRESSURE")

        trap_score = clamp(trap.trap_risk * 0.70 + trap.liquidity_risk * 0.30)
        if trap_score >= 55:
            reasons.append("TRAP_AFTER_ENTRY")

        range_score = clamp(state.range_probability * 0.65)
        if state.market_state == "RANGE":
            range_score += 20
            reasons.append("STATE_RETURNED_TO_RANGE")
        range_score = clamp(range_score)

        invalidation = 0.0
        if movement.freshness in {"LATE", "DEAD"}:
            invalidation += 25
            reasons.append("MOVEMENT_NO_LONGER_FRESH")
        if movement.continuation_probability < 35:
            invalidation += 25
            reasons.append("CONTINUATION_PROBABILITY_LOW")
        if state.market_state in {"EXHAUSTION", "REVERSAL"}:
            invalidation += 25
            reasons.append("STATE_INVALIDATION")

        emergency = 0.0
        if current_pnl < -abs(snapshot.atr_percent) * 1.8 and trap.trap_risk >= 75:
            emergency += 65
            reasons.append("EMERGENCY_TRAP_LOSS")
        if not snapshot.valid or not movement.valid or not trap.valid or not state.valid:
            emergency += 40
            reasons.append("INVALID_MONITORING_INPUT")

        total = clamp(
            profit_protection * 0.20
            + momentum_weakness * 0.25
            + reversal * 0.20
            + trap_score * 0.14
            + range_score * 0.10
            + invalidation * 0.18
            + emergency * 0.30
        )

        return ExitScore(
            profit_protection_score=clamp(profit_protection),
            momentum_weakness_score=clamp(momentum_weakness),
            reversal_score=clamp(reversal),
            trap_score=clamp(trap_score),
            range_score=clamp(range_score),
            invalidation_score=clamp(invalidation),
            emergency_score=clamp(emergency),
            total_exit_score=total,
        ), reasons


class ExitActionClassifier:
    """Converts scores into HOLD / protect SL / AI close recommendation."""

    def classify(self, ctx: PositionContext, score: ExitScore) -> Tuple[str, bool, bool, List[str]]:
        reasons: List[str] = []
        current_pnl = pnl_percent(ctx.direction, ctx.entry, ctx.current_price)

        if score.emergency_score >= 70:
            reasons.append("EXIT_EMERGENCY_SCORE")
            return EXIT_EMERGENCY, True, False, reasons

        # In profit: save-profit logic.
        # Before TP1: if price has moved meaningfully toward TP1 and AI detects weakness,
        # close in profit with confirmation instead of letting profit disappear.
        if current_pnl > 0:
            tp1_progress = progress_to_tp1(ctx)
            tp2_progress = progress_tp1_to_tp2(ctx)

            if not ctx.tp1_hit and tp1_progress >= 0.45 and (
                score.momentum_weakness_score >= 45
                or score.reversal_score >= 50
                or score.invalidation_score >= 45
                or score.trap_score >= 60
            ):
                reasons.append("AI_CLOSE_PROFIT_WEAKNESS_BEFORE_TP1")
                return EXIT_AI_CLOSE, True, False, reasons

            # After TP1: the runner must be protected at TP1 first.
            # If it travels toward TP2 and weakness appears, close the runner in profit.
            if ctx.tp1_hit:
                if tp2_progress >= 0.20 and (
                    score.momentum_weakness_score >= 35
                    or score.reversal_score >= 45
                    or score.invalidation_score >= 40
                    or score.trap_score >= 55
                ):
                    reasons.append("AI_CLOSE_RUNNER_PROFIT_WEAKNESS_BEFORE_TP2")
                    return EXIT_AI_CLOSE, True, False, reasons
                if score.total_exit_score >= 42:
                    reasons.append("PROTECT_AFTER_TP1")
                    return EXIT_PROTECT_PROFIT, False, True, reasons

            if score.total_exit_score >= 70:
                reasons.append("AI_CLOSE_PROFIT_WEAKNESS")
                return EXIT_AI_CLOSE, True, False, reasons
            if score.total_exit_score >= 45:
                reasons.append("PROTECT_OPEN_PROFIT")
                return EXIT_PROTECT_PROFIT, False, True, reasons

        # In loss: do not randomly AI-close unless emergency; SL handles normal invalidation.
        if score.total_exit_score >= 85 and score.emergency_score >= 50:
            reasons.append("AI_CLOSE_EMERGENCY_ONLY")
            return EXIT_AI_CLOSE, True, False, reasons

        return EXIT_HOLD, False, False, reasons


class ExitConfirmationEngine:
    """
    Requires repeated confirmation for AI_CLOSE.

    Profit-protection SL move can be recommended immediately.
    Actual execution is done by position_monitor/real_trade_manager.
    """

    def __init__(self, required_count: int = 2, confirmation_window_seconds: int = 70):
        self.required_count = max(1, int(required_count))
        self.confirmation_window_seconds = max(10, int(confirmation_window_seconds))
        self._states: Dict[str, ExitConfirmationState] = {}

    def confirm(self, position_id: str, action: str, score: float) -> Tuple[str, bool]:
        ts = now_ts()
        if action not in {EXIT_AI_CLOSE, EXIT_EMERGENCY}:
            self._states.pop(position_id, None)
            return CONFIRM_CONFIRMED, action == EXIT_EMERGENCY

        state = self._states.get(position_id)
        if state is None or state.pending_action != action or ts - state.first_seen_at > self.confirmation_window_seconds:
            state = ExitConfirmationState(
                position_id=position_id,
                pending_action=action,
                first_seen_at=ts,
                last_seen_at=ts,
                confirmation_count=1,
                last_score=score,
            )
            self._states[position_id] = state
        else:
            state.last_seen_at = ts
            state.confirmation_count += 1
            state.last_score = score

        if action == EXIT_EMERGENCY:
            return CONFIRM_CONFIRMED, True

        if state.confirmation_count >= self.required_count:
            self._states.pop(position_id, None)
            return CONFIRM_CONFIRMED, True

        return CONFIRM_WAITING, False


class ExitEngine:
    """Main AI exit engine."""

    def __init__(self):
        self.scorer = ExitSignalScorer()
        self.classifier = ExitActionClassifier()
        self.confirmation = ExitConfirmationEngine(required_count=2, confirmation_window_seconds=70)

    def evaluate(
        self,
        ctx: PositionContext,
        snapshot: SensorSnapshot,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
        decision: Optional[AIDecision] = None,
        plan: Optional[TPSLPlan] = None,
    ) -> ExitDecision:
        reasons: List[str] = []
        warnings: List[str] = []

        score, r = self.scorer.score(ctx, snapshot, movement, trap, state)
        reasons.extend(r)

        action, wants_close, wants_protect, r = self.classifier.classify(ctx, score)
        reasons.extend(r)

        confirmation_status, confirmed_close = self.confirmation.confirm(ctx.position_id, action, score.total_exit_score)

        if action == EXIT_AI_CLOSE and confirmation_status == CONFIRM_WAITING:
            warnings.append("AI_EXIT_WAITING_FOR_CONFIRMATION")
            wants_close = False

        protected_sl = 0.0
        if wants_protect:
            protected_sl = protected_sl_price(ctx)
            reasons.append("PROTECTED_SL_RECOMMENDED")

        current_pnl = pnl_percent(ctx.direction, ctx.entry, ctx.current_price)

        valid = bool(ctx.entry > 0 and ctx.current_price > 0 and snapshot.valid and movement.valid and trap.valid and state.valid)
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
            protected_sl=protected_sl,
            exit_price=safe_float(ctx.current_price),
            expected_pnl_percent=current_pnl,
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
    movement: MovementHunterResult,
    trap: TrapResult,
    state: StateResult,
    decision: Optional[AIDecision] = None,
    plan: Optional[TPSLPlan] = None,
) -> ExitDecision:
    return engine().evaluate(
        ctx=ctx,
        snapshot=snapshot,
        movement=movement,
        trap=trap,
        state=state,
        decision=decision,
        plan=plan,
    )


def exit_engine(
    ctx: PositionContext,
    snapshot: SensorSnapshot,
    movement: MovementHunterResult,
    trap: TrapResult,
    state: StateResult,
    decision: Optional[AIDecision] = None,
    plan: Optional[TPSLPlan] = None,
) -> ExitDecision:
    return evaluate_exit(
        ctx=ctx,
        snapshot=snapshot,
        movement=movement,
        trap=trap,
        state=state,
        decision=decision,
        plan=plan,
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
