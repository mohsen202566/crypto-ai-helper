"""
tp_sl_engine.py
Level 4 / 1H Smart Scalp Bot

Smart TP/SL planning engine for Level 4 / 1H Smart Scalp.

Architecture lock:
- Builds and validates TP/SL plans only.
- Does not make final REAL/GHOST/REJECT decisions.
- Does not place orders, monitor positions, write JSON state, or build Telegram text.
- Uses already-built snapshots and runtime trade config.
- Allowed project imports:
  constants.py, utils.py, models.py only.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

import constants
from constants import DIRECTION_LONG, DIRECTION_SHORT, SYSTEM_VERSION
from models import LiquiditySnapshot, MarketContextSnapshot, MomentumSnapshot, SensorSnapshot, StructureSnapshot, TPSLPlan
from utils import (
    clamp,
    fee_estimate,
    normalize_direction,
    normalize_symbol,
    notional_value,
    profit_usdt,
    round_price,
    safe_float,
    safe_str,
    utc_now_iso,
)


TP_SL_ENGINE_VERSION: str = SYSTEM_VERSION


# Fallbacks keep this file compatible with the already-created constants.py.
DEFAULT_LEVEL_4_RR_CONFIG: dict[str, float] = {
    "min_rr": 0.75,
    "max_rr": 2.80,
    "tp1_atr_mult": 1.15,
    "tp2_atr_mult": 1.85,
    "sl_atr_mult": 0.95,
}

DEFAULT_TP_SL_CONFIG: dict[str, float] = {
    "default_price_tick": 0.0001,
    "fallback_atr_pct": 0.006,
}

DEFAULT_FEE_CONFIG: dict[str, float] = {
    "taker_fee_rate": 0.0006,
}

DEFAULT_MIN_NET_PROFIT_USDT: float = 0.10


def _rr_config() -> Mapping[str, Any]:
    return getattr(constants, "LEVEL_4_RR_CONFIG", DEFAULT_LEVEL_4_RR_CONFIG)


def _tp_sl_config() -> Mapping[str, Any]:
    return getattr(constants, "TP_SL_CONFIG", DEFAULT_TP_SL_CONFIG)


def _fee_config() -> Mapping[str, Any]:
    return getattr(constants, "FEE_CONFIG", DEFAULT_FEE_CONFIG)


def _min_net_profit_usdt() -> float:
    return safe_float(getattr(constants, "MIN_NET_PROFIT_USDT", DEFAULT_MIN_NET_PROFIT_USDT), DEFAULT_MIN_NET_PROFIT_USDT) or DEFAULT_MIN_NET_PROFIT_USDT


def _fee_rate() -> float:
    """Return configured fee estimate rate per side."""
    return safe_float(_fee_config().get("taker_fee_rate"), 0.0006) or 0.0006


def _price_tick(symbol: str = "") -> float:
    """Default price tick. Exchange-specific tick can be refined by real_trade_manager."""
    return safe_float(_tp_sl_config().get("default_price_tick"), 0.0001) or 0.0001


def estimate_quantity(entry: Any, margin_usdt: Any, leverage: Any) -> float:
    """Estimate quantity from margin * leverage / entry."""
    entry_f = safe_float(entry, 0.0) or 0.0
    margin = safe_float(margin_usdt, 0.0) or 0.0
    lev = safe_float(leverage, 1.0) or 1.0
    if entry_f <= 0 or margin <= 0 or lev <= 0:
        return 0.0
    return (margin * lev) / entry_f


def directional_price(entry: float, direction: str, distance: float, *, target: bool) -> float:
    """Return price moved by distance in TP or SL direction."""
    d = normalize_direction(direction)
    if d == DIRECTION_LONG:
        return entry + distance if target else entry - distance
    if d == DIRECTION_SHORT:
        return entry - distance if target else entry + distance
    return entry


def calculate_rr(entry: float, tp1: float, sl: float, direction: str) -> float:
    """Calculate risk/reward to TP1."""
    d = normalize_direction(direction)
    if entry <= 0:
        return 0.0
    if d == DIRECTION_LONG:
        reward = tp1 - entry
        risk = entry - sl
    elif d == DIRECTION_SHORT:
        reward = entry - tp1
        risk = sl - entry
    else:
        return 0.0
    if risk <= 0:
        return 0.0
    return reward / risk


def price_distance_pct(entry: float, price: float) -> float:
    if entry <= 0:
        return 0.0
    return abs(price - entry) / entry * 100.0


def base_atr_distance(sensor: SensorSnapshot) -> float:
    """Return ATR fallback distance."""
    price = safe_float(sensor.price, 0.0) or 0.0
    atr = safe_float(sensor.atr, 0.0) or 0.0
    if atr > 0:
        return atr
    return price * (safe_float(_tp_sl_config().get("fallback_atr_pct"), 0.006) or 0.006)


def tp1_atr_multiplier(momentum: MomentumSnapshot, liquidity: LiquiditySnapshot, context: MarketContextSnapshot) -> float:
    """Dynamic TP1 ATR multiplier for 45-90 minute Level 4 scalp."""
    cfg = _rr_config()
    base = safe_float(cfg.get("tp1_atr_mult"), 1.15) or 1.15

    if safe_float(momentum.continuation_score, 50.0) >= 70:
        base += 0.15
    if safe_float(momentum.momentum_score, 50.0) >= 75:
        base += 0.10
    if safe_float(liquidity.trap_risk_score, 0.0) >= 60:
        base -= 0.20
    if safe_float(context.context_score, 50.0) < 45:
        base -= 0.15

    return clamp(base, 0.75, 1.55)


def tp2_atr_multiplier(momentum: MomentumSnapshot, liquidity: LiquiditySnapshot, context: MarketContextSnapshot) -> float:
    """Dynamic TP2 ATR multiplier."""
    cfg = _rr_config()
    base = safe_float(cfg.get("tp2_atr_mult"), 1.85) or 1.85

    if safe_float(momentum.continuation_score, 50.0) >= 72:
        base += 0.25
    if safe_float(context.context_score, 50.0) >= 65:
        base += 0.15
    if safe_float(liquidity.trap_risk_score, 0.0) >= 55:
        base -= 0.25

    return clamp(base, 1.20, 2.60)


def sl_atr_multiplier(structure: StructureSnapshot, liquidity: LiquiditySnapshot) -> float:
    """Dynamic SL ATR multiplier. Avoid too wide stop."""
    cfg = _rr_config()
    base = safe_float(cfg.get("sl_atr_mult"), 0.95) or 0.95

    if structure.is_range:
        base -= 0.10
    if safe_float(liquidity.trap_risk_score, 0.0) >= 60:
        base -= 0.10
    if safe_float(structure.structure_score, 50.0) >= 70:
        base += 0.10

    return clamp(base, 0.65, 1.25)


def adjust_tp_for_structure(entry: float, proposed_tp: float, direction: str, structure: StructureSnapshot) -> float:
    """Pull TP before nearby resistance/support when appropriate."""
    d = normalize_direction(direction)
    tp = proposed_tp

    if d == DIRECTION_LONG and structure.nearest_resistance:
        res = safe_float(structure.nearest_resistance, None)
        if res is not None and entry < res < proposed_tp:
            tp = entry + (res - entry) * 0.88

    elif d == DIRECTION_SHORT and structure.nearest_support:
        sup = safe_float(structure.nearest_support, None)
        if sup is not None and proposed_tp < sup < entry:
            tp = entry - (entry - sup) * 0.88

    return tp


def adjust_sl_for_structure(entry: float, proposed_sl: float, direction: str, structure: StructureSnapshot, atr_distance: float) -> float:
    """Place SL beyond nearest useful structure but avoid over-widening."""
    d = normalize_direction(direction)
    sl = proposed_sl
    max_extra = atr_distance * 0.35

    if d == DIRECTION_LONG and structure.nearest_support:
        support = safe_float(structure.nearest_support, None)
        if support is not None and support < entry:
            candidate = support - atr_distance * 0.12
            if entry - candidate <= (entry - proposed_sl) + max_extra:
                sl = min(sl, candidate)

    elif d == DIRECTION_SHORT and structure.nearest_resistance:
        resistance = safe_float(structure.nearest_resistance, None)
        if resistance is not None and resistance > entry:
            candidate = resistance + atr_distance * 0.12
            if candidate - entry <= (proposed_sl - entry) + max_extra:
                sl = max(sl, candidate)

    return sl


def validate_directional_plan(entry: float, tp1: float, sl: float, direction: str, tp2: Optional[float] = None) -> tuple[bool, list[str]]:
    """Validate TP/SL side correctness."""
    d = normalize_direction(direction)
    errors: list[str] = []

    if entry <= 0:
        errors.append("INVALID_ENTRY")

    if d == DIRECTION_LONG:
        if tp1 <= entry:
            errors.append("TP1_NOT_ABOVE_ENTRY")
        if sl >= entry:
            errors.append("SL_NOT_BELOW_ENTRY")
        if tp2 is not None and tp2 <= tp1:
            errors.append("TP2_NOT_ABOVE_TP1")
    elif d == DIRECTION_SHORT:
        if tp1 >= entry:
            errors.append("TP1_NOT_BELOW_ENTRY")
        if sl <= entry:
            errors.append("SL_NOT_ABOVE_ENTRY")
        if tp2 is not None and tp2 >= tp1:
            errors.append("TP2_NOT_BELOW_TP1")
    else:
        errors.append("INVALID_DIRECTION")

    return not errors, errors


def validate_rr(rr: float) -> tuple[bool, str]:
    """Validate Level 4 RR range."""
    cfg = _rr_config()
    min_rr = safe_float(cfg.get("min_rr"), 0.75) or 0.75
    max_rr = safe_float(cfg.get("max_rr"), 2.8) or 2.8

    if rr < min_rr:
        return False, "RR_TOO_LOW"
    if rr > max_rr:
        return False, "RR_TOO_HIGH"
    return True, ""


def validate_min_net_profit(
    *,
    direction: str,
    entry: float,
    tp1: float,
    quantity: float,
    fee_rate: float,
) -> tuple[bool, float, float, float, str]:
    """Validate TP1 estimated net profit after fee."""
    gross = profit_usdt(direction, entry, tp1, quantity)
    notional = notional_value(entry, quantity)
    fees = fee_estimate(notional, fee_rate, sides=2)
    net = gross - fees
    min_net = _min_net_profit_usdt()

    if net < min_net:
        return False, gross, fees, net, "TP1_NET_PROFIT_TOO_LOW"
    return True, gross, fees, net, ""


def validate_tp_sl_plan(plan: TPSLPlan, *, quantity: float = 0.0, fee_rate: Optional[float] = None) -> tuple[bool, list[str]]:
    """Full validation for TP/SL plan."""
    errors: list[str] = []

    side_ok, side_errors = validate_directional_plan(plan.entry, plan.tp1, plan.sl, plan.direction, plan.tp2)
    if not side_ok:
        errors.extend(side_errors)

    rr_ok, rr_error = validate_rr(plan.rr)
    if not rr_ok:
        errors.append(rr_error)

    if quantity > 0:
        fee = _fee_rate() if fee_rate is None else fee_rate
        profit_ok, _gross, _fees, _net, profit_error = validate_min_net_profit(
            direction=plan.direction,
            entry=plan.entry,
            tp1=plan.tp1,
            quantity=quantity,
            fee_rate=fee,
        )
        if not profit_ok:
            errors.append(profit_error)

    return not errors, errors


def build_tp_sl_plan(
    *,
    symbol: str,
    direction: str,
    entry: float,
    sensor: SensorSnapshot,
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
    context: MarketContextSnapshot,
    trade_config: Optional[Mapping[str, Any]] = None,
) -> TPSLPlan:
    """Build a smart TP/SL plan for Level 4."""
    d = normalize_direction(direction)
    normalized_symbol = normalize_symbol(symbol)
    entry_f = safe_float(entry, 0.0) or safe_float(sensor.price, 0.0) or 0.0
    atr_distance = base_atr_distance(sensor)

    tp1_mult = tp1_atr_multiplier(momentum, liquidity, context)
    tp2_mult = tp2_atr_multiplier(momentum, liquidity, context)
    sl_mult = sl_atr_multiplier(structure, liquidity)

    tp1_distance = atr_distance * tp1_mult
    tp2_distance = atr_distance * tp2_mult
    sl_distance = atr_distance * sl_mult

    proposed_tp1 = directional_price(entry_f, d, tp1_distance, target=True)
    proposed_tp2 = directional_price(entry_f, d, tp2_distance, target=True)
    proposed_sl = directional_price(entry_f, d, sl_distance, target=False)

    adjusted_tp1 = adjust_tp_for_structure(entry_f, proposed_tp1, d, structure)
    adjusted_tp2 = adjust_tp_for_structure(entry_f, proposed_tp2, d, structure)
    adjusted_sl = adjust_sl_for_structure(entry_f, proposed_sl, d, structure, atr_distance)

    tick = _price_tick(normalized_symbol)
    tp1 = round_price(adjusted_tp1, tick)
    tp2 = round_price(adjusted_tp2, tick)
    sl = round_price(adjusted_sl, tick)

    rr = calculate_rr(entry_f, tp1, sl, d)

    margin = safe_float((trade_config or {}).get("margin_usdt"), 0.0) or 0.0
    leverage = safe_float((trade_config or {}).get("leverage"), 1.0) or 1.0
    quantity = estimate_quantity(entry_f, margin, leverage)
    fee_rate = _fee_rate()

    gross = 0.0
    fees = 0.0
    net = 0.0
    if quantity > 0:
        gross = profit_usdt(d, entry_f, tp1, quantity)
        notional = notional_value(entry_f, quantity)
        fees = fee_estimate(notional, fee_rate, sides=2)
        net = gross - fees

    plan = TPSLPlan(
        symbol=normalized_symbol,
        direction=d,
        entry=entry_f,
        tp1=tp1,
        tp2=tp2,
        sl=sl,
        rr=rr,
        tp1_net_profit_estimate=net,
        tp1_gross_profit_estimate=gross,
        fee_estimate=fees,
        valid=True,
        reason_codes=[],
        raw={
            "atr_distance": atr_distance,
            "tp1_distance": tp1_distance,
            "tp2_distance": tp2_distance,
            "sl_distance": sl_distance,
            "tp1_atr_multiplier": tp1_mult,
            "tp2_atr_multiplier": tp2_mult,
            "sl_atr_multiplier": sl_mult,
            "estimated_quantity": quantity,
            "margin_usdt": margin,
            "leverage": leverage,
            "fee_rate": fee_rate,
            "used_fallback_rr_config": not hasattr(constants, "LEVEL_4_RR_CONFIG"),
            "used_fallback_tp_sl_config": not hasattr(constants, "TP_SL_CONFIG"),
            "created_at": utc_now_iso(),
        },
    )

    valid, errors = validate_tp_sl_plan(plan, quantity=quantity, fee_rate=fee_rate)
    plan.valid = valid
    plan.reason_codes = ["TP_SL_VALID"] if valid else errors

    return plan


def make_invalid_plan(symbol: str, direction: str, entry: float, reason: str) -> TPSLPlan:
    """Create invalid plan safely."""
    return TPSLPlan(
        symbol=normalize_symbol(symbol),
        direction=normalize_direction(direction),
        entry=safe_float(entry, 0.0) or 0.0,
        tp1=0.0,
        tp2=None,
        sl=0.0,
        rr=0.0,
        valid=False,
        reason_codes=[safe_str(reason, "INVALID_TP_SL")],
    )


def validate_tp_sl_output(plan: TPSLPlan) -> dict[str, Any]:
    """Lightweight output validation."""
    errors: list[str] = []

    if plan.system_version != SYSTEM_VERSION:
        errors.append("INVALID_SYSTEM_VERSION")
    if not normalize_symbol(plan.symbol):
        errors.append("MISSING_SYMBOL")
    if normalize_direction(plan.direction) not in {DIRECTION_LONG, DIRECTION_SHORT}:
        errors.append("INVALID_DIRECTION")
    side_ok, side_errors = validate_directional_plan(plan.entry, plan.tp1, plan.sl, plan.direction, plan.tp2)
    if not side_ok:
        errors.extend(side_errors)
    if plan.rr <= 0:
        errors.append("INVALID_RR")

    return {
        "valid": not errors,
        "errors": errors,
        "symbol": plan.symbol,
        "direction": plan.direction,
        "rr": plan.rr,
        "tp1_net_profit_estimate": plan.tp1_net_profit_estimate,
    }


__all__ = [
    "TP_SL_ENGINE_VERSION",
    "DEFAULT_LEVEL_4_RR_CONFIG",
    "DEFAULT_TP_SL_CONFIG",
    "DEFAULT_FEE_CONFIG",
    "DEFAULT_MIN_NET_PROFIT_USDT",
    "estimate_quantity",
    "directional_price",
    "calculate_rr",
    "price_distance_pct",
    "base_atr_distance",
    "tp1_atr_multiplier",
    "tp2_atr_multiplier",
    "sl_atr_multiplier",
    "adjust_tp_for_structure",
    "adjust_sl_for_structure",
    "validate_directional_plan",
    "validate_rr",
    "validate_min_net_profit",
    "validate_tp_sl_plan",
    "build_tp_sl_plan",
    "make_invalid_plan",
    "validate_tp_sl_output",
]
