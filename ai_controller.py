from __future__ import annotations

from dataclasses import dataclass

from adaptive_tp_sl_engine import AdaptiveTpSlEngine
from candle_hunter_engine import CandleHunterEngine
from config import SIGNAL_THRESHOLD, TIMEFRAME_15M, TIMEFRAME_1H, TIMEFRAME_4H, TIMEFRAME_5M, WATCH_THRESHOLD, WEIGHTS
from cost_engine import CostEngine
from direction_engine import DirectionEngine
from entry_stage_engine import EntryStageEngine
from indicators import IndicatorSnapshot, calculate_indicators
from ignition_entry_engine import IgnitionEntryEngine
from learning_engine import LearningEngine
from levels_engine import LevelsEngine
from market_context import MarketContextEngine
from okx_data import Candle
from order_block_engine import OrderBlockEngine
from pre_ignition_engine import PreIgnitionEngine
from scorer import Direction, ScoreBreakdown, SignalDecision
from session_engine import SessionEngine
from storage import Storage


@dataclass(frozen=True)
class AnalysisInput:
    symbol_name: str
    candles_by_tf: dict[str, list[Candle]]
    btc_1h: list[Candle] | None = None
    eth_1h: list[Candle] | None = None
    watch_mode: bool = False


class AIController:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.direction_engine = DirectionEngine()
        self.pre_ignition = PreIgnitionEngine()
        self.candle_hunter = CandleHunterEngine()
        self.entry_stage = EntryStageEngine()
        self.ignition = IgnitionEntryEngine()
        self.levels_engine = LevelsEngine()
        self.tp_sl = AdaptiveTpSlEngine()
        self.cost_engine = CostEngine()
        self.market_engine = MarketContextEngine()
        self.order_block_engine = OrderBlockEngine()
        self.session_engine = SessionEngine()
        self.learning_engine = LearningEngine()

    def analyze(self, data: AnalysisInput) -> SignalDecision:
        snapshots = {tf: calculate_indicators(candles) for tf, candles in data.candles_by_tf.items()}
        s4h = snapshots[TIMEFRAME_4H]
        s1h = snapshots[TIMEFRAME_1H]
        s15 = snapshots[TIMEFRAME_15M]
        s5 = snapshots[TIMEFRAME_5M]
        entry = s5.close
        dir1h = self.direction_engine.analyze_1h(s1h)
        if dir1h.state not in ("LONG", "SHORT"):
            return self._reject(entry=entry, breakdown=ScoreBreakdown(score_direction=dir1h.score), reason="1H خنثی است؛ real و سیگنال رد شد.", code="1H_NEUTRAL", direction_state_1h=dir1h.state, direction_confidence_1h=dir1h.confidence, notes=dir1h.reasons)
        direction: Direction = "LONG" if dir1h.state == "LONG" else "SHORT"
        bias4h = self.direction_engine.analyze_4h_bias(s4h, direction)
        pre = self.pre_ignition.analyze(s15, s5, direction)
        candle = self.candle_hunter.analyze(data.candles_by_tf[TIMEFRAME_5M], direction)
        stage = self.entry_stage.analyze(s5, direction)
        ignition = self.ignition.analyze(candle, stage)
        levels = self.levels_engine.detect(data.candles_by_tf[TIMEFRAME_15M], entry)
        memory = self.learning_engine.analyze(self.storage, data.symbol_name, direction, candle.label, s5.rsi, s15.adx, s15.volume_ratio)
        risk = self.tp_sl.build(direction=direction, entry=entry, snapshot_15m=s15, levels=levels, learned_expected_pct=memory.expected_move_pct)
        cost = self.cost_engine.evaluate(direction=direction, entry=entry, tp=risk.tp, margin_usdt=self.storage.margin_usdt(), leverage=self.storage.leverage(), min_profit_usdt=self.storage.min_profit_usdt(), min_profit_pct=self.storage.min_profit_pct())
        btc_snapshot = self._safe_snapshot(data.btc_1h)
        eth_snapshot = self._safe_snapshot(data.eth_1h)
        market = self.market_engine.analyze(btc_snapshot, eth_snapshot, direction)
        ob = self.order_block_engine.analyze(data.candles_by_tf[TIMEFRAME_15M], direction, entry, s15.atr)
        session = self.session_engine.analyze(self.storage, data.symbol_name, direction)
        score_risk = min(WEIGHTS.risk_net, risk.score + cost.score_bonus)
        score_order_block = min(WEIGHTS.order_block, bias4h.score + ob.score)
        score_candle = min(WEIGHTS.candle_entry, ignition.score)
        breakdown = ScoreBreakdown(
            score_direction=dir1h.score,
            score_pre_ignition=pre.score,
            score_candle_entry=score_candle,
            score_ai_memory=memory.score,
            score_risk_net=score_risk,
            score_session=session.score,
            score_order_block=score_order_block,
        )
        total = max(0, min(100, breakdown.total))
        notes = dir1h.reasons + bias4h.reasons + pre.reasons + candle.reasons + stage.reasons + ignition.reasons + risk.reasons + cost.reasons + market.reasons + ob.reasons + session.reasons + memory.reasons
        if ignition.state == "LATE":
            return self._reject(entry=entry, tp=risk.tp, sl=risk.sl, breakdown=breakdown, reason="ورود دیر/وسط/آخر حرکت تشخیص داده شد.", code="LATE_OR_CHASE", direction=direction, hard=True, direction_state_1h=dir1h.state, direction_confidence_1h=dir1h.confidence, bias_4h=bias4h.state, setup_15m=pre.state, entry_5m=ignition.state, candle_pattern=candle.label, entry_stage_pct=stage.stage_pct, ai_confidence=memory.confidence, ai_experience=memory.experience, net_edge=cost.net_edge, estimated_profit_usdt=cost.estimated_profit_usdt, estimated_profit_pct=cost.estimated_profit_pct, risk_reward=risk.risk_reward, estimated_cost_pct=cost.estimated_cost_pct, session_state=session.state, order_block_state=ob.state, notes=notes)
        if not risk.ok or not cost.ok:
            return self._reject(entry=entry, tp=risk.tp, sl=risk.sl, breakdown=breakdown, reason="TP/SL، Net Edge، حداقل سود دلاری یا درصد سود قابل قبول نیست.", code="RISK_OR_PROFIT", direction=direction, hard=True, direction_state_1h=dir1h.state, direction_confidence_1h=dir1h.confidence, bias_4h=bias4h.state, setup_15m=pre.state, entry_5m=ignition.state, candle_pattern=candle.label, entry_stage_pct=stage.stage_pct, ai_confidence=memory.confidence, ai_experience=memory.experience, net_edge=cost.net_edge, estimated_profit_usdt=cost.estimated_profit_usdt, estimated_profit_pct=cost.estimated_profit_pct, risk_reward=risk.risk_reward, estimated_cost_pct=cost.estimated_cost_pct, session_state=session.state, order_block_state=ob.state, notes=notes)
        if session.state == "BAD_REAL_ONLY_NORMAL":
            notes = notes + ("ساعت بد است؛ فقط عادی مجاز است.",)
        ready_alert = ignition.state == "PRE_WATCH" and total >= WATCH_THRESHOLD - 3
        if ignition.state == "PRE_WATCH" or (pre.score >= 12 and total >= WATCH_THRESHOLD and ignition.state != "IGNITION_READY"):
            return SignalDecision(
                action="WATCH", accepted=False, direction=direction, entry=entry, tp=risk.tp, sl=risk.sl, score=total, threshold=SIGNAL_THRESHOLD,
                breakdown=breakdown, reason="شکارگاه آماده است ولی کندل شروع کامل نشده.", ready_alert=ready_alert, hunter=True, signal_label="شکار",
                direction_state_1h=dir1h.state, direction_confidence_1h=dir1h.confidence, bias_4h=bias4h.state, setup_15m=pre.state,
                entry_5m=ignition.state, candle_pattern=candle.label, entry_stage_pct=stage.stage_pct, ai_confidence=memory.confidence, ai_experience=memory.experience,
                ai_adjustment=memory.adjustment, net_edge=cost.net_edge, estimated_profit_usdt=cost.estimated_profit_usdt, estimated_profit_pct=cost.estimated_profit_pct,
                risk_reward=risk.risk_reward, estimated_cost_pct=cost.estimated_cost_pct, market_bias=market.bias, session_state=session.state,
                order_block_state=ob.state, notes=notes,
            )
        accepted = ignition.state == "IGNITION_READY" and total >= SIGNAL_THRESHOLD
        return SignalDecision(
            action="SIGNAL" if accepted else "REJECT", accepted=accepted, direction=direction, entry=entry, tp=risk.tp, sl=risk.sl, score=total,
            threshold=SIGNAL_THRESHOLD, breakdown=breakdown, reason="سیگنال شکار معتبر است." if accepted else "امتیاز یا کندل شکار به حد نهایی نرسید.",
            reject_code=None if accepted else "LOW_SCORE_OR_NO_IGNITION", hunter=candle.label == "IGNITION_START", signal_label="شکار" if candle.label == "IGNITION_START" else "عادی",
            direction_state_1h=dir1h.state, direction_confidence_1h=dir1h.confidence, bias_4h=bias4h.state, setup_15m=pre.state,
            entry_5m=ignition.state, candle_pattern=candle.label, entry_stage_pct=stage.stage_pct, ai_confidence=memory.confidence, ai_experience=memory.experience,
            ai_adjustment=memory.adjustment, net_edge=cost.net_edge, estimated_profit_usdt=cost.estimated_profit_usdt, estimated_profit_pct=cost.estimated_profit_pct,
            risk_reward=risk.risk_reward, estimated_cost_pct=cost.estimated_cost_pct, market_bias=market.bias, session_state=session.state, order_block_state=ob.state, notes=notes,
        )

    def _safe_snapshot(self, candles: list[Candle] | None) -> IndicatorSnapshot | None:
        if not candles:
            return None
        try:
            return calculate_indicators(candles)
        except Exception:
            return None

    def _reject(self, *, entry: float, breakdown: ScoreBreakdown, reason: str, code: str, direction: Direction | None = None, tp: float = 0.0, sl: float = 0.0, hard: bool = False, direction_state_1h="NEUTRAL", direction_confidence_1h: int = 0, bias_4h="NEUTRAL", setup_15m="NEUTRAL", entry_5m="NO_ENTRY", candle_pattern="NOISE", entry_stage_pct: float = 100.0, ai_confidence: int = 0, ai_experience: int = 0, net_edge: float = 0.0, estimated_profit_usdt: float = 0.0, estimated_profit_pct: float = 0.0, risk_reward: float = 0.0, estimated_cost_pct: float = 0.0, session_state="NORMAL", order_block_state="NEUTRAL", notes: tuple[str, ...] = ()) -> SignalDecision:
        return SignalDecision(action="REJECT", accepted=False, direction=direction, entry=entry, tp=tp, sl=sl, score=max(0, min(100, breakdown.total)), threshold=SIGNAL_THRESHOLD, breakdown=breakdown, reason=reason, hard_reject=hard, reject_code=code, direction_state_1h=direction_state_1h, direction_confidence_1h=direction_confidence_1h, bias_4h=bias_4h, setup_15m=setup_15m, entry_5m=entry_5m, candle_pattern=candle_pattern, entry_stage_pct=entry_stage_pct, ai_confidence=ai_confidence, ai_experience=ai_experience, net_edge=net_edge, estimated_profit_usdt=estimated_profit_usdt, estimated_profit_pct=estimated_profit_pct, risk_reward=risk_reward, estimated_cost_pct=estimated_cost_pct, session_state=session_state, order_block_state=order_block_state, notes=notes)
