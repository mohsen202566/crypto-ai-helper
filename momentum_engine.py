"""
momentum_engine.py
Level 4 / 1H Smart Scalp Bot

Momentum engine for 1H Smart Scalp.

Architecture lock:
- Scores momentum, continuation, acceleration, and weakness only.
- No final AI decision, no REAL/GHOST/REJECT, no TP/SL final decision,
  no exchange trading, no JSON state writes, no Telegram text.
- Allowed project imports: constants.py, utils.py, models.py, technical_sensors.py only.
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
# Directional helper checks
# =============================================================================

def rsi_slope_ok(sensor: SensorSnapshot, direction: str, min_abs_slope: float = 0.15) -> bool:
    """Return True if RSI slope supports direction."""
    d = normalize_direction(direction)
    value = safe_float(sensor.rsi_slope, None)
    if value is None:
        return False
    if d == DIRECTION_LONG:
        return value >= min_abs_slope
    if d == DIRECTION_SHORT:
        return value <= -min_abs_slope
    return False


def macd_hist_slope_ok(sensor: SensorSnapshot, direction: str, min_abs_slope: float = 0.0) -> bool:
    """Return True if MACD histogram slope supports direction."""
    d = normalize_direction(direction)
    value = safe_float(sensor.macd_hist_slope, None)
    if value is None:
        return False
    if d == DIRECTION_LONG:
        return value > min_abs_slope
    if d == DIRECTION_SHORT:
        return value < -min_abs_slope
    return False


def power_shift_ok(sensor: SensorSnapshot, direction: str, min_gap: float = 5.0) -> bool:
    """Return True if buy/sell power supports direction."""
    d = normalize_direction(direction)
    buy = safe_float(sensor.buy_power, None)
    sell = safe_float(sensor.sell_power, None)
    if buy is None or sell is None:
        return False
    if d == DIRECTION_LONG:
        return (buy - sell) >= min_gap
    if d == DIRECTION_SHORT:
        return (sell - buy) >= min_gap
    return False


def volume_participation_ok(sensor: SensorSnapshot, min_ratio: float = 0.85) -> bool:
    """Return True if recent volume is not dead."""
    ratio = safe_float(sensor.volume_ratio, None)
    if ratio is None:
        return False
    return ratio >= min_ratio


def price_ema_alignment_ok(sensor: SensorSnapshot, direction: str) -> bool:
    """Return True if price is aligned with EMA20 in direction."""
    d = normalize_direction(direction)
    price = safe_float(sensor.price, None)
    ema20 = safe_float(sensor.ema20, None)
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
    price = safe_float(sensor.price, None)
    vwap = safe_float(sensor.vwap, None)
    if price is None or vwap is None:
        return False
    if d == DIRECTION_LONG:
        return price >= vwap
    if d == DIRECTION_SHORT:
        return price <= vwap
    return False


# =============================================================================
# Scoring components
# =============================================================================

def score_rsi(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score RSI value and slope for Level 4."""
    reasons: list[str] = []
    d = normalize_direction(direction)
    rsi = safe_float(sensor.rsi, None)
    rsi_sl = safe_float(sensor.rsi_slope, None)

    score = 50.0
    if rsi is None:
        return 45.0, ["RSI_MISSING"]

    if d == DIRECTION_LONG:
        if 52 <= rsi <= 68:
            score += 18
            reasons.append("RSI_LONG_HEALTHY")
        elif rsi > 75:
            score -= 15
            reasons.append("RSI_LONG_OVERHEATED")
        elif rsi < 45:
            score -= 12
            reasons.append("RSI_LONG_WEAK")
    elif d == DIRECTION_SHORT:
        if 32 <= rsi <= 48:
            score += 18
            reasons.append("RSI_SHORT_HEALTHY")
        elif rsi < 25:
            score -= 15
            reasons.append("RSI_SHORT_OVERHEATED")
        elif rsi > 55:
            score -= 12
            reasons.append("RSI_SHORT_WEAK")

    if rsi_sl is not None:
        if (d == DIRECTION_LONG and rsi_sl > 0) or (d == DIRECTION_SHORT and rsi_sl < 0):
            score += 10
            reasons.append("RSI_SLOPE_ALIGNED")
        else:
            score -= 8
            reasons.append("RSI_SLOPE_AGAINST")
    else:
        reasons.append("RSI_SLOPE_MISSING")

    return clamp(score, 0.0, 100.0), reasons


def score_macd(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score MACD histogram and slope."""
    reasons: list[str] = []
    d = normalize_direction(direction)
    hist = safe_float(sensor.macd_hist, None)
    hist_slope = safe_float(sensor.macd_hist_slope, None)

    score = 50.0
    if hist is None:
        return 45.0, ["MACD_MISSING"]

    if (d == DIRECTION_LONG and hist > 0) or (d == DIRECTION_SHORT and hist < 0):
        score += 14
        reasons.append("MACD_HIST_ALIGNED")
    else:
        score -= 8
        reasons.append("MACD_HIST_AGAINST")

    if hist_slope is not None:
        if (d == DIRECTION_LONG and hist_slope > 0) or (d == DIRECTION_SHORT and hist_slope < 0):
            score += 16
            reasons.append("MACD_HIST_SLOPE_ALIGNED")
        else:
            score -= 12
            reasons.append("MACD_HIST_SLOPE_AGAINST")
    else:
        reasons.append("MACD_HIST_SLOPE_MISSING")

    return clamp(score, 0.0, 100.0), reasons


def score_power(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score buy/sell power balance."""
    reasons: list[str] = []
    d = normalize_direction(direction)
    buy = safe_float(sensor.buy_power, None)
    sell = safe_float(sensor.sell_power, None)

    if buy is None or sell is None:
        return 45.0, ["POWER_MISSING"]

    gap = buy - sell if d == DIRECTION_LONG else sell - buy
    score = 50.0 + clamp(gap * 1.4, -35.0, 35.0)

    if gap >= 15:
        reasons.append("POWER_STRONG_ALIGNED")
    elif gap >= 5:
        reasons.append("POWER_ALIGNED")
    elif gap <= -10:
        reasons.append("POWER_AGAINST")
    else:
        reasons.append("POWER_NEUTRAL")

    return clamp(score, 0.0, 100.0), reasons


def score_volume(sensor: SensorSnapshot) -> tuple[float, list[str]]:
    """Score recent volume participation."""
    ratio = safe_float(sensor.volume_ratio, None)
    if ratio is None:
        return 45.0, ["VOLUME_RATIO_MISSING"]

    if ratio >= 1.4:
        return 78.0, ["VOLUME_EXPANDING"]
    if ratio >= 1.0:
        return 65.0, ["VOLUME_OK"]
    if ratio >= 0.75:
        return 48.0, ["VOLUME_SOFT"]
    return 30.0, ["VOLUME_WEAK"]


def score_ema_vwap(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score EMA/VWAP alignment as momentum support."""
    reasons: list[str] = []
    score = 50.0

    if price_ema_alignment_ok(sensor, direction):
        score += 15
        reasons.append("EMA20_ALIGNED")
    else:
        score -= 10
        reasons.append("EMA20_NOT_ALIGNED")

    if price_vwap_alignment_ok(sensor, direction):
        score += 12
        reasons.append("VWAP_ALIGNED")
    else:
        score -= 8
        reasons.append("VWAP_NOT_ALIGNED")

    return clamp(score, 0.0, 100.0), reasons


def score_acceleration(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score early acceleration from RSI slope, MACD slope, and power gap."""
    d = normalize_direction(direction)
    score = 50.0
    reasons: list[str] = []

    if rsi_slope_ok(sensor, d):
        score += 12
        reasons.append("ACCEL_RSI_OK")
    else:
        score -= 5
        reasons.append("ACCEL_RSI_WEAK")

    if macd_hist_slope_ok(sensor, d):
        score += 16
        reasons.append("ACCEL_MACD_OK")
    else:
        score -= 8
        reasons.append("ACCEL_MACD_WEAK")

    if power_shift_ok(sensor, d, min_gap=5.0):
        score += 14
        reasons.append("ACCEL_POWER_OK")
    else:
        score -= 6
        reasons.append("ACCEL_POWER_WEAK")

    if volume_participation_ok(sensor, min_ratio=0.85):
        score += 8
        reasons.append("ACCEL_VOLUME_OK")
    else:
        score -= 5
        reasons.append("ACCEL_VOLUME_WEAK")

    return clamp(score, 0.0, 100.0), reasons


def score_weakness(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """
    Score weakness/reversal risk.

    Higher score = more weakness against the current direction.
    """
    d = normalize_direction(direction)
    score = 0.0
    reasons: list[str] = []

    if not rsi_slope_ok(sensor, d, min_abs_slope=0.05):
        score += 18
        reasons.append("WEAK_RSI_SLOPE")

    if not macd_hist_slope_ok(sensor, d):
        score += 22
        reasons.append("WEAK_MACD_SLOPE")

    if not power_shift_ok(sensor, d, min_gap=2.0):
        score += 18
        reasons.append("WEAK_POWER_SHIFT")

    if not price_ema_alignment_ok(sensor, d):
        score += 14
        reasons.append("WEAK_EMA_LOSS")

    if not price_vwap_alignment_ok(sensor, d):
        score += 12
        reasons.append("WEAK_VWAP_LOSS")

    if not volume_participation_ok(sensor, min_ratio=0.7):
        score += 8
        reasons.append("WEAK_VOLUME")

    return clamp(score, 0.0, 100.0), reasons


def _directional_power_gap(sensor: SensorSnapshot, direction: str) -> float:
    """Return buy/sell power gap in the requested direction."""
    d = normalize_direction(direction)
    buy = safe_float(sensor.buy_power, 50.0) or 50.0
    sell = safe_float(sensor.sell_power, 50.0) or 50.0
    if d == DIRECTION_LONG:
        return buy - sell
    if d == DIRECTION_SHORT:
        return sell - buy
    return 0.0


def _directional_rsi_slope(sensor: SensorSnapshot, direction: str) -> float:
    """Return RSI slope normalized so positive means aligned with direction."""
    d = normalize_direction(direction)
    value = safe_float(sensor.rsi_slope, 0.0) or 0.0
    return value if d == DIRECTION_LONG else -value if d == DIRECTION_SHORT else 0.0


def _directional_macd_slope(sensor: SensorSnapshot, direction: str) -> float:
    """Return MACD histogram slope normalized so positive means aligned with direction."""
    d = normalize_direction(direction)
    value = safe_float(sensor.macd_hist_slope, 0.0) or 0.0
    return value if d == DIRECTION_LONG else -value if d == DIRECTION_SHORT else 0.0


def score_fresh_momentum(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """
    Score whether momentum is fresh and still developing.

    Higher score = better start/continuation quality for a Level 4 entry.
    This is intentionally not a final decision; ai_brain/timing use it as context.
    """
    d = normalize_direction(direction)
    score = 50.0
    reasons: list[str] = []

    rsi = safe_float(sensor.rsi, None)
    rsi_dir_slope = _directional_rsi_slope(sensor, d)
    macd_dir_slope = _directional_macd_slope(sensor, d)
    power_gap = _directional_power_gap(sensor, d)
    volume = safe_float(sensor.volume_ratio, None)
    body = safe_float(sensor.candle_body_pct, 0.0) or 0.0

    if rsi_dir_slope >= 0.20:
        score += 14.0
        reasons.append("FRESH_RSI_SLOPE_STRONG")
    elif rsi_dir_slope >= 0.05:
        score += 8.0
        reasons.append("FRESH_RSI_SLOPE_OK")
    else:
        score -= 10.0
        reasons.append("FRESH_RSI_SLOPE_WEAK")

    if macd_dir_slope > 0:
        score += 16.0
        reasons.append("FRESH_MACD_ACCEL_OK")
    else:
        score -= 14.0
        reasons.append("FRESH_MACD_ACCEL_WEAK")

    if power_gap >= 12:
        score += 14.0
        reasons.append("FRESH_POWER_STRONG")
    elif power_gap >= 4:
        score += 8.0
        reasons.append("FRESH_POWER_OK")
    elif power_gap <= -6:
        score -= 14.0
        reasons.append("FRESH_POWER_AGAINST")
    else:
        score -= 4.0
        reasons.append("FRESH_POWER_NEUTRAL")

    if volume is not None:
        if volume >= 1.20:
            score += 8.0
            reasons.append("FRESH_VOLUME_EXPANDING")
        elif volume >= 0.85:
            score += 3.0
            reasons.append("FRESH_VOLUME_ACCEPTABLE")
        else:
            score -= 8.0
            reasons.append("FRESH_VOLUME_WEAK")
    else:
        score -= 3.0
        reasons.append("FRESH_VOLUME_MISSING")

    if price_ema_alignment_ok(sensor, d):
        score += 4.0
        reasons.append("FRESH_EMA_ALIGNED")
    else:
        score -= 6.0
        reasons.append("FRESH_EMA_NOT_ALIGNED")

    if price_vwap_alignment_ok(sensor, d):
        score += 3.0
        reasons.append("FRESH_VWAP_ALIGNED")
    else:
        score -= 5.0
        reasons.append("FRESH_VWAP_NOT_ALIGNED")

    if body >= 0.55 and power_gap > 0 and macd_dir_slope > 0:
        score += 4.0
        reasons.append("FRESH_CANDLE_QUALITY_OK")

    if rsi is not None:
        if d == DIRECTION_LONG and rsi >= 74 and rsi_dir_slope <= 0.10:
            score -= 16.0
            reasons.append("FRESH_LONG_OVERHEATED_FADING")
        elif d == DIRECTION_SHORT and rsi <= 26 and rsi_dir_slope <= 0.10:
            score -= 16.0
            reasons.append("FRESH_SHORT_OVERHEATED_FADING")

    return clamp(score, 0.0, 100.0), reasons


def score_exhaustion(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """
    Score exhaustion/chase risk in the requested direction.

    Higher score = more risk that the bot is entering after the move is consumed.
    """
    d = normalize_direction(direction)
    score = 0.0
    reasons: list[str] = []

    rsi = safe_float(sensor.rsi, None)
    rsi_dir_slope = _directional_rsi_slope(sensor, d)
    macd_dir_slope = _directional_macd_slope(sensor, d)
    power_gap = _directional_power_gap(sensor, d)
    volume = safe_float(sensor.volume_ratio, None)
    body = safe_float(sensor.candle_body_pct, 0.0) or 0.0
    upper_wick = safe_float(sensor.upper_wick_pct, 0.0) or 0.0
    lower_wick = safe_float(sensor.lower_wick_pct, 0.0) or 0.0

    if rsi is not None:
        if d == DIRECTION_LONG and rsi >= 74:
            score += 18.0
            reasons.append("EXH_LONG_RSI_OVERHEATED")
        elif d == DIRECTION_SHORT and rsi <= 26:
            score += 18.0
            reasons.append("EXH_SHORT_RSI_OVERHEATED")

    if rsi_dir_slope < 0:
        score += 16.0
        reasons.append("EXH_RSI_SLOPE_FADING")
    elif rsi_dir_slope < 0.05:
        score += 8.0
        reasons.append("EXH_RSI_SLOPE_FLAT")

    if macd_dir_slope <= 0:
        score += 22.0
        reasons.append("EXH_MACD_FADING")

    if power_gap <= -6:
        score += 18.0
        reasons.append("EXH_POWER_REVERSING")
    elif power_gap < 3:
        score += 8.0
        reasons.append("EXH_POWER_NOT_CONFIRMED")

    if volume is not None and volume < 0.75:
        score += 8.0
        reasons.append("EXH_VOLUME_DRY")

    if not price_ema_alignment_ok(sensor, d):
        score += 10.0
        reasons.append("EXH_EMA_LOST")
    if not price_vwap_alignment_ok(sensor, d):
        score += 8.0
        reasons.append("EXH_VWAP_LOST")

    if d == DIRECTION_LONG and upper_wick >= 0.45:
        score += 10.0
        reasons.append("EXH_UPPER_WICK_REJECTION")
    elif d == DIRECTION_SHORT and lower_wick >= 0.45:
        score += 10.0
        reasons.append("EXH_LOWER_WICK_REJECTION")

    if body <= 0.25 and (rsi_dir_slope < 0.05 or macd_dir_slope <= 0):
        score += 6.0
        reasons.append("EXH_WEAK_CANDLE_BODY")

    if not reasons:
        reasons.append("EXH_NORMAL")

    return clamp(score, 0.0, 100.0), reasons


def calculate_chase_pressure(
    *,
    weakness_score: float,
    exhaustion_score: float,
    fresh_momentum_score: float,
) -> float:
    """
    Calculate late/chase pressure.

    Higher score = the move is more likely already consumed.
    This does not decide REAL/GHOST; it only weakens momentum quality.
    """
    weakness = safe_float(weakness_score, 0.0) or 0.0
    exhaustion = safe_float(exhaustion_score, 0.0) or 0.0
    fresh = safe_float(fresh_momentum_score, 50.0) or 50.0

    pressure = (
        exhaustion * 0.52
        + weakness * 0.30
        + max(0.0, 55.0 - fresh) * 0.38
    )
    return clamp(pressure, 0.0, 100.0)


def apply_chase_pressure_to_component(component_score: float, chase_pressure: float, strength: float) -> float:
    """
    Reduce an individual momentum component when chase/late pressure is high.

    strength controls how sensitive that component is to late-entry risk.
    """
    base = safe_float(component_score, 50.0) or 50.0
    pressure = safe_float(chase_pressure, 0.0) or 0.0
    penalty = max(0.0, pressure - 35.0) * strength
    return clamp(base - penalty, 0.0, 100.0)


def cap_late_momentum_score(score: float, chase_pressure: float, exhaustion_score: float) -> float:
    """
    Cap momentum/continuation when move is clearly late or exhausted.

    This prevents strong old RSI/MACD/Power readings from keeping a consumed
    move attractive for Level 4 entries.
    """
    value = safe_float(score, 0.0) or 0.0
    chase = safe_float(chase_pressure, 0.0) or 0.0
    exhaustion = safe_float(exhaustion_score, 0.0) or 0.0

    cap = 100.0
    if exhaustion >= 75 or chase >= 75:
        cap = 48.0
    elif exhaustion >= 65 or chase >= 65:
        cap = 55.0
    elif exhaustion >= 55 or chase >= 55:
        cap = 64.0
    elif exhaustion >= 45 or chase >= 45:
        cap = 72.0

    return clamp(min(value, cap), 0.0, 100.0)


# =============================================================================
# Combined momentum snapshot
# =============================================================================

def combine_momentum_score(parts: list[float]) -> float:
    """Weighted average for momentum score."""
    if not parts:
        return 0.0
    # RSI, MACD, Power, Volume, EMA/VWAP, Acceleration
    weights = [0.15, 0.22, 0.20, 0.12, 0.14, 0.17]
    total = 0.0
    weight_sum = 0.0
    for idx, score in enumerate(parts):
        w = weights[idx] if idx < len(weights) else 0.1
        total += score * w
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
    fresh_momentum_score, fresh_reasons = score_fresh_momentum(sensor, d)
    exhaustion_score, exhaustion_reasons = score_exhaustion(sensor, d)

    chase_pressure = calculate_chase_pressure(
        weakness_score=weakness_score,
        exhaustion_score=exhaustion_score,
        fresh_momentum_score=fresh_momentum_score,
    )

    adjusted_macd_score = apply_chase_pressure_to_component(macd_score, chase_pressure, strength=0.34)
    adjusted_power_score = apply_chase_pressure_to_component(power_score, chase_pressure, strength=0.28)
    adjusted_acceleration_score = apply_chase_pressure_to_component(acceleration_score, chase_pressure, strength=0.38)

    reason_codes.extend(rsi_reasons)
    reason_codes.extend(macd_reasons)
    reason_codes.extend(power_reasons)
    reason_codes.extend(volume_reasons)
    reason_codes.extend(ema_vwap_reasons)
    reason_codes.extend(acceleration_reasons)
    reason_codes.extend(fresh_reasons)
    reason_codes.extend(exhaustion_reasons)

    if weakness_score >= 60:
        reason_codes.append("WEAKNESS_HIGH")
    elif weakness_score >= 40:
        reason_codes.append("WEAKNESS_MEDIUM")
    else:
        reason_codes.append("WEAKNESS_LOW")

    if fresh_momentum_score >= 65:
        reason_codes.append("FRESH_MOMENTUM_HIGH")
    elif fresh_momentum_score <= 42:
        reason_codes.append("FRESH_MOMENTUM_LOW")
    else:
        reason_codes.append("FRESH_MOMENTUM_MEDIUM")

    if exhaustion_score >= 60:
        reason_codes.append("EXHAUSTION_HIGH")
    elif exhaustion_score >= 40:
        reason_codes.append("EXHAUSTION_MEDIUM")
    else:
        reason_codes.append("EXHAUSTION_LOW")

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
        + max(0.0, fresh_momentum_score - 55.0) * 0.16
        - max(0.0, exhaustion_score - 42.0) * 0.32
        - max(0.0, weakness_score - 48.0) * 0.16
        - max(0.0, chase_pressure - 45.0) * 0.24,
        0.0,
        100.0,
    )
    continuation_score = cap_late_momentum_score(continuation_score, chase_pressure, exhaustion_score)

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
        + max(0.0, fresh_momentum_score - 55.0) * 0.20
        - max(0.0, exhaustion_score - 42.0) * 0.38
        - max(0.0, weakness_score - 52.0) * 0.22
        - max(0.0, chase_pressure - 45.0) * 0.28,
        0.0,
        100.0,
    )
    momentum_score = cap_late_momentum_score(momentum_score, chase_pressure, exhaustion_score)

    reversal_risk_score = clamp(
        weakness_score * 0.58
        + exhaustion_score * 0.28
        + chase_pressure * 0.14,
        0.0,
        100.0,
    )

    return MomentumSnapshot(
        symbol=sensor.symbol,
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
        reason_codes=reason_codes,
        raw={
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
            "fresh_momentum_score": fresh_momentum_score,
            "exhaustion_score": exhaustion_score,
            "chase_pressure": chase_pressure,
            "fresh_reasons": fresh_reasons,
            "exhaustion_reasons": exhaustion_reasons,
            "weakness_reasons": weakness_reasons,
            "directional_power_gap": _directional_power_gap(sensor, d),
            "directional_rsi_slope": _directional_rsi_slope(sensor, d),
            "directional_macd_slope": _directional_macd_slope(sensor, d),
            "sensor_created_at": sensor.created_at,
        },
    )


def build_momentum_snapshot_from_market(market_snapshot: MarketSnapshot, direction: str) -> MomentumSnapshot:
    """Convenience helper for tests/backfills; later code usually uses technical_sensors first."""
    from technical_sensors import build_sensor_snapshot

    sensor = build_sensor_snapshot(market_snapshot)
    return build_momentum_snapshot(sensor, direction)


def validate_momentum_snapshot(snapshot: MomentumSnapshot) -> dict[str, Any]:
    """Lightweight validation for momentum snapshot."""
    errors: list[str] = []

    if not snapshot.symbol:
        errors.append("missing_symbol")
    if snapshot.direction not in {DIRECTION_LONG, DIRECTION_SHORT}:
        errors.append("invalid_direction")

    for key in ["momentum_score", "continuation_score", "reversal_risk_score", "acceleration_score", "weakness_score"]:
        value = safe_float(getattr(snapshot, key), -1.0)
        if value is None or not (0.0 <= value <= 100.0):
            errors.append(f"invalid_{key}")

    return {
        "valid": not errors,
        "errors": errors,
        "symbol": snapshot.symbol,
        "direction": snapshot.direction,
        "momentum_score": snapshot.momentum_score,
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
    "score_fresh_momentum",
    "score_exhaustion",
    "calculate_chase_pressure",
    "apply_chase_pressure_to_component",
    "cap_late_momentum_score",
    "combine_momentum_score",
    "build_momentum_snapshot",
    "build_momentum_snapshot_from_market",
    "validate_momentum_snapshot",
]
