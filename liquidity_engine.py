"""
liquidity_engine.py
Level 4 / 1H Smart Scalp Bot

Liquidity / trap risk engine.

Architecture lock:
- Scores stop hunts, liquidity sweeps, fake breaks, wick rejection, and breakout survival.
- Does not make final REAL/GHOST/REJECT decisions.
- Does not place orders, monitor positions, write JSON state, or build Telegram text.
- Allowed project imports: constants.py, utils.py, models.py, structure_engine.py, technical_sensors.py only.
"""

from __future__ import annotations

from typing import Any, Optional

from constants import DIRECTION_LONG, DIRECTION_SHORT, SYSTEM_VERSION
from models import Candle, LiquiditySnapshot, MarketSnapshot, SensorSnapshot, StructureSnapshot
from structure_engine import find_swing_highs, find_swing_lows, nearest_resistance, nearest_support
from technical_sensors import atr, lower_wick_pct, upper_wick_pct, volume_ratio
from utils import clamp, normalize_direction, pct_distance, safe_float


LIQUIDITY_ENGINE_VERSION: str = SYSTEM_VERSION


# =============================================================================
# Basic liquidity helpers
# =============================================================================

def recent_high(candles: list[Candle], period: int = 20) -> Optional[float]:
    if not candles:
        return None
    sample = candles[-period:] if period > 0 else candles
    return max(safe_float(c.high, 0.0) or 0.0 for c in sample)


def recent_low(candles: list[Candle], period: int = 20) -> Optional[float]:
    if not candles:
        return None
    sample = candles[-period:] if period > 0 else candles
    return min(safe_float(c.low, 0.0) or 0.0 for c in sample)


def candle_closed_back_inside(candle: Candle, level: float, direction: str) -> bool:
    """
    Detect fake break close back inside.

    LONG trap: wick above resistance but close below level.
    SHORT trap: wick below support but close above level.
    """
    d = normalize_direction(direction)
    close = safe_float(candle.close, 0.0) or 0.0
    high = safe_float(candle.high, 0.0) or 0.0
    low = safe_float(candle.low, 0.0) or 0.0

    if d == DIRECTION_LONG:
        return high > level and close < level
    if d == DIRECTION_SHORT:
        return low < level and close > level
    return False


def swept_level(candle: Candle, level: Optional[float], direction: str, tolerance_pct: float = 0.15) -> bool:
    """Return True if candle swept a nearby structural level."""
    if level is None or level <= 0:
        return False

    d = normalize_direction(direction)
    high = safe_float(candle.high, 0.0) or 0.0
    low = safe_float(candle.low, 0.0) or 0.0
    tol = level * (tolerance_pct / 100.0)

    if d == DIRECTION_LONG:
        return high >= level - tol
    if d == DIRECTION_SHORT:
        return low <= level + tol
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
    body_range = abs((safe_float(last.close, 0.0) or 0.0) - (safe_float(last.open, 0.0) or 0.0))

    score = 0.0
    if d == DIRECTION_LONG:
        score = upper * 100.0
    elif d == DIRECTION_SHORT:
        score = lower * 100.0

    # Strong wick with tiny body is more suspicious.
    candle_range = (safe_float(last.high, 0.0) or 0.0) - (safe_float(last.low, 0.0) or 0.0)
    if candle_range > 0 and body_range / candle_range < 0.25:
        score += 15.0

    return clamp(score, 0.0, 100.0)


def liquidity_sweep_score(
    candles: list[Candle],
    direction: str,
    structure: Optional[StructureSnapshot] = None,
) -> float:
    """
    Score possible liquidity sweep near S/R.

    Higher score = more sweep/trap risk against intended direction.
    """
    if len(candles) < 5:
        return 0.0

    d = normalize_direction(direction)
    last = candles[-1]
    price = safe_float(last.close, 0.0) or 0.0
    atr_value = atr(candles, 14) or 0.0

    swing_highs = structure.swing_highs if structure else find_swing_highs(candles)
    swing_lows = structure.swing_lows if structure else find_swing_lows(candles)
    support = structure.nearest_support if structure else nearest_support(price, swing_lows)
    resistance = structure.nearest_resistance if structure else nearest_resistance(price, swing_highs)

    score = 0.0

    if d == DIRECTION_LONG and resistance:
        if swept_level(last, resistance, DIRECTION_LONG):
            score += 35.0
            if candle_closed_back_inside(last, resistance, DIRECTION_LONG):
                score += 35.0

        dist = pct_distance(price, resistance)
        if dist <= 0.35:
            score += 12.0

    elif d == DIRECTION_SHORT and support:
        if swept_level(last, support, DIRECTION_SHORT):
            score += 35.0
            if candle_closed_back_inside(last, support, DIRECTION_SHORT):
                score += 35.0

        dist = pct_distance(price, support)
        if dist <= 0.35:
            score += 12.0

    # Big wick relative to ATR increases sweep suspicion.
    if atr_value > 0:
        wick_size = 0.0
        if d == DIRECTION_LONG:
            wick_size = (safe_float(last.high, 0.0) or 0.0) - max(safe_float(last.open, 0.0) or 0.0, safe_float(last.close, 0.0) or 0.0)
        elif d == DIRECTION_SHORT:
            wick_size = min(safe_float(last.open, 0.0) or 0.0, safe_float(last.close, 0.0) or 0.0) - (safe_float(last.low, 0.0) or 0.0)
        if wick_size >= atr_value * 0.45:
            score += 18.0

    return clamp(score, 0.0, 100.0)


def fake_break_risk_score(
    candles: list[Candle],
    direction: str,
    structure: Optional[StructureSnapshot] = None,
) -> float:
    """Score fake breakout/breakdown risk."""
    if len(candles) < 10:
        return 0.0

    d = normalize_direction(direction)
    last = candles[-1]
    prev = candles[-2]
    price = safe_float(last.close, 0.0) or 0.0
    vol_ratio = volume_ratio(candles, 5, 30) or 1.0

    swing_highs = structure.swing_highs if structure else find_swing_highs(candles)
    swing_lows = structure.swing_lows if structure else find_swing_lows(candles)
    resistance = structure.nearest_resistance if structure else nearest_resistance(price, swing_highs)
    support = structure.nearest_support if structure else nearest_support(price, swing_lows)

    score = 0.0

    if d == DIRECTION_LONG and resistance:
        prev_close = safe_float(prev.close, 0.0) or 0.0
        last_close = safe_float(last.close, 0.0) or 0.0
        last_high = safe_float(last.high, 0.0) or 0.0
        if prev_close <= resistance and last_high > resistance and last_close <= resistance:
            score += 55.0
        if last_close > resistance and vol_ratio < 0.8:
            score += 20.0

    elif d == DIRECTION_SHORT and support:
        prev_close = safe_float(prev.close, 0.0) or 0.0
        last_close = safe_float(last.close, 0.0) or 0.0
        last_low = safe_float(last.low, 0.0) or 0.0
        if prev_close >= support and last_low < support and last_close >= support:
            score += 55.0
        if last_close < support and vol_ratio < 0.8:
            score += 20.0

    if wick_rejection_score(candles, d) >= 55:
        score += 15.0

    return clamp(score, 0.0, 100.0)


def breakout_survival_score(
    candles: list[Candle],
    direction: str,
    structure: Optional[StructureSnapshot] = None,
) -> float:
    """
    Score whether breakout/breakdown looks survivable.

    Higher = better survival/confirmation. This is not a final entry decision.
    """
    if len(candles) < 10:
        return 45.0

    d = normalize_direction(direction)
    last = candles[-1]
    price = safe_float(last.close, 0.0) or 0.0
    vol_ratio = volume_ratio(candles, 5, 30) or 1.0

    swing_highs = structure.swing_highs if structure else find_swing_highs(candles)
    swing_lows = structure.swing_lows if structure else find_swing_lows(candles)
    resistance = structure.nearest_resistance if structure else nearest_resistance(price, swing_highs)
    support = structure.nearest_support if structure else nearest_support(price, swing_lows)

    score = 50.0

    if d == DIRECTION_LONG:
        if resistance and price > resistance:
            score += 18.0
        if upper_wick_pct(last) > 0.45:
            score -= 18.0
    elif d == DIRECTION_SHORT:
        if support and price < support:
            score += 18.0
        if lower_wick_pct(last) > 0.45:
            score -= 18.0

    if vol_ratio >= 1.2:
        score += 14.0
    elif vol_ratio < 0.75:
        score -= 12.0

    fake_risk = fake_break_risk_score(candles, d, structure)
    score -= fake_risk * 0.25

    return clamp(score, 0.0, 100.0)


def stop_hunt_detected(
    candles: list[Candle],
    direction: str,
    structure: Optional[StructureSnapshot] = None,
    threshold: float = 60.0,
) -> bool:
    """Return True when stop-hunt/sweep score is high."""
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
    sweep = liquidity_sweep_score(candles, direction, structure)
    fake = fake_break_risk_score(candles, direction, structure)
    wick = wick_rejection_score(candles, direction)
    survival = breakout_survival_score(candles, direction, structure)

    score = (sweep * 0.35) + (fake * 0.35) + (wick * 0.20) + ((100.0 - survival) * 0.10)

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

    if not reasons:
        reasons.append("LIQUIDITY_NORMAL")

    return clamp(score, 0.0, 100.0), reasons


def build_liquidity_snapshot(
    market_snapshot: MarketSnapshot,
    direction: str,
    structure: Optional[StructureSnapshot] = None,
    sensor: Optional[SensorSnapshot] = None,
) -> LiquiditySnapshot:
    """Build LiquiditySnapshot from market candles and optional structure/sensor."""
    candles = list(market_snapshot.candles or [])
    d = normalize_direction(direction)

    sweep = liquidity_sweep_score(candles, d, structure)
    fake = fake_break_risk_score(candles, d, structure)
    wick = wick_rejection_score(candles, d)
    survival = breakout_survival_score(candles, d, structure)
    trap, reasons = trap_risk_score(candles, d, structure)
    stop_hunt = sweep >= 60.0
    likely_trap = trap >= 65.0 or fake >= 70.0

    raw = {
        "candle_count": len(candles),
        "sensor_price": sensor.price if sensor else None,
        "structure_score": structure.structure_score if structure else None,
        "structure_trend": structure.trend if structure else None,
    }

    return LiquiditySnapshot(
        symbol=market_snapshot.symbol,
        direction=d,
        trap_risk_score=trap,
        liquidity_sweep_score=sweep,
        fake_break_risk=fake,
        wick_rejection_score=wick,
        breakout_survival_score=survival,
        stop_hunt_detected=stop_hunt,
        likely_trap=likely_trap,
        reason_codes=reasons,
        raw=raw,
    )


def validate_liquidity_snapshot(snapshot: LiquiditySnapshot) -> dict[str, Any]:
    """Lightweight validation for liquidity snapshot."""
    errors: list[str] = []

    if not snapshot.symbol:
        errors.append("missing_symbol")
    if snapshot.direction not in {DIRECTION_LONG, DIRECTION_SHORT}:
        errors.append("invalid_direction")

    for key in [
        "trap_risk_score",
        "liquidity_sweep_score",
        "fake_break_risk",
        "wick_rejection_score",
        "breakout_survival_score",
    ]:
        value = safe_float(getattr(snapshot, key), -1.0)
        if value is None or not (0.0 <= value <= 100.0):
            errors.append(f"invalid_{key}")

    return {
        "valid": not errors,
        "errors": errors,
        "symbol": snapshot.symbol,
        "direction": snapshot.direction,
        "trap_risk_score": snapshot.trap_risk_score,
    }


__all__ = [
    "LIQUIDITY_ENGINE_VERSION",
    "recent_high",
    "recent_low",
    "candle_closed_back_inside",
    "swept_level",
    "wick_rejection_score",
    "liquidity_sweep_score",
    "fake_break_risk_score",
    "breakout_survival_score",
    "stop_hunt_detected",
    "trap_risk_score",
    "build_liquidity_snapshot",
    "validate_liquidity_snapshot",
]
