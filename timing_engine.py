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

Core rule:
- Hunt the start of a pump/dump movement, not the middle/end of it.
- Do not wait for late candle confirmation.
- Prefer early evidence: fresh momentum, ATR expansion start, micro-structure shift,
  power shift, MACD histogram acceleration, RSI slope turn, and enough room to target.
- Penalize late/finished movement: extended structure, exhaustion, old move age,
  high reversal probability, high weakness, and trap risk.
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
# Safe helpers
# =============================================================================

def _num(value: Any, default: float = 0.0) -> float:
    """Return a safe float while preserving valid zero values."""
    parsed = safe_float(value, None)
    if parsed is None:
        return float(default)
    return float(parsed)


def _bool(value: Any, default: bool = False) -> bool:
    """Read booleans safely, including common string representations."""
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
    """Read .raw safely from dataclass-like snapshots."""
    raw = getattr(obj, "raw", None) or {}
    if isinstance(raw, Mapping):
        return raw
    return {}


def _nested_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


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
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "raw": dict(raw or {}),
    }


# =============================================================================
# Cross-snapshot diagnostics
# =============================================================================

def _momentum_raw_value(momentum: MomentumSnapshot, key: str, default: float = 0.0) -> float:
    """Read extra momentum diagnostics stored in MomentumSnapshot.raw safely."""
    return _num(_raw_mapping(momentum).get(key), default)


def _structure_start_raw(structure: Optional[StructureSnapshot]) -> Mapping[str, Any]:
    """Read structure_engine move_start_zone diagnostics safely."""
    if structure is None:
        return {}
    raw = _raw_mapping(structure)
    nested = _nested_mapping(raw.get("move_start_zone"))
    if nested:
        return nested
    # Compatibility: structure_engine also exposes these values at raw top-level.
    return raw


def structure_move_start_active(structure: Optional[StructureSnapshot]) -> bool:
    raw = _structure_start_raw(structure)
    return _bool(raw.get("active"), False)


def structure_move_start_score(structure: Optional[StructureSnapshot]) -> float:
    raw = _structure_start_raw(structure)
    return _num(raw.get("score"), 0.0)


def structure_move_extended(structure: Optional[StructureSnapshot]) -> bool:
    raw = _structure_start_raw(structure)
    return _bool(raw.get("move_already_extended"), False) or bool(getattr(structure, "is_late_move", False))


def score_structure_start_alignment(structure: StructureSnapshot) -> tuple[float, list[str]]:
    """Score timing directly from structure_engine start-of-move diagnostics."""
    raw = _structure_start_raw(structure)
    score = 45.0
    reasons: list[str] = []

    active = _bool(raw.get("active"), False)
    start_score = _num(raw.get("score"), 0.0)
    compression = _bool(raw.get("compression"), False)
    atr_start = _bool(raw.get("atr_expansion_start"), False)
    volume_start = _bool(raw.get("volume_pressure_start"), False)
    micro_shift = _bool(raw.get("micro_structure_shift"), False)
    sd_reaction = _bool(raw.get("supply_demand_reaction"), False)
    room_ok = _bool(raw.get("room_to_target"), True)
    extended = _bool(raw.get("move_already_extended"), False) or bool(getattr(structure, "is_late_move", False))

    if active:
        score += 24.0
        reasons.append("TIME_STRUCTURE_START_ZONE_ACTIVE")
    elif start_score >= 55:
        score += 12.0
        reasons.append("TIME_STRUCTURE_START_FORMING")
    elif start_score <= 28:
        score -= 12.0
        reasons.append("TIME_STRUCTURE_START_WEAK")

    if compression:
        score += 7.0
        reasons.append("TIME_COMPRESSION_BASE_OK")
    if atr_start:
        score += 14.0
        reasons.append("TIME_ATR_EXPANSION_START_OK")
    if volume_start:
        score += 8.0
        reasons.append("TIME_VOLUME_PRESSURE_START_OK")
    if micro_shift:
        score += 14.0
        reasons.append("TIME_MICRO_STRUCTURE_SHIFT_OK")
    if sd_reaction:
        score += 10.0
        reasons.append("TIME_SUPPLY_DEMAND_REACTION_OK")

    if room_ok:
        score += 8.0
        reasons.append("TIME_ROOM_TO_TARGET_OK")
    else:
        score -= 18.0
        reasons.append("TIME_ROOM_TO_TARGET_WEAK")

    if extended:
        score -= 42.0
        reasons.append("TIME_STRUCTURE_MOVE_EXTENDED")

    if not reasons:
        reasons.append("TIME_STRUCTURE_START_NEUTRAL")

    return clamp(score, 0.0, 100.0), reasons


# =============================================================================
# Component scoring
# =============================================================================

def score_early_timing(
    sensor: SensorSnapshot,
    momentum: MomentumSnapshot,
    direction: str,
    structure: Optional[StructureSnapshot] = None,
) -> tuple[float, list[str]]:
    """Score whether the move is fresh and starting, not already finished."""
    d = normalize_direction(direction)
    score = 50.0
    reasons: list[str] = []

    if rsi_slope_ok(sensor, d, min_abs_slope=0.04):
        score += 12.0
        reasons.append("TIME_RSI_TURN_OK")
    else:
        score -= 5.0
        reasons.append("TIME_RSI_TURN_WEAK")

    if macd_hist_slope_ok(sensor, d):
        score += 16.0
        reasons.append("TIME_MACD_ACCEL_OK")
    else:
        score -= 9.0
        reasons.append("TIME_MACD_ACCEL_WEAK")

    if power_shift_ok(sensor, d, min_gap=3.0):
        score += 15.0
        reasons.append("TIME_POWER_SHIFT_OK")
    else:
        score -= 8.0
        reasons.append("TIME_POWER_SHIFT_WEAK")

    acceleration = _num(getattr(momentum, "acceleration_score", 50.0), 50.0)
    if acceleration >= 66:
        score += 13.0
        reasons.append("TIME_ACCELERATION_GOOD")
    elif acceleration >= 56:
        score += 6.0
        reasons.append("TIME_ACCELERATION_FORMING")
    elif acceleration <= 42:
        score -= 12.0
        reasons.append("TIME_ACCELERATION_BAD")

    fresh_momentum = _momentum_raw_value(momentum, "fresh_momentum_score", 50.0)
    exhaustion = _momentum_raw_value(momentum, "exhaustion_score", 0.0)
    move_age = _momentum_raw_value(momentum, "move_age_score", 50.0)

    if fresh_momentum >= 70:
        score += 18.0
        reasons.append("TIME_FRESH_MOMENTUM_STRONG")
    elif fresh_momentum >= 58:
        score += 9.0
        reasons.append("TIME_FRESH_MOMENTUM_OK")
    elif fresh_momentum <= 42:
        score -= 18.0
        reasons.append("TIME_FRESH_MOMENTUM_WEAK")

    if move_age <= 42 and fresh_momentum >= 55:
        score += 8.0
        reasons.append("TIME_MOVE_AGE_EARLY")
    elif move_age >= 70:
        score -= 16.0
        reasons.append("TIME_MOVE_AGE_LATE")

    if structure is not None:
        start_score, start_reasons = score_structure_start_alignment(structure)
        score += (start_score - 50.0) * 0.45
        reasons.extend(start_reasons)

    if exhaustion >= 70:
        score -= 28.0
        reasons.append("TIME_MOMENTUM_EXHAUSTED")
    elif exhaustion >= 55:
        score -= 16.0
        reasons.append("TIME_MOMENTUM_LATE_WARNING")
    elif exhaustion <= 28 and fresh_momentum >= 55:
        score += 6.0
        reasons.append("TIME_EXHAUSTION_LOW")

    return clamp(score, 0.0, 100.0), reasons


def score_late_risk(
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
    reversal_snapshot: Optional[Mapping[str, Any]] = None,
) -> tuple[float, list[str]]:
    """Score risk that entry is late, exhausted, reversed, or trap-prone."""
    score = 0.0
    reasons: list[str] = []

    start_raw = _structure_start_raw(structure)
    start_active = _bool(start_raw.get("active"), False)
    start_score = _num(start_raw.get("score"), 0.0)
    extended = _bool(start_raw.get("move_already_extended"), False) or bool(getattr(structure, "is_late_move", False))
    room_ok = _bool(start_raw.get("room_to_target"), True)

    if extended:
        score += 48.0
        reasons.append("TIME_LATE_STRUCTURE_EXTENDED")
    elif bool(getattr(structure, "is_late_move", False)):
        score += 42.0
        reasons.append("TIME_LATE_STRUCTURE")

    fresh_zone = _num(getattr(structure, "fresh_zone_score", 50.0), 50.0)
    if fresh_zone <= 30:
        score += 24.0
        reasons.append("TIME_FRESH_ZONE_VERY_WEAK")
    elif fresh_zone <= 42:
        score += 12.0
        reasons.append("TIME_FRESH_ZONE_WEAK")

    if not room_ok:
        score += 16.0
        reasons.append("TIME_NO_ROOM_TO_TARGET")

    weakness_score = _num(getattr(momentum, "weakness_score", 0.0), 0.0)
    if weakness_score >= 70:
        score += 30.0
        reasons.append("TIME_WEAKNESS_HIGH")
    elif weakness_score >= 55:
        score += 18.0
        reasons.append("TIME_WEAKNESS_VISIBLE")

    fresh_momentum = _momentum_raw_value(momentum, "fresh_momentum_score", 50.0)
    exhaustion = _momentum_raw_value(momentum, "exhaustion_score", 0.0)
    move_age = _momentum_raw_value(momentum, "move_age_score", 50.0)

    if fresh_momentum <= 35:
        score += 32.0
        reasons.append("TIME_FRESH_MOMENTUM_TOO_WEAK")
    elif fresh_momentum <= 48 and not start_active:
        score += 16.0
        reasons.append("TIME_FRESH_MOMENTUM_SOFT")

    if exhaustion >= 72:
        score += 38.0
        reasons.append("TIME_MOMENTUM_EXHAUSTION_HIGH")
    elif exhaustion >= 58:
        score += 24.0
        reasons.append("TIME_MOMENTUM_EXHAUSTION_MEDIUM")
    elif exhaustion >= 42:
        score += 9.0
        reasons.append("TIME_MOMENTUM_EXHAUSTION_EARLY_WARNING")

    if move_age >= 80:
        score += 30.0
        reasons.append("TIME_MOVE_AGE_VERY_LATE")
    elif move_age >= 68:
        score += 18.0
        reasons.append("TIME_MOVE_AGE_LATE")

    if _num(getattr(liquidity, "trap_risk_score", 0.0), 0.0) >= 70:
        score += 24.0
        reasons.append("TIME_TRAP_RISK_VERY_HIGH")
    elif _num(getattr(liquidity, "trap_risk_score", 0.0), 0.0) >= 58:
        score += 14.0
        reasons.append("TIME_TRAP_RISK_HIGH")

    if reversal_snapshot:
        reversal_probability = _num(reversal_snapshot.get("reversal_probability"), 0.0)
        exhaustion_probability = _num(reversal_snapshot.get("exhaustion_probability"), 0.0)

        if reversal_probability >= 72:
            score += 32.0
            reasons.append("TIME_REVERSAL_PROB_HIGH")
        elif reversal_probability >= 58:
            score += 18.0
            reasons.append("TIME_REVERSAL_PROB_MEDIUM")

        if exhaustion_probability >= 72:
            score += 28.0
            reasons.append("TIME_EXHAUSTION_HIGH")
        elif exhaustion_probability >= 58:
            score += 14.0
            reasons.append("TIME_EXHAUSTION_MEDIUM")

    # A real start zone can reduce soft late-risk, but never hides hard extension/exhaustion.
    if start_active and start_score >= 55 and not extended and exhaustion < 60:
        reduction = 16.0 if fresh_momentum >= 55 else 8.0
        score -= reduction
        reasons.append("TIME_START_ZONE_REDUCES_LATE_RISK")

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
    """Score Level 4 pattern alignment with start-of-move priority."""
    d = normalize_direction(direction)
    score = 50.0
    reasons: list[str] = []

    start_score, start_reasons = score_structure_start_alignment(structure)
    if start_score >= 68:
        score += 16.0
        reasons.append("TIME_START_PATTERN_ALIGNED")
    elif start_score >= 55:
        score += 7.0
        reasons.append("TIME_START_PATTERN_FORMING")
    elif start_score <= 35:
        score -= 14.0
        reasons.append("TIME_START_PATTERN_WEAK")
    reasons.extend(start_reasons)

    if _num(getattr(structure, "structure_score", 0.0), 0.0) >= 62:
        score += 9.0
        reasons.append("TIME_STRUCTURE_ALIGNED")
    elif _num(getattr(structure, "structure_score", 0.0), 0.0) <= 42:
        score -= 9.0
        reasons.append("TIME_STRUCTURE_WEAK")

    if _num(getattr(momentum, "momentum_score", 0.0), 0.0) >= 64:
        score += 13.0
        reasons.append("TIME_MOMENTUM_ALIGNED")
    elif _num(getattr(momentum, "momentum_score", 0.0), 0.0) <= 45:
        score -= 10.0
        reasons.append("TIME_MOMENTUM_WEAK")

    if _num(getattr(momentum, "continuation_score", 0.0), 0.0) >= 60:
        score += 9.0
        reasons.append("TIME_CONTINUATION_OK")
    elif _num(getattr(momentum, "continuation_score", 0.0), 0.0) <= 45:
        score -= 8.0
        reasons.append("TIME_CONTINUATION_WEAK")

    trap_risk = _num(getattr(liquidity, "trap_risk_score", 0.0), 0.0)
    if trap_risk <= 42:
        score += 8.0
        reasons.append("TIME_TRAP_ACCEPTABLE")
    elif trap_risk >= 62:
        score -= 16.0
        reasons.append("TIME_TRAP_NOT_ACCEPTABLE")

    if bool(getattr(context, "aligned_with_direction", False)):
        score += 8.0
        reasons.append("TIME_CONTEXT_ALIGNED")
    else:
        score -= 5.0
        reasons.append("TIME_CONTEXT_NOT_ALIGNED")

    ema_ok = price_ema_alignment_ok(sensor, d)
    vwap_ok = price_vwap_alignment_ok(sensor, d)
    if ema_ok and vwap_ok:
        score += 10.0
        reasons.append("TIME_PRICE_EMA_VWAP_OK")
    elif ema_ok or vwap_ok:
        score += 4.0
        reasons.append("TIME_PRICE_PARTIAL_ALIGNMENT")
    else:
        score -= 10.0
        reasons.append("TIME_PRICE_NOT_ALIGNED")

    return clamp(score, 0.0, 100.0), reasons


def classify_entry_quality(timing_score: float, late_risk_score: float) -> str:
    """Classify timing quality."""
    timing = _num(timing_score, 0.0)
    late = _num(late_risk_score, 0.0)

    if late >= 80:
        return "BAD"
    if late >= 66 and timing < 84:
        return "WEAK"
    if timing >= 84 and late <= 24:
        return "EXCELLENT"
    if timing >= 74 and late <= 36:
        return "GOOD"
    if timing >= 61 and late <= 50:
        return "ACCEPTABLE"
    if timing >= 48:
        return "WEAK"
    return "BAD"


def should_wait_for_better_entry(
    timing_score: float,
    late_risk_score: float,
    reversal_probability: float = 0.0,
    fresh_momentum_score: float = 50.0,
    exhaustion_score: float = 0.0,
    start_zone_active: bool = False,
) -> bool:
    """
    Suggest waiting when timing is weak/late.

    This is not final reject; AI Brain decides final action.
    """
    timing = _num(timing_score, 0.0)
    late = _num(late_risk_score, 0.0)
    rev = _num(reversal_probability, 0.0)
    fresh = _num(fresh_momentum_score, 50.0)
    exhaustion = _num(exhaustion_score, 0.0)
    start_ok = bool(start_zone_active)

    if exhaustion >= 68:
        return True
    if rev >= 68:
        return True
    if late >= 72:
        return True
    if exhaustion >= 58 and late >= 45:
        return True
    if rev >= 58 and late >= 45:
        return True
    if fresh <= 40 and late >= 35:
        return True
    if fresh <= 50 and late >= 60:
        return True
    if timing < 55 and late >= 40:
        return True

    # Do not wait only because the move lacks old-style confirmation when a fresh start zone is active.
    if start_ok and timing >= 58 and late <= 45 and fresh >= 52:
        return False

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

    early_score, early_reasons = score_early_timing(sensor, momentum, d, structure=structure)
    late_risk_score, late_reasons = score_late_risk(structure, momentum, liquidity, reversal_snapshot)
    pattern_score, pattern_reasons = score_pattern_alignment(sensor, structure, momentum, liquidity, context, d)

    reason_codes.extend(early_reasons)
    reason_codes.extend(late_reasons)
    reason_codes.extend(pattern_reasons)

    reversal_probability = 0.0
    exhaustion_probability = 0.0
    if reversal_snapshot:
        reversal_probability = _num(reversal_snapshot.get("reversal_probability"), 0.0)
        exhaustion_probability = _num(reversal_snapshot.get("exhaustion_probability"), 0.0)

    fresh_momentum_score = _momentum_raw_value(momentum, "fresh_momentum_score", 50.0)
    exhaustion_score = max(_momentum_raw_value(momentum, "exhaustion_score", 0.0), exhaustion_probability)
    move_age_score = _momentum_raw_value(momentum, "move_age_score", 50.0)
    start_raw = _structure_start_raw(structure)
    start_zone_active = _bool(start_raw.get("active"), False)
    start_zone_score = _num(start_raw.get("score"), 0.0)
    move_extended = _bool(start_raw.get("move_already_extended"), False) or bool(getattr(structure, "is_late_move", False))

    timing_score = (
        early_score * 0.42
        + pattern_score * 0.35
        + (100.0 - late_risk_score) * 0.23
    )

    # Start-of-move evidence should improve timing; late/exhaustion/reversal should reduce it.
    if start_zone_active:
        timing_score += 8.0
        reason_codes.append("ENTRY_START_ZONE_OK")
    elif start_zone_score >= 55:
        timing_score += 3.0
        reason_codes.append("ENTRY_START_ZONE_FORMING")

    timing_score += max(0.0, fresh_momentum_score - 55.0) * 0.24
    timing_score -= max(0.0, late_risk_score - 42.0) * 0.28
    timing_score -= reversal_probability * 0.18
    timing_score -= exhaustion_score * 0.18

    # Hard caps prevent after-pump/after-dump entries from looking acceptable.
    if move_extended or late_risk_score >= 78 or exhaustion_score >= 75 or reversal_probability >= 75:
        timing_score = min(timing_score, 48.0)
        reason_codes.append("ENTRY_HARD_LATE_CAP")
    elif late_risk_score >= 66 or exhaustion_score >= 66 or reversal_probability >= 66:
        timing_score = min(timing_score, 58.0)
        reason_codes.append("ENTRY_SOFT_LATE_CAP")
    elif late_risk_score >= 55 and fresh_momentum_score <= 50:
        timing_score = min(timing_score, 62.0)
        reason_codes.append("ENTRY_LATE_WITHOUT_FRESH_CAP")

    timing_score = clamp(timing_score, 0.0, 100.0)

    quality = classify_entry_quality(timing_score, late_risk_score)
    wait = should_wait_for_better_entry(
        timing_score,
        late_risk_score,
        reversal_probability,
        fresh_momentum_score,
        exhaustion_score,
        start_zone_active=start_zone_active,
    )

    if quality in {"EXCELLENT", "GOOD"}:
        reason_codes.append("TIMING_QUALITY_OK")
    elif quality == "ACCEPTABLE":
        reason_codes.append("TIMING_QUALITY_ACCEPTABLE")
    else:
        reason_codes.append("TIMING_QUALITY_WEAK")

    if fresh_momentum_score >= 65 and exhaustion_score < 45:
        reason_codes.append("ENTRY_FRESH_MOMENTUM_OK")
    if move_extended:
        reason_codes.append("ENTRY_EXTENDED_MOVE_BLOCK_HINT")
    elif exhaustion_score >= 65:
        reason_codes.append("ENTRY_LATE_EXHAUSTED_BLOCK_HINT")
    elif move_age_score >= 70:
        reason_codes.append("ENTRY_MOVE_AGE_LATE_HINT")

    if wait:
        reason_codes.append("WAIT_FOR_BETTER_ENTRY")

    symbol = safe_str(getattr(sensor, "symbol", "")) or safe_str(getattr(structure, "symbol", "")) or safe_str(getattr(liquidity, "symbol", ""))

    return make_timing_snapshot(
        symbol=symbol,
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
            "exhaustion_probability": exhaustion_probability,
            "fresh_momentum_score": fresh_momentum_score,
            "exhaustion_score": exhaustion_score,
            "move_age_score": move_age_score,
            "start_zone_active": start_zone_active,
            "start_zone_score": start_zone_score,
            "move_already_extended": move_extended,
            "structure_start_zone": dict(start_raw),
            "sensor_created_at": getattr(sensor, "created_at", None),
            "structure_created_at": getattr(structure, "created_at", None),
            "momentum_created_at": getattr(momentum, "created_at", None),
            "liquidity_created_at": getattr(liquidity, "created_at", None),
            "context_created_at": getattr(context, "created_at", None),
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
    "structure_move_start_active",
    "structure_move_start_score",
    "structure_move_extended",
    "score_structure_start_alignment",
    "score_early_timing",
    "score_late_risk",
    "score_pattern_alignment",
    "classify_entry_quality",
    "should_wait_for_better_entry",
    "build_timing_snapshot",
    "validate_timing_snapshot",
]
