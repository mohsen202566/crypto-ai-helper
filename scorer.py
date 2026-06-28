from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["LONG", "SHORT"]
DirectionState = Literal["LONG", "SHORT", "NEUTRAL", "DANGEROUS"]
DecisionAction = Literal["REJECT", "WATCH", "SIGNAL"]
EntryState = Literal["IGNITION_READY", "PRE_WATCH", "LATE", "CHASE", "NO_ENTRY"]
PatternLabel = Literal["IGNITION_START", "PRE_IGNITION_WATCH", "MID_MOVE", "LATE_CHASE", "PULLBACK", "EXHAUSTION", "NOISE"]
SessionState = Literal["GOOD", "NORMAL", "BAD_REAL_ONLY_NORMAL"]
OrderBlockState = Literal["WITH_SIGNAL", "AGAINST_SIGNAL", "NEUTRAL"]


@dataclass(frozen=True)
class ScoreBreakdown:
    score_direction: int = 0
    score_pre_ignition: int = 0
    score_candle_entry: int = 0
    score_ai_memory: int = 0
    score_risk_net: int = 0
    score_session: int = 0
    score_order_block: int = 0

    @property
    def total(self) -> int:
        return int(
            self.score_direction
            + self.score_pre_ignition
            + self.score_candle_entry
            + self.score_ai_memory
            + self.score_risk_net
            + self.score_session
            + self.score_order_block
        )


@dataclass(frozen=True)
class SignalDecision:
    action: DecisionAction
    accepted: bool
    direction: Direction | None
    entry: float
    tp: float
    sl: float
    score: int
    threshold: int
    breakdown: ScoreBreakdown
    reason: str
    hard_reject: bool = False
    reject_code: str | None = None
    ready_alert: bool = False
    hunter: bool = False
    signal_label: str = "عادی"
    direction_state_1h: DirectionState = "NEUTRAL"
    direction_confidence_1h: int = 0
    bias_4h: DirectionState = "NEUTRAL"
    setup_15m: DirectionState = "NEUTRAL"
    entry_5m: EntryState = "NO_ENTRY"
    candle_pattern: PatternLabel = "NOISE"
    entry_stage_pct: float = 100.0
    ai_confidence: int = 0
    ai_experience: int = 0
    ai_adjustment: int = 0
    net_edge: float = 0.0
    estimated_profit_usdt: float = 0.0
    estimated_profit_pct: float = 0.0
    risk_reward: float = 0.0
    estimated_cost_pct: float = 0.0
    market_bias: DirectionState = "NEUTRAL"
    session_state: SessionState = "NORMAL"
    order_block_state: OrderBlockState = "NEUTRAL"
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def real_priority(self) -> float:
        return self.score + self.ai_confidence * 0.35 - self.entry_stage_pct * 0.25 + max(0.0, self.net_edge * 1000.0)


@dataclass(frozen=True)
class EngineResult:
    state: str
    score: int
    confidence: int
    reasons: tuple[str, ...]
