"""
reversal_engine.py
Level 4 / 1H Smart Scalp Bot

Continuation vs Reversal Probability engine.

Architecture lock:
- Estimates continuation, reversal, exhaustion, and weakness probabilities only.
- Does not modify models.py; output is a stable ReversalSnapshot-like dict.
- Does not make final REAL/GHOST/REJECT decisions.
- Does not place orders, monitor positions, write JSON state, or build Telegram text.
- Uses already-built snapshots; no market fetching here.
- Allowed project imports:
  constants.py, utils.py, models.py, momentum_engine.py only.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from constants import DIRECTION_LONG, DIRECTION_SHORT, SYSTEM_VERSION
from models import (
    LiquiditySnapshot,
    MarketContextSnapshot,
    MomentumSnapshot,
    SensorSnapshot,
    StructureSnapshot,
)
from momentum_engine import (
    macd_hist_slope_ok,
    power_shift_ok,
    price_ema_alignment_ok,
    price_vwap_alignment_ok,
    rsi_slope_ok,
)
from utils import clamp, normalize_direction, normalize_symbol, safe_float, safe_str, utc_now_iso


REVERSAL_ENGINE_VERSION: str = SYSTEM_VERSION


# =============================================================================
# Output contract
# =============================================================================

def make_reversal_snapshot(
    *,
    symbol: str,
    direction: str,
    continuation_probability: float,
    reversal_probability: float,
    exhaustion_probability: float,
    weakness_level: str,
    continuation_score: float,
    reversal_score: float,
    reason_codes: list[str],
    raw: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """
    Create a stable ReversalSnapshot-like dict.

    Kept as a dict intentionally so models.py remains unchanged after being
    created and checked earlier in the build sequence.
    """
    return {
        "system_version": SYSTEM_VERSION,
        "created_at": utc_now_iso(),
        "symbol": normalize_symbol(symbol),
        "direction": normalize_direction(direction),
        "continuation_probability": clamp(continuation_probability, 0.0, 100.0),
        "reversal_probability": clamp(reversal_probability, 0.0, 100.0),
        "exhaustion_probability": clamp(exhaustion_probability, 0.0, 100.0),
        "weakness_level": safe_str(weakness_level, "UNKNOWN").upper(),
        "continuation_score": clamp(continuation_score, 0.0, 100.0),
        "reversal_score": clamp(reversal_score, 0.0, 100.0),
        "reason_codes": list(reason_codes),
        "raw": dict(raw or {}),
    }


# =============================================================================
# Component scores
# =============================================================================

def score_sensor_weakness(sensor: SensorSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score weakness directly from raw sensor alignment."""
    d = normalize_direction(direction)
    score = 0.0
    reasons: list[str] = []

    if not rsi_slope_ok(sensor, d, min_abs_slope=0.05):
        score += 18.0
        reasons.append("REV_RSI_SLOPE_WEAK")

    if not macd_hist_slope_ok(sensor, d):
        score += 24.0
        reasons.append("REV_MACD_SLOPE_WEAK")

    if not power_shift_ok(sensor, d, min_gap=2.0):
        score += 18.0
        reasons.append("REV_POWER_WEAK")

    if not price_ema_alignment_ok(sensor, d):
        score += 14.0
        reasons.append("REV_EMA_LOST")

    if not price_vwap_alignment_ok(sensor, d):
        score += 12.0
        reasons.append("REV_VWAP_LOST")

    rsi = safe_float(sensor.rsi, None)
    if rsi is not None:
        if d == DIRECTION_LONG and rsi >= 74:
            score += 10.0
            reasons.append("REV_LONG_RSI_OVERHEATED")
        elif d == DIRECTION_SHORT and rsi <= 26:
            score += 10.0
            reasons.append("REV_SHORT_RSI_OVERHEATED")

    return clamp(score, 0.0, 100.0), reasons


def score_structure_reversal_risk(structure: StructureSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score reversal risk from structure location."""
    d = normalize_direction(direction)
    score = 0.0
    reasons: list[str] = []

    if structure.is_late_move:
        score += 30.0
        reasons.append("REV_STRUCTURE_LATE_MOVE")

    if structure.is_range:
        score += 14.0
        reasons.append("REV_STRUCTURE_RANGE")

    if safe_float(structure.fresh_zone_score, 50.0) <= 35:
        score += 18.0
        reasons.append("REV_FRESH_ZONE_WEAK")

    trend = safe_str(structure.trend).upper()
    if trend == "UPTREND" and d == DIRECTION_SHORT:
        score += 12.0
        reasons.append("REV_COUNTER_UPTREND")
    elif trend == "DOWNTREND" and d == DIRECTION_LONG:
        score += 12.0
        reasons.append("REV_COUNTER_DOWNTREND")

    raw = structure.raw or {}
    if d == DIRECTION_LONG:
        resistance_distance = safe_float(raw.get("resistance_distance_pct"), None)
        if resistance_distance is not None and resistance_distance <= 0.35:
            score += 12.0
            reasons.append("REV_NEAR_RESISTANCE")
    elif d == DIRECTION_SHORT:
        support_distance = safe_float(raw.get("support_distance_pct"), None)
        if support_distance is not None and support_distance <= 0.35:
            score += 12.0
            reasons.append("REV_NEAR_SUPPORT")

    return clamp(score, 0.0, 100.0), reasons


def score_momentum_reversal_risk(momentum: MomentumSnapshot) -> tuple[float, list[str]]:
    """Score reversal risk from momentum snapshot."""
    score = 0.0
    reasons: list[str] = []

    weakness = safe_float(momentum.weakness_score, 0.0) or 0.0
    reversal = safe_float(momentum.reversal_risk_score, 0.0) or 0.0
    continuation = safe_float(momentum.continuation_score, 50.0) or 50.0
    acceleration = safe_float(momentum.acceleration_score, 50.0) or 50.0

    score += weakness * 0.35
    score += reversal * 0.30
    score += max(0.0, 50.0 - continuation) * 0.25
    score += max(0.0, 50.0 - acceleration) * 0.20

    if weakness >= 65:
        reasons.append("REV_MOMENTUM_WEAKNESS_HIGH")
    elif weakness >= 45:
        reasons.append("REV_MOMENTUM_WEAKNESS_MEDIUM")

    if continuation <= 40:
        reasons.append("REV_CONTINUATION_WEAK")

    if acceleration <= 42:
        reasons.append("REV_ACCELERATION_WEAK")

    return clamp(score, 0.0, 100.0), reasons


def score_liquidity_reversal_risk(liquidity: LiquiditySnapshot) -> tuple[float, list[str]]:
    """Score reversal risk from liquidity/trap snapshot."""
    score = 0.0
    reasons: list[str] = []

    trap = safe_float(liquidity.trap_risk_score, 0.0) or 0.0
    sweep = safe_float(liquidity.liquidity_sweep_score, 0.0) or 0.0
    fake = safe_float(liquidity.fake_break_risk, 0.0) or 0.0
    wick = safe_float(liquidity.wick_rejection_score, 0.0) or 0.0
    survival = safe_float(liquidity.breakout_survival_score, 50.0) or 50.0

    score += trap * 0.35
    score += sweep * 0.20
    score += fake * 0.25
    score += wick * 0.10
    score += max(0.0, 50.0 - survival) * 0.20

    if liquidity.stop_hunt_detected:
        score += 10.0
        reasons.append("REV_STOP_HUNT_DETECTED")

    if liquidity.likely_trap:
        score += 15.0
        reasons.append("REV_LIKELY_TRAP")

    if fake >= 60:
        reasons.append("REV_FAKE_BREAK_HIGH")

    if survival <= 40:
        reasons.append("REV_BREAKOUT_SURVIVAL_WEAK")

    return clamp(score, 0.0, 100.0), reasons


def score_context_reversal_risk(context: MarketContextSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score reversal risk from broad market context."""
    d = normalize_direction(direction)
    score = 0.0
    reasons: list[str] = []

    context_score = safe_float(context.context_score, 50.0) or 50.0
    market_risk = safe_float(context.market_risk_score, 50.0) or 50.0

    score += max(0.0, 50.0 - context_score) * 0.55
    score += max(0.0, market_risk - 45.0) * 0.35

    if context.choppy:
        score += 12.0
        reasons.append("REV_CONTEXT_CHOPPY")

    if not context.aligned_with_direction:
        score += 10.0
        reasons.append("REV_CONTEXT_NOT_ALIGNED")

    mode = safe_str(context.market_mode).upper()
    if mode == "BULLISH" and d == DIRECTION_SHORT:
        score += 8.0
        reasons.append("REV_SHORT_AGAINST_BULL_MARKET")
    elif mode == "BEARISH" and d == DIRECTION_LONG:
        score += 8.0
        reasons.append("REV_LONG_AGAINST_BEAR_MARKET")

    return clamp(score, 0.0, 100.0), reasons


# =============================================================================
# Probabilities
# =============================================================================

def classify_weakness_level(reversal_probability: float, exhaustion_probability: float) -> str:
    """Classify weakness level."""
    rev = safe_float(reversal_probability, 0.0) or 0.0
    exh = safe_float(exhaustion_probability, 0.0) or 0.0

    if rev >= 75 or exh >= 80:
        return "VERY_HIGH"
    if rev >= 62 or exh >= 65:
        return "HIGH"
    if rev >= 45 or exh >= 48:
        return "MEDIUM"
    if rev >= 30 or exh >= 32:
        return "LOW"
    return "VERY_LOW"


def calculate_continuation_probability(
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
    context: MarketContextSnapshot,
    reversal_risk: float,
) -> float:
    """Calculate continuation probability from aligned components."""
    structure_score = safe_float(structure.structure_score, 50.0) or 50.0
    momentum_score = safe_float(momentum.momentum_score, 50.0) or 50.0
    continuation_score = safe_float(momentum.continuation_score, 50.0) or 50.0
    survival_score = safe_float(liquidity.breakout_survival_score, 50.0) or 50.0
    context_score = safe_float(context.context_score, 50.0) or 50.0

    probability = (
        structure_score * 0.22
        + momentum_score * 0.27
        + continuation_score * 0.20
        + survival_score * 0.16
        + context_score * 0.15
    )

    probability -= (safe_float(reversal_risk, 0.0) or 0.0) * 0.25

    return clamp(probability, 0.0, 100.0)


def calculate_exhaustion_probability(
    sensor: SensorSnapshot,
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
) -> tuple[float, list[str]]:
    """Calculate move exhaustion probability."""
    score = 0.0
    reasons: list[str] = []

    if structure.is_late_move:
        score += 32.0
        reasons.append("EXH_LATE_STRUCTURE")

    if safe_float(momentum.acceleration_score, 50.0) <= 42:
        score += 18.0
        reasons.append("EXH_ACCELERATION_FADING")

    if safe_float(momentum.weakness_score, 0.0) >= 55:
        score += 20.0
        reasons.append("EXH_WEAKNESS_VISIBLE")

    if safe_float(liquidity.wick_rejection_score, 0.0) >= 55:
        score += 16.0
        reasons.append("EXH_WICK_REJECTION")

    rsi = safe_float(sensor.rsi, None)
    if rsi is not None and (rsi >= 76 or rsi <= 24):
        score += 12.0
        reasons.append("EXH_RSI_EXTREME")

    if not reasons:
        reasons.append("EXH_NORMAL")

    return clamp(score, 0.0, 100.0), reasons


# =============================================================================
# Builder / validator
# =============================================================================

def build_reversal_snapshot(
    *,
    sensor: SensorSnapshot,
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
    context: MarketContextSnapshot,
    direction: str,
) -> dict[str, Any]:
    """Build ReversalSnapshot-like dict from existing snapshots."""
    d = normalize_direction(direction)
    reason_codes: list[str] = []

    sensor_risk, sensor_reasons = score_sensor_weakness(sensor, d)
    structure_risk, structure_reasons = score_structure_reversal_risk(structure, d)
    momentum_risk, momentum_reasons = score_momentum_reversal_risk(momentum)
    liquidity_risk, liquidity_reasons = score_liquidity_reversal_risk(liquidity)
    context_risk, context_reasons = score_context_reversal_risk(context, d)
    exhaustion_probability, exhaustion_reasons = calculate_exhaustion_probability(sensor, structure, momentum, liquidity)

    reason_codes.extend(sensor_reasons)
    reason_codes.extend(structure_reasons)
    reason_codes.extend(momentum_reasons)
    reason_codes.extend(liquidity_reasons)
    reason_codes.extend(context_reasons)
    reason_codes.extend(exhaustion_reasons)

    reversal_score = (
        sensor_risk * 0.22
        + structure_risk * 0.18
        + momentum_risk * 0.26
        + liquidity_risk * 0.22
        + context_risk * 0.12
    )

    # Exhaustion can turn into reversal but should not dominate alone.
    reversal_probability = (reversal_score * 0.78) + (exhaustion_probability * 0.22)

    continuation_probability = calculate_continuation_probability(
        structure=structure,
        momentum=momentum,
        liquidity=liquidity,
        context=context,
        reversal_risk=reversal_probability,
    )

    weakness_level = classify_weakness_level(reversal_probability, exhaustion_probability)

    if reversal_probability >= 70:
        reason_codes.append("REVERSAL_PROBABILITY_HIGH")
    elif reversal_probability >= 50:
        reason_codes.append("REVERSAL_PROBABILITY_MEDIUM")
    else:
        reason_codes.append("REVERSAL_PROBABILITY_LOW")

    if continuation_probability >= 65:
        reason_codes.append("CONTINUATION_PROBABILITY_OK")
    elif continuation_probability <= 40:
        reason_codes.append("CONTINUATION_PROBABILITY_WEAK")

    return make_reversal_snapshot(
        symbol=sensor.symbol or structure.symbol or liquidity.symbol,
        direction=d,
        continuation_probability=continuation_probability,
        reversal_probability=reversal_probability,
        exhaustion_probability=exhaustion_probability,
        weakness_level=weakness_level,
        continuation_score=continuation_probability,
        reversal_score=reversal_score,
        reason_codes=reason_codes,
        raw={
            "sensor_risk": sensor_risk,
            "structure_risk": structure_risk,
            "momentum_risk": momentum_risk,
            "liquidity_risk": liquidity_risk,
            "context_risk": context_risk,
            "sensor_created_at": sensor.created_at,
            "structure_created_at": structure.created_at,
            "momentum_created_at": momentum.created_at,
            "liquidity_created_at": liquidity.created_at,
            "context_created_at": context.created_at,
        },
    )


def validate_reversal_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Lightweight validation for ReversalSnapshot-like dict."""
    errors: list[str] = []

    if safe_str(snapshot.get("system_version")) != SYSTEM_VERSION:
        errors.append("invalid_system_version")

    if not normalize_symbol(snapshot.get("symbol")):
        errors.append("missing_symbol")

    if normalize_direction(snapshot.get("direction")) not in {DIRECTION_LONG, DIRECTION_SHORT}:
        errors.append("invalid_direction")

    for key in [
        "continuation_probability",
        "reversal_probability",
        "exhaustion_probability",
        "continuation_score",
        "reversal_score",
    ]:
        value = safe_float(snapshot.get(key), None)
        if value is None or not (0.0 <= value <= 100.0):
            errors.append(f"invalid_{key}")

    if safe_str(snapshot.get("weakness_level")).upper() not in {"VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"}:
        errors.append("invalid_weakness_level")

    return {
        "valid": not errors,
        "errors": errors,
        "symbol": normalize_symbol(snapshot.get("symbol")),
        "direction": normalize_direction(snapshot.get("direction")),
        "weakness_level": safe_str(snapshot.get("weakness_level")).upper(),
    }


__all__ = [
    "REVERSAL_ENGINE_VERSION",
    "make_reversal_snapshot",
    "score_sensor_weakness",
    "score_structure_reversal_risk",
    "score_momentum_reversal_risk",
    "score_liquidity_reversal_risk",
    "score_context_reversal_risk",
    "classify_weakness_level",
    "calculate_continuation_probability",
    "calculate_exhaustion_probability",
    "build_reversal_snapshot",
    "validate_reversal_snapshot",
]
