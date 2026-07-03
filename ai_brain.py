from __future__ import annotations

from dataclasses import dataclass

import config
from indicators import build_snapshot
from market_context import analyze as analyze_context
from market_state import classify
from range_learning import RangeLearningBrain
from symbols import MarketSymbol
from target_engine import TargetEngine


@dataclass(frozen=True)
class SignalDecision:
    accepted: bool
    real_allowed: bool
    symbol_name: str
    okx_symbol: str
    toobit_symbol: str
    entry: float
    target: float
    predicted_move_pct: float
    target_distance_pct: float
    estimated_net_profit_usdt: float
    estimated_fee_usdt: float
    expected_hold_minutes: int
    confidence: int
    samples: int
    win_rate: float
    features_key: str
    market_state: str
    alignment: str
    indicator_profile: str
    reason: str
    shadows: tuple[tuple[str, float], ...]


class AIBrain:
    def __init__(self, storage) -> None:
        self.storage = storage
        self.range_brain = RangeLearningBrain()
        self.target_engine = TargetEngine()

    def analyze(self, *, symbol: MarketSymbol, candles_by_tf: dict[str, list], btc_tfs: dict[str, list] | None, eth_tfs: dict[str, list] | None, trade_usdt: float) -> SignalDecision:
        s5 = build_snapshot(candles_by_tf[config.TIMEFRAME_ENTRY])
        s15 = build_snapshot(candles_by_tf[config.TIMEFRAME_CONFIRM])
        s1h = build_snapshot(candles_by_tf[config.TIMEFRAME_1H])
        s4h = build_snapshot(candles_by_tf[config.TIMEFRAME_4H])
        s1d = build_snapshot(candles_by_tf[config.TIMEFRAME_1D])
        btc = self._context_snapshots(btc_tfs)
        eth = self._context_snapshots(eth_tfs)
        context = analyze_context({"1D": s1d, "4H": s4h, "1H": s1h}, btc, eth)
        state = classify(s5, s15)
        features_key = self.range_brain.make_features_key(symbol_name=symbol.name, market_state=state, alignment=context.alignment, snapshot=s5)
        verdict = self.range_brain.verdict(self.storage, features_key)
        indicator_text = f"RSI {s5.rsi14:.1f} | ADX {s5.adx14:.1f} | DI+ {s5.di_plus:.1f}/DI- {s5.di_minus:.1f} | Vol {s5.volume_ratio:.2f} | ATR {s5.atr_pct*100:.2f}% | VWAP {s5.dist_vwap_pct*100:.2f}%"
        reject = self._hard_reject(s5, context, state, verdict)
        if reject:
            return self._no(symbol, s5.close, features_key, state, context.alignment, indicator_text, reject, verdict)
        plan = self.target_engine.build(entry=s5.close, snapshot_5m=s5, profile=self.storage.get_range_profile(features_key), trade_usdt=trade_usdt)
        if not plan.ok:
            return self._no(symbol, s5.close, features_key, state, context.alignment, indicator_text, plan.reason, verdict)
        confidence = self._confidence(s5, context, state, verdict)
        real_allowed = confidence >= config.REAL_MIN_CONFIDENCE and context.real_ok and state not in {"DEAD_MARKET", "CLIMAX_RISK", "NOISY"}
        reason = " | ".join([context.reason, verdict.message, plan.reason])
        return SignalDecision(True, real_allowed, symbol.name, symbol.okx_inst_id, symbol.toobit_symbol, s5.close, plan.target_price, plan.predicted_move_pct, plan.target_distance_pct, plan.estimated_net_profit_usdt, plan.estimated_fee_usdt, plan.expected_hold_minutes, confidence, verdict.samples, verdict.win_rate, features_key, state, context.alignment, indicator_text, reason, plan.shadows)

    @staticmethod
    def _context_snapshots(tfs: dict[str, list] | None) -> dict[str, object] | None:
        if not tfs:
            return None
        return {tf: build_snapshot(candles) for tf, candles in tfs.items() if tf in {config.TIMEFRAME_1D, config.TIMEFRAME_4H, config.TIMEFRAME_1H}}

    @staticmethod
    def _hard_reject(snapshot, context, state: str, verdict) -> str | None:
        if not context.long_ok:
            return "جهت 1D/4H/1H برای خرید اسپات مناسب نیست."
        if state in {"DEAD_MARKET", "CLIMAX_RISK", "NOISY"}:
            return f"حالت بازار برای ورود مناسب نیست: {state}"
        if snapshot.rsi14 < 38 or snapshot.rsi14 > 76:
            return f"RSI در بازه مناسب ورود اسپات نیست: {snapshot.rsi14:.1f}"
        if snapshot.adx14 < config.MIN_ADX_HARD_BLOCK:
            return f"ADX خیلی ضعیف است: {snapshot.adx14:.1f}"
        if snapshot.volume_ratio < config.MIN_VOLUME_RATIO_HARD or snapshot.volume_ratio > config.MAX_VOLUME_RATIO_HARD:
            return f"حجم غیرعادی یا ضعیف است: {snapshot.volume_ratio:.2f}"
        if snapshot.atr_pct < config.MIN_ATR_PCT or snapshot.atr_pct > config.MAX_ATR_PCT:
            return f"ATR برای ورود مناسب نیست: {snapshot.atr_pct*100:.2f}%"
        if verdict.samples >= 20 and verdict.net_profit < 0 and verdict.win_rate < 38:
            return "این بازه در حافظه AI سابقه ضعیف دارد."
        return None

    @staticmethod
    def _confidence(snapshot, context, state: str, verdict) -> int:
        value = 0
        value += min(35, verdict.confidence * 0.45)
        value += 18 if context.risk == "LOW" else 10 if context.risk == "MEDIUM" else 0
        value += 12 if state in {"TREND_UP", "PULLBACK_BUY", "BREAKOUT"} else 5 if state == "NORMAL" else 0
        value += 8 if 45 <= snapshot.rsi14 <= 66 else 2
        value += 8 if snapshot.di_plus >= snapshot.di_minus else 0
        value += 8 if 0.7 <= snapshot.volume_ratio <= 3.5 else 2
        value += min(10, verdict.samples / 15)
        return int(max(0, min(100, value)))

    @staticmethod
    def _no(symbol, entry: float, features_key: str, state: str, alignment: str, indicator_text: str, reason: str, verdict) -> SignalDecision:
        return SignalDecision(False, False, symbol.name, symbol.okx_inst_id, symbol.toobit_symbol, entry, 0.0, 0.0, 0.0, 0.0, 0.0, 0, max(0, verdict.confidence), verdict.samples, verdict.win_rate, features_key, state, alignment, indicator_text, reason, ())
