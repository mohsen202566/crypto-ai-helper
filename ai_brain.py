from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from config import TIMEFRAME_1D, TIMEFRAME_1H, TIMEFRAME_4H, TIMEFRAME_ENTRY
from indicators import IndicatorSnapshot, calculate_htf_snapshot, calculate_indicators
from market_context import MarketContextEngine, MarketContextResult
from market_state import MarketStateEngine, MarketStateResult
from okx_data import Candle
from range_learning import RangeFeatures, RangeLearningEngine, RangeVerdict
from tp_sl_engine import TpSlEngine, TpSlPlan

Direction = Literal["LONG", "SHORT"]
DecisionAction = Literal["NO_SIGNAL", "SIGNAL"]


@dataclass(frozen=True)
class AnalysisInput:
    symbol_name: str
    candles_by_tf: dict[str, list[Candle]]
    btc_1h: list[Candle] | None = None
    eth_1h: list[Candle] | None = None
    live_price: float | None = None


@dataclass(frozen=True)
class SignalDecision:
    action: DecisionAction
    accepted: bool
    direction: Direction | None
    entry: float
    tp: float
    sl: float
    signal_type_hint: str
    real_allowed: bool
    reason: str
    features_key: str = ""
    confidence: int = 0
    samples: int = 0
    win_rate: float = 0.0
    predicted_move_pct: float = 0.0
    tp_distance_pct: float = 0.0
    sl_distance_pct: float = 0.0
    risk_reward: float = 0.0
    estimated_net_profit_usdt: float = 0.0
    estimated_cost_pct: float = 0.0
    market_state: str = ""
    alignment: str = ""
    indicator_profile: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)
    shadow_plans: tuple[tuple[str, float, float], ...] = field(default_factory=tuple)
    rsi: float = 0.0
    adx: float = 0.0
    atr_pct: float = 0.0
    volume_ratio: float = 0.0


class AIBrain:
    def __init__(self, storage) -> None:
        self.storage = storage
        self.context_engine = MarketContextEngine()
        self.state_engine = MarketStateEngine()
        self.range_engine = RangeLearningEngine()
        self.tp_sl_engine = TpSlEngine()

    def analyze(self, data: AnalysisInput) -> SignalDecision:
        snapshots = self._snapshots(data)
        entry_snapshot = snapshots[TIMEFRAME_ENTRY]
        entry = data.live_price if data.live_price and data.live_price > 0 else entry_snapshot.close
        candidates: list[SignalDecision] = []
        for direction in ("LONG", "SHORT"):
            candidates.append(self._analyze_direction(data.symbol_name, direction, entry, entry_snapshot, snapshots, data.btc_1h, data.eth_1h))
        accepted = [item for item in candidates if item.accepted]
        if not accepted:
            best = max(candidates, key=lambda item: item.estimated_net_profit_usdt, default=None)
            if best:
                return best
            return SignalDecision("NO_SIGNAL", False, None, entry, 0.0, 0.0, "none", False, "هیچ جهت معتبری ساخته نشد.")
        accepted.sort(key=lambda item: (item.real_allowed, item.estimated_net_profit_usdt, item.confidence), reverse=True)
        return accepted[0]

    def _analyze_direction(self, symbol_name: str, direction: Direction, entry: float, s5: IndicatorSnapshot, snapshots: dict[str, IndicatorSnapshot], btc_1h: list[Candle] | None, eth_1h: list[Candle] | None) -> SignalDecision:
        btc_snapshot = self._safe_snapshot(btc_1h)
        eth_snapshot = self._safe_snapshot(eth_1h)
        context = self.context_engine.analyze(direction, snapshots.get(TIMEFRAME_1D), snapshots.get(TIMEFRAME_4H), snapshots.get(TIMEFRAME_1H), btc_snapshot, eth_snapshot)
        state = self.state_engine.analyze(s5, direction)
        features = self.range_engine.build_features(symbol_name, direction, s5, context, state)
        verdict = self.range_engine.evaluate(self.storage, features, s5, context)
        if not verdict.normal_allowed:
            return self._reject(direction, entry, features, verdict, state, context, "بازه/کانتکست برای سیگنال نرم هم مجاز نیست.")
        margin = self.storage.margin_usdt()
        leverage = self.storage.leverage()
        plan = self.tp_sl_engine.build(direction=direction, entry=entry, snapshot=s5, verdict=verdict, margin_usdt=margin, leverage=leverage)
        if not plan.ok:
            return self._reject(direction, entry, features, verdict, state, context, plan.reason)
        indicator_profile = self._indicator_profile(s5)
        real_allowed = bool(verdict.real_allowed and context.real_ok)
        reason = " | ".join(tuple(context.reasons) + tuple(state.reasons) + tuple(verdict.reasons) + (plan.reason,))
        return SignalDecision(
            action="SIGNAL", accepted=True, direction=direction, entry=entry, tp=plan.tp, sl=plan.sl,
            signal_type_hint="real" if real_allowed else "normal", real_allowed=real_allowed, reason=reason,
            features_key=features.key, confidence=verdict.confidence, samples=verdict.samples, win_rate=verdict.win_rate,
            predicted_move_pct=plan.predicted_move_pct, tp_distance_pct=plan.tp_distance_pct, sl_distance_pct=plan.sl_distance_pct,
            risk_reward=plan.risk_reward, estimated_net_profit_usdt=plan.estimated_net_profit_usdt, estimated_cost_pct=plan.estimated_cost_pct,
            market_state=state.state, alignment=context.alignment, indicator_profile=indicator_profile,
            notes=tuple(context.reasons) + tuple(state.reasons) + tuple(verdict.reasons),
            shadow_plans=tuple((p.name, p.tp, p.sl) for p in plan.shadow_plans),
            rsi=s5.rsi, adx=s5.adx, atr_pct=s5.atr_pct, volume_ratio=s5.volume_ratio,
        )

    def _reject(self, direction: Direction, entry: float, features: RangeFeatures, verdict: RangeVerdict, state: MarketStateResult, context: MarketContextResult, reason: str) -> SignalDecision:
        return SignalDecision(
            action="NO_SIGNAL", accepted=False, direction=direction, entry=entry, tp=0.0, sl=0.0, signal_type_hint="none", real_allowed=False,
            reason=reason, features_key=features.key, confidence=verdict.confidence, samples=verdict.samples, win_rate=verdict.win_rate,
            predicted_move_pct=verdict.predicted_move_pct, market_state=state.state, alignment=context.alignment,
        )

    def _snapshots(self, data: AnalysisInput) -> dict[str, IndicatorSnapshot]:
        required = (TIMEFRAME_ENTRY, TIMEFRAME_1H, TIMEFRAME_4H, TIMEFRAME_1D)
        out: dict[str, IndicatorSnapshot] = {}
        for tf in required:
            candles = data.candles_by_tf.get(tf)
            if not candles:
                raise RuntimeError(f"کندل تایم {tf} برای {data.symbol_name} وجود ندارد.")
            out[tf] = calculate_indicators(candles) if tf == TIMEFRAME_ENTRY else calculate_htf_snapshot(candles)
        return out

    @staticmethod
    def _safe_snapshot(candles: list[Candle] | None) -> IndicatorSnapshot | None:
        if not candles:
            return None
        try:
            return calculate_htf_snapshot(candles)
        except Exception:
            return None

    @staticmethod
    def _indicator_profile(s: IndicatorSnapshot) -> str:
        return f"RSI {s.rsi:.1f} | ADX {s.adx:.1f} | DI+ {s.plus_di:.1f} / DI- {s.minus_di:.1f} | ATR {s.atr_pct*100:.3f}% | Vol {s.volume_ratio:.2f} | VWAP {s.price_vs_vwap_pct*100:.3f}% | EMA20/50 {s.ema20_50_gap_pct*100:.3f}%"
