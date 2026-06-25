"""
momentum_engine.py
Level 4 / 1H Smart Scalp Bot

Momentum engine for 1H Smart Scalp.

Architecture lock:
- Scores momentum, continuation, acceleration, weakness, freshness, and chase risk only.
- No final AI decision, no REAL/GHOST/REJECT, no TP/SL final decision,
  no exchange trading, no JSON state writes, no Telegram text.
- Allowed project imports: constants.py, utils.py, models.py, technical_sensors.py only.

Core rule:
- Hunt the start of a pump/dump movement, not the middle/end of it.
- Do not reward old confirmed momentum just because RSI/MACD/Power are already strong.
- Prefer early evidence: RSI slope turn, MACD histogram acceleration, short power shift,
  volume/participation expansion, EMA/VWAP reclaim/loss, and clean candle pressure.
- Penalize late/finished movement: exhaustion, fading slope, weak power, wick rejection,
  volume climax/dryness, overheat, and old move-age proxy.
"""

from __future__ import annotations

from typing import Any, Optional

from constants import DIRECTION_LONG, DIRECTION_SHORT, SYSTEM_VERSION
from models import Candle, MarketSnapshot, MomentumSnapshot, SensorSnapshot
from technical_sensors import (
    buy_sell_power,
    candles_to_closes,
    ema,
    macd_values,
    rsi_series,
    slope,
    volume_ratio,
)
from utils import clamp, normalize_direction, safe_float


MOMENTUM_ENGINE_VERSION: str = SYSTEM_VERSION


# =============================================================================
# Safe helpers
# =============================================================================

def _num(value: Any, default: float = 0.0) -> float:
    """Return safe float while preserving valid zero values."""
    parsed = safe_float(value, None)
    if parsed is None:
        return float(default)
    return float(parsed)


def _sensor_value(sensor: SensorSnapshot, key: str, default: float = 0.0) -> float:
    """Read optional SensorSnapshot values safely."""
    return _num(getattr(sensor, key, None), default)


def _directional_value(sensor: SensorSnapshot, key: str, direction: str, default: float = 0.0) -> float:
    """Normalize a signed sensor value so positive means aligned with direction."""
    d = normalize_direction(direction)
    value = _sensor_value(sensor, key, default)
    if d == DIRECTION_LONG:
        return value
    if d == DIRECTION_SHORT:
        return -value
    return 0.0


# =============================================================================
# Directional helper checks
# =============================================================================

def rsi_slope_ok(sensor: SensorSnapshot, direction: str, min_abs_slope: float = 0.12) -> bool:
    """Return True if RSI slope supports direction. Slightly early-friendly."""
    d = normalize_direction(direction)
    value = safe_float(getattr(sensor, "rsi_slope", None), None)
    if value is None:
        return False
    if d == DIRECTION_LONG:
        return value >= min_abs_slope
    if d == DIRECTION_SHORT:
        return value <= -min_abs_slope
    return False


def macd_hist_slope_ok(sensor: SensorSnapshot, direction: str, min_abs_slope: float = 0.0) -> bool:
    """Return True if MACD histogram acceleration supports direction."""
    d = normalize_direction(direction)
    value = safe_float(getattr(sensor, "macd_hist_slope", None), None)
    if value is None:
        return False
    if d == DIRECTION_LONG:
        return value > min_abs_slope
    if d == DIRECTION_SHORT:
        return value < -min_abs_slope
    return False


def power_shift_ok(sensor: SensorSnapshot, direction: str, min_gap: float = 4.0) -> bool:
    """Return True if buy/sell power has shifted in direction."""
    d = normalize_direction(direction)
    buy = safe_float(getattr(sensor, "buy_power", None), None)
    sell = safe_float(getattr(sensor, "sell_power", None), None)
    if buy is None or sell is None:
        return False
    if d == DIRECTION_LONG:
        return (buy - sell) >= min_gap
    if d == DIRECTION_SHORT:
        return (sell - buy) >= min_gap
    return False


def volume_participation_ok(sensor: SensorSnapshot, min_ratio: float = 0.85) -> bool:
    """Return True if recent volume/participation is not dead."""
    ratio = safe_float(getattr(sensor, "volume_ratio", None), None)
    if ratio is None:
        return False
    return ratio >= min_ratio


def price_ema_alignment_ok(sensor: SensorSnapshot, direction: str) -> bool:
    """Return True if price is aligned with EMA20 in direction."""
    d = normalize_direction(direction)
    price = safe_float(getattr(sensor, "price", None), None)
    ema20 = safe_float(getattr(sensor, "ema20", None), None)
    if price is None or ema20 is None:
        return False
    if d == DIRECTION_LONG:
        return price >= ema20
    if d == DIRECTION_SHORT:
        return price <= ema20
    return False


def price_vwap_alignment_ok(sensor: SensorSnapshot, direction: str) -> bool:
    """Return True if price is aligned with VWAP in direction."""
    d = normalize_direction(direction)
    price = safe_float(getattr(sensor, "price", None), None)
    vwap = safe_float(getattr(sensor, "vwap", None), None)
    if price is None or vwap is None:
        return False
    if d == DIRECTION_LONG:
        return price >= vwap
    if d == DIRECTION_SHORT:
        return price <= vwap
    return False


# =============================================================================
# Directional raw helpers
# =============================================================================

def _directional_power_gap(sensor: SensorSnapshot, direction: str) -> float:
    """Return buy/sell power gap in the requested direction."""
    d = normalize_direction(direction)
    buy = _sensor_value(sensor, "buy_power", 50.0)
    sell = _sensor_value(sensor, "sell_power", 50.0)
    if d == DIRECTION_LONG:
        return buy - sell
    if d == DIRECTION_SHORT:
        return sell - buy
    return 0.0


def _directional_rsi_slope(sensor: SensorSnapshot, direction: str) -> float:
    """Return RSI slope normalized so positive means aligned with direction."""
    return _directional_value(sensor, "rsi_slope", direction, 0.0)


def _directional_macd_slope(sensor: SensorSnapshot, direction: str) -> float:
    """Return MACD histogram slope normalized so positive means aligned with direction."""
    return _directional_value(sensor, "macd_hist_slope", direction, 0.0)


def _directional_macd_hist(sensor: SensorSnapshot, direction: str) -> float:
    """Return MACD histogram normalized so positive means already aligned."""
    return _directional_value(sensor, "macd_hist", direction, 0.0)


def _opposite_rejection_wick(sensor: SensorSnapshot, direction: str) -> float:
    """Wick that often means rejection against requested entry direction."""
    d = normalize_direction(direction)
    if d == DIRECTION_LONG:
        return _sensor_value(sensor, "upper_wick_pct", 0.0)
    if d == DIRECTION_SHORT:
        return _sensor_value(sensor, "lower_wick_pct", 0.0)
    return 0.0


# =============================================================================
# Scoring components
# =============================================================================

def score_rsi(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score RSI value and slope for Level 4 without rewarding late overheat."""
    reasons: list[str] = []
    d = normalize_direction(direction)
    rsi = safe_float(getattr(sensor, "rsi", None), None)
    rsi_sl = safe_float(getattr(sensor, "rsi_slope", None), None)

    score = 50.0
    if rsi is None:
        return 45.0, ["RSI_MISSING"]

    if d == DIRECTION_LONG:
        if 49 <= rsi <= 64:
            score += 18.0
            reasons.append("RSI_LONG_START_ZONE")
        elif 64 < rsi <= 71:
            score += 8.0
            reasons.append("RSI_LONG_STRONG_BUT_WATCH_EXTENSION")
        elif rsi > 75:
            score -= 18.0
            reasons.append("RSI_LONG_OVERHEATED")
        elif rsi < 43:
            score -= 12.0
            reasons.append("RSI_LONG_WEAK")
    elif d == DIRECTION_SHORT:
        if 36 <= rsi <= 51:
            score += 18.0
            reasons.append("RSI_SHORT_START_ZONE")
        elif 29 <= rsi < 36:
            score += 8.0
            reasons.append("RSI_SHORT_STRONG_BUT_WATCH_EXTENSION")
        elif rsi < 25:
            score -= 18.0
            reasons.append("RSI_SHORT_OVERHEATED")
        elif rsi > 57:
            score -= 12.0
            reasons.append("RSI_SHORT_WEAK")

    if rsi_sl is not None:
        if (d == DIRECTION_LONG and rsi_sl > 0) or (d == DIRECTION_SHORT and rsi_sl < 0):
            score += 12.0
            reasons.append("RSI_SLOPE_ALIGNED")
        else:
            score -= 10.0
            reasons.append("RSI_SLOPE_AGAINST")
    else:
        reasons.append("RSI_SLOPE_MISSING")

    return clamp(score, 0.0, 100.0), reasons


def score_macd(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score MACD histogram and, more importantly, histogram acceleration."""
    reasons: list[str] = []
    hist_dir = _directional_macd_hist(sensor, direction)
    hist_slope_dir = _directional_macd_slope(sensor, direction)
    hist_missing = safe_float(getattr(sensor, "macd_hist", None), None) is None
    slope_missing = safe_float(getattr(sensor, "macd_hist_slope", None), None) is None

    if hist_missing:
        return 45.0, ["MACD_MISSING"]

    score = 50.0
    if hist_dir > 0:
        score += 10.0
        reasons.append("MACD_HIST_ALIGNED")
    elif hist_slope_dir > 0:
        # Early turn before histogram fully crosses is valuable for hunting starts.
        score += 4.0
        reasons.append("MACD_HIST_NOT_CROSSED_BUT_ACCELERATING")
    else:
        score -= 8.0
        reasons.append("MACD_HIST_AGAINST")

    if not slope_missing:
        if hist_slope_dir > 0:
            score += 18.0
            reasons.append("MACD_HIST_ACCEL_ALIGNED")
        else:
            score -= 14.0
            reasons.append("MACD_HIST_ACCEL_AGAINST")
    else:
        reasons.append("MACD_HIST_SLOPE_MISSING")

    return clamp(score, 0.0, 100.0), reasons


def score_power(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score buy/sell power balance with early power-shift priority."""
    buy = safe_float(getattr(sensor, "buy_power", None), None)
    sell = safe_float(getattr(sensor, "sell_power", None), None)
    if buy is None or sell is None:
        return 45.0, ["POWER_MISSING"]

    gap = _directional_power_gap(sensor, direction)
    score = 50.0 + clamp(gap * 1.55, -38.0, 38.0)
    reasons: list[str] = []

    if gap >= 16:
        reasons.append("POWER_START_STRONG")
    elif gap >= 6:
        reasons.append("POWER_SHIFT_ALIGNED")
    elif gap <= -8:
        reasons.append("POWER_AGAINST")
    else:
        reasons.append("POWER_NEUTRAL")

    return clamp(score, 0.0, 100.0), reasons


def score_volume(sensor: SensorSnapshot) -> tuple[float, list[str]]:
    """Score recent volume/participation. Expansion is good; dry market is bad."""
    ratio = safe_float(getattr(sensor, "volume_ratio", None), None)
    if ratio is None:
        return 45.0, ["VOLUME_RATIO_MISSING"]

    if ratio >= 1.85:
        return 76.0, ["VOLUME_EXPANDING_STRONG"]
    if ratio >= 1.20:
        return 74.0, ["VOLUME_EXPANDING_START"]
    if ratio >= 0.90:
        return 63.0, ["VOLUME_OK"]
    if ratio >= 0.70:
        return 48.0, ["VOLUME_SOFT"]
    return 30.0, ["VOLUME_WEAK"]


def score_ema_vwap(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score EMA/VWAP alignment as momentum support."""
    reasons: list[str] = []
    score = 50.0

    if price_ema_alignment_ok(sensor, direction):
        score += 14.0
        reasons.append("EMA20_ALIGNED")
    else:
        score -= 11.0
        reasons.append("EMA20_NOT_ALIGNED")

    if price_vwap_alignment_ok(sensor, direction):
        score += 12.0
        reasons.append("VWAP_ALIGNED")
    else:
        score -= 9.0
        reasons.append("VWAP_NOT_ALIGNED")

    return clamp(score, 0.0, 100.0), reasons


def score_acceleration(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score early acceleration from RSI slope, MACD acceleration, power, and participation."""
    d = normalize_direction(direction)
    score = 50.0
    reasons: list[str] = []

    if rsi_slope_ok(sensor, d, min_abs_slope=0.05):
        score += 13.0
        reasons.append("ACCEL_RSI_TURN_OK")
    else:
        score -= 6.0
        reasons.append("ACCEL_RSI_WEAK")

    if macd_hist_slope_ok(sensor, d):
        score += 18.0
        reasons.append("ACCEL_MACD_HIST_OK")
    else:
        score -= 10.0
        reasons.append("ACCEL_MACD_WEAK")

    if power_shift_ok(sensor, d, min_gap=3.0):
        score += 15.0
        reasons.append("ACCEL_POWER_SHIFT_OK")
    else:
        score -= 7.0
        reasons.append("ACCEL_POWER_WEAK")

    volume = safe_float(getattr(sensor, "volume_ratio", None), None)
    if volume is not None and volume >= 1.15:
        score += 10.0
        reasons.append("ACCEL_VOLUME_EXPANSION_OK")
    elif volume_participation_ok(sensor, min_ratio=0.85):
        score += 5.0
        reasons.append("ACCEL_VOLUME_OK")
    else:
        score -= 6.0
        reasons.append("ACCEL_VOLUME_WEAK")

    body = _sensor_value(sensor, "candle_body_pct", 0.0)
    if body >= 0.45 and _directional_power_gap(sensor, d) > 0 and _directional_macd_slope(sensor, d) > 0:
        score += 5.0
        reasons.append("ACCEL_CANDLE_PRESSURE_OK")

    return clamp(score, 0.0, 100.0), reasons


def score_weakness(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score weakness/reversal risk. Higher score = more weakness."""
    d = normalize_direction(direction)
    score = 0.0
    reasons: list[str] = []

    if not rsi_slope_ok(sensor, d, min_abs_slope=0.04):
        score += 18.0
        reasons.append("WEAK_RSI_SLOPE")
    if not macd_hist_slope_ok(sensor, d):
        score += 22.0
        reasons.append("WEAK_MACD_ACCEL")
    if not power_shift_ok(sensor, d, min_gap=2.0):
        score += 18.0
        reasons.append("WEAK_POWER_SHIFT")
    if not price_ema_alignment_ok(sensor, d):
        score += 14.0
        reasons.append("WEAK_EMA_LOSS")
    if not price_vwap_alignment_ok(sensor, d):
        score += 12.0
        reasons.append("WEAK_VWAP_LOSS")
    if not volume_participation_ok(sensor, min_ratio=0.70):
        score += 8.0
        reasons.append("WEAK_VOLUME")

    rejection = _opposite_rejection_wick(sensor, d)
    if rejection >= 0.50:
        score += 8.0
        reasons.append("WEAK_REJECTION_WICK")

    if not reasons:
        reasons.append("WEAKNESS_LOW_RAW")

    return clamp(score, 0.0, 100.0), reasons


def score_start_pressure(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """
    Score signs that a move is just starting.

    This is the momentum-side equivalent of structure's start-zone logic.
    It is intentionally raw and does not decide final trade action.
    """
    d = normalize_direction(direction)
    score = 40.0
    reasons: list[str] = []

    rsi_s = _directional_rsi_slope(sensor, d)
    macd_s = _directional_macd_slope(sensor, d)
    power_gap = _directional_power_gap(sensor, d)
    volume = safe_float(getattr(sensor, "volume_ratio", None), None)
    body = _sensor_value(sensor, "candle_body_pct", 0.0)
    hist = _directional_macd_hist(sensor, d)

    if rsi_s >= 0.05:
        score += 12.0
        reasons.append("START_RSI_TURN")
    if macd_s > 0:
        score += 18.0
        reasons.append("START_MACD_ACCEL")
    if power_gap >= 3.0:
        score += 16.0
        reasons.append("START_POWER_SHIFT")
    if volume is not None and volume >= 1.12:
        score += 10.0
        reasons.append("START_VOLUME_PRESSURE")
    elif volume is not None and volume < 0.70:
        score -= 8.0
        reasons.append("START_VOLUME_TOO_WEAK")

    if hist <= 0 and macd_s > 0 and power_gap >= 3.0:
        score += 6.0
        reasons.append("START_BEFORE_FULL_MACD_CONFIRM")

    if body >= 0.35 and macd_s > 0 and power_gap > 0:
        score += 5.0
        reasons.append("START_CANDLE_PRESSURE")

    if not price_ema_alignment_ok(sensor, d) and not price_vwap_alignment_ok(sensor, d):
        score -= 12.0
        reasons.append("START_PRICE_NOT_RECLAIMED")

    if not reasons:
        reasons.append("START_PRESSURE_NEUTRAL")

    return clamp(score, 0.0, 100.0), reasons


def score_fresh_momentum(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score whether momentum is fresh and still developing."""
    d = normalize_direction(direction)
    score = 48.0
    reasons: list[str] = []

    rsi = safe_float(getattr(sensor, "rsi", None), None)
    rsi_dir_slope = _directional_rsi_slope(sensor, d)
    macd_dir_slope = _directional_macd_slope(sensor, d)
    macd_hist_dir = _directional_macd_hist(sensor, d)
    power_gap = _directional_power_gap(sensor, d)
    volume = safe_float(getattr(sensor, "volume_ratio", None), None)
    body = _sensor_value(sensor, "candle_body_pct", 0.0)
    rejection = _opposite_rejection_wick(sensor, d)

    if rsi_dir_slope >= 0.20:
        score += 15.0
        reasons.append("FRESH_RSI_SLOPE_STRONG")
    elif rsi_dir_slope >= 0.05:
        score += 9.0
        reasons.append("FRESH_RSI_SLOPE_OK")
    else:
        score -= 12.0
        reasons.append("FRESH_RSI_SLOPE_WEAK")

    if macd_dir_slope > 0:
        score += 18.0
        reasons.append("FRESH_MACD_ACCEL_OK")
    else:
        score -= 16.0
        reasons.append("FRESH_MACD_ACCEL_WEAK")

    if macd_hist_dir <= 0 and macd_dir_slope > 0:
        score += 5.0
        reasons.append("FRESH_EARLY_BEFORE_MACD_CROSS")

    if power_gap >= 14:
        score += 15.0
        reasons.append("FRESH_POWER_STRONG")
    elif power_gap >= 4:
        score += 9.0
        reasons.append("FRESH_POWER_OK")
    elif power_gap <= -6:
        score -= 16.0
        reasons.append("FRESH_POWER_AGAINST")
    else:
        score -= 5.0
        reasons.append("FRESH_POWER_NEUTRAL")

    if volume is not None:
        if 1.10 <= volume <= 1.85:
            score += 9.0
            reasons.append("FRESH_VOLUME_EXPANDING")
        elif volume > 1.85:
            score += 4.0
            reasons.append("FRESH_VOLUME_HIGH_WATCH_CLIMAX")
        elif volume >= 0.85:
            score += 3.0
            reasons.append("FRESH_VOLUME_ACCEPTABLE")
        else:
            score -= 9.0
            reasons.append("FRESH_VOLUME_WEAK")
    else:
        score -= 3.0
        reasons.append("FRESH_VOLUME_MISSING")

    if price_ema_alignment_ok(sensor, d):
        score += 5.0
        reasons.append("FRESH_EMA_ALIGNED")
    else:
        score -= 6.0
        reasons.append("FRESH_EMA_NOT_ALIGNED")

    if price_vwap_alignment_ok(sensor, d):
        score += 4.0
        reasons.append("FRESH_VWAP_ALIGNED")
    else:
        score -= 5.0
        reasons.append("FRESH_VWAP_NOT_ALIGNED")

    if body >= 0.45 and power_gap > 0 and macd_dir_slope > 0:
        score += 5.0
        reasons.append("FRESH_CANDLE_QUALITY_OK")

    if rejection >= 0.50:
        score -= 8.0
        reasons.append("FRESH_REJECTION_WICK_WARNING")

    if rsi is not None:
        if d == DIRECTION_LONG and rsi >= 74 and rsi_dir_slope <= 0.10:
            score -= 18.0
            reasons.append("FRESH_LONG_OVERHEATED_FADING")
        elif d == DIRECTION_SHORT and rsi <= 26 and rsi_dir_slope <= 0.10:
            score -= 18.0
            reasons.append("FRESH_SHORT_OVERHEATED_FADING")

    return clamp(score, 0.0, 100.0), reasons


def score_exhaustion(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score exhaustion/chase risk. Higher score = more late-entry risk."""
    d = normalize_direction(direction)
    score = 0.0
    reasons: list[str] = []

    rsi = safe_float(getattr(sensor, "rsi", None), None)
    rsi_dir_slope = _directional_rsi_slope(sensor, d)
    macd_dir_slope = _directional_macd_slope(sensor, d)
    power_gap = _directional_power_gap(sensor, d)
    volume = safe_float(getattr(sensor, "volume_ratio", None), None)
    body = _sensor_value(sensor, "candle_body_pct", 0.0)
    rejection = _opposite_rejection_wick(sensor, d)

    if rsi is not None:
        if d == DIRECTION_LONG and rsi >= 74:
            score += 18.0
            reasons.append("EXH_LONG_RSI_OVERHEATED")
        elif d == DIRECTION_SHORT and rsi <= 26:
            score += 18.0
            reasons.append("EXH_SHORT_RSI_OVERHEATED")

    if rsi_dir_slope < 0:
        score += 18.0
        reasons.append("EXH_RSI_SLOPE_FADING")
    elif rsi_dir_slope < 0.05:
        score += 8.0
        reasons.append("EXH_RSI_SLOPE_FLAT")

    if macd_dir_slope <= 0:
        score += 24.0
        reasons.append("EXH_MACD_FADING")

    if power_gap <= -6:
        score += 20.0
        reasons.append("EXH_POWER_REVERSING")
    elif power_gap < 3:
        score += 9.0
        reasons.append("EXH_POWER_NOT_CONFIRMED")

    if volume is not None:
        if volume < 0.70:
            score += 9.0
            reasons.append("EXH_VOLUME_DRY")
        elif volume > 2.20 and (rsi_dir_slope < 0.08 or macd_dir_slope <= 0):
            score += 10.0
            reasons.append("EXH_VOLUME_CLIMAX_RISK")

    if not price_ema_alignment_ok(sensor, d):
        score += 10.0
        reasons.append("EXH_EMA_LOST")
    if not price_vwap_alignment_ok(sensor, d):
        score += 8.0
        reasons.append("EXH_VWAP_LOST")

    if rejection >= 0.45:
        score += 11.0
        reasons.append("EXH_REJECTION_WICK")

    if body <= 0.25 and (rsi_dir_slope < 0.05 or macd_dir_slope <= 0):
        score += 7.0
        reasons.append("EXH_WEAK_CANDLE_BODY")

    if not reasons:
        reasons.append("EXH_NORMAL")

    return clamp(score, 0.0, 100.0), reasons


def score_move_age(sensor: SensorSnapshot, direction: str, fresh_momentum_score: float, exhaustion_score: float) -> tuple[float, list[str]]:
    """
    Estimate whether momentum looks early or old.

    Higher score = older / more likely after pump-dump.
    Lower score = earlier / healthier start. This is a proxy based on SensorSnapshot only.
    """
    d = normalize_direction(direction)
    score = 45.0
    reasons: list[str] = []

    rsi = safe_float(getattr(sensor, "rsi", None), None)
    rsi_s = _directional_rsi_slope(sensor, d)
    macd_s = _directional_macd_slope(sensor, d)
    power_gap = _directional_power_gap(sensor, d)
    volume = safe_float(getattr(sensor, "volume_ratio", None), None)
    body = _sensor_value(sensor, "candle_body_pct", 0.0)
    rejection = _opposite_rejection_wick(sensor, d)
    fresh = _num(fresh_momentum_score, 50.0)
    exhaustion = _num(exhaustion_score, 0.0)

    if fresh >= 65 and exhaustion < 40 and rsi_s > 0 and macd_s > 0:
        score -= 18.0
        reasons.append("AGE_EARLY_FRESH_ACCELERATION")
    elif fresh >= 55 and macd_s > 0 and power_gap >= 3:
        score -= 10.0
        reasons.append("AGE_FORMING_MOVE")

    if rsi is not None:
        if d == DIRECTION_LONG and rsi >= 74:
            score += 16.0
            reasons.append("AGE_LONG_RSI_EXTENDED")
        elif d == DIRECTION_SHORT and rsi <= 26:
            score += 16.0
            reasons.append("AGE_SHORT_RSI_EXTENDED")

    if exhaustion >= 65:
        score += 24.0
        reasons.append("AGE_EXHAUSTION_HIGH")
    elif exhaustion >= 50:
        score += 12.0
        reasons.append("AGE_EXHAUSTION_MEDIUM")

    if rsi_s <= 0 or macd_s <= 0:
        score += 10.0
        reasons.append("AGE_ACCELERATION_FADING")

    if power_gap < 2:
        score += 8.0
        reasons.append("AGE_POWER_NOT_FRESH")

    if volume is not None and volume > 2.20 and body >= 0.60:
        score += 8.0
        reasons.append("AGE_VOLUME_BODY_CLIMAX_RISK")

    if rejection >= 0.50:
        score += 9.0
        reasons.append("AGE_REJECTION_WICK")

    return clamp(score, 0.0, 100.0), reasons


def calculate_chase_pressure(
    *,
    weakness_score: float,
    exhaustion_score: float,
    fresh_momentum_score: float,
    move_age_score: float = 50.0,
) -> float:
    """Calculate late/chase pressure. Higher = move more likely consumed."""
    weakness = _num(weakness_score, 0.0)
    exhaustion = _num(exhaustion_score, 0.0)
    fresh = _num(fresh_momentum_score, 50.0)
    age = _num(move_age_score, 50.0)

    pressure = (
        exhaustion * 0.42
        + weakness * 0.24
        + age * 0.24
        + max(0.0, 55.0 - fresh) * 0.34
    )
    return clamp(pressure, 0.0, 100.0)


def apply_chase_pressure_to_component(component_score: float, chase_pressure: float, strength: float) -> float:
    """Reduce a momentum component when chase/late pressure is high."""
    base = _num(component_score, 50.0)
    pressure = _num(chase_pressure, 0.0)
    penalty = max(0.0, pressure - 35.0) * strength
    return clamp(base - penalty, 0.0, 100.0)


def cap_late_momentum_score(score: float, chase_pressure: float, exhaustion_score: float, move_age_score: float = 50.0) -> float:
    """Cap momentum/continuation when move is late, old, or exhausted."""
    value = _num(score, 0.0)
    chase = _num(chase_pressure, 0.0)
    exhaustion = _num(exhaustion_score, 0.0)
    age = _num(move_age_score, 50.0)

    cap = 100.0
    if exhaustion >= 75 or chase >= 78 or age >= 82:
        cap = 46.0
    elif exhaustion >= 66 or chase >= 68 or age >= 72:
        cap = 55.0
    elif exhaustion >= 56 or chase >= 58 or age >= 64:
        cap = 64.0
    elif exhaustion >= 46 or chase >= 48 or age >= 56:
        cap = 74.0

    return clamp(min(value, cap), 0.0, 100.0)


# =============================================================================
# Combined momentum snapshot
# =============================================================================

def combine_momentum_score(parts: list[float]) -> float:
    """Weighted average for momentum score."""
    if not parts:
        return 0.0
    weights = [0.15, 0.22, 0.20, 0.12, 0.14, 0.17]
    total = 0.0
    weight_sum = 0.0
    for idx, score in enumerate(parts):
        w = weights[idx] if idx < len(weights) else 0.1
        total += _num(score, 0.0) * w
        weight_sum += w
    if weight_sum <= 0:
        return 0.0
    return clamp(total / weight_sum, 0.0, 100.0)


def build_momentum_snapshot(sensor: SensorSnapshot, direction: str) -> MomentumSnapshot:
    """Build MomentumSnapshot from raw SensorSnapshot."""
    d = normalize_direction(direction)
    reason_codes: list[str] = []

    rsi_score, rsi_reasons = score_rsi(sensor, d)
    macd_score, macd_reasons = score_macd(sensor, d)
    power_score, power_reasons = score_power(sensor, d)
    volume_score, volume_reasons = score_volume(sensor)
    ema_vwap_score, ema_vwap_reasons = score_ema_vwap(sensor, d)
    acceleration_score, acceleration_reasons = score_acceleration(sensor, d)
    weakness_score, weakness_reasons = score_weakness(sensor, d)
    start_pressure_score, start_reasons = score_start_pressure(sensor, d)
    fresh_momentum_score, fresh_reasons = score_fresh_momentum(sensor, d)
    exhaustion_score, exhaustion_reasons = score_exhaustion(sensor, d)
    move_age_score, move_age_reasons = score_move_age(sensor, d, fresh_momentum_score, exhaustion_score)

    chase_pressure = calculate_chase_pressure(
        weakness_score=weakness_score,
        exhaustion_score=exhaustion_score,
        fresh_momentum_score=fresh_momentum_score,
        move_age_score=move_age_score,
    )

    adjusted_macd_score = apply_chase_pressure_to_component(macd_score, chase_pressure, strength=0.34)
    adjusted_power_score = apply_chase_pressure_to_component(power_score, chase_pressure, strength=0.28)
    adjusted_acceleration_score = apply_chase_pressure_to_component(acceleration_score, chase_pressure, strength=0.38)

    for codes in [
        rsi_reasons,
        macd_reasons,
        power_reasons,
        volume_reasons,
        ema_vwap_reasons,
        acceleration_reasons,
        start_reasons,
        fresh_reasons,
        exhaustion_reasons,
        move_age_reasons,
    ]:
        reason_codes.extend(codes)

    if weakness_score >= 60:
        reason_codes.append("WEAKNESS_HIGH")
    elif weakness_score >= 40:
        reason_codes.append("WEAKNESS_MEDIUM")
    else:
        reason_codes.append("WEAKNESS_LOW")

    if start_pressure_score >= 68:
        reason_codes.append("MOMENTUM_START_PRESSURE_HIGH")
    elif start_pressure_score >= 55:
        reason_codes.append("MOMENTUM_START_PRESSURE_MEDIUM")
    else:
        reason_codes.append("MOMENTUM_START_PRESSURE_LOW")

    if fresh_momentum_score >= 65:
        reason_codes.append("FRESH_MOMENTUM_HIGH")
    elif fresh_momentum_score <= 42:
        reason_codes.append("FRESH_MOMENTUM_LOW")
    else:
        reason_codes.append("FRESH_MOMENTUM_MEDIUM")

    if exhaustion_score >= 62:
        reason_codes.append("EXHAUSTION_HIGH")
    elif exhaustion_score >= 42:
        reason_codes.append("EXHAUSTION_MEDIUM")
    else:
        reason_codes.append("EXHAUSTION_LOW")

    if move_age_score >= 70:
        reason_codes.append("MOVE_AGE_LATE")
    elif move_age_score <= 38:
        reason_codes.append("MOVE_AGE_EARLY")
    else:
        reason_codes.append("MOVE_AGE_NORMAL")

    if chase_pressure >= 70:
        reason_codes.append("CHASE_PRESSURE_HIGH")
    elif chase_pressure >= 52:
        reason_codes.append("CHASE_PRESSURE_MEDIUM")
    else:
        reason_codes.append("CHASE_PRESSURE_LOW")

    continuation_score = combine_momentum_score([
        adjusted_macd_score,
        adjusted_power_score,
        volume_score,
        ema_vwap_score,
    ])
    continuation_score = clamp(
        continuation_score
        + max(0.0, start_pressure_score - 55.0) * 0.18
        + max(0.0, fresh_momentum_score - 55.0) * 0.18
        - max(0.0, exhaustion_score - 42.0) * 0.34
        - max(0.0, weakness_score - 48.0) * 0.17
        - max(0.0, chase_pressure - 45.0) * 0.26
        - max(0.0, move_age_score - 58.0) * 0.18,
        0.0,
        100.0,
    )
    continuation_score = cap_late_momentum_score(continuation_score, chase_pressure, exhaustion_score, move_age_score)

    base_momentum_score = combine_momentum_score([
        rsi_score,
        adjusted_macd_score,
        adjusted_power_score,
        volume_score,
        ema_vwap_score,
        adjusted_acceleration_score,
    ])

    momentum_score = clamp(
        base_momentum_score
        + max(0.0, start_pressure_score - 55.0) * 0.22
        + max(0.0, fresh_momentum_score - 55.0) * 0.22
        - max(0.0, exhaustion_score - 42.0) * 0.40
        - max(0.0, weakness_score - 52.0) * 0.23
        - max(0.0, chase_pressure - 45.0) * 0.30
        - max(0.0, move_age_score - 58.0) * 0.20,
        0.0,
        100.0,
    )
    momentum_score = cap_late_momentum_score(momentum_score, chase_pressure, exhaustion_score, move_age_score)

    reversal_risk_score = clamp(
        weakness_score * 0.48
        + exhaustion_score * 0.25
        + chase_pressure * 0.17
        + max(0.0, move_age_score - 50.0) * 0.25,
        0.0,
        100.0,
    )

    raw = {
        "rsi_score": rsi_score,
        "macd_score": macd_score,
        "power_score": power_score,
        "volume_score": volume_score,
        "ema_vwap_score": ema_vwap_score,
        "acceleration_score": acceleration_score,
        "adjusted_macd_score": adjusted_macd_score,
        "adjusted_power_score": adjusted_power_score,
        "adjusted_acceleration_score": adjusted_acceleration_score,
        "base_momentum_score": base_momentum_score,
        "start_pressure_score": start_pressure_score,
        "fresh_momentum_score": fresh_momentum_score,
        "exhaustion_score": exhaustion_score,
        "move_age_score": move_age_score,
        "chase_pressure": chase_pressure,
        "fresh_reasons": fresh_reasons,
        "exhaustion_reasons": exhaustion_reasons,
        "weakness_reasons": weakness_reasons,
        "move_age_reasons": move_age_reasons,
        "start_reasons": start_reasons,
        "directional_power_gap": _directional_power_gap(sensor, d),
        "directional_rsi_slope": _directional_rsi_slope(sensor, d),
        "directional_macd_slope": _directional_macd_slope(sensor, d),
        "directional_macd_hist": _directional_macd_hist(sensor, d),
        "volume_pressure_start": _sensor_value(sensor, "volume_ratio", 0.0) >= 1.12,
        "momentum_start_active": start_pressure_score >= 60.0 and fresh_momentum_score >= 55.0 and exhaustion_score < 62.0,
        "sensor_created_at": getattr(sensor, "created_at", None),
    }

    return MomentumSnapshot(
        symbol=getattr(sensor, "symbol", ""),
        direction=d,
        momentum_score=momentum_score,
        continuation_score=continuation_score,
        reversal_risk_score=reversal_risk_score,
        acceleration_score=adjusted_acceleration_score,
        weakness_score=weakness_score,
        rsi_slope_ok=rsi_slope_ok(sensor, d),
        macd_hist_slope_ok=macd_hist_slope_ok(sensor, d),
        power_shift_ok=power_shift_ok(sensor, d),
        volume_participation_ok=volume_participation_ok(sensor),
        reason_codes=list(dict.fromkeys(reason_codes)),
        raw=raw,
    )


def build_momentum_snapshot_from_market(market_snapshot: MarketSnapshot, direction: str) -> MomentumSnapshot:
    """Convenience helper for tests/backfills; later code usually uses technical_sensors first."""
    from technical_sensors import build_sensor_snapshot

    sensor = build_sensor_snapshot(market_snapshot)
    return build_momentum_snapshot(sensor, direction)


def validate_momentum_snapshot(snapshot: MomentumSnapshot) -> dict[str, Any]:
    """Lightweight validation for momentum snapshot."""
    errors: list[str] = []

    if not getattr(snapshot, "symbol", ""):
        errors.append("missing_symbol")
    if getattr(snapshot, "direction", None) not in {DIRECTION_LONG, DIRECTION_SHORT}:
        errors.append("invalid_direction")

    for key in ["momentum_score", "continuation_score", "reversal_risk_score", "acceleration_score", "weakness_score"]:
        value = safe_float(getattr(snapshot, key, None), None)
        if value is None or not (0.0 <= value <= 100.0):
            errors.append(f"invalid_{key}")

    raw = getattr(snapshot, "raw", None) or {}
    if not isinstance(raw, dict):
        errors.append("invalid_raw")
    else:
        for key in ["fresh_momentum_score", "exhaustion_score", "move_age_score", "chase_pressure", "start_pressure_score"]:
            value = safe_float(raw.get(key), None)
            if value is None or not (0.0 <= value <= 100.0):
                errors.append(f"invalid_raw_{key}")

    return {
        "valid": not errors,
        "errors": errors,
        "symbol": getattr(snapshot, "symbol", ""),
        "direction": getattr(snapshot, "direction", None),
        "momentum_score": getattr(snapshot, "momentum_score", None),
    }


__all__ = [
    "MOMENTUM_ENGINE_VERSION",
    "rsi_slope_ok",
    "macd_hist_slope_ok",
    "power_shift_ok",
    "volume_participation_ok",
    "price_ema_alignment_ok",
    "price_vwap_alignment_ok",
    "score_rsi",
    "score_macd",
    "score_power",
    "score_volume",
    "score_ema_vwap",
    "score_acceleration",
    "score_weakness",
    "score_start_pressure",
    "score_fresh_momentum",
    "score_exhaustion",
    "score_move_age",
    "calculate_chase_pressure",
    "apply_chase_pressure_to_component",
    "cap_late_momentum_score",
    "combine_momentum_score",
    "build_momentum_snapshot",
    "build_momentum_snapshot_from_market",
    "validate_momentum_snapshot",
]
