from __future__ import annotations

from dataclasses import dataclass

import config
from indicators import IndicatorSnapshot
from utils import net_profit_after_fees


@dataclass(frozen=True)
class TargetPlan:
    ok: bool
    target_price: float
    predicted_move_pct: float
    target_distance_pct: float
    estimated_net_profit_usdt: float
    estimated_fee_usdt: float
    expected_hold_minutes: int
    reason: str
    shadows: tuple[tuple[str, float], ...]


class TargetEngine:
    def build(self, *, entry: float, snapshot_5m: IndicatorSnapshot, profile: dict | None, trade_usdt: float) -> TargetPlan:
        profile = profile or {}
        avg_mfe = float(profile.get("avg_mfe_pct") or 0.0)
        best_target = float(profile.get("best_target_pct") or 0.0)
        atr_move = max(snapshot_5m.atr_pct * 2.2, config.MIN_TARGET_MOVE_PCT)
        momentum_boost = 0.004 if snapshot_5m.volume_ratio >= 1.4 and snapshot_5m.di_plus > snapshot_5m.di_minus else 0.0
        predicted = max(atr_move + momentum_boost, avg_mfe * 0.85, best_target * 1.05, config.MIN_TARGET_MOVE_PCT)
        predicted = min(predicted, config.MAX_TARGET_MOVE_PCT)
        confidence = int(profile.get("confidence") or 0)
        fraction = config.SAFE_TARGET_FRACTION_MIN + (config.SAFE_TARGET_FRACTION_MAX - config.SAFE_TARGET_FRACTION_MIN) * min(confidence, 100) / 100
        target_distance = max(config.MIN_TARGET_MOVE_PCT, predicted * fraction)
        target_price = entry * (1 + target_distance)
        net, fee = net_profit_after_fees(entry, target_price, trade_usdt, config.SPOT_TAKER_FEE_RATE, config.SPOT_TAKER_FEE_RATE)
        if net < config.MIN_NET_PROFIT_USDT:
            return TargetPlan(False, target_price, predicted, target_distance, net, fee, 0, f"سود خالص بعد کارمزد کافی نیست: {net:.4f} USDT", ())
        if target_distance > predicted * 0.95 and confidence < 35:
            return TargetPlan(False, target_price, predicted, target_distance, net, fee, 0, "هدف نسبت به حرکت محتمل زیادی دور است.", ())
        expected_minutes = int(max(20, min(480, 30 + target_distance / max(snapshot_5m.atr_pct, 0.0005) * 25)))
        shadows = (
            ("target_nearer", entry * (1 + target_distance * 0.80)),
            ("target_main", target_price),
            ("target_wider", entry * (1 + min(predicted, target_distance * 1.20))),
        )
        return TargetPlan(True, target_price, predicted, target_distance, net, fee, expected_minutes, "هدف با حرکت محتمل، ATR و سود خالص بعد کارمزد سازگار است.", shadows)
