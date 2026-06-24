"""
timing_engine.py
Level 4 / 1H Smart Scalp Bot

Timing / pattern alignment engine for 1H Smart Scalp.

Architecture lock:
- Scores entry timing quality only.
- Does not make final REAL/GHOST/REJECT decisions.
- Does not place orders, monitor positions, write JSON state, or build Telegram text.
- Uses already-built snapshots; no market fetching here.
- Output is a stable TimingSnapshot-like dict to avoid modifying locked models.py.
- Allowed project imports:
  constants.py, utils.py, models.py, momentum_engine.py only.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from constants import DIRECTION_LONG, DIRECTION_SHORT, SYSTEM_VERSION
from models import LiquiditySnapshot, MarketContextSnapshot, MomentumSnapshot, SensorSnapshot, StructureSnapshot
from momentum_engine import (
    macd_hist_slope_ok,
    power_shift_ok,
    price_ema_alignment_ok,
    price_vwap_alignment_ok,
    rsi_slope_ok,
)
from utils import clamp, normalize_direction, normalize_symbol, safe_float, safe_str, utc_now_iso


TIMING_ENGINE_VERSION: str = SYSTEM_VERSION


# =============================================================================
# Output contract
# =============================================================================

def make_timing_snapshot(
    *,
    symbol: str,
    direction: str,
    timing_score: float,
    entry_quality: str,
    early_score: float,
    late_risk_score: float,
    pattern_alignment_score: float,
    wait_for_better_entry: bool,
    reason_codes: list[str],
    raw: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Create stable TimingSnapshot-like dict without modifying models.py."""
    return {
        "system_version": SYSTEM_VERSION,
        "created_at": utc_now_iso(),
        "symbol": normalize_symbol(symbol),
        "direction": normalize_direction(direction),
        "timing_score": clamp(timing_score, 0.0, 100.0),
        "entry_quality": safe_str(entry_quality, "UNKNOWN").upper(),
        "early_score": clamp(early_score, 0.0, 100.0),
        "late_risk_score": clamp(late_risk_score, 0.0, 100.0),
        "pattern_alignment_score": clamp(pattern_alignment_score, 0.0, 100.0),
        "wait_for_better_entry": bool(wait_for_better_entry),
        "reason_codes": list(reason_codes),
        "raw": dict(raw or {}),
    }


# =============================================================================
# Component scoring
# =============================================================================

def score_early_timing(sensor: SensorSnapshot, momentum: MomentumSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score whether the move is fresh enough and not too late."""
    d = normalize_direction(direction)
    score = 50.0
    reasons: list[str] = []

    if rsi_slope_ok(sensor, d, min_abs_slope=0.05):
        score += 12.0
        reasons.append("TIME_RSI_TURN_OK")
    else:
        score -= 6.0
        reasons.append("TIME_RSI_TURN_WEAK")

    if macd_hist_slope_ok(sensor, d):
        score += 16.0
        reasons.append("TIME_MACD_ACCEL_OK")
    else:
        score -= 10.0
        reasons.append("TIME_MACD_ACCEL_WEAK")

    if power_shift_ok(sensor, d, min_gap=3.0):
        score += 14.0
        reasons.append("TIME_POWER_SHIFT_OK")
    else:
        score -= 8.0
        reasons.append("TIME_POWER_SHIFT_WEAK")

    acceleration = safe_float(momentum.acceleration_score, 50.0) or 50.0
    if acceleration >= 65:
        score += 12.0
        reasons.append("TIME_ACCELERATION_GOOD")
    elif acceleration <= 42:
        score -= 12.0
        reasons.append("TIME_ACCELERATION_BAD")

    return clamp(score, 0.0, 100.0), reasons


def score_late_risk(
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
    reversal_snapshot: Optional[Mapping[str, Any]] = None,
) -> tuple[float, list[str]]:
    """Score risk that entry is late or exhausted."""
    score = 0.0
    reasons: list[str] = []

    if structure.is_late_move:
        score += 32.0
        reasons.append("TIME_LATE_STRUCTURE")

    if safe_float(structure.fresh_zone_score, 50.0) <= 35:
        score += 18.0
        reasons.append("TIME_FRESH_ZONE_WEAK")

    if safe_float(momentum.weakness_score, 0.0) >= 55:
        score += 18.0
        reasons.append("TIME_WEAKNESS_VISIBLE")

    if safe_float(liquidity.trap_risk_score, 0.0) >= 60:
        score += 18.0
        reasons.append("TIME_TRAP_RISK_HIGH")

    if reversal_snapshot:
        reversal_probability = safe_float(reversal_snapshot.get("reversal_probability"), 0.0) or 0.0
        exhaustion_probability = safe_float(reversal_snapshot.get("exhaustion_probability"), 0.0) or 0.0

        if reversal_probability >= 65:
            score += 18.0
            reasons.append("TIME_REVERSAL_PROB_HIGH")
        elif reversal_probability >= 50:
            score += 10.0
            reasons.append("TIME_REVERSAL_PROB_MEDIUM")

        if exhaustion_probability >= 65:
            score += 16.0
            reasons.append("TIME_EXHAUSTION_HIGH")

    if not reasons:
        reasons.append("TIME_NOT_LATE")

    return clamp(score, 0.0, 100.0), reasons


def score_pattern_alignment(
    sensor: SensorSnapshot,
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
    context: MarketContextSnapshot,
    direction: str,
) -> tuple[float, list[str]]:
    """Score alignment of prepared Level 4 pattern components."""
    d = normalize_direction(direction)
    score = 50.0
    reasons: list[str] = []

    if safe_float(structure.structure_score, 0.0) >= 60:
        score += 12.0
        reasons.append("TIME_STRUCTURE_ALIGNED")
    else:
        score -= 8.0
        reasons.append("TIME_STRUCTURE_WEAK")

    if safe_float(momentum.momentum_score, 0.0) >= 62:
        score += 14.0
        reasons.append("TIME_MOMENTUM_ALIGNED")
    else:
        score -= 8.0
        reasons.append("TIME_MOMENTUM_WEAK")

    if safe_float(momentum.continuation_score, 0.0) >= 58:
        score += 10.0
        reasons.append("TIME_CONTINUATION_OK")
    else:
        score -= 8.0
        reasons.append("TIME_CONTINUATION_WEAK")

    if safe_float(liquidity.trap_risk_score, 0.0) <= 45:
        score += 10.0
        reasons.append("TIME_TRAP_ACCEPTABLE")
    else:
        score -= 14.0
        reasons.append("TIME_TRAP_NOT_ACCEPTABLE")

    if context.aligned_with_direction:
        score += 8.0
        reasons.append("TIME_CONTEXT_ALIGNED")
    else:
        score -= 6.0
        reasons.append("TIME_CONTEXT_NOT_ALIGNED")

    if price_ema_alignment_ok(sensor, d) and price_vwap_alignment_ok(sensor, d):
        score += 10.0
        reasons.append("TIME_PRICE_EMA_VWAP_OK")
    elif price_ema_alignment_ok(sensor, d) or price_vwap_alignment_ok(sensor, d):
        score += 4.0
        reasons.append("TIME_PRICE_PARTIAL_ALIGNMENT")
    else:
        score -= 10.0
        reasons.append("TIME_PRICE_NOT_ALIGNED")

    return clamp(score, 0.0, 100.0), reasons


def classify_entry_quality(timing_score: float, late_risk_score: float) -> str:
    """Classify timing quality."""
    timing = safe_float(timing_score, 0.0) or 0.0
    late = safe_float(late_risk_score, 0.0) or 0.0

    if timing >= 78 and late <= 28:
        return "EXCELLENT"
    if timing >= 68 and late <= 40:
        return "GOOD"
    if timing >= 55 and late <= 55:
        return "ACCEPTABLE"
    if timing >= 45:
        return "WEAK"
    return "BAD"


def should_wait_for_better_entry(timing_score: float, late_risk_score: float, reversal_probability: float = 0.0) -> bool:
    """
    Suggest waiting when timing is weak/late.

    This is not final reject; AI Brain decides final action.
    """
    timing = safe_float(timing_score, 0.0) or 0.0
    late = safe_float(late_risk_score, 0.0) or 0.0
    rev = safe_float(reversal_probability, 0.0) or 0.0

    if late >= 70:
        return True
    if rev >= 70:
        return True
    if timing < 50 and late >= 45:
        return True
    return False


# =============================================================================
# Builder / validator
# =============================================================================

def build_timing_snapshot(
    *,
    sensor: SensorSnapshot,
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
    context: MarketContextSnapshot,
    direction: str,
    reversal_snapshot: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build TimingSnapshot-like dict from existing snapshots."""
    d = normalize_direction(direction)
    reason_codes: list[str] = []

    early_score, early_reasons = score_early_timing(sensor, momentum, d)
    late_risk_score, late_reasons = score_late_risk(structure, momentum, liquidity, reversal_snapshot)
    pattern_score, pattern_reasons = score_pattern_alignment(sensor, structure, momentum, liquidity, context, d)

    reason_codes.extend(early_reasons)
    reason_codes.extend(late_reasons)
    reason_codes.extend(pattern_reasons)

    reversal_probability = 0.0
    if reversal_snapshot:
        reversal_probability = safe_float(reversal_snapshot.get("reversal_probability"), 0.0) or 0.0

    timing_score = (
        early_score * 0.34
        + pattern_score * 0.46
        + (100.0 - late_risk_score) * 0.20
    )

    # Reversal probability softly reduces timing quality.
    timing_score -= reversal_probability * 0.12
    timing_score = clamp(timing_score, 0.0, 100.0)

    quality = classify_entry_quality(timing_score, late_risk_score)
    wait = should_wait_for_better_entry(timing_score, late_risk_score, reversal_probability)

    if quality in {"EXCELLENT", "GOOD"}:
        reason_codes.append("TIMING_QUALITY_OK")
    elif quality == "ACCEPTABLE":
        reason_codes.append("TIMING_QUALITY_ACCEPTABLE")
    else:
        reason_codes.append("TIMING_QUALITY_WEAK")

    if wait:
        reason_codes.append("WAIT_FOR_BETTER_ENTRY")

    return make_timing_snapshot(
        symbol=sensor.symbol or structure.symbol or liquidity.symbol,
        direction=d,
        timing_score=timing_score,
        entry_quality=quality,
        early_score=early_score,
        late_risk_score=late_risk_score,
        pattern_alignment_score=pattern_score,
        wait_for_better_entry=wait,
        reason_codes=reason_codes,
        raw={
            "reversal_probability": reversal_probability,
            "sensor_created_at": sensor.created_at,
            "structure_created_at": structure.created_at,
            "momentum_created_at": momentum.created_at,
            "liquidity_created_at": liquidity.created_at,
            "context_created_at": context.created_at,
        },
    )


def validate_timing_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Lightweight validation for TimingSnapshot-like dict."""
    errors: list[str] = []

    if safe_str(snapshot.get("system_version")) != SYSTEM_VERSION:
        errors.append("invalid_system_version")

    if not normalize_symbol(snapshot.get("symbol")):
        errors.append("missing_symbol")

    if normalize_direction(snapshot.get("direction")) not in {DIRECTION_LONG, DIRECTION_SHORT}:
        errors.append("invalid_direction")

    for key in ["timing_score", "early_score", "late_risk_score", "pattern_alignment_score"]:
        value = safe_float(snapshot.get(key), None)
        if value is None or not (0.0 <= value <= 100.0):
            errors.append(f"invalid_{key}")

    if safe_str(snapshot.get("entry_quality")).upper() not in {"EXCELLENT", "GOOD", "ACCEPTABLE", "WEAK", "BAD"}:
        errors.append("invalid_entry_quality")

    return {
        "valid": not errors,
        "errors": errors,
        "symbol": normalize_symbol(snapshot.get("symbol")),
        "direction": normalize_direction(snapshot.get("direction")),
        "entry_quality": safe_str(snapshot.get("entry_quality")).upper(),
    }


__all__ = [
    "TIMING_ENGINE_VERSION",
    "make_timing_snapshot",
    "score_early_timing",
    "score_late_risk",
    "score_pattern_alignment",
    "classify_entry_quality",
    "should_wait_for_better_entry",
    "build_timing_snapshot",
    "validate_timing_snapshot",
]
