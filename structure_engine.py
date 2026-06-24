"""
structure_engine.py
Level 4 / 1H Smart Scalp Bot

Market structure engine for 1H Smart Scalp.

Architecture lock:
- Provides raw structure analysis only.
- No AI final decision, no REAL/GHOST/REJECT, no TP/SL final decision,
  no exchange trading, no JSON state writes, no Telegram text.
- Allowed project imports: constants.py, utils.py, models.py, technical_sensors.py only.
"""

from __future__ import annotations

from typing import Any, Optional

from constants import DIRECTION_LONG, DIRECTION_SHORT, SYSTEM_VERSION
from models import Candle, MarketSnapshot, SensorSnapshot, StructureSnapshot
from technical_sensors import atr, candles_to_closes, ema, pct_slope
from utils import clamp, normalize_direction, normalize_symbol, pct_distance, safe_float, safe_str


STRUCTURE_ENGINE_VERSION: str = SYSTEM_VERSION


# =============================================================================
# Swing detection
# =============================================================================

def find_swing_highs(candles: list[Candle], lookback: int = 2, limit: int = 10) -> list[float]:
    """Find recent swing highs using left/right lookback."""
    if lookback <= 0 or len(candles) < (lookback * 2) + 1:
        return []

    highs: list[float] = []
    for i in range(lookback, len(candles) - lookback):
        current = safe_float(candles[i].high, 0.0) or 0.0
        left = [safe_float(candles[j].high, 0.0) or 0.0 for j in range(i - lookback, i)]
        right = [safe_float(candles[j].high, 0.0) or 0.0 for j in range(i + 1, i + lookback + 1)]
        if current > max(left) and current >= max(right):
            highs.append(current)

    return highs[-limit:]


def find_swing_lows(candles: list[Candle], lookback: int = 2, limit: int = 10) -> list[float]:
    """Find recent swing lows using left/right lookback."""
    if lookback <= 0 or len(candles) < (lookback * 2) + 1:
        return []

    lows: list[float] = []
    for i in range(lookback, len(candles) - lookback):
        current = safe_float(candles[i].low, 0.0) or 0.0
        left = [safe_float(candles[j].low, 0.0) or 0.0 for j in range(i - lookback, i)]
        right = [safe_float(candles[j].low, 0.0) or 0.0 for j in range(i + 1, i + lookback + 1)]
        if current < min(left) and current <= min(right):
            lows.append(current)

    return lows[-limit:]


def nearest_support(price: float, swing_lows: list[float]) -> Optional[float]:
    """Return closest swing low below or equal to price."""
    candidates = [level for level in swing_lows if level <= price and level > 0]
    if not candidates:
        return None
    return max(candidates)


def nearest_resistance(price: float, swing_highs: list[float]) -> Optional[float]:
    """Return closest swing high above or equal to price."""
    candidates = [level for level in swing_highs if level >= price and level > 0]
    if not candidates:
        return None
    return min(candidates)


# =============================================================================
# Structure classification
# =============================================================================

def classify_trend(candles: list[Candle], sensor: Optional[SensorSnapshot] = None) -> str:
    """
    Classify trend from EMA slope and price location.

    Output: UPTREND / DOWNTREND / SIDEWAYS / UNKNOWN
    """
    closes = candles_to_closes(candles)
    if len(closes) < 30:
        return "UNKNOWN"

    ema20 = sensor.ema20 if sensor else ema(closes, 20)
    ema50 = sensor.ema50 if sensor else ema(closes, 50)
    price = safe_float(sensor.price, closes[-1] if closes else 0.0) if sensor else closes[-1]
    slope20 = pct_slope(closes, 8)

    if ema20 is None or price is None:
        return "UNKNOWN"

    if ema50 is not None:
        if price > ema20 > ema50 and (slope20 is None or slope20 > 0):
            return "UPTREND"
        if price < ema20 < ema50 and (slope20 is None or slope20 < 0):
            return "DOWNTREND"

    if slope20 is not None:
        if slope20 > 0.35:
            return "UPTREND"
        if slope20 < -0.35:
            return "DOWNTREND"

    return "SIDEWAYS"


def is_range_market(candles: list[Candle], period: int = 24, range_atr_multiple: float = 3.0) -> bool:
    """Detect tight range when high-low span is small vs ATR."""
    if len(candles) < max(period, 15):
        return False

    sample = candles[-period:]
    high = max(safe_float(c.high, 0.0) or 0.0 for c in sample)
    low = min(safe_float(c.low, 0.0) or 0.0 for c in sample)
    atr_value = atr(candles, 14)

    if atr_value is None or atr_value <= 0:
        return False

    return (high - low) <= (atr_value * range_atr_multiple)


def is_impulse_market(candles: list[Candle], period: int = 8, atr_multiple: float = 2.2) -> bool:
    """Detect impulse when recent net movement is large vs ATR."""
    if len(candles) < max(period + 1, 15):
        return False

    atr_value = atr(candles, 14)
    if atr_value is None or atr_value <= 0:
        return False

    start = safe_float(candles[-period].close, 0.0) or 0.0
    end = safe_float(candles[-1].close, 0.0) or 0.0
    return abs(end - start) >= atr_value * atr_multiple


def is_late_move(candles: list[Candle], direction: str, period: int = 6, atr_multiple: float = 2.4) -> bool:
    """
    Detect late/exhausted move.

    Level 4 should avoid entering in the middle/end of a fully extended move.
    """
    if len(candles) < max(period + 1, 15):
        return False

    d = normalize_direction(direction)
    atr_value = atr(candles, 14)
    if atr_value is None or atr_value <= 0:
        return False

    start = safe_float(candles[-period].close, 0.0) or 0.0
    end = safe_float(candles[-1].close, 0.0) or 0.0
    move = end - start

    if d == DIRECTION_LONG:
        return move >= atr_value * atr_multiple
    if d == DIRECTION_SHORT:
        return -move >= atr_value * atr_multiple
    return abs(move) >= atr_value * atr_multiple


def fresh_zone_score(candles: list[Candle], direction: str) -> float:
    """
    Score whether price is not too extended and has room from nearby structure.

    Higher score = fresher/better structure location.
    """
    if not candles:
        return 0.0

    d = normalize_direction(direction)
    price = safe_float(candles[-1].close, 0.0) or 0.0
    highs = find_swing_highs(candles)
    lows = find_swing_lows(candles)
    support = nearest_support(price, lows)
    resistance = nearest_resistance(price, highs)
    atr_value = atr(candles, 14) or 0.0

    score = 50.0

    if is_late_move(candles, d):
        score -= 25.0
    else:
        score += 15.0

    if atr_value > 0:
        if d == DIRECTION_LONG and resistance is not None:
            distance_atr = (resistance - price) / atr_value
            score += clamp(distance_atr * 8.0, -15.0, 25.0)
        elif d == DIRECTION_SHORT and support is not None:
            distance_atr = (price - support) / atr_value
            score += clamp(distance_atr * 8.0, -15.0, 25.0)

    if is_range_market(candles):
        score -= 15.0

    return clamp(score, 0.0, 100.0)


def detect_supply_demand_zones(candles: list[Candle], lookback: int = 40) -> dict[str, Optional[dict[str, Any]]]:
    """
    Lightweight supply/demand zones from recent swing extremes.

    This is intentionally simple; detailed TP/SL and AI layers may refine later.
    """
    if len(candles) < 10:
        return {"supply": None, "demand": None}

    sample = candles[-lookback:]
    highs = find_swing_highs(sample, lookback=2, limit=3)
    lows = find_swing_lows(sample, lookback=2, limit=3)
    atr_value = atr(candles, 14) or 0.0
    buffer = atr_value * 0.35 if atr_value > 0 else 0.0

    supply = None
    demand = None

    if highs:
        level = max(highs)
        supply = {
            "type": "SUPPLY",
            "low": level - buffer,
            "high": level + buffer,
            "level": level,
        }

    if lows:
        level = min(lows)
        demand = {
            "type": "DEMAND",
            "low": level - buffer,
            "high": level + buffer,
            "level": level,
        }

    return {"supply": supply, "demand": demand}


# =============================================================================
# Scoring
# =============================================================================

def score_trend_alignment(trend: str, direction: str) -> float:
    """Score trend alignment for requested direction."""
    d = normalize_direction(direction)
    trend = safe_str(trend).upper()

    if trend == "UPTREND" and d == DIRECTION_LONG:
        return 80.0
    if trend == "DOWNTREND" and d == DIRECTION_SHORT:
        return 80.0
    if trend == "SIDEWAYS":
        return 48.0
    if trend == "UPTREND" and d == DIRECTION_SHORT:
        return 30.0
    if trend == "DOWNTREND" and d == DIRECTION_LONG:
        return 30.0
    return 45.0


def score_structure(
    candles: list[Candle],
    direction: str,
    sensor: Optional[SensorSnapshot] = None,
) -> tuple[float, list[str]]:
    """Return structure score and reason codes."""
    reasons: list[str] = []
    trend = classify_trend(candles, sensor)
    score = score_trend_alignment(trend, direction)

    if trend == "UPTREND":
        reasons.append("STRUCTURE_UPTREND")
    elif trend == "DOWNTREND":
        reasons.append("STRUCTURE_DOWNTREND")
    elif trend == "SIDEWAYS":
        reasons.append("STRUCTURE_SIDEWAYS")
    else:
        reasons.append("STRUCTURE_UNKNOWN")

    if is_range_market(candles):
        score -= 12.0
        reasons.append("RANGE_MARKET")

    if is_impulse_market(candles):
        score += 8.0
        reasons.append("IMPULSE_MARKET")

    if is_late_move(candles, direction):
        score -= 20.0
        reasons.append("LATE_MOVE_RISK")
    else:
        score += 8.0
        reasons.append("NOT_LATE_MOVE")

    fz = fresh_zone_score(candles, direction)
    if fz >= 65:
        score += 8.0
        reasons.append("FRESH_ZONE_OK")
    elif fz <= 35:
        score -= 8.0
        reasons.append("FRESH_ZONE_WEAK")

    return clamp(score, 0.0, 100.0), reasons


# =============================================================================
# Snapshot builder
# =============================================================================

def build_structure_snapshot(
    market_snapshot: MarketSnapshot,
    direction: str,
    sensor: Optional[SensorSnapshot] = None,
) -> StructureSnapshot:
    """Build StructureSnapshot from market candles and optional sensor data."""
    candles = list(market_snapshot.candles or [])
    d = normalize_direction(direction)
    price = safe_float(market_snapshot.current_price, 0.0) or (safe_float(candles[-1].close, 0.0) if candles else 0.0) or 0.0

    swing_highs = find_swing_highs(candles)
    swing_lows = find_swing_lows(candles)
    support = nearest_support(price, swing_lows)
    resistance = nearest_resistance(price, swing_highs)
    zones = detect_supply_demand_zones(candles)
    trend = classify_trend(candles, sensor)
    range_state = is_range_market(candles)
    impulse_state = is_impulse_market(candles)
    late_state = is_late_move(candles, d)
    fz_score = fresh_zone_score(candles, d)
    structure_score, reasons = score_structure(candles, d, sensor)

    raw = {
        "price": price,
        "support_distance_pct": pct_distance(price, support) if support else None,
        "resistance_distance_pct": pct_distance(price, resistance) if resistance else None,
        "candle_count": len(candles),
    }

    return StructureSnapshot(
        symbol=market_snapshot.symbol,
        direction=d,
        trend=trend,
        structure_score=structure_score,
        is_range=range_state,
        is_impulse=impulse_state,
        is_late_move=late_state,
        fresh_zone_score=fz_score,
        nearest_support=support,
        nearest_resistance=resistance,
        supply_zone=zones["supply"],
        demand_zone=zones["demand"],
        swing_highs=swing_highs,
        swing_lows=swing_lows,
        reason_codes=reasons,
        raw=raw,
    )


def validate_structure_snapshot(snapshot: StructureSnapshot) -> dict[str, Any]:
    """Lightweight validation for structure snapshot."""
    errors: list[str] = []
    if not snapshot.symbol:
        errors.append("missing_symbol")
    if snapshot.direction not in {DIRECTION_LONG, DIRECTION_SHORT}:
        errors.append("invalid_direction")
    if not (0.0 <= safe_float(snapshot.structure_score, -1.0) <= 100.0):
        errors.append("invalid_structure_score")
    if not (0.0 <= safe_float(snapshot.fresh_zone_score, -1.0) <= 100.0):
        errors.append("invalid_fresh_zone_score")

    return {
        "valid": not errors,
        "errors": errors,
        "symbol": snapshot.symbol,
        "direction": snapshot.direction,
        "trend": snapshot.trend,
    }


__all__ = [
    "STRUCTURE_ENGINE_VERSION",
    "find_swing_highs",
    "find_swing_lows",
    "nearest_support",
    "nearest_resistance",
    "classify_trend",
    "is_range_market",
    "is_impulse_market",
    "is_late_move",
    "fresh_zone_score",
    "detect_supply_demand_zones",
    "score_trend_alignment",
    "score_structure",
    "build_structure_snapshot",
    "validate_structure_snapshot",
]
