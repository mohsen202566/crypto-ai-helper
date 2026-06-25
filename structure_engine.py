"""
structure_engine.py
Level 4 / 1H Smart Scalp Bot

Market structure engine for 1H Smart Scalp.

Architecture lock:
- Provides raw structure analysis only.
- No AI final decision, no REAL/GHOST/REJECT, no TP/SL final decision,
  no exchange trading, no JSON state writes, no Telegram text.
- Allowed project imports: constants.py, utils.py, models.py, technical_sensors.py only.

Core rule:
- Hunt the start of a pump/dump movement, not the middle/end of it.
- Do not wait for late candle confirmation.
- Detect early structure pressure: compression, first ATR expansion,
  participation/volume pressure, micro-structure shift, supply/demand reaction,
  and enough room to target before nearby structure.
"""

from __future__ import annotations

from typing import Any, Optional

from constants import DIRECTION_LONG, DIRECTION_SHORT, SYSTEM_VERSION
from models import Candle, MarketSnapshot, SensorSnapshot, StructureSnapshot
from technical_sensors import atr, candles_to_closes, ema, pct_slope
from utils import clamp, normalize_direction, normalize_symbol, pct_distance, safe_float, safe_str


STRUCTURE_ENGINE_VERSION: str = SYSTEM_VERSION


# =============================================================================
# Small safe helpers
# =============================================================================

def _candle_volume(candle: Candle) -> float:
    """Read candle volume safely without requiring a strict Candle field contract."""
    return safe_float(getattr(candle, "volume", 0.0), 0.0) or 0.0


def _candle_body(candle: Candle) -> float:
    """Absolute candle body size."""
    return abs((safe_float(candle.close, 0.0) or 0.0) - (safe_float(candle.open, 0.0) or 0.0))


def _candle_range(candle: Candle) -> float:
    """High-low candle range."""
    return max((safe_float(candle.high, 0.0) or 0.0) - (safe_float(candle.low, 0.0) or 0.0), 0.0)


def _avg(values: list[float]) -> Optional[float]:
    """Safe average."""
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


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
    Classify broad structure trend.

    This is context only. It must not force late entries.
    Output: UPTREND / DOWNTREND / SIDEWAYS / UNKNOWN
    """
    closes = candles_to_closes(candles)
    if len(closes) < 30:
        return "UNKNOWN"

    ema20 = sensor.ema20 if sensor else ema(closes, 20)
    ema50 = sensor.ema50 if sensor else ema(closes, 50)
    price = safe_float(sensor.price, closes[-1] if closes else 0.0) if sensor else closes[-1]
    fast_slope = pct_slope(closes, 3)
    slow_slope = pct_slope(closes, 8)

    if ema20 is None or price is None:
        return "UNKNOWN"

    # Fast slope catches early structure turn; slow slope is only context.
    if ema50 is not None:
        if price > ema20 and ema20 >= ema50 and (fast_slope is None or fast_slope >= -0.08):
            return "UPTREND"
        if price < ema20 and ema20 <= ema50 and (fast_slope is None or fast_slope <= 0.08):
            return "DOWNTREND"

    if fast_slope is not None:
        if fast_slope > 0.18 or (slow_slope is not None and slow_slope > 0.35):
            return "UPTREND"
        if fast_slope < -0.18 or (slow_slope is not None and slow_slope < -0.35):
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


def detect_compression(candles: list[Candle], period: int = 10, range_atr_multiple: float = 1.9) -> bool:
    """
    Detect local compression before possible expansion.

    This is useful because many strong moves start after a tight range.
    """
    if len(candles) < max(period + 5, 20):
        return False

    atr_value = atr(candles, 14)
    if atr_value is None or atr_value <= 0:
        return False

    sample = candles[-period:]
    high = max(safe_float(c.high, 0.0) or 0.0 for c in sample)
    low = min(safe_float(c.low, 0.0) or 0.0 for c in sample)
    avg_body = _avg([_candle_body(c) for c in sample]) or 0.0

    return (high - low) <= atr_value * range_atr_multiple and avg_body <= atr_value * 0.55


def detect_atr_expansion_start(candles: list[Candle], short_period: int = 3, long_period: int = 12) -> bool:
    """
    Detect the beginning of volatility expansion, not a completed impulse.

    Uses recent candle ranges compared with older ranges and ATR.
    """
    if len(candles) < max(long_period + short_period + 2, 20):
        return False

    atr_value = atr(candles, 14)
    if atr_value is None or atr_value <= 0:
        return False

    recent = candles[-short_period:]
    previous = candles[-(short_period + long_period):-short_period]
    recent_avg_range = _avg([_candle_range(c) for c in recent]) or 0.0
    previous_avg_range = _avg([_candle_range(c) for c in previous]) or 0.0
    last_range = _candle_range(candles[-1])

    if previous_avg_range <= 0:
        return False

    first_expansion = recent_avg_range >= previous_avg_range * 1.18 and last_range >= atr_value * 0.65
    not_overextended = not is_move_already_extended(candles, DIRECTION_LONG) and not is_move_already_extended(candles, DIRECTION_SHORT)
    return first_expansion and not_overextended


def detect_volume_pressure_start(candles: list[Candle], short_period: int = 3, long_period: int = 12) -> bool:
    """
    Detect early participation/volume pressure.

    If volume is unavailable, returns False safely.
    """
    if len(candles) < max(short_period + long_period, 16):
        return False

    recent_volumes = [_candle_volume(c) for c in candles[-short_period:]]
    previous_volumes = [_candle_volume(c) for c in candles[-(short_period + long_period):-short_period]]
    if not any(recent_volumes) or not any(previous_volumes):
        return False

    recent_avg = _avg(recent_volumes) or 0.0
    previous_avg = _avg(previous_volumes) or 0.0
    if previous_avg <= 0:
        return False

    return recent_avg >= previous_avg * 1.20


def detect_micro_structure_shift(candles: list[Candle], direction: str, lookback: int = 6) -> bool:
    """
    Detect the first local structure shift.

    LONG: price starts reclaiming recent micro high.
    SHORT: price starts losing recent micro low.
    This avoids waiting for two confirmed candles after the move.
    """
    if len(candles) < lookback + 2:
        return False

    d = normalize_direction(direction)
    previous = candles[-(lookback + 1):-1]
    last = candles[-1]
    last_close = safe_float(last.close, 0.0) or 0.0
    last_open = safe_float(last.open, 0.0) or 0.0

    micro_high = max(safe_float(c.high, 0.0) or 0.0 for c in previous)
    micro_low = min(safe_float(c.low, 0.0) or 0.0 for c in previous)

    if d == DIRECTION_LONG:
        return last_close > micro_high or (last_close > last_open and last_close >= micro_high * 0.998)
    if d == DIRECTION_SHORT:
        return last_close < micro_low or (last_close < last_open and last_close <= micro_low * 1.002)
    return False


def detect_supply_demand_reaction(candles: list[Candle], direction: str) -> bool:
    """
    Detect early reaction from demand/supply zone.

    LONG: last candle rejects/recovers from demand area.
    SHORT: last candle rejects/turns down from supply area.
    """
    if len(candles) < 15:
        return False

    d = normalize_direction(direction)
    zones = detect_supply_demand_zones(candles)
    last = candles[-1]
    high = safe_float(last.high, 0.0) or 0.0
    low = safe_float(last.low, 0.0) or 0.0
    close = safe_float(last.close, 0.0) or 0.0
    open_ = safe_float(last.open, 0.0) or 0.0

    demand = zones.get("demand")
    supply = zones.get("supply")

    if d == DIRECTION_LONG and demand:
        touched = low <= safe_float(demand.get("high"), 0.0)
        recovered = close > open_ and close > safe_float(demand.get("level"), 0.0)
        return bool(touched and recovered)

    if d == DIRECTION_SHORT and supply:
        touched = high >= safe_float(supply.get("low"), 0.0)
        rejected = close < open_ and close < safe_float(supply.get("level"), 0.0)
        return bool(touched and rejected)

    return False


def is_impulse_market(candles: list[Candle], period: int = 5, atr_multiple: float = 1.45) -> bool:
    """
    Detect current impulse pressure earlier than the old late-confirmed impulse.

    This intentionally uses a shorter period so the structure layer can notice
    the beginning of expansion instead of waiting until the move is finished.
    """
    if len(candles) < max(period + 1, 15):
        return False

    atr_value = atr(candles, 14)
    if atr_value is None or atr_value <= 0:
        return False

    start = safe_float(candles[-period].close, 0.0) or 0.0
    end = safe_float(candles[-1].close, 0.0) or 0.0
    return abs(end - start) >= atr_value * atr_multiple


def is_move_already_extended(candles: list[Candle], direction: str, period: int = 5, atr_multiple: float = 2.15) -> bool:
    """
    Detect a move that is already too extended for a fresh entry.

    This is stricter than old late detection because the bot must avoid
    entering after pump/dump completion.
    """
    if len(candles) < max(period + 1, 20):
        return False

    d = normalize_direction(direction)
    atr_value = atr(candles, 14)
    if atr_value is None or atr_value <= 0:
        return False

    start = safe_float(candles[-period].close, 0.0) or 0.0
    end = safe_float(candles[-1].close, 0.0) or 0.0
    ema20_value = ema(candles_to_closes(candles), 20)
    move = end - start
    distance_from_ema = abs(end - ema20_value) if ema20_value is not None else 0.0

    same_color_count = 0
    for candle in reversed(candles[-4:]):
        close = safe_float(candle.close, 0.0) or 0.0
        open_ = safe_float(candle.open, 0.0) or 0.0
        if d == DIRECTION_LONG and close > open_:
            same_color_count += 1
        elif d == DIRECTION_SHORT and close < open_:
            same_color_count += 1
        else:
            break

    if d == DIRECTION_LONG:
        directional_extension = move >= atr_value * atr_multiple
    elif d == DIRECTION_SHORT:
        directional_extension = -move >= atr_value * atr_multiple
    else:
        directional_extension = abs(move) >= atr_value * atr_multiple

    ema_extension = distance_from_ema >= atr_value * 1.35
    candle_extension = same_color_count >= 3 and abs(move) >= atr_value * 1.25

    return bool((directional_extension and same_color_count >= 3) or (ema_extension and same_color_count >= 4) or candle_extension)


def is_late_move(candles: list[Candle], direction: str, period: int = 5, atr_multiple: float = 2.15) -> bool:
    """
    Detect late/exhausted move.

    Level 4 should avoid entering in the middle/end of a fully extended move.
    """
    return is_move_already_extended(candles, direction, period=period, atr_multiple=atr_multiple)


def has_room_to_target(candles: list[Candle], direction: str, min_atr_room: float = 0.85) -> bool:
    """Check whether price has enough room before nearest opposite structure."""
    if not candles:
        return False

    d = normalize_direction(direction)
    price = safe_float(candles[-1].close, 0.0) or 0.0
    atr_value = atr(candles, 14) or 0.0
    if price <= 0 or atr_value <= 0:
        return False

    highs = find_swing_highs(candles)
    lows = find_swing_lows(candles)
    support = nearest_support(price, lows)
    resistance = nearest_resistance(price, highs)

    if d == DIRECTION_LONG and resistance is not None:
        return (resistance - price) >= atr_value * min_atr_room
    if d == DIRECTION_SHORT and support is not None:
        return (price - support) >= atr_value * min_atr_room

    # If no nearby opposite structure exists, do not block structure freshness.
    return True


def detect_move_start_zone(candles: list[Candle], direction: str) -> dict[str, Any]:
    """
    Detect whether current structure is near the start of movement.

    This is a raw sensor-style structure result. It does not decide trade action.
    """
    d = normalize_direction(direction)
    compression = detect_compression(candles)
    atr_start = detect_atr_expansion_start(candles)
    volume_start = detect_volume_pressure_start(candles)
    micro_shift = detect_micro_structure_shift(candles, d)
    sd_reaction = detect_supply_demand_reaction(candles, d)
    room_ok = has_room_to_target(candles, d)
    extended = is_move_already_extended(candles, d)

    score = 0.0
    reasons: list[str] = []

    if compression:
        score += 18.0
        reasons.append("COMPRESSION_BEFORE_MOVE")
    if atr_start:
        score += 22.0
        reasons.append("ATR_EXPANSION_START")
    if volume_start:
        score += 14.0
        reasons.append("VOLUME_PRESSURE_START")
    if micro_shift:
        score += 22.0
        reasons.append("MICRO_STRUCTURE_SHIFT")
    if sd_reaction:
        score += 16.0
        reasons.append("SUPPLY_DEMAND_REACTION")
    if room_ok:
        score += 12.0
        reasons.append("ROOM_TO_TARGET_OK")
    else:
        score -= 16.0
        reasons.append("ROOM_TO_TARGET_WEAK")
    if extended:
        score -= 45.0
        reasons.append("MOVE_ALREADY_EXTENDED")

    # True means enough early evidence exists and movement is not already exhausted.
    active = score >= 45.0 and not extended and (micro_shift or atr_start or sd_reaction)

    if active:
        reasons.append("MOVE_START_ZONE")
    elif extended:
        reasons.append("NOT_START_ZONE_EXTENDED")
    else:
        reasons.append("START_ZONE_NOT_CONFIRMED")

    return {
        "active": bool(active),
        "score": clamp(score, 0.0, 100.0),
        "compression": compression,
        "atr_expansion_start": atr_start,
        "volume_pressure_start": volume_start,
        "micro_structure_shift": micro_shift,
        "supply_demand_reaction": sd_reaction,
        "room_to_target": room_ok,
        "move_already_extended": extended,
        "reason_codes": reasons,
    }


def fresh_zone_score(candles: list[Candle], direction: str) -> float:
    """
    Score whether price is close to the start of movement and still has room.

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
    start_zone = detect_move_start_zone(candles, d)

    score = 48.0

    if start_zone["active"]:
        score += 24.0
    elif start_zone["move_already_extended"]:
        score -= 35.0
    else:
        score += clamp(safe_float(start_zone["score"], 0.0) * 0.18, 0.0, 12.0)

    if atr_value > 0:
        if d == DIRECTION_LONG and resistance is not None:
            distance_atr = (resistance - price) / atr_value
            score += clamp(distance_atr * 8.0, -18.0, 24.0)
        elif d == DIRECTION_SHORT and support is not None:
            distance_atr = (price - support) / atr_value
            score += clamp(distance_atr * 8.0, -18.0, 24.0)

    if is_range_market(candles) and not start_zone["atr_expansion_start"]:
        score -= 12.0

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
    """Score broad trend context for requested direction."""
    d = normalize_direction(direction)
    trend = safe_str(trend).upper()

    if trend == "UPTREND" and d == DIRECTION_LONG:
        return 70.0
    if trend == "DOWNTREND" and d == DIRECTION_SHORT:
        return 70.0
    if trend == "SIDEWAYS":
        return 50.0
    if trend == "UPTREND" and d == DIRECTION_SHORT:
        return 36.0
    if trend == "DOWNTREND" and d == DIRECTION_LONG:
        return 36.0
    return 45.0


def score_structure(
    candles: list[Candle],
    direction: str,
    sensor: Optional[SensorSnapshot] = None,
) -> tuple[float, list[str]]:
    """Return structure score and reason codes."""
    reasons: list[str] = []
    d = normalize_direction(direction)
    trend = classify_trend(candles, sensor)
    score = score_trend_alignment(trend, d)
    start_zone = detect_move_start_zone(candles, d)

    if trend == "UPTREND":
        reasons.append("STRUCTURE_UPTREND")
    elif trend == "DOWNTREND":
        reasons.append("STRUCTURE_DOWNTREND")
    elif trend == "SIDEWAYS":
        reasons.append("STRUCTURE_SIDEWAYS")
    else:
        reasons.append("STRUCTURE_UNKNOWN")

    reasons.extend(start_zone["reason_codes"])

    if start_zone["active"]:
        score += 26.0
    else:
        score += clamp(safe_float(start_zone["score"], 0.0) * 0.12, 0.0, 10.0)

    if is_range_market(candles):
        if start_zone["atr_expansion_start"] or start_zone["micro_structure_shift"]:
            score += 6.0
            reasons.append("RANGE_BREAK_ATTEMPT")
        else:
            score -= 12.0
            reasons.append("RANGE_MARKET")

    if is_impulse_market(candles):
        if start_zone["move_already_extended"]:
            score -= 18.0
            reasons.append("IMPULSE_ALREADY_EXTENDED")
        else:
            score += 10.0
            reasons.append("EARLY_IMPULSE_PRESSURE")

    if is_late_move(candles, d):
        score -= 34.0
        reasons.append("LATE_MOVE_RISK")
    else:
        score += 10.0
        reasons.append("NOT_LATE_MOVE")

    fz = fresh_zone_score(candles, d)
    if fz >= 68:
        score += 10.0
        reasons.append("FRESH_ZONE_OK")
    elif fz <= 35:
        score -= 12.0
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
    symbol = normalize_symbol(market_snapshot.symbol)
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
    start_zone = detect_move_start_zone(candles, d)
    structure_score, reasons = score_structure(candles, d, sensor)

    raw = {
        "price": price,
        "support_distance_pct": pct_distance(price, support) if support else None,
        "resistance_distance_pct": pct_distance(price, resistance) if resistance else None,
        "candle_count": len(candles),
        "move_start_zone": start_zone,
        "compression": start_zone["compression"],
        "atr_expansion_start": start_zone["atr_expansion_start"],
        "volume_pressure_start": start_zone["volume_pressure_start"],
        "micro_structure_shift": start_zone["micro_structure_shift"],
        "supply_demand_reaction": start_zone["supply_demand_reaction"],
        "room_to_target": start_zone["room_to_target"],
        "move_already_extended": start_zone["move_already_extended"],
    }

    return StructureSnapshot(
        symbol=symbol,
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
    "detect_compression",
    "detect_atr_expansion_start",
    "detect_volume_pressure_start",
    "detect_micro_structure_shift",
    "detect_supply_demand_reaction",
    "is_impulse_market",
    "is_move_already_extended",
    "is_late_move",
    "has_room_to_target",
    "detect_move_start_zone",
    "fresh_zone_score",
    "detect_supply_demand_zones",
    "score_trend_alignment",
    "score_structure",
    "build_structure_snapshot",
    "validate_structure_snapshot",
]
