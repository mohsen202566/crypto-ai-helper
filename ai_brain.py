from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from config import TIMEFRAME_1D, TIMEFRAME_1H, TIMEFRAME_4H, TIMEFRAME_ENTRY
from entry_zone import EntryZoneEngine, EntryZoneResult
from indicators import IndicatorSnapshot, calculate_htf_snapshot, calculate_indicators
from market_context import MarketContextEngine, MarketContextResult
from market_state import MarketStateEngine, MarketStateResult
from okx_data import Candle
from range_learning import RangeFeatures, RangeLearningEngine, RangeVerdict
from tp_sl_engine import TpSlEngine

Direction = Literal["LONG", "SHORT"]
DecisionAction = Literal["NO_SIGNAL", "SIGNAL"]


@dataclass(frozen=True)
class AnalysisInput:
    symbol_name: str
    candles_by_tf: dict[str, list[Candle]]
    btc_candles_by_tf: dict[str, list[Candle]] | None = None
    eth_candles_by_tf: dict[str, list[Candle]] | None = None
    # Backward-compatible fields for old callers. They are used only when btc_candles_by_tf is not available.
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
        self.entry_zone_engine = EntryZoneEngine()
        self.range_engine = RangeLearningEngine()
        self.tp_sl_engine = TpSlEngine()

    def analyze(self, data: AnalysisInput) -> SignalDecision:
        snapshots = self._snapshots(data)
        entry_snapshot = snapshots[TIMEFRAME_ENTRY]
        entry = data.live_price if data.live_price and data.live_price > 0 else entry_snapshot.close
        btc_snapshots = self._context_snapshots(data.btc_candles_by_tf, data.btc_1h)
        candidates: list[SignalDecision] = []
        for direction in ("LONG", "SHORT"):
            candidates.append(self._analyze_direction(data.symbol_name, direction, entry, entry_snapshot, snapshots, btc_snapshots))
        accepted = [item for item in candidates if item.accepted]
        if not accepted:
            best = max(candidates, key=lambda item: (item.confidence, item.estimated_net_profit_usdt), default=None)
            if best:
                return best
            return SignalDecision("NO_SIGNAL", False, None, entry, 0.0, 0.0, "none", False, "هیچ جهت معتبری ساخته نشد.")
        accepted.sort(key=lambda item: (item.real_allowed, item.estimated_net_profit_usdt, item.confidence), reverse=True)
        return accepted[0]

    def _analyze_direction(
        self,
        symbol_name: str,
        direction: Direction,
        entry: float,
        entry_snapshot: IndicatorSnapshot,
        snapshots: dict[str, IndicatorSnapshot],
        btc_snapshots: dict[str, IndicatorSnapshot | None],
    ) -> SignalDecision:
        context = self.context_engine.analyze(
            direction,
            snapshots.get(TIMEFRAME_1D),
            snapshots.get(TIMEFRAME_4H),
            snapshots.get(TIMEFRAME_1H),
            btc_snapshots.get(TIMEFRAME_1D),
            btc_snapshots.get(TIMEFRAME_4H),
            btc_snapshots.get(TIMEFRAME_1H),
        )
        state = self.state_engine.analyze(entry_snapshot, direction)
        features = self.range_engine.build_features(symbol_name, direction, entry_snapshot, context, state)
        empty_verdict = RangeVerdict(False, False, 0, 0, 0.0, 0.0, 0.0, 0.72, 1.15, tuple(context.reasons))
        if not context.normal_ok:
            return self._reject(direction, entry, features, empty_verdict, state, context, "Direction Gate رد شد؛ بیت‌کوین/1D/4H/1H جهت را تایید نکردند.")

        entry_zone = self.entry_zone_engine.analyze(entry_snapshot, direction, entry)
        if not entry_zone.ok:
            return self._reject(direction, entry, features, empty_verdict, state, context, entry_zone.reason)

        verdict = self.range_engine.evaluate(self.storage, features, entry_snapshot, context)
        if not verdict.normal_allowed:
            return self._reject(direction, entry, features, verdict, state, context, "بازه/کانتکست برای سیگنال Normal هم مجاز نیست.")
        margin = self.storage.margin_usdt()
        leverage = self.storage.leverage()
        plan = self.tp_sl_engine.build(direction=direction, entry=entry, snapshot=entry_snapshot, verdict=verdict, margin_usdt=margin, leverage=leverage)
        if not plan.ok:
            return self._reject(direction, entry, features, verdict, state, context, plan.reason)
        indicator_profile = self._indicator_profile(entry_snapshot, entry_zone)
        real_allowed = bool(verdict.real_allowed and context.real_ok)
        reason = " | ".join(tuple(context.reasons) + (entry_zone.reason,) + tuple(state.reasons) + tuple(verdict.reasons) + (plan.reason,))
        return SignalDecision(
            action="SIGNAL", accepted=True, direction=direction, entry=entry, tp=plan.tp, sl=plan.sl,
            signal_type_hint="real" if real_allowed else "normal", real_allowed=real_allowed, reason=reason,
            features_key=features.key, confidence=verdict.confidence, samples=verdict.samples, win_rate=verdict.win_rate,
            predicted_move_pct=plan.predicted_move_pct, tp_distance_pct=plan.tp_distance_pct, sl_distance_pct=plan.sl_distance_pct,
            risk_reward=plan.risk_reward, estimated_net_profit_usdt=plan.estimated_net_profit_usdt, estimated_cost_pct=plan.estimated_cost_pct,
            market_state=state.state, alignment=context.alignment, indicator_profile=indicator_profile,
            notes=tuple(context.reasons) + (entry_zone.reason,) + tuple(state.reasons) + tuple(verdict.reasons),
            shadow_plans=tuple((p.name, p.tp, p.sl) for p in plan.shadow_plans),
            rsi=entry_snapshot.rsi, adx=entry_snapshot.adx, atr_pct=entry_snapshot.atr_pct, volume_ratio=entry_snapshot.volume_ratio,
        )

    def _reject(self, direction: Direction, entry: float, features: RangeFeatures, verdict: RangeVerdict, state: MarketStateResult, context: MarketContextResult, reason: str) -> SignalDecision:
        return SignalDecision(
            action="NO_SIGNAL", accepted=False, direction=direction, entry=entry, tp=0.0, sl=0.0, signal_type_hint="none", real_allowed=False,
            reason=reason, features_key=features.key, confidence=verdict.confidence, samples=verdict.samples, win_rate=verdict.win_rate,
            predicted_move_pct=verdict.predicted_move_pct, market_state=state.state, alignment=context.alignment,
        )

    def _snapshots(self, data: AnalysisInput) -> dict[str, IndicatorSnapshot]:
        required = tuple(dict.fromkeys((TIMEFRAME_ENTRY, TIMEFRAME_1H, TIMEFRAME_4H, TIMEFRAME_1D)))
        out: dict[str, IndicatorSnapshot] = {}
        for tf in required:
            candles = data.candles_by_tf.get(tf)
            if not candles:
                raise RuntimeError(f"کندل تایم {tf} برای {data.symbol_name} وجود ندارد.")
            out[tf] = calculate_indicators(candles) if tf == TIMEFRAME_ENTRY else calculate_htf_snapshot(candles)
        return out

    def _context_snapshots(self, candles_by_tf: dict[str, list[Candle]] | None, legacy_1h: list[Candle] | None) -> dict[str, IndicatorSnapshot | None]:
        out: dict[str, IndicatorSnapshot | None] = {TIMEFRAME_1D: None, TIMEFRAME_4H: None, TIMEFRAME_1H: None}
        if candles_by_tf:
            for tf in (TIMEFRAME_1D, TIMEFRAME_4H, TIMEFRAME_1H):
                out[tf] = self._safe_snapshot(candles_by_tf.get(tf))
        elif legacy_1h:
            out[TIMEFRAME_1H] = self._safe_snapshot(legacy_1h)
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
    def _indicator_profile(s: IndicatorSnapshot, entry_zone: EntryZoneResult | None = None) -> str:
        base = f"TF 1H | RSI {s.rsi:.1f} | ADX {s.adx:.1f} | DI+ {s.plus_di:.1f} / DI- {s.minus_di:.1f} | ATR {s.atr_pct*100:.3f}% | Vol {s.volume_ratio:.2f} | VWAP {s.price_vs_vwap_pct*100:.3f}% | EMA20/50 {s.ema20_50_gap_pct*100:.3f}%"
        if entry_zone:
            base += f" | EntryZone {entry_zone.status} pos {entry_zone.range_position*100:.1f}%"
        return base
