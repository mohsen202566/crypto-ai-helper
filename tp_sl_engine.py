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


def _mapping_from_obj(value: Any) -> dict[str, Any]:
    """Best-effort mapping extraction without depending on model internals."""
    if isinstance(value, Mapping):
        return dict(value)
    raw = getattr(value, "raw", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    meta = getattr(value, "metadata", None)
    if isinstance(meta, Mapping):
        return dict(meta)
    return {}


def _first_feature_value(key: str, *sources: Any) -> Any:
    """Return first non-None feature value from snapshots/mappings/metadata."""
    for source in sources:
        if source is None:
            continue
        if isinstance(source, Mapping) and key in source:
            return source.get(key)
        if hasattr(source, key):
            value = getattr(source, key)
            if value is not None:
                return value
        mapped = _mapping_from_obj(source)
        if key in mapped:
            return mapped.get(key)
        nested = mapped.get("hunter_features")
        if isinstance(nested, Mapping) and key in nested:
            return nested.get(key)
    return None


def _bool_feature(value: Any) -> bool:
    """Stable bool conversion for metadata flags."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def extract_hunter_tp_sl_features(
    *,
    sensor: SensorSnapshot,
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
    context: MarketContextSnapshot,
    trade_config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Extract start-of-move and anti-chase features for TP/SL planning.

    TP/SL must not blindly validate a late/chasing entry. These fields are
    produced by the hunter/timing/selector stack and are kept optional so this
    engine remains compatible with older snapshots.
    """
    cfg = dict(trade_config or {})
    features = dict(cfg.get("hunter_features", {})) if isinstance(cfg.get("hunter_features"), Mapping) else {}
    sources = (cfg, sensor, structure, momentum, liquidity, context)
    keys = [
        "start_score",
        "start_active",
        "start_signal_count",
        "chase_risk_score",
        "chase_active",
        "move_age_score",
        "late_risk_score",
        "fresh_momentum_score",
        "exhaustion_score",
        "start_pressure_score",
        "structure_start_active",
        "momentum_start_active",
        "liquidity_start_active",
        "fresh_context_active",
        "early_start_synergy",
        "selector_rank_score",
        "selector_selected_for_real",
    ]
    for key in keys:
        if key not in features:
            value = _first_feature_value(key, *sources)
            if value is not None:
                features[key] = value
    return features


def hunter_tp_sl_risk_reasons(features: Mapping[str, Any]) -> list[str]:
    """Return reasons when TP/SL should not validate a late/chasing entry."""
    start = safe_float(features.get("start_score"), None)
    chase = safe_float(features.get("chase_risk_score"), None)
    age = safe_float(features.get("move_age_score"), None)
    late = safe_float(features.get("late_risk_score"), None)
    fresh = safe_float(features.get("fresh_momentum_score"), None)
    exhaustion = safe_float(features.get("exhaustion_score"), None)

    reasons: list[str] = []
    if chase is not None and chase >= 70:
        reasons.append("CHASE_RISK_HIGH")
    if late is not None and late >= 65:
        reasons.append("LATE_RISK_HIGH")
    if age is not None and age >= 70:
        reasons.append("MOVE_TOO_OLD")
    if exhaustion is not None and exhaustion >= 70:
        reasons.append("EXHAUSTION_HIGH")
    if start is not None and fresh is not None and start < 45 and fresh < 45:
        reasons.append("FRESH_MOMENTUM_WEAK")
    return reasons


def adjust_multipliers_for_hunter_quality(
    *,
    tp1_mult: float,
    tp2_mult: float,
    sl_mult: float,
    features: Mapping[str, Any],
) -> tuple[float, float, float, list[str]]:
    """Make TP/SL distances aware of start quality without becoming a signal engine."""
    start = safe_float(features.get("start_score"), None)
    chase = safe_float(features.get("chase_risk_score"), None)
    age = safe_float(features.get("move_age_score"), None)
    late = safe_float(features.get("late_risk_score"), None)
    fresh = safe_float(features.get("fresh_momentum_score"), None)
    pressure = safe_float(features.get("start_pressure_score"), None)

    reasons: list[str] = []
    start_strong = start is not None and start >= 70
    fresh_ok = fresh is None or fresh >= 50
    low_chase = chase is None or chase <= 35
    low_late = late is None or late <= 35
    not_old = age is None or age <= 45

    if start_strong and fresh_ok and low_chase and low_late and not_old:
        tp1_mult += 0.08
        tp2_mult += 0.18
        reasons.append("TP_SL_HUNTER_START_BONUS")
    elif start is not None and start >= 55 and low_chase:
        tp1_mult += 0.03
        reasons.append("TP_SL_VALID_START")

    if pressure is not None and pressure >= 65 and low_chase:
        tp1_mult += 0.04
        reasons.append("TP_SL_START_PRESSURE_BONUS")

    if chase is not None and chase >= 55:
        tp1_mult -= 0.12
        tp2_mult -= 0.25
        sl_mult = min(sl_mult, 0.95)
        reasons.append("TP_SL_CHASE_RISK_REDUCED_TARGET")
    if late is not None and late >= 50:
        tp1_mult -= 0.10
        tp2_mult -= 0.22
        reasons.append("TP_SL_LATE_RISK_REDUCED_TARGET")
    if age is not None and age >= 55:
        tp1_mult -= 0.08
        tp2_mult -= 0.18
        reasons.append("TP_SL_OLD_MOVE_REDUCED_TARGET")
    if fresh is not None and fresh < 40:
        tp1_mult -= 0.07
        tp2_mult -= 0.15
        reasons.append("TP_SL_FRESH_MOMENTUM_WEAK")

    return clamp(tp1_mult, 0.70, 1.60), clamp(tp2_mult, 1.10, 2.70), clamp(sl_mult, 0.65, 1.25), reasons


def should_enable_tp2(
    *,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
    context: MarketContextSnapshot,
    features: Mapping[str, Any],
) -> tuple[bool, str]:
    """Enable TP2 only when continuation quality is strong enough for Level 4."""
    continuation = safe_float(momentum.continuation_score, 50.0) or 50.0
    momentum_score = safe_float(momentum.momentum_score, 50.0) or 50.0
    trap = safe_float(liquidity.trap_risk_score, 0.0) or 0.0
    ctx = safe_float(context.context_score, 50.0) or 50.0
    chase = safe_float(features.get("chase_risk_score"), 0.0) or 0.0
    late = safe_float(features.get("late_risk_score"), 0.0) or 0.0
    age = safe_float(features.get("move_age_score"), 0.0) or 0.0
    start = safe_float(features.get("start_score"), None)
    fresh = safe_float(features.get("fresh_momentum_score"), None)

    if trap >= 60:
        return False, "TP2_DISABLED_TRAP_RISK"
    if chase >= 55 or late >= 55 or age >= 60:
        return False, "TP2_DISABLED_LATE_OR_CHASE"
    if continuation < 68 or momentum_score < 58 or ctx < 48:
        return False, "TP2_DISABLED_WEAK_CONTINUATION"
    if start is not None and start < 50:
        return False, "TP2_DISABLED_WEAK_START"
    if fresh is not None and fresh < 45:
        return False, "TP2_DISABLED_WEAK_FRESH_MOMENTUM"
    return True, "TP2_ENABLED_STRONG_CONTINUATION"


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


def validate_tp_sl_plan(
    plan: TPSLPlan,
    *,
    quantity: float = 0.0,
    fee_rate: Optional[float] = None,
    hunter_features: Optional[Mapping[str, Any]] = None,
) -> tuple[bool, list[str]]:
    """Full validation for TP/SL plan, including hunter anti-chase quality."""
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

    if hunter_features:
        errors.extend(hunter_tp_sl_risk_reasons(hunter_features))

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

    hunter_features = extract_hunter_tp_sl_features(
        sensor=sensor,
        structure=structure,
        momentum=momentum,
        liquidity=liquidity,
        context=context,
        trade_config=trade_config,
    )

    tp1_mult = tp1_atr_multiplier(momentum, liquidity, context)
    tp2_mult = tp2_atr_multiplier(momentum, liquidity, context)
    sl_mult = sl_atr_multiplier(structure, liquidity)
    tp1_mult, tp2_mult, sl_mult, hunter_adjust_reasons = adjust_multipliers_for_hunter_quality(
        tp1_mult=tp1_mult,
        tp2_mult=tp2_mult,
        sl_mult=sl_mult,
        features=hunter_features,
    )
    tp2_enabled, tp2_reason = should_enable_tp2(
        momentum=momentum,
        liquidity=liquidity,
        context=context,
        features=hunter_features,
    )

    tp1_distance = atr_distance * tp1_mult
    tp2_distance = atr_distance * tp2_mult if tp2_enabled else 0.0
    sl_distance = atr_distance * sl_mult

    proposed_tp1 = directional_price(entry_f, d, tp1_distance, target=True)
    proposed_tp2 = directional_price(entry_f, d, tp2_distance, target=True) if tp2_enabled else None
    proposed_sl = directional_price(entry_f, d, sl_distance, target=False)

    adjusted_tp1 = adjust_tp_for_structure(entry_f, proposed_tp1, d, structure)
    adjusted_tp2 = adjust_tp_for_structure(entry_f, proposed_tp2, d, structure) if proposed_tp2 is not None else None
    adjusted_sl = adjust_sl_for_structure(entry_f, proposed_sl, d, structure, atr_distance)

    tick = _price_tick(normalized_symbol)
    tp1 = round_price(adjusted_tp1, tick)
    tp2 = round_price(adjusted_tp2, tick) if adjusted_tp2 is not None else None
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
            "tp2_enabled": tp2_enabled,
            "tp2_reason": tp2_reason,
            "hunter_features": dict(hunter_features),
            "hunter_adjust_reasons": list(hunter_adjust_reasons),
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

    valid, errors = validate_tp_sl_plan(
        plan,
        quantity=quantity,
        fee_rate=fee_rate,
        hunter_features=hunter_features,
    )
    plan.valid = valid
    if valid:
        plan.reason_codes = ["TP_SL_VALID", tp2_reason, *hunter_adjust_reasons]
    else:
        plan.reason_codes = errors

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
    "extract_hunter_tp_sl_features",
    "hunter_tp_sl_risk_reasons",
    "adjust_multipliers_for_hunter_quality",
    "should_enable_tp2",
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
