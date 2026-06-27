from __future__ import annotations

from dataclasses import dataclass

from config import SIGNAL_THRESHOLD, TIMEFRAME_15M, TIMEFRAME_1H, TIMEFRAME_4H, TIMEFRAME_5M, WEIGHTS
from cost_engine import CostEngine
from direction_engine import DirectionEngine
from entry_engine import EntryEngine
from indicators import IndicatorSnapshot, calculate_indicators
from late_entry_guard import LateEntryGuard
from levels_engine import LevelsEngine
from market_context import MarketContextEngine
from okx_data import Candle
from risk_engine import RiskEngine
from scorer import Direction, ScoreBreakdown, SignalDecision
from setup_engine import SetupEngine


@dataclass(frozen=True)
class AnalysisInput:
    symbol_name: str
    candles_by_tf: dict[str, list[Candle]]
    btc_1h: list[Candle] | None = None
    eth_1h: list[Candle] | None = None


class AIController:
    def __init__(self) -> None:
        self.direction_engine = DirectionEngine()
        self.setup_engine = SetupEngine()
        self.entry_engine = EntryEngine()
        self.late_guard = LateEntryGuard()
        self.levels_engine = LevelsEngine()
        self.risk_engine = RiskEngine()
        self.cost_engine = CostEngine()
        self.market_engine = MarketContextEngine()

    def analyze(self, data: AnalysisInput) -> SignalDecision:
        snapshots = {tf: calculate_indicators(candles) for tf, candles in data.candles_by_tf.items()}
        s4h = snapshots[TIMEFRAME_4H]
        s1h = snapshots[TIMEFRAME_1H]
        s15 = snapshots[TIMEFRAME_15M]
        s5 = snapshots[TIMEFRAME_5M]
        entry = s5.close

        dir1h = self.direction_engine.analyze_1h(s1h)
        if dir1h.state == "NEUTRAL":
            return self._reject(
                entry=entry,
                breakdown=ScoreBreakdown(score_1h=dir1h.score),
                reason="1H خنثی است؛ طبق قانون اصلی سیگنال صادر نشد.",
                code="1H_NEUTRAL",
                direction_state_1h=dir1h.state,
                direction_confidence_1h=dir1h.confidence,
                notes=dir1h.reasons,
            )

        direction: Direction = "LONG" if dir1h.state == "LONG" else "SHORT"
        bias4h = self.direction_engine.analyze_4h_bias(s4h, direction)
        setup15 = self.setup_engine.analyze(s15, direction)
        entry5 = self.entry_engine.analyze(s5, direction)
        late = self.late_guard.check(s5, direction)
        if not late.ok:
            breakdown = ScoreBreakdown(
                score_1h=dir1h.score,
                score_15m=setup15.score,
                score_5m=entry5.score,
                score_late=late.score,
                score_4h=bias4h.score,
            )
            return self._reject(
                entry=entry,
                breakdown=breakdown,
                reason="ورود دیر یا آخر حرکت تشخیص داده شد.",
                code="LATE_ENTRY",
                direction=direction,
                hard=True,
                direction_state_1h=dir1h.state,
                direction_confidence_1h=dir1h.confidence,
                bias_4h=bias4h.state,
                setup_15m=setup15.state,
                entry_5m=entry5.state,
                late_entry_ok=False,
                notes=dir1h.reasons + bias4h.reasons + setup15.reasons + entry5.reasons + late.reasons,
            )

        levels = self.levels_engine.detect(data.candles_by_tf[TIMEFRAME_15M], entry)
        risk = self.risk_engine.build_tp_sl(direction=direction, entry=entry, snapshot_15m=s15, levels=levels)
        cost = self.cost_engine.evaluate(direction=direction, entry=entry, tp=risk.tp)
        if not risk.ok or not cost.ok:
            score_risk = min(WEIGHTS.risk_reward_net, risk.score + cost.score_bonus)
            breakdown = ScoreBreakdown(
                score_1h=dir1h.score,
                score_15m=setup15.score,
                score_5m=entry5.score,
                score_late=late.score,
                score_risk=score_risk,
                score_4h=bias4h.score,
            )
            return self._reject(
                entry=entry,
                tp=risk.tp,
                sl=risk.sl,
                breakdown=breakdown,
                reason="TP/SL یا Net Edge بعد از کارمزد قابل قبول نیست.",
                code="RISK_OR_NET_EDGE",
                direction=direction,
                hard=True,
                direction_state_1h=dir1h.state,
                direction_confidence_1h=dir1h.confidence,
                bias_4h=bias4h.state,
                setup_15m=setup15.state,
                entry_5m=entry5.state,
                late_entry_ok=True,
                net_edge=cost.net_edge,
                risk_reward=risk.risk_reward,
                estimated_cost_pct=cost.estimated_cost_pct,
                notes=dir1h.reasons + bias4h.reasons + setup15.reasons + entry5.reasons + late.reasons + risk.reasons + cost.reasons,
            )

        btc_snapshot = self._safe_snapshot(data.btc_1h)
        eth_snapshot = self._safe_snapshot(data.eth_1h)
        market = self.market_engine.analyze(btc_snapshot, eth_snapshot, direction)

        risk_score = min(WEIGHTS.risk_reward_net, risk.score + cost.score_bonus)
        breakdown = ScoreBreakdown(
            score_1h=dir1h.score,
            score_15m=setup15.score,
            score_5m=entry5.score,
            score_late=late.score,
            score_risk=risk_score,
            score_market=market.score,
            score_4h=bias4h.score,
        )
        total = min(100, breakdown.total)
        accepted = total >= SIGNAL_THRESHOLD
        reason = "سیگنال معتبر است." if accepted else "امتیاز به حداقل لازم نرسید."
        notes = dir1h.reasons + bias4h.reasons + setup15.reasons + entry5.reasons + late.reasons + risk.reasons + cost.reasons + market.reasons
        return SignalDecision(
            accepted=accepted,
            direction=direction,
            entry=entry,
            tp=risk.tp,
            sl=risk.sl,
            score=total,
            threshold=SIGNAL_THRESHOLD,
            breakdown=breakdown,
            reason=reason,
            hard_reject=False,
            reject_code=None if accepted else "LOW_SCORE",
            direction_state_1h=dir1h.state,
            direction_confidence_1h=dir1h.confidence,
            bias_4h=bias4h.state,
            setup_15m=setup15.state,
            entry_5m=entry5.state,
            late_entry_ok=True,
            net_edge=cost.net_edge,
            risk_reward=risk.risk_reward,
            estimated_cost_pct=cost.estimated_cost_pct,
            market_bias=market.bias,
            notes=notes,
        )

    def _safe_snapshot(self, candles: list[Candle] | None) -> IndicatorSnapshot | None:
        if not candles:
            return None
        try:
            return calculate_indicators(candles)
        except Exception:
            return None

    def _reject(
        self,
        *,
        entry: float,
        breakdown: ScoreBreakdown,
        reason: str,
        code: str,
        direction: Direction | None = None,
        tp: float = 0.0,
        sl: float = 0.0,
        hard: bool = True,
        direction_state_1h: str = "NEUTRAL",
        direction_confidence_1h: int = 0,
        bias_4h: str = "NEUTRAL",
        setup_15m: str = "NEUTRAL",
        entry_5m: str = "WAIT",
        late_entry_ok: bool = False,
        net_edge: float = 0.0,
        risk_reward: float = 0.0,
        estimated_cost_pct: float = 0.0,
        notes: tuple[str, ...] = (),
    ) -> SignalDecision:
        return SignalDecision(
            accepted=False,
            direction=direction,
            entry=entry,
            tp=tp,
            sl=sl,
            score=min(100, breakdown.total),
            threshold=SIGNAL_THRESHOLD,
            breakdown=breakdown,
            reason=reason,
            hard_reject=hard,
            reject_code=code,
            direction_state_1h=direction_state_1h,  # type: ignore[arg-type]
            direction_confidence_1h=direction_confidence_1h,
            bias_4h=bias_4h,  # type: ignore[arg-type]
            setup_15m=setup_15m,  # type: ignore[arg-type]
            entry_5m=entry_5m,  # type: ignore[arg-type]
            late_entry_ok=late_entry_ok,
            net_edge=net_edge,
            risk_reward=risk_reward,
            estimated_cost_pct=estimated_cost_pct,
            notes=notes,
        )
