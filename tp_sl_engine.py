from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from config import MIN_NET_PROFIT_USDT, MIN_RISK_REWARD, PRICE_TICK_DECIMALS
from indicators import IndicatorSnapshot
from range_learning import RangeVerdict
from utils import clamp, net_profit_for_move, required_move_for_min_profit, round_price, total_round_trip_cost_rate

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
    """Cautious 1H TP/SL engine.

    TP is not allowed to sit beyond the nearest 1H support/resistance when there is no room.
    SL is placed behind the 1H swing/noise, but rejected if that destroys RR.
    """

    def build(self, *, direction: Direction, entry: float, snapshot: IndicatorSnapshot, verdict: RangeVerdict, margin_usdt: float, leverage: int) -> TpSlPlan:
        if entry <= 0:
            return self._bad("قیمت ورود نامعتبر است.")

        min_profitable = required_move_for_min_profit(margin_usdt, leverage, MIN_NET_PROFIT_USDT)
        atr_pct = max(snapshot.atr_pct, 0.0005)

        # A 1H target must be large enough to pay fees, but not chase an unrealistic move.
        base_predicted = max(verdict.predicted_move_pct, atr_pct * 1.75, min_profitable * 1.18)
        base_predicted = clamp(base_predicted, min_profitable, atr_pct * 3.0)
        safe_fraction = clamp(verdict.safe_tp_fraction, 0.65, 0.82)
        candidate_tp_pct = max(base_predicted * safe_fraction, min_profitable, atr_pct * 0.85)

        if direction == "LONG":
            resistance_pct = max(0.0, (max(snapshot.recent_high, snapshot.swing_high) - entry) / entry)
            support_sl_pct = max(0.0, (entry - min(snapshot.swing_low, snapshot.low)) / entry)
            if resistance_pct > 0:
                resistance_safe_pct = max(0.0, resistance_pct - atr_pct * 0.12)
                if resistance_safe_pct < min_profitable:
                    return self._bad("تا مقاومت/قله 1H فضای کافی برای حداقل سود خالص ۲ سنت نیست.")
                tp_distance_pct = min(candidate_tp_pct, resistance_safe_pct, atr_pct * 2.60)
            else:
                tp_distance_pct = min(candidate_tp_pct, atr_pct * 2.20)
            sl_distance_pct = max(atr_pct * 0.85, support_sl_pct + atr_pct * 0.22)
        else:
            support_pct = max(0.0, (entry - min(snapshot.recent_low, snapshot.swing_low)) / entry)
            resistance_sl_pct = max(0.0, (max(snapshot.swing_high, snapshot.high) - entry) / entry)
            if support_pct > 0:
                support_safe_pct = max(0.0, support_pct - atr_pct * 0.12)
                if support_safe_pct < min_profitable:
                    return self._bad("تا حمایت/کف 1H فضای کافی برای حداقل سود خالص ۲ سنت نیست.")
                tp_distance_pct = min(candidate_tp_pct, support_safe_pct, atr_pct * 2.60)
            else:
                tp_distance_pct = min(candidate_tp_pct, atr_pct * 2.20)
            sl_distance_pct = max(atr_pct * 0.85, resistance_sl_pct + atr_pct * 0.22)

        # Never accept a TP that became too small after support/resistance protection.
        if tp_distance_pct < min_profitable:
            return self._bad("TP محتاطانه بعد از کارمزد حداقل سود خالص لازم را نمی‌دهد.")
        if tp_distance_pct < atr_pct * 0.65:
            return self._bad("TP نسبت به نویز 1H خیلی نزدیک است.")

        max_sl_by_rr = tp_distance_pct / MIN_RISK_REWARD
        if sl_distance_pct > max_sl_by_rr:
            compact_sl = max(atr_pct * 0.80, min(sl_distance_pct, max_sl_by_rr))
            if compact_sl <= max_sl_by_rr and compact_sl >= atr_pct * 0.72:
                sl_distance_pct = compact_sl
            else:
                return self._bad("SL پشت کف/قله 1H بیش از حد دور است و RR خراب می‌شود.")

        risk_reward = tp_distance_pct / max(sl_distance_pct, 0.000001)
        if risk_reward < MIN_RISK_REWARD:
            return self._bad("نسبت سود به ضرر برای 1H کافی نیست.")

        net_profit = net_profit_for_move(margin_usdt, leverage, tp_distance_pct)
        if net_profit < MIN_NET_PROFIT_USDT:
            return self._bad("TP تحلیلی بعد از کارمزد حداقل +0.02 USDT سود خالص نمی‌دهد.")

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
            f"TP/SL بر اساس 1H ساخته شد: TP {tp_distance_pct*100:.3f}%، "
            f"حداقل اقتصادی {min_profitable*100:.3f}%، سود خالص تخمینی {net_profit:.4f} USDT، "
            f"SL پشت کف/قله و نویز 1H {sl_distance_pct*100:.3f}%، RR={risk_reward:.2f}."
        )
        return TpSlPlan(True, tp, sl, base_predicted, tp_distance_pct, sl_distance_pct, risk_reward, net_profit, total_round_trip_cost_rate(), reason, shadows)

    def _shadow(self, direction: Direction, entry: float, tp_pct: float, sl_pct: float) -> tuple[ShadowPlan, ...]:
        plans = []
        for name, tp_mult, sl_mult in (("tp_safer", 0.82, 1.00), ("tp_wider", 1.12, 1.00), ("sl_tighter", 1.00, 0.88), ("sl_wider", 1.00, 1.12)):
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
