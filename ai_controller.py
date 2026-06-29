from __future__ import annotations

from dataclasses import dataclass

from adaptive_tp_sl_engine import AdaptiveTpSlEngine
from ai_market_mode import MarketModeBrain
from ai_meta_brain import AIMetaBrain
from ai_sensitivity_engine import AISensitivityEngine
from ai_threshold_engine import AIThresholdEngine
from candle_hunter_engine import CandleHunterEngine
from config import BASE_SIGNAL_THRESHOLD, MARKET_CONTEXT_SYMBOLS, TIMEFRAME_15M, TIMEFRAME_1H, TIMEFRAME_30M, TIMEFRAME_4H, WEIGHTS
from cost_engine import CostEngine
from direction_engine import DirectionEngine
from entry_precision_engine import EntryPrecisionEngine
from entry_quality import EntryQualityEngine
from ignition_entry_engine import IgnitionEntryEngine
from indicators import IndicatorSnapshot, calculate_indicators
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
    live_price: float | None = None


class AIController:
    """Crypto AI Helper 1H controller.

    Same soft-AI learning architecture as the 5m bot, retuned to 1H:
    4H context, 1H main setup, 30m entry timing, 15m micro confirmation.
    """

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.direction_engine = DirectionEngine()
        self.pre_ignition = PreIgnitionEngine()
        self.candle_hunter = CandleHunterEngine()
        self.entry_precision = EntryPrecisionEngine()
        self.ignition = IgnitionEntryEngine()
        self.entry_quality_engine = EntryQualityEngine()
        self.levels_engine = LevelsEngine()
        self.tp_sl = AdaptiveTpSlEngine()
        self.cost_engine = CostEngine()
        self.market_context = MarketContextEngine()
        self.market_mode = MarketModeBrain()
        self.order_block_engine = OrderBlockEngine()
        self.session_engine = SessionEngine()
        self.learning = LearningEngine()
        self.sensitivity = AISensitivityEngine()
        self.threshold_engine = AIThresholdEngine()
        self.meta = AIMetaBrain()

    def analyze(self, data: AnalysisInput) -> SignalDecision:
        snapshots = {tf: calculate_indicators(candles) for tf, candles in data.candles_by_tf.items()}
        s4h = snapshots[TIMEFRAME_4H]
        s1h = snapshots[TIMEFRAME_1H]
        s30 = snapshots[TIMEFRAME_30M]
        s15 = snapshots[TIMEFRAME_15M]
        entry = float(data.live_price) if data.live_price and data.live_price > 0 else s30.close

        dir1h = self.direction_engine.analyze_1h_scalp(s1h, s30, s4h)
        if dir1h.state not in ("LONG", "SHORT"):
            return self._reject(
                entry=entry,
                breakdown=ScoreBreakdown(score_direction=dir1h.score),
                reason="AI هنوز جهت تمیز 1H را قطعی نکرده؛ فرصت فقط داخلی/Watch می‌ماند.",
                code="DIRECTION_1H_NOT_READY",
                setup_15m=dir1h.state,
                notes=dir1h.reasons,
                rsi_5m=s30.rsi,
                rsi_15m=s1h.rsi,
                macd_hist_5m=s30.macd_hist,
                macd_hist_15m=s1h.macd_hist,
                adx_15m=s1h.adx,
                atr_pct_15m=s1h.atr_pct,
                volume_ratio_5m=s30.volume_ratio,
                volume_ratio_15m=s1h.volume_ratio,
            )
        direction: Direction = "LONG" if dir1h.state == "LONG" else "SHORT"
        context1h = self.direction_engine.analyze_1h_context(s1h, direction)
        bias4h = self.direction_engine.analyze_4h_bias(s4h, direction)
        pre = self.pre_ignition.analyze_1h_setup(s1h, s30, direction)
        candle = self.candle_hunter.analyze(data.candles_by_tf[TIMEFRAME_30M], direction)
        precision = self.entry_precision.analyze(s30, direction)
        ignition = self.ignition.analyze(candle, precision)
        entry_quality = self.entry_quality_engine.analyze(direction=direction, snapshot_5m=s30, snapshot_15m=s1h, candle=candle, precision=precision)
        levels = self.levels_engine.detect(data.candles_by_tf[TIMEFRAME_1H], entry, lookback=120)
        mode = self.market_mode.analyze(s30, s1h)
        pattern = self.learning.analyze_pattern(self.storage, symbol_name=data.symbol_name, direction=direction, entry_quality=entry_quality.quality, candle_pattern=candle.label, market_mode=mode.mode, precision_pct=precision.precision_pct)
        range_ai = self.learning.analyze_range(self.storage, symbol_name=data.symbol_name, direction=direction, snapshot_5m=s30, snapshot_15m=s1h, entry_quality=entry_quality.quality, candle_pattern=candle.label)
        learned_expected_pct = range_ai.expected_move_pct or pattern.expected_move_pct
        learned_mae_pct = range_ai.expected_mae_pct or pattern.expected_mae_pct
        risk = self.tp_sl.build(direction=direction, entry=entry, snapshot_15m=s1h, levels=levels, learned_expected_pct=learned_expected_pct, learned_mae_pct=learned_mae_pct)
        cost = self.cost_engine.evaluate(direction=direction, entry=entry, tp=risk.tp, margin_usdt=self.storage.margin_usdt(), leverage=self.storage.leverage())
        btc_snapshot = self._safe_snapshot(data.btc_1h)
        eth_snapshot = self._safe_snapshot(data.eth_1h)
        market = self.market_context.analyze(btc_snapshot, eth_snapshot, direction)
        ob = self.order_block_engine.analyze(data.candles_by_tf[TIMEFRAME_1H], direction, entry, s1h.atr)
        session = self.session_engine.analyze(self.storage, data.symbol_name, direction)
        sensitivity = self.sensitivity.analyze(self.storage, data.symbol_name, direction)
        thresholds = self.threshold_engine.analyze(self.storage, data.symbol_name, direction)

        score_direction = min(WEIGHTS.direction, dir1h.score + max(0, bias4h.score - 5) // 3)
        score_pre = min(WEIGHTS.pre_ignition, pre.score)
        score_candle = max(0, min(WEIGHTS.candle_entry, ignition.score + entry_quality.score_bonus))
        score_precision = max(0, min(WEIGHTS.entry_precision, precision.score))
        score_ai = max(0, min(WEIGHTS.ai_memory, max(pattern.score, range_ai.score) + sensitivity.score_adjustment))
        score_tp_sl = max(0, min(WEIGHTS.tp_sl, risk.score))
        score_market = max(0, min(WEIGHTS.market_mode, mode.score + market.score + ob.score - 2))
        score_session = min(WEIGHTS.session, session.score)
        score_net = min(WEIGHTS.net_sync, cost.score)
        breakdown = ScoreBreakdown(score_direction=score_direction, score_pre_ignition=score_pre, score_candle_entry=score_candle, score_entry_precision=score_precision, score_ai_memory=score_ai, score_tp_sl=score_tp_sl, score_market_mode=score_market, score_session=score_session, score_net_sync=score_net)
        total = max(0, min(100, breakdown.total))
        verdicts = {pattern.verdict, range_ai.verdict}
        negative = "NEGATIVE" if "NEGATIVE" in verdicts else "NEUTRAL"
        positive = "POSITIVE" if "POSITIVE" in verdicts and negative != "NEGATIVE" else negative
        context_stats = self.storage.adaptive_context_stats(
            symbol_name=data.symbol_name,
            direction=direction,
            entry_quality=entry_quality.quality,
            candle_pattern=candle.label,
            pattern_id=pattern.pattern_id,
            market_mode=mode.mode,
            session_state=session.state,
            score=total,
        )
        meta = self.meta.decide(
            total_score=total,
            signal_threshold=thresholds.signal_threshold,
            real_threshold=thresholds.real_threshold,
            entry_quality=entry_quality.quality,
            risk_ok=risk.ok,
            net_profit_ok=cost.ok,
            range_verdict=range_ai.verdict,
            pattern_verdict=pattern.verdict,
            session_state=session.state,
            market_mode=mode.mode,
            context_stats=context_stats,
        )
        ai_confidence = max(0, min(99, max(pattern.confidence, range_ai.confidence, entry_quality.confidence, dir1h.confidence) + sensitivity.confidence_adjustment))
        ai_experience = max(pattern.experience, range_ai.experience)
        notes = dir1h.reasons + context1h.reasons + bias4h.reasons + pre.reasons + candle.reasons + precision.reasons + ignition.reasons + entry_quality.reasons + risk.reasons + cost.reasons + market.reasons + mode.reasons + ob.reasons + session.reasons + pattern.reasons + range_ai.reasons + sensitivity.reasons + thresholds.reasons + meta.reasons
        common = dict(
            direction=direction,
            direction_state_1h=context1h.state,
            direction_confidence_1h=context1h.confidence,
            bias_4h=bias4h.state,
            setup_15m=dir1h.state,
            entry_5m=ignition.state,
            candle_pattern=candle.label,
            entry_precision_pct=precision.precision_pct,
            entry_quality=entry_quality.quality,
            technical_zone=ob.state,
            indicator_profile=range_ai.profile,
            pattern_id=pattern.pattern_id,
            ai_confidence=ai_confidence,
            ai_experience=ai_experience,
            ai_adjustment=pattern.adjustment + range_ai.adjustment + sensitivity.score_adjustment,
            ai_effect=positive,
            net_edge=cost.net_edge,
            estimated_profit_usdt=cost.estimated_profit_usdt,
            estimated_net_profit_usdt=cost.estimated_net_profit_usdt,
            estimated_profit_pct=cost.estimated_profit_pct,
            risk_reward=risk.risk_reward,
            estimated_cost_pct=cost.estimated_cost_pct,
            market_bias=market.bias,
            market_mode=mode.mode,
            session_state=session.state,
            order_block_state=ob.state,
            rsi_5m=s30.rsi,
            rsi_15m=s1h.rsi,
            macd_hist_5m=s30.macd_hist,
            macd_hist_15m=s1h.macd_hist,
            adx_15m=s1h.adx,
            atr_pct_15m=s1h.atr_pct,
            volume_ratio_5m=s30.volume_ratio,
            volume_ratio_15m=s1h.volume_ratio,
            notes=notes,
        )
        return SignalDecision(action=meta.action, accepted=meta.accepted, entry=entry, tp=risk.tp, sl=risk.sl, score=total, threshold=meta.signal_threshold, signal_threshold=meta.signal_threshold, real_threshold=meta.real_threshold, threshold_source=meta.source, breakdown=breakdown, reason=meta.reason, reject_code=None if meta.accepted else meta.real_block_reason or "SOFT_WATCH", ready_alert=meta.ready_alert, hunter=entry_quality.quality in {"EARLY_IGNITION", "GOOD_ENTRY", "POWER_BUILDING", "REVERSAL_BUILDING", "PRECISION_WAIT"}, signal_label=meta.signal_label, real_allowed=meta.real_allowed, real_block_reason=meta.real_block_reason, **common)

    def _safe_snapshot(self, candles: list[Candle] | None) -> IndicatorSnapshot | None:
        if not candles:
            return None
        try:
            return calculate_indicators(candles)
        except Exception:
            return None

    def _reject(self, *, entry: float, breakdown: ScoreBreakdown, reason: str, code: str, hard: bool = False, **extra) -> SignalDecision:
        action = "REJECT" if hard else "WATCH"
        return SignalDecision(action=action, accepted=False, direction=extra.pop("direction", None), entry=entry, tp=extra.pop("tp", entry), sl=extra.pop("sl", entry), score=max(0, min(100, breakdown.total)), threshold=BASE_SIGNAL_THRESHOLD, signal_threshold=BASE_SIGNAL_THRESHOLD, breakdown=breakdown, reason=reason, hard_reject=hard, reject_code=code, **extra)
