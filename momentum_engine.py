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

    reason_codes.extend(rsi_reasons)
    reason_codes.extend(macd_reasons)
    reason_codes.extend(power_reasons)
    reason_codes.extend(volume_reasons)
    reason_codes.extend(ema_vwap_reasons)
    reason_codes.extend(acceleration_reasons)

    if weakness_score >= 60:
        reason_codes.append("WEAKNESS_HIGH")
    elif weakness_score >= 40:
        reason_codes.append("WEAKNESS_MEDIUM")
    else:
        reason_codes.append("WEAKNESS_LOW")

    continuation_score = combine_momentum_score([
        macd_score,
        power_score,
        volume_score,
        ema_vwap_score,
    ])

    momentum_score = combine_momentum_score([
        rsi_score,
        macd_score,
        power_score,
        volume_score,
        ema_vwap_score,
        acceleration_score,
    ])

    reversal_risk_score = weakness_score

    return MomentumSnapshot(
        symbol=sensor.symbol,
        direction=d,
        momentum_score=momentum_score,
        continuation_score=continuation_score,
        reversal_risk_score=reversal_risk_score,
        acceleration_score=acceleration_score,
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
            "weakness_reasons": weakness_reasons,
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
    "combine_momentum_score",
    "build_momentum_snapshot",
    "build_momentum_snapshot_from_market",
    "validate_momentum_snapshot",
]
