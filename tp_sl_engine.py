from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from config import MIN_NET_PROFIT_USDT, MIN_RISK_REWARD, PRICE_TICK_DECIMALS
from indicators import IndicatorSnapshot
from range_learning import RangeVerdict
from utils import net_profit_for_move, required_move_for_min_profit, round_price, total_round_trip_cost_rate

Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class ShadowPlan:
    name: str
    tp: float
    sl: float


@dataclass(frozen=True)
class TpSlPlan:
    ok: bool
    tp: float
    sl: float
    predicted_move_pct: float
    tp_distance_pct: float
    sl_distance_pct: float
    risk_reward: float
    estimated_net_profit_usdt: float
    estimated_cost_pct: float
    reason: str
    shadow_plans: tuple[ShadowPlan, ...] = ()


class TpSlEngine:
    def build(self, *, direction: Direction, entry: float, snapshot: IndicatorSnapshot, verdict: RangeVerdict, margin_usdt: float, leverage: int) -> TpSlPlan:
        if entry <= 0:
            return self._bad("قیمت ورود نامعتبر است.")
        min_profitable = required_move_for_min_profit(margin_usdt, leverage)
        predicted = max(verdict.predicted_move_pct, snapshot.atr_pct * 2.4, min_profitable * 1.25)
        safe_fraction = max(0.62, min(0.84, verdict.safe_tp_fraction))
        tp_distance_pct = max(predicted * safe_fraction, min_profitable, snapshot.atr_pct * 1.15)
        noise_pct = max(snapshot.atr_pct * verdict.sl_atr_mult, snapshot.atr_pct * 0.90, 0.0015)
        if direction == "LONG":
            structure_sl_pct = max(0.0, (entry - min(snapshot.swing_low, snapshot.low)) / entry)
        else:
            structure_sl_pct = max(0.0, (max(snapshot.swing_high, snapshot.high) - entry) / entry)
        sl_distance_pct = max(noise_pct, structure_sl_pct + snapshot.atr_pct * 0.18)
        max_sl_by_rr = tp_distance_pct / MIN_RISK_REWARD
        if sl_distance_pct > max_sl_by_rr:
            compact_sl = max(noise_pct, snapshot.atr_pct * 0.95)
            if compact_sl <= max_sl_by_rr:
                sl_distance_pct = compact_sl
            else:
                return self._bad("SL منطقی برای پشت نویز بیش از حد دور است و RR خراب می‌شود.")
        risk_reward = tp_distance_pct / max(sl_distance_pct, 0.000001)
        if risk_reward < MIN_RISK_REWARD:
            return self._bad("نسبت سود به ضرر برای این شکار حرکت کافی نیست.")
        net_profit = net_profit_for_move(margin_usdt, leverage, tp_distance_pct)
        if net_profit < MIN_NET_PROFIT_USDT:
            return self._bad("TP تحلیلی بعد از کارمزد حداقل سود خالص لازم را نمی‌دهد.")
        if direction == "LONG":
            tp = entry * (1.0 + tp_distance_pct)
            sl = entry * (1.0 - sl_distance_pct)
        else:
            tp = entry * (1.0 - tp_distance_pct)
            sl = entry * (1.0 + sl_distance_pct)
        tp = round_price(tp, PRICE_TICK_DECIMALS)
        sl = round_price(sl, PRICE_TICK_DECIMALS)
        shadows = self._shadow(direction, entry, tp_distance_pct, sl_distance_pct)
        reason = (
            f"TP شکار محتاطانه حرکت {tp_distance_pct*100:.3f}% است؛ "
            f"حداقل اقتصادی {min_profitable*100:.3f}%، سود خالص تخمینی {net_profit:.4f} USDT، "
            f"SL پشت نویز/ساختار {sl_distance_pct*100:.3f}% و RR={risk_reward:.2f}."
        )
        return TpSlPlan(True, tp, sl, predicted, tp_distance_pct, sl_distance_pct, risk_reward, net_profit, total_round_trip_cost_rate(), reason, shadows)

    def _shadow(self, direction: Direction, entry: float, tp_pct: float, sl_pct: float) -> tuple[ShadowPlan, ...]:
        plans = []
        for name, tp_mult, sl_mult in (("tp_safer", 0.80, 1.00), ("tp_wider", 1.18, 1.00), ("sl_tighter", 1.00, 0.82), ("sl_wider", 1.00, 1.18)):
            if direction == "LONG":
                tp = entry * (1.0 + tp_pct * tp_mult)
                sl = entry * (1.0 - sl_pct * sl_mult)
            else:
                tp = entry * (1.0 - tp_pct * tp_mult)
                sl = entry * (1.0 + sl_pct * sl_mult)
            plans.append(ShadowPlan(name, round_price(tp, PRICE_TICK_DECIMALS), round_price(sl, PRICE_TICK_DECIMALS)))
        return tuple(plans)

    @staticmethod
    def _bad(reason: str) -> TpSlPlan:
        return TpSlPlan(False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, total_round_trip_cost_rate(), reason)
