from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["LONG", "SHORT"]
DirectionState = Literal["LONG", "SHORT", "NEUTRAL", "DANGEROUS"]
DecisionAction = Literal["REJECT", "WATCH", "SIGNAL"]
EntryState = Literal[
    "EARLY_IGNITION",
    "GOOD_ENTRY",
    "POWER_BUILDING",
    "REVERSAL_BUILDING",
    "PRECISION_WAIT",
    "WEAK_MOVEMENT",
    "NOISE_RISK",
    "EXHAUSTION_RISK",
    "NO_ENTRY",
]
PatternLabel = Literal[
    "IGNITION_START",
    "POWER_BUILDING",
    "REVERSAL_BUILDING",
    "PRE_IGNITION_WATCH",
    "EXHAUSTION",
    "PULLBACK",
    "NOISE",
]
SessionState = Literal["GOOD", "NORMAL", "BAD_REAL_ONLY_NORMAL"]
OrderBlockState = Literal["WITH_SIGNAL", "AGAINST_SIGNAL", "NEUTRAL"]


@dataclass(frozen=True)
class ScoreBreakdown:
    score_direction: int = 0
    score_pre_ignition: int = 0
    score_candle_entry: int = 0
    score_entry_precision: int = 0
    score_ai_memory: int = 0
    score_tp_sl: int = 0
    score_market_mode: int = 0
    score_session: int = 0
    score_net_sync: int = 0

    @property
    def total(self) -> int:
        return int(
            self.score_direction
            + self.score_pre_ignition
            + self.score_candle_entry
            + self.score_entry_precision
            + self.score_ai_memory
            + self.score_tp_sl
            + self.score_market_mode
            + self.score_session
            + self.score_net_sync
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
    signal_threshold: int = 68
    real_threshold: int = 76
    threshold_source: str = "BOOT"
    hard_reject: bool = False
    reject_code: str | None = None
    ready_alert: bool = False
    hunter: bool = False
    signal_label: str = "عادی"
    real_allowed: bool = False
    real_block_reason: str | None = None
    direction_state_1h: DirectionState = "NEUTRAL"
    direction_confidence_1h: int = 0
    bias_4h: DirectionState = "NEUTRAL"
    setup_15m: DirectionState = "NEUTRAL"
    entry_5m: EntryState = "NO_ENTRY"
    candle_pattern: PatternLabel = "NOISE"
    entry_precision_pct: float = 0.0
    entry_quality: str = "NO_ENTRY"
    technical_zone: str = "NEUTRAL"
    indicator_profile: str = ""
    pattern_id: str = ""
    ai_confidence: int = 0
    ai_experience: int = 0
    ai_adjustment: int = 0
    ai_effect: str = "NEUTRAL"
    net_edge: float = 0.0
    estimated_net_profit_usdt: float = 0.0
    estimated_profit_usdt: float = 0.0
    estimated_profit_pct: float = 0.0
    risk_reward: float = 0.0
    estimated_cost_pct: float = 0.0
    market_bias: DirectionState = "NEUTRAL"
    market_mode: str = "NORMAL"
    session_state: SessionState = "NORMAL"
    order_block_state: OrderBlockState = "NEUTRAL"
    rsi_5m: float = 0.0
    rsi_15m: float = 0.0
    macd_hist_5m: float = 0.0
    macd_hist_15m: float = 0.0
    adx_15m: float = 0.0
    atr_pct_15m: float = 0.0
    volume_ratio_5m: float = 0.0
    volume_ratio_15m: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def real_priority(self) -> float:
        q_bonus = {
            "EARLY_IGNITION": 22,
            "GOOD_ENTRY": 16,
            "REVERSAL_BUILDING": 14,
            "POWER_BUILDING": 12,
        }.get(self.entry_quality, 0)
        profit_bonus = max(0.0, self.estimated_net_profit_usdt) * 4.0
        return self.score + q_bonus + self.ai_confidence * 0.20 + profit_bonus


@dataclass(frozen=True)
class EngineResult:
    state: str
    score: int
    confidence: int
    reasons: tuple[str, ...]
