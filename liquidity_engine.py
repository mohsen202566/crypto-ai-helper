"""
liquidity_engine.py
Level 4 / 1H Smart Scalp Bot

Liquidity / trap risk engine.

Architecture lock:
- Scores stop hunts, liquidity sweeps, fake breaks, wick rejection, and breakout survival.
- Does not make final REAL/GHOST/REJECT decisions.
- Does not place orders, monitor positions, write JSON state, or build Telegram text.
- Allowed project imports: constants.py, utils.py, models.py, structure_engine.py, technical_sensors.py only.

Core rule:
- Hunt the start of a pump/dump movement, not the middle/end of it.
- Separate a useful liquidity grab near the start from a dangerous fake break after extension.
- Fresh start evidence from structure_engine can improve survival and reduce false trap noise.
- Late/extended movement, fake break, rejection wick, and weak participation must raise trap risk.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from constants import DIRECTION_LONG, DIRECTION_SHORT, SYSTEM_VERSION
from models import Candle, LiquiditySnapshot, MarketSnapshot, SensorSnapshot, StructureSnapshot
from structure_engine import find_swing_highs, find_swing_lows, nearest_resistance, nearest_support
from technical_sensors import atr, lower_wick_pct, upper_wick_pct, volume_ratio
from utils import clamp, normalize_direction, pct_distance, safe_float, safe_str


LIQUIDITY_ENGINE_VERSION: str = SYSTEM_VERSION


# =============================================================================
# Safe helpers
# =============================================================================

def _num(value: Any, default: float = 0.0) -> float:
    """Return safe float while preserving valid 0.0 values."""
    parsed = safe_float(value, None)
    if parsed is None:
        return float(default)
    return float(parsed)


def _bool(value: Any, default: bool = False) -> bool:
    """Read booleans safely, including common string forms."""
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    text = safe_str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _raw_mapping(obj: Any) -> Mapping[str, Any]:
    raw = getattr(obj, "raw", None) or {}
    if isinstance(raw, Mapping):
        return raw
    return {}


def _nested_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _structure_start_raw(structure: Optional[StructureSnapshot]) -> Mapping[str, Any]:
    """Read structure_engine move_start_zone diagnostics safely."""
    if structure is None:
        return {}
    raw = _raw_mapping(structure)
    nested = _nested_mapping(raw.get("move_start_zone"))
    if nested:
        return nested
    return raw


def _structure_start_active(structure: Optional[StructureSnapshot]) -> bool:
    raw = _structure_start_raw(structure)
    return _bool(raw.get("active"), False)


def _structure_start_score(structure: Optional[StructureSnapshot]) -> float:
    raw = _structure_start_raw(structure)
    return _num(raw.get("score"), 0.0)


def _structure_extended(structure: Optional[StructureSnapshot]) -> bool:
    if structure is None:
        return False
    raw = _structure_start_raw(structure)
    return _bool(raw.get("move_already_extended"), False) or bool(getattr(structure, "is_late_move", False))


def _structure_room_ok(structure: Optional[StructureSnapshot]) -> bool:
    raw = _structure_start_raw(structure)
    return _bool(raw.get("room_to_target"), True)


def _current_price(candle: Candle) -> float:
    return _num(getattr(candle, "close", 0.0), 0.0)


def _candle_range(candle: Candle) -> float:
    return max(0.0, _num(getattr(candle, "high", 0.0), 0.0) - _num(getattr(candle, "low", 0.0), 0.0))


def _body_size(candle: Candle) -> float:
    return abs(_num(getattr(candle, "close", 0.0), 0.0) - _num(getattr(candle, "open", 0.0), 0.0))


def _body_pct(candle: Candle) -> float:
    rng = _candle_range(candle)
    if rng <= 0:
        return 0.0
    return clamp(_body_size(candle) / rng, 0.0, 1.0)


# =============================================================================
# Basic liquidity helpers
# =============================================================================

def recent_high(candles: list[Candle], period: int = 20) -> Optional[float]:
    if not candles:
        return None
    sample = candles[-period:] if period > 0 else candles
    return max(_num(getattr(c, "high", 0.0), 0.0) for c in sample)


def recent_low(candles: list[Candle], period: int = 20) -> Optional[float]:
    if not candles:
        return None
    sample = candles[-period:] if period > 0 else candles
    return min(_num(getattr(c, "low", 0.0), 0.0) for c in sample)


def candle_closed_back_inside(candle: Candle, level: float, direction: str) -> bool:
    """
    Detect fake break close back inside.

    LONG trap: wick above resistance but close below level.
    SHORT trap: wick below support but close above level.
    """
    d = normalize_direction(direction)
    close = _num(getattr(candle, "close", 0.0), 0.0)
    high = _num(getattr(candle, "high", 0.0), 0.0)
    low = _num(getattr(candle, "low", 0.0), 0.0)

    if level <= 0:
        return False
    if d == DIRECTION_LONG:
        return high > level and close < level
    if d == DIRECTION_SHORT:
        return low < level and close > level
    return False


def candle_reclaimed_after_sweep(candle: Candle, level: float, direction: str) -> bool:
    """
    Detect useful liquidity grab before movement.

    LONG start: wick below support/demand then close back above level.
    SHORT start: wick above resistance/supply then close back below level.
    """
    d = normalize_direction(direction)
    close = _num(getattr(candle, "close", 0.0), 0.0)
    high = _num(getattr(candle, "high", 0.0), 0.0)
    low = _num(getattr(candle, "low", 0.0), 0.0)

    if level <= 0:
        return False
    if d == DIRECTION_LONG:
        return low < level and close > level
    if d == DIRECTION_SHORT:
        return high > level and close < level
    return False


def swept_level(candle: Candle, level: Optional[float], direction: str, tolerance_pct: float = 0.15) -> bool:
    """Return True if candle swept a nearby structural level in the requested direction."""
    if level is None or level <= 0:
        return False

    d = normalize_direction(direction)
    high = _num(getattr(candle, "high", 0.0), 0.0)
    low = _num(getattr(candle, "low", 0.0), 0.0)
    tol = level * (tolerance_pct / 100.0)

    if d == DIRECTION_LONG:
        return high >= level - tol
    if d == DIRECTION_SHORT:
        return low <= level + tol
    return False


def swept_opposite_level(candle: Candle, level: Optional[float], direction: str, tolerance_pct: float = 0.15) -> bool:
    """Return True if candle swept the opposite side, often a useful start liquidity grab."""
    if level is None or level <= 0:
        return False

    d = normalize_direction(direction)
    high = _num(getattr(candle, "high", 0.0), 0.0)
    low = _num(getattr(candle, "low", 0.0), 0.0)
    tol = level * (tolerance_pct / 100.0)

    if d == DIRECTION_LONG:
        return low <= level + tol
    if d == DIRECTION_SHORT:
        return high >= level - tol
    return False


def wick_rejection_score(candles: list[Candle], direction: str) -> float:
    """
    Score wick rejection against entry direction.

    Higher score = more rejection/trap risk.
    """
    if not candles:
        return 0.0

    d = normalize_direction(direction)
    last = candles[-1]
    upper = upper_wick_pct(last)
    lower = lower_wick_pct(last)

    score = 0.0
    if d == DIRECTION_LONG:
        score = upper * 100.0
    elif d == DIRECTION_SHORT:
        score = lower * 100.0

    # Strong rejection wick with tiny body is more suspicious.
    if _candle_range(last) > 0 and _body_pct(last) < 0.25:
        score += 15.0

    return clamp(score, 0.0, 100.0)


def start_liquidity_score(
    candles: list[Candle],
    direction: str,
    structure: Optional[StructureSnapshot] = None,
) -> tuple[float, list[str]]:
    """
    Score useful liquidity conditions near the beginning of a move.

    Higher score = a potential liquidity grab / reclaim / fresh breakout start,
    not a reason to enter by itself.
    """
    if len(candles) < 5:
        return 0.0, ["LIQ_START_NOT_ENOUGH_CANDLES"]

    d = normalize_direction(direction)
    last = candles[-1]
    price = _current_price(last)
    vol = _num(volume_ratio(candles, 5, 30), 1.0)
    atr_value = _num(atr(candles, 14), 0.0)

    swing_highs = getattr(structure, "swing_highs", None) if structure else None
    swing_lows = getattr(structure, "swing_lows", None) if structure else None
    swing_highs = swing_highs if swing_highs is not None else find_swing_highs(candles)
    swing_lows = swing_lows if swing_lows is not None else find_swing_lows(candles)

    support = getattr(structure, "nearest_support", None) if structure else None
    resistance = getattr(structure, "nearest_resistance", None) if structure else None
    support = support if support else nearest_support(price, swing_lows)
    resistance = resistance if resistance else nearest_resistance(price, swing_highs)

    sraw = _structure_start_raw(structure)
    structure_start = _bool(sraw.get("active"), False)
    atr_start = _bool(sraw.get("atr_expansion_start"), False)
    micro_shift = _bool(sraw.get("micro_structure_shift"), False)
    volume_pressure = _bool(sraw.get("volume_pressure_start"), False)
    sd_reaction = _bool(sraw.get("supply_demand_reaction"), False)
    room_ok = _bool(sraw.get("room_to_target"), True)
    extended = _structure_extended(structure)

    score = 25.0
    reasons: list[str] = []

    if d == DIRECTION_LONG and support:
        if swept_opposite_level(last, support, d):
            score += 18.0
            reasons.append("LIQ_START_SUPPORT_SWEEP")
            if candle_reclaimed_after_sweep(last, support, d):
                score += 18.0
                reasons.append("LIQ_START_SUPPORT_RECLAIM")
        dist = pct_distance(price, support)
        if dist <= 0.45:
            score += 7.0
            reasons.append("LIQ_START_NEAR_SUPPORT")
    elif d == DIRECTION_SHORT and resistance:
        if swept_opposite_level(last, resistance, d):
            score += 18.0
            reasons.append("LIQ_START_RESISTANCE_SWEEP")
            if candle_reclaimed_after_sweep(last, resistance, d):
                score += 18.0
                reasons.append("LIQ_START_RESISTANCE_RECLAIM")
        dist = pct_distance(price, resistance)
        if dist <= 0.45:
            score += 7.0
            reasons.append("LIQ_START_NEAR_RESISTANCE")

    if structure_start:
        score += 12.0
        reasons.append("LIQ_STRUCTURE_START_ZONE")
    if atr_start:
        score += 7.0
        reasons.append("LIQ_ATR_EXPANSION_START")
    if micro_shift:
        score += 7.0
        reasons.append("LIQ_MICRO_SHIFT_START")
    if volume_pressure or vol >= 1.12:
        score += 7.0
        reasons.append("LIQ_VOLUME_PRESSURE_START")
    elif vol < 0.70:
        score -= 10.0
        reasons.append("LIQ_VOLUME_TOO_WEAK")

    if sd_reaction:
        score += 7.0
        reasons.append("LIQ_SUPPLY_DEMAND_REACTION")

    if atr_value > 0:
        rng = _candle_range(last)
        if 0.35 * atr_value <= rng <= 1.45 * atr_value:
            score += 5.0
            reasons.append("LIQ_HEALTHY_RANGE_EXPANSION")
        elif rng > 2.0 * atr_value:
            score -= 12.0
            reasons.append("LIQ_RANGE_TOO_EXTENDED")

    if not room_ok:
        score -= 14.0
        reasons.append("LIQ_NO_ROOM_TO_TARGET")

    if extended:
        score -= 28.0
        reasons.append("LIQ_BLOCKED_BY_EXTENDED_MOVE")

    if not reasons:
        reasons.append("LIQ_START_NEUTRAL")

    return clamp(score, 0.0, 100.0), reasons


def liquidity_sweep_score(
    candles: list[Candle],
    direction: str,
    structure: Optional[StructureSnapshot] = None,
) -> float:
    """
    Score dangerous sweep/trap risk against intended direction.

    Higher score = more sweep/trap risk. Useful start liquidity grabs are handled
    separately by start_liquidity_score and can reduce soft risk later.
    """
    if len(candles) < 5:
        return 0.0

    d = normalize_direction(direction)
    last = candles[-1]
    price = _current_price(last)
    atr_value = _num(atr(candles, 14), 0.0)

    swing_highs = getattr(structure, "swing_highs", None) if structure else None
    swing_lows = getattr(structure, "swing_lows", None) if structure else None
    swing_highs = swing_highs if swing_highs is not None else find_swing_highs(candles)
    swing_lows = swing_lows if swing_lows is not None else find_swing_lows(candles)

    support = getattr(structure, "nearest_support", None) if structure else None
    resistance = getattr(structure, "nearest_resistance", None) if structure else None
    support = support if support else nearest_support(price, swing_lows)
    resistance = resistance if resistance else nearest_resistance(price, swing_highs)

    score = 0.0

    if d == DIRECTION_LONG and resistance:
        if swept_level(last, resistance, DIRECTION_LONG):
            score += 32.0
            if candle_closed_back_inside(last, resistance, DIRECTION_LONG):
                score += 38.0
        if pct_distance(price, resistance) <= 0.35:
            score += 10.0

    elif d == DIRECTION_SHORT and support:
        if swept_level(last, support, DIRECTION_SHORT):
            score += 32.0
            if candle_closed_back_inside(last, support, DIRECTION_SHORT):
                score += 38.0
        if pct_distance(price, support) <= 0.35:
            score += 10.0

    # Big rejection wick relative to ATR increases dangerous sweep suspicion.
    if atr_value > 0:
        wick_size = 0.0
        if d == DIRECTION_LONG:
            wick_size = _num(getattr(last, "high", 0.0), 0.0) - max(_num(getattr(last, "open", 0.0), 0.0), _num(getattr(last, "close", 0.0), 0.0))
        elif d == DIRECTION_SHORT:
            wick_size = min(_num(getattr(last, "open", 0.0), 0.0), _num(getattr(last, "close", 0.0), 0.0)) - _num(getattr(last, "low", 0.0), 0.0)
        if wick_size >= atr_value * 0.45:
            score += 18.0

    # Fresh start can reduce soft sweep noise, but not a clear fake break.
    start_score, _ = start_liquidity_score(candles, d, structure)
    if start_score >= 62 and not _structure_extended(structure):
        score -= 10.0
    elif start_score >= 52 and not _structure_extended(structure):
        score -= 5.0

    return clamp(score, 0.0, 100.0)


def fake_break_risk_score(
    candles: list[Candle],
    direction: str,
    structure: Optional[StructureSnapshot] = None,
) -> float:
    """Score fake breakout/breakdown risk with start-zone awareness."""
    if len(candles) < 10:
        return 0.0

    d = normalize_direction(direction)
    last = candles[-1]
    prev = candles[-2]
    price = _current_price(last)
    vol = _num(volume_ratio(candles, 5, 30), 1.0)

    swing_highs = getattr(structure, "swing_highs", None) if structure else None
    swing_lows = getattr(structure, "swing_lows", None) if structure else None
    swing_highs = swing_highs if swing_highs is not None else find_swing_highs(candles)
    swing_lows = swing_lows if swing_lows is not None else find_swing_lows(candles)

    resistance = getattr(structure, "nearest_resistance", None) if structure else None
    support = getattr(structure, "nearest_support", None) if structure else None
    resistance = resistance if resistance else nearest_resistance(price, swing_highs)
    support = support if support else nearest_support(price, swing_lows)

    score = 0.0

    if d == DIRECTION_LONG and resistance:
        prev_close = _num(getattr(prev, "close", 0.0), 0.0)
        last_close = _num(getattr(last, "close", 0.0), 0.0)
        last_high = _num(getattr(last, "high", 0.0), 0.0)
        if prev_close <= resistance and last_high > resistance and last_close <= resistance:
            score += 58.0
        if last_close > resistance and vol < 0.80:
            score += 20.0
    elif d == DIRECTION_SHORT and support:
        prev_close = _num(getattr(prev, "close", 0.0), 0.0)
        last_close = _num(getattr(last, "close", 0.0), 0.0)
        last_low = _num(getattr(last, "low", 0.0), 0.0)
        if prev_close >= support and last_low < support and last_close >= support:
            score += 58.0
        if last_close < support and vol < 0.80:
            score += 20.0

    wick = wick_rejection_score(candles, d)
    if wick >= 55:
        score += 15.0

    start_score, _ = start_liquidity_score(candles, d, structure)
    structure_start = _structure_start_active(structure)
    extended = _structure_extended(structure)

    # A real start zone should not be punished like a classic fake break unless
    # the candle closed back inside or wick rejection is high.
    if not extended and (structure_start or start_score >= 62) and wick < 55:
        score -= 12.0
    if extended:
        score += 12.0

    return clamp(score, 0.0, 100.0)


def breakout_survival_score(
    candles: list[Candle],
    direction: str,
    structure: Optional[StructureSnapshot] = None,
) -> float:
    """
    Score whether a fresh breakout/breakdown attempt looks survivable.

    Higher = better survival/start quality. This should not wait only for a
    fully confirmed breakout because Level 4 is designed to catch the start.
    """
    if len(candles) < 10:
        return 45.0

    d = normalize_direction(direction)
    last = candles[-1]
    price = _current_price(last)
    vol = _num(volume_ratio(candles, 5, 30), 1.0)

    swing_highs = getattr(structure, "swing_highs", None) if structure else None
    swing_lows = getattr(structure, "swing_lows", None) if structure else None
    swing_highs = swing_highs if swing_highs is not None else find_swing_highs(candles)
    swing_lows = swing_lows if swing_lows is not None else find_swing_lows(candles)

    resistance = getattr(structure, "nearest_resistance", None) if structure else None
    support = getattr(structure, "nearest_support", None) if structure else None
    resistance = resistance if resistance else nearest_resistance(price, swing_highs)
    support = support if support else nearest_support(price, swing_lows)

    start_score, _ = start_liquidity_score(candles, d, structure)
    sraw = _structure_start_raw(structure)
    structure_start = _bool(sraw.get("active"), False)
    atr_start = _bool(sraw.get("atr_expansion_start"), False)
    micro_shift = _bool(sraw.get("micro_structure_shift"), False)
    room_ok = _bool(sraw.get("room_to_target"), True)
    extended = _structure_extended(structure)

    score = 48.0

    # Do not require old-style full confirmation; give credit for start signals.
    if structure_start:
        score += 12.0
    if start_score >= 62:
        score += 12.0
    elif start_score >= 52:
        score += 6.0
    if atr_start:
        score += 6.0
    if micro_shift:
        score += 6.0

    if d == DIRECTION_LONG:
        if resistance and price > resistance:
            score += 12.0
        elif resistance and pct_distance(price, resistance) <= 0.45 and start_score >= 55:
            score += 5.0
        if upper_wick_pct(last) > 0.45:
            score -= 18.0
    elif d == DIRECTION_SHORT:
        if support and price < support:
            score += 12.0
        elif support and pct_distance(price, support) <= 0.45 and start_score >= 55:
            score += 5.0
        if lower_wick_pct(last) > 0.45:
            score -= 18.0

    if 1.10 <= vol <= 1.90:
        score += 12.0
    elif vol > 1.90:
        score += 5.0
    elif vol < 0.75:
        score -= 14.0

    fake_risk = fake_break_risk_score(candles, d, structure)
    sweep_risk = liquidity_sweep_score(candles, d, structure)
    score -= fake_risk * 0.24
    score -= max(0.0, sweep_risk - 45.0) * 0.12

    if not room_ok:
        score -= 16.0
    if extended:
        score = min(score, 42.0)

    return clamp(score, 0.0, 100.0)


def stop_hunt_detected(
    candles: list[Candle],
    direction: str,
    structure: Optional[StructureSnapshot] = None,
    threshold: float = 60.0,
) -> bool:
    """Return True when dangerous stop-hunt/sweep score is high."""
    return liquidity_sweep_score(candles, direction, structure) >= threshold


# =============================================================================
# Combined snapshot
# =============================================================================

def trap_risk_score(
    candles: list[Candle],
    direction: str,
    structure: Optional[StructureSnapshot] = None,
) -> tuple[float, list[str]]:
    """Combined trap risk score and reason codes."""
    reasons: list[str] = []
    d = normalize_direction(direction)
    sweep = liquidity_sweep_score(candles, d, structure)
    fake = fake_break_risk_score(candles, d, structure)
    wick = wick_rejection_score(candles, d)
    survival = breakout_survival_score(candles, d, structure)
    start_score, start_reasons = start_liquidity_score(candles, d, structure)
    extended = _structure_extended(structure)

    score = (sweep * 0.34) + (fake * 0.34) + (wick * 0.20) + ((100.0 - survival) * 0.12)

    # Legitimate start liquidity reduces soft trap noise, but not hard fake breaks.
    if not extended and start_score >= 65 and fake < 60 and wick < 60:
        score -= 16.0
        reasons.append("LIQ_START_REDUCES_TRAP_RISK")
    elif not extended and start_score >= 55 and fake < 65:
        score -= 8.0
        reasons.append("LIQ_START_SOFTENS_TRAP_RISK")

    if extended:
        score += 14.0
        reasons.append("LIQ_EXTENDED_MOVE_TRAP_RISK")

    if sweep >= 60:
        reasons.append("LIQUIDITY_SWEEP_RISK")
    elif sweep >= 35:
        reasons.append("LIQUIDITY_SWEEP_SOFT")

    if fake >= 60:
        reasons.append("FAKE_BREAK_RISK")
    elif fake >= 35:
        reasons.append("FAKE_BREAK_SOFT")

    if wick >= 55:
        reasons.append("WICK_REJECTION_RISK")

    if survival >= 65:
        reasons.append("BREAKOUT_SURVIVAL_OK")
    elif survival <= 40:
        reasons.append("BREAKOUT_SURVIVAL_WEAK")

    if start_score >= 65:
        reasons.append("LIQUIDITY_START_HIGH")
    elif start_score >= 55:
        reasons.append("LIQUIDITY_START_MEDIUM")

    reasons.extend(start_reasons)

    if not reasons:
        reasons.append("LIQUIDITY_NORMAL")

    return clamp(score, 0.0, 100.0), list(dict.fromkeys(reasons))


def build_liquidity_snapshot(
    market_snapshot: MarketSnapshot,
    direction: str,
    structure: Optional[StructureSnapshot] = None,
    sensor: Optional[SensorSnapshot] = None,
) -> LiquiditySnapshot:
    """Build LiquiditySnapshot from market candles and optional structure/sensor."""
    candles = list(getattr(market_snapshot, "candles", None) or [])
    d = normalize_direction(direction)

    sweep = liquidity_sweep_score(candles, d, structure)
    fake = fake_break_risk_score(candles, d, structure)
    wick = wick_rejection_score(candles, d)
    survival = breakout_survival_score(candles, d, structure)
    start_score, start_reasons = start_liquidity_score(candles, d, structure)
    trap, reasons = trap_risk_score(candles, d, structure)

    stop_hunt = sweep >= 60.0
    likely_trap = trap >= 65.0 or fake >= 70.0 or (_structure_extended(structure) and trap >= 55.0)
    liquidity_start_active = start_score >= 60.0 and survival >= 50.0 and not likely_trap

    sraw = _structure_start_raw(structure)
    raw = {
        "candle_count": len(candles),
        "sensor_price": getattr(sensor, "price", None) if sensor else None,
        "structure_score": getattr(structure, "structure_score", None) if structure else None,
        "structure_trend": getattr(structure, "trend", None) if structure else None,
        "structure_start_active": _structure_start_active(structure),
        "structure_start_score": _structure_start_score(structure),
        "structure_extended": _structure_extended(structure),
        "room_to_target": _structure_room_ok(structure),
        "atr_expansion_start": _bool(sraw.get("atr_expansion_start"), False),
        "micro_structure_shift": _bool(sraw.get("micro_structure_shift"), False),
        "volume_pressure_start": _bool(sraw.get("volume_pressure_start"), False),
        "supply_demand_reaction": _bool(sraw.get("supply_demand_reaction"), False),
        "start_liquidity_score": start_score,
        "liquidity_start_active": liquidity_start_active,
        "start_reasons": start_reasons,
    }

    return LiquiditySnapshot(
        symbol=getattr(market_snapshot, "symbol", ""),
        direction=d,
        trap_risk_score=trap,
        liquidity_sweep_score=sweep,
        fake_break_risk=fake,
        wick_rejection_score=wick,
        breakout_survival_score=survival,
        stop_hunt_detected=stop_hunt,
        likely_trap=likely_trap,
        reason_codes=list(dict.fromkeys(reasons)),
        raw=raw,
    )


def validate_liquidity_snapshot(snapshot: LiquiditySnapshot) -> dict[str, Any]:
    """Lightweight validation for liquidity snapshot."""
    errors: list[str] = []

    if not getattr(snapshot, "symbol", ""):
        errors.append("missing_symbol")
    if getattr(snapshot, "direction", None) not in {DIRECTION_LONG, DIRECTION_SHORT}:
        errors.append("invalid_direction")

    for key in [
        "trap_risk_score",
        "liquidity_sweep_score",
        "fake_break_risk",
        "wick_rejection_score",
        "breakout_survival_score",
    ]:
        value = safe_float(getattr(snapshot, key, None), None)
        if value is None or not (0.0 <= value <= 100.0):
            errors.append(f"invalid_{key}")

    raw = getattr(snapshot, "raw", None) or {}
    if not isinstance(raw, Mapping):
        errors.append("invalid_raw")
    else:
        for key in ["start_liquidity_score"]:
            value = safe_float(raw.get(key), None)
            if value is None or not (0.0 <= value <= 100.0):
                errors.append(f"invalid_raw_{key}")

    return {
        "valid": not errors,
        "errors": errors,
        "symbol": getattr(snapshot, "symbol", ""),
        "direction": getattr(snapshot, "direction", None),
        "trap_risk_score": getattr(snapshot, "trap_risk_score", None),
    }


__all__ = [
    "LIQUIDITY_ENGINE_VERSION",
    "recent_high",
    "recent_low",
    "candle_closed_back_inside",
    "candle_reclaimed_after_sweep",
    "swept_level",
    "swept_opposite_level",
    "wick_rejection_score",
    "start_liquidity_score",
    "liquidity_sweep_score",
    "fake_break_risk_score",
    "breakout_survival_score",
    "stop_hunt_detected",
    "trap_risk_score",
    "build_liquidity_snapshot",
    "validate_liquidity_snapshot",
]
