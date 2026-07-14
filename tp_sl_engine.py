"""موتور Entry/TP/SL چندتایمی با RR پویا و محاسبه دائمی هزینه‌ها."""
from __future__ import annotations

from typing import Any

import config
from models import Decision, FeatureSnapshot, TradePlan
from utils import clamp, round_to_tick

TP_SL_VERSION = "tp-sl-high-tf-v2"


class TPSLEngine:
    def build_plan(
        self,
        snapshot: FeatureSnapshot,
        decision: Decision,
        profile: dict[str, Any],
        margin_usdt: float,
        leverage: int,
        tick_size: float = 0.0,
        min_net_profit: float = config.DEFAULT_MIN_NET_PROFIT_USDT,
        tier: str = "INITIAL",
    ) -> TradePlan:
        selected = snapshot.raw["selected"]
        cfg = profile.get("config") or {}
        entry = float(selected["last"])
        atr = float(selected["atr_natr"]["atr"])
        recent_high = float(selected["recent_high"])
        recent_low = float(selected["recent_low"])
        trade_tf = snapshot.trade_timeframe
        rr_min, rr_max = config.RR_BOUNDS_BY_TIMEFRAME[trade_tf]
        rr = float(cfg.get("rr", config.RR_DEFAULT_BY_TIMEFRAME[trade_tf]))
        rr = clamp(rr, rr_min, rr_max)
        base_tp = {"30m": 1.55, "1H": 1.85, "4H": 2.25, "1D": 2.80}[trade_tf]
        base_sl = {"30m": 0.95, "1H": 1.05, "4H": 1.20, "1D": 1.40}[trade_tf]
        tp_mult = clamp(float(cfg.get("tp_atr_multiplier", base_tp)), 0.50, 8.0)
        sl_mult = clamp(float(cfg.get("sl_atr_multiplier", base_sl)), 0.40, 8.0)

        # Behavior and strength alter starting geometry softly; learning may later replace these values.
        if decision.behavior in {"TRUE_BREAKOUT", "TREND_START"}:
            tp_mult *= 1.10
        elif decision.behavior in {"RANGE", "FALSE_BREAKOUT"}:
            tp_mult *= 0.85
        # Learned, single-change factors are applied only after Validator promotion.
        tp_mult *= clamp(float((cfg.get("behavior_tp_factors") or {}).get(decision.behavior, 1.0)), 0.5, 2.0)
        sl_mult *= clamp(float((cfg.get("behavior_sl_factors") or {}).get(decision.behavior, 1.0)), 0.5, 2.0)
        if decision.strength_score >= 72:
            tp_mult *= 1.08
        if decision.noise_risk >= 65:
            sl_mult *= 1.15

        min_stop_pct = {"30m": 0.0010, "1H": 0.0014, "4H": 0.0022, "1D": 0.0035}[trade_tf]
        noise_buffer = 1.0 + clamp(decision.noise_risk / 100.0, 0.0, 1.0) * 0.22
        atr_stop = max(atr * sl_mult * noise_buffer, entry * min_stop_pct)
        if decision.side == "LONG":
            structural = max(0.0, entry - recent_low)
            stop_distance = max(atr_stop, min(structural + atr * 0.12, atr_stop * 2.5))
            target_distance = max(atr * tp_mult, stop_distance * rr)
            sl = entry - stop_distance
            tp = entry + target_distance
            if tick_size > 0:
                sl = round_to_tick(sl, tick_size, "down")
                tp = round_to_tick(tp, tick_size, "up")
        else:
            structural = max(0.0, recent_high - entry)
            stop_distance = max(atr_stop, min(structural + atr * 0.12, atr_stop * 2.5))
            target_distance = max(atr * tp_mult, stop_distance * rr)
            sl = entry + stop_distance
            tp = entry - target_distance
            if tick_size > 0:
                sl = round_to_tick(sl, tick_size, "up")
                tp = round_to_tick(tp, tick_size, "down")

        # Tick rounding must not push the effective RR outside this timeframe's
        # learned safety envelope. Keep the stop structural and only bring the
        # target back inside the allowed geometry when necessary.
        actual_stop_distance = abs(entry - sl)
        actual_target_distance = abs(tp - entry)
        min_target_distance = actual_stop_distance * rr_min
        max_target_distance = actual_stop_distance * rr_max
        if actual_stop_distance > 0 and actual_target_distance > max_target_distance:
            raw_tp = entry + max_target_distance if decision.side == "LONG" else entry - max_target_distance
            tp = round_to_tick(raw_tp, tick_size, "down" if decision.side == "LONG" else "up") if tick_size > 0 else raw_tp
        elif actual_stop_distance > 0 and actual_target_distance < min_target_distance:
            raw_tp = entry + min_target_distance if decision.side == "LONG" else entry - min_target_distance
            tp = round_to_tick(raw_tp, tick_size, "up" if decision.side == "LONG" else "down") if tick_size > 0 else raw_tp

        tp_percent = abs(tp - entry) / entry if entry > 0 else 0.0
        sl_percent = abs(sl - entry) / entry if entry > 0 else 0.0
        actual_rr = tp_percent / sl_percent if sl_percent > 0 else 0.0
        notional = float(margin_usdt) * int(leverage)
        gross_profit = notional * tp_percent
        cost = notional * (
            config.TOOBIT_TAKER_FEE_RATE * 2
            + config.DEFAULT_SLIPPAGE_RATE_ROUND_TRIP
            + config.DEFAULT_FUNDING_RESERVE_RATE
        )
        expected_net = gross_profit - cost

        valid = entry > 0 and tp > 0 and sl > 0 and actual_rr > 0
        reason = ""
        if not valid:
            reason = "INVALID_TP_SL"
        elif expected_net <= 0:
            valid = False
            reason = "NEGATIVE_NET_ECONOMICS"
        elif tier in {"MEDIUM", "REAL"} and expected_net < min_net_profit:
            valid = False
            reason = "MIN_NET_PROFIT"

        return TradePlan(
            entry=entry,
            tp=tp,
            sl=sl,
            rr=actual_rr,
            tp_percent=tp_percent,
            sl_percent=sl_percent,
            expected_gross_profit=gross_profit,
            expected_net_profit=expected_net,
            expected_cost=cost,
            margin_usdt=float(margin_usdt),
            leverage=int(leverage),
            notional_usdt=notional,
            valid=valid,
            reject_reason=reason,
        )

    @staticmethod
    def realized_virtual_pnl(signal: dict[str, Any], result: str) -> float:
        notional = float(signal.get("notional_usdt") or 0)
        entry = float(signal.get("entry") or 0)
        exit_price = float(signal.get("tp") if result == "TP" else signal.get("sl") or 0)
        if notional <= 0 or entry <= 0 or exit_price <= 0:
            return 0.0
        side = signal.get("side")
        gross_rate = (exit_price - entry) / entry if side == "LONG" else (entry - exit_price) / entry
        gross = notional * gross_rate
        cost = notional * (
            config.TOOBIT_TAKER_FEE_RATE * 2
            + config.DEFAULT_SLIPPAGE_RATE_ROUND_TRIP
            + config.DEFAULT_FUNDING_RESERVE_RATE
        )
        return gross - cost
