from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["LONG", "SHORT"]
DirectionState = Literal["LONG", "SHORT", "NEUTRAL"]
EntryState = Literal["READY", "WAIT", "BAD"]


@dataclass(frozen=True)
class ScoreBreakdown:
    score_1h: int = 0
    score_15m: int = 0
    score_5m: int = 0
    score_late: int = 0
    score_risk: int = 0
    score_market: int = 0
    score_4h: int = 0

    @property
    def total(self) -> int:
        return int(
            self.score_1h
            + self.score_15m
            + self.score_5m
            + self.score_late
            + self.score_risk
            + self.score_market
            + self.score_4h
        )


@dataclass(frozen=True)
class SignalDecision:
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
    direction_state_1h: DirectionState = "NEUTRAL"
    direction_confidence_1h: int = 0
    bias_4h: DirectionState = "NEUTRAL"
    setup_15m: DirectionState = "NEUTRAL"
    entry_5m: EntryState = "WAIT"
    late_entry_ok: bool = False
    net_edge: float = 0.0
    risk_reward: float = 0.0
    estimated_cost_pct: float = 0.0
    market_bias: DirectionState = "NEUTRAL"
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EngineResult:
    state: str
    score: int
    confidence: int
    reasons: tuple[str, ...]
