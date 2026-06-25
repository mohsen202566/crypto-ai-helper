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

Core rule:
- Hunt the start of a pump/dump movement, not the middle/end of it.
- Fresh start evidence from structure_engine and momentum_engine should support
  continuation and reduce false reversal noise.
- Late/chase/exhaustion evidence should cap continuation and raise reversal risk.
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
# Safe helpers
# =============================================================================

def _num(value: Any, default: float = 0.0) -> float:
    """Return safe float while preserving real 0.0 values."""
    parsed = safe_float(value, None)
    if parsed is None:
        return float(default)
    return float(parsed)


def _bool(value: Any, default: bool = False) -> bool:
    """Read bool safely, including common string forms."""
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


def _momentum_raw(momentum: Optional[MomentumSnapshot]) -> Mapping[str, Any]:
    if momentum is None:
        return {}
    return _raw_mapping(momentum)


def _structure_start_active(structure: StructureSnapshot) -> bool:
    raw = _structure_start_raw(structure)
    return _bool(raw.get("active"), False)


def _structure_start_score(structure: StructureSnapshot) -> float:
    raw = _structure_start_raw(structure)
    return _num(raw.get("score"), 0.0)


def _structure_extended(structure: StructureSnapshot) -> bool:
    raw = _structure_start_raw(structure)
    return _bool(raw.get("move_already_extended"), False) or bool(getattr(structure, "is_late_move", False))


def _structure_room_ok(structure: StructureSnapshot) -> bool:
    raw = _structure_start_raw(structure)
    return _bool(raw.get("room_to_target"), True)


def _momentum_start_active(momentum: MomentumSnapshot) -> bool:
    raw = _momentum_raw(momentum)
    return _bool(raw.get("momentum_start_active"), False)


def _momentum_start_score(momentum: MomentumSnapshot) -> float:
    raw = _momentum_raw(momentum)
    return _num(raw.get("start_pressure_score"), 0.0)


def _fresh_momentum(momentum: MomentumSnapshot) -> float:
    return _num(_momentum_raw(momentum).get("fresh_momentum_score"), 50.0)


def _momentum_exhaustion(momentum: MomentumSnapshot) -> float:
    return _num(_momentum_raw(momentum).get("exhaustion_score"), 0.0)


def _move_age(momentum: MomentumSnapshot) -> float:
    return _num(_momentum_raw(momentum).get("move_age_score"), 50.0)


def _chase_pressure(momentum: MomentumSnapshot) -> float:
    return _num(_momentum_raw(momentum).get("chase_pressure"), 0.0)


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
    """Create a stable ReversalSnapshot-like dict without changing models.py."""
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
        "reason_codes": list(dict.fromkeys(reason_codes)),
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

    if not rsi_slope_ok(sensor, d, min_abs_slope=0.04):
        score += 18.0
        reasons.append("REV_RSI_SLOPE_WEAK")

    if not macd_hist_slope_ok(sensor, d):
        score += 24.0
        reasons.append("REV_MACD_ACCEL_WEAK")

    if not power_shift_ok(sensor, d, min_gap=2.0):
        score += 18.0
        reasons.append("REV_POWER_WEAK")

    if not price_ema_alignment_ok(sensor, d):
        score += 14.0
        reasons.append("REV_EMA_LOST")

    if not price_vwap_alignment_ok(sensor, d):
        score += 12.0
        reasons.append("REV_VWAP_LOST")

    rsi = safe_float(getattr(sensor, "rsi", None), None)
    if rsi is not None:
        if d == DIRECTION_LONG and rsi >= 74:
            score += 10.0
            reasons.append("REV_LONG_RSI_OVERHEATED")
        elif d == DIRECTION_SHORT and rsi <= 26:
            score += 10.0
            reasons.append("REV_SHORT_RSI_OVERHEATED")

    if not reasons:
        reasons.append("REV_SENSOR_WEAKNESS_LOW")

    return clamp(score, 0.0, 100.0), reasons


def score_structure_reversal_risk(structure: StructureSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score reversal risk from structure location with start-zone awareness."""
    d = normalize_direction(direction)
    score = 0.0
    reasons: list[str] = []

    start_raw = _structure_start_raw(structure)
    start_active = _bool(start_raw.get("active"), False)
    start_score = _num(start_raw.get("score"), 0.0)
    extended = _bool(start_raw.get("move_already_extended"), False) or bool(getattr(structure, "is_late_move", False))
    room_ok = _bool(start_raw.get("room_to_target"), True)

    if extended:
        score += 36.0
        reasons.append("REV_STRUCTURE_EXTENDED_MOVE")
    elif bool(getattr(structure, "is_late_move", False)):
        score += 30.0
        reasons.append("REV_STRUCTURE_LATE_MOVE")

    # Range is not automatically bad if it is breaking out from a start zone.
    if bool(getattr(structure, "is_range", False)):
        if start_active:
            score += 4.0
            reasons.append("REV_RANGE_BREAK_START_ZONE")
        else:
            score += 14.0
            reasons.append("REV_STRUCTURE_RANGE")

    fresh_zone = _num(getattr(structure, "fresh_zone_score", 50.0), 50.0)
    if fresh_zone <= 30:
        score += 22.0
        reasons.append("REV_FRESH_ZONE_VERY_WEAK")
    elif fresh_zone <= 42 and not start_active:
        score += 12.0
        reasons.append("REV_FRESH_ZONE_WEAK")

    if not room_ok:
        score += 16.0
        reasons.append("REV_NO_ROOM_TO_TARGET")

    if start_active and start_score >= 55 and not extended and room_ok:
        score -= 14.0
        reasons.append("REV_STRUCTURE_START_REDUCES_RISK")
    elif start_score >= 55 and not extended:
        score -= 6.0
        reasons.append("REV_STRUCTURE_START_FORMING")

    trend = safe_str(getattr(structure, "trend", "")).upper()
    if trend == "UPTREND" and d == DIRECTION_SHORT:
        score += 12.0
        reasons.append("REV_COUNTER_UPTREND")
    elif trend == "DOWNTREND" and d == DIRECTION_LONG:
        score += 12.0
        reasons.append("REV_COUNTER_DOWNTREND")

    raw = _raw_mapping(structure)
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

    if not reasons:
        reasons.append("REV_STRUCTURE_RISK_LOW")

    return clamp(score, 0.0, 100.0), reasons


def score_momentum_reversal_risk(momentum: MomentumSnapshot) -> tuple[float, list[str]]:
    """Score reversal risk from momentum snapshot, including start/chase separation."""
    score = 0.0
    reasons: list[str] = []

    weakness = _num(getattr(momentum, "weakness_score", 0.0), 0.0)
    reversal = _num(getattr(momentum, "reversal_risk_score", 0.0), 0.0)
    continuation = _num(getattr(momentum, "continuation_score", 50.0), 50.0)
    acceleration = _num(getattr(momentum, "acceleration_score", 50.0), 50.0)

    fresh = _fresh_momentum(momentum)
    exhaustion = _momentum_exhaustion(momentum)
    chase = _chase_pressure(momentum)
    age = _move_age(momentum)
    start_pressure = _momentum_start_score(momentum)
    start_active = _momentum_start_active(momentum)

    score += weakness * 0.24
    score += reversal * 0.18
    score += max(0.0, 52.0 - continuation) * 0.34
    score += max(0.0, 52.0 - acceleration) * 0.25
    score += exhaustion * 0.28
    score += chase * 0.30
    score += max(0.0, age - 55.0) * 0.20
    score += max(0.0, 55.0 - fresh) * 0.24

    # Fresh start evidence should reduce false reversal alarms.
    if start_active and start_pressure >= 60 and fresh >= 55 and exhaustion < 62 and chase < 62:
        score -= 18.0
        reasons.append("REV_MOMENTUM_START_REDUCES_RISK")
    elif start_pressure >= 55 and fresh >= 52 and exhaustion < 65:
        score -= 8.0
        reasons.append("REV_MOMENTUM_START_FORMING")

    if weakness >= 65:
        reasons.append("REV_MOMENTUM_WEAKNESS_HIGH")
    elif weakness >= 45:
        reasons.append("REV_MOMENTUM_WEAKNESS_MEDIUM")

    if continuation <= 40:
        reasons.append("REV_CONTINUATION_WEAK")
    elif continuation <= 52:
        reasons.append("REV_CONTINUATION_SOFT")

    if acceleration <= 42:
        reasons.append("REV_ACCELERATION_WEAK")

    if exhaustion >= 65:
        reasons.append("REV_MOMENTUM_EXHAUSTION_HIGH")
    elif exhaustion >= 45:
        reasons.append("REV_MOMENTUM_EXHAUSTION_MEDIUM")

    if chase >= 70:
        reasons.append("REV_CHASE_PRESSURE_HIGH")
    elif chase >= 52:
        reasons.append("REV_CHASE_PRESSURE_MEDIUM")

    if age >= 72:
        reasons.append("REV_MOVE_AGE_LATE")
    elif age <= 38:
        reasons.append("REV_MOVE_AGE_EARLY")

    if fresh <= 42:
        reasons.append("REV_FRESH_MOMENTUM_LOW")
    elif fresh >= 65:
        reasons.append("REV_FRESH_MOMENTUM_OK")

    if not reasons:
        reasons.append("REV_MOMENTUM_RISK_LOW")

    return clamp(score, 0.0, 100.0), reasons


def score_liquidity_reversal_risk(liquidity: LiquiditySnapshot) -> tuple[float, list[str]]:
    """Score reversal risk from liquidity/trap snapshot."""
    score = 0.0
    reasons: list[str] = []

    trap = _num(getattr(liquidity, "trap_risk_score", 0.0), 0.0)
    sweep = _num(getattr(liquidity, "liquidity_sweep_score", 0.0), 0.0)
    fake = _num(getattr(liquidity, "fake_break_risk", 0.0), 0.0)
    wick = _num(getattr(liquidity, "wick_rejection_score", 0.0), 0.0)
    survival = _num(getattr(liquidity, "breakout_survival_score", 50.0), 50.0)

    score += trap * 0.35
    score += sweep * 0.18
    score += fake * 0.25
    score += wick * 0.12
    score += max(0.0, 50.0 - survival) * 0.20

    if bool(getattr(liquidity, "stop_hunt_detected", False)):
        score += 10.0
        reasons.append("REV_STOP_HUNT_DETECTED")

    if bool(getattr(liquidity, "likely_trap", False)):
        score += 15.0
        reasons.append("REV_LIKELY_TRAP")

    if fake >= 60:
        reasons.append("REV_FAKE_BREAK_HIGH")

    if survival <= 40:
        reasons.append("REV_BREAKOUT_SURVIVAL_WEAK")

    if not reasons:
        reasons.append("REV_LIQUIDITY_RISK_LOW")

    return clamp(score, 0.0, 100.0), reasons


def score_context_reversal_risk(context: MarketContextSnapshot, direction: str) -> tuple[float, list[str]]:
    """Score reversal risk from broad market context."""
    d = normalize_direction(direction)
    score = 0.0
    reasons: list[str] = []

    context_score = _num(getattr(context, "context_score", 50.0), 50.0)
    market_risk = _num(getattr(context, "market_risk_score", 50.0), 50.0)

    score += max(0.0, 50.0 - context_score) * 0.55
    score += max(0.0, market_risk - 45.0) * 0.35

    if bool(getattr(context, "choppy", False)):
        score += 12.0
        reasons.append("REV_CONTEXT_CHOPPY")

    if not bool(getattr(context, "aligned_with_direction", False)):
        score += 10.0
        reasons.append("REV_CONTEXT_NOT_ALIGNED")

    mode = safe_str(getattr(context, "market_mode", "")).upper()
    if mode == "BULLISH" and d == DIRECTION_SHORT:
        score += 8.0
        reasons.append("REV_SHORT_AGAINST_BULL_MARKET")
    elif mode == "BEARISH" and d == DIRECTION_LONG:
        score += 8.0
        reasons.append("REV_LONG_AGAINST_BEAR_MARKET")

    if not reasons:
        reasons.append("REV_CONTEXT_RISK_LOW")

    return clamp(score, 0.0, 100.0), reasons


# =============================================================================
# Start and late synergies
# =============================================================================

def calculate_early_start_synergy(
    *,
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
    context: MarketContextSnapshot,
) -> tuple[float, list[str]]:
    """
    Calculate nonlinear positive evidence for a fresh movement start.

    Higher score = more reason to expect continuation rather than reversal.
    """
    reasons: list[str] = []

    sraw = _structure_start_raw(structure)
    structure_start = _bool(sraw.get("active"), False)
    structure_start_score = _num(sraw.get("score"), 0.0)
    atr_start = _bool(sraw.get("atr_expansion_start"), False)
    micro_shift = _bool(sraw.get("micro_structure_shift"), False)
    volume_structure = _bool(sraw.get("volume_pressure_start"), False)
    sd_reaction = _bool(sraw.get("supply_demand_reaction"), False)
    room_ok = _bool(sraw.get("room_to_target"), True)
    structure_extended = _bool(sraw.get("move_already_extended"), False) or bool(getattr(structure, "is_late_move", False))

    momentum_start = _momentum_start_active(momentum)
    start_pressure = _momentum_start_score(momentum)
    fresh = _fresh_momentum(momentum)
    exhaustion = _momentum_exhaustion(momentum)
    chase = _chase_pressure(momentum)
    age = _move_age(momentum)

    survival = _num(getattr(liquidity, "breakout_survival_score", 50.0), 50.0)
    trap = _num(getattr(liquidity, "trap_risk_score", 0.0), 0.0)
    context_aligned = bool(getattr(context, "aligned_with_direction", False))

    synergy = 0.0

    if structure_start:
        synergy += 18.0
        reasons.append("EARLY_STRUCTURE_START_ZONE")
    elif structure_start_score >= 55:
        synergy += 8.0
        reasons.append("EARLY_STRUCTURE_START_FORMING")

    if momentum_start:
        synergy += 18.0
        reasons.append("EARLY_MOMENTUM_START_ACTIVE")
    elif start_pressure >= 60:
        synergy += 10.0
        reasons.append("EARLY_MOMENTUM_PRESSURE")

    if atr_start:
        synergy += 8.0
        reasons.append("EARLY_ATR_EXPANSION")
    if micro_shift:
        synergy += 8.0
        reasons.append("EARLY_MICRO_STRUCTURE_SHIFT")
    if volume_structure:
        synergy += 5.0
        reasons.append("EARLY_STRUCTURE_VOLUME")
    if sd_reaction:
        synergy += 6.0
        reasons.append("EARLY_SUPPLY_DEMAND_REACTION")

    if fresh >= 65:
        synergy += 10.0
        reasons.append("EARLY_FRESH_MOMENTUM_HIGH")
    elif fresh >= 55:
        synergy += 5.0
        reasons.append("EARLY_FRESH_MOMENTUM_OK")

    if age <= 42:
        synergy += 6.0
        reasons.append("EARLY_MOVE_AGE_OK")

    if survival >= 55 and trap <= 55:
        synergy += 5.0
        reasons.append("EARLY_LIQUIDITY_SURVIVAL_OK")

    if context_aligned:
        synergy += 4.0
        reasons.append("EARLY_CONTEXT_ALIGNED")

    if room_ok:
        synergy += 4.0
        reasons.append("EARLY_ROOM_TO_TARGET_OK")
    else:
        synergy -= 12.0
        reasons.append("EARLY_ROOM_TO_TARGET_WEAK")

    # Fresh start cannot override clear exhaustion/chase/extension.
    if structure_extended:
        synergy -= 28.0
        reasons.append("EARLY_BLOCKED_BY_STRUCTURE_EXTENSION")
    if exhaustion >= 65:
        synergy -= 18.0
        reasons.append("EARLY_BLOCKED_BY_EXHAUSTION")
    if chase >= 68:
        synergy -= 16.0
        reasons.append("EARLY_BLOCKED_BY_CHASE")
    if trap >= 70:
        synergy -= 12.0
        reasons.append("EARLY_BLOCKED_BY_TRAP")

    if not reasons:
        reasons.append("EARLY_START_SYNERGY_LOW")

    return clamp(synergy, 0.0, 100.0), reasons


def calculate_late_entry_synergy(
    *,
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
) -> tuple[float, list[str]]:
    """Calculate nonlinear late-entry danger."""
    reasons: list[str] = []

    chase = _chase_pressure(momentum)
    fresh = _fresh_momentum(momentum)
    exhaustion = _momentum_exhaustion(momentum)
    age = _move_age(momentum)
    weakness = _num(getattr(momentum, "weakness_score", 0.0), 0.0)

    trap = _num(getattr(liquidity, "trap_risk_score", 0.0), 0.0)
    fake = _num(getattr(liquidity, "fake_break_risk", 0.0), 0.0)
    wick = _num(getattr(liquidity, "wick_rejection_score", 0.0), 0.0)
    survival = _num(getattr(liquidity, "breakout_survival_score", 50.0), 50.0)

    structure_extended = _structure_extended(structure)
    structure_late = 100.0 if structure_extended else 0.0
    fresh_zone_weak = max(0.0, 45.0 - _num(getattr(structure, "fresh_zone_score", 50.0), 50.0)) * 1.7
    liquidity_danger = max(trap, fake, wick, max(0.0, 55.0 - survival))

    synergy = 0.0

    if structure_extended and (exhaustion >= 45 or chase >= 52 or age >= 64):
        synergy += 22.0
        reasons.append("SYNERGY_LATE_EXHAUSTED")

    if liquidity_danger >= 55 and (structure_extended or chase >= 55 or age >= 68):
        synergy += 18.0
        reasons.append("SYNERGY_TRAP_LATE")

    if liquidity_danger >= 50 and weakness >= 55:
        synergy += 14.0
        reasons.append("SYNERGY_TRAP_WEAKNESS")

    if fresh <= 42 and chase >= 55:
        synergy += 12.0
        reasons.append("SYNERGY_NOT_FRESH_CHASE")

    if age >= 72 and (fresh <= 50 or exhaustion >= 45):
        synergy += 12.0
        reasons.append("SYNERGY_MOVE_AGE_LATE")

    if fresh_zone_weak >= 18 and (exhaustion >= 45 or liquidity_danger >= 50):
        synergy += 10.0
        reasons.append("SYNERGY_WEAK_ZONE_RISK")

    synergy += structure_late * 0.05
    synergy += max(0.0, chase - 45.0) * 0.20
    synergy += max(0.0, exhaustion - 45.0) * 0.18
    synergy += max(0.0, age - 58.0) * 0.18
    synergy += max(0.0, liquidity_danger - 45.0) * 0.14
    synergy += max(0.0, 50.0 - fresh) * 0.12

    if not reasons:
        reasons.append("SYNERGY_LOW")

    return clamp(synergy, 0.0, 100.0), reasons


def adjust_reversal_probability_nonlinear(
    *,
    base_probability: float,
    continuation_probability: float,
    exhaustion_probability: float,
    late_entry_synergy: float,
    early_start_synergy: float,
    momentum: MomentumSnapshot,
    structure: StructureSnapshot,
) -> float:
    """Nonlinear reversal adjustment for fresh starts vs dangerous late entries."""
    probability = _num(base_probability, 0.0)
    continuation = _num(continuation_probability, 50.0)
    exhaustion = _num(exhaustion_probability, 0.0)
    late = _num(late_entry_synergy, 0.0)
    early = _num(early_start_synergy, 0.0)

    fresh = _fresh_momentum(momentum)
    chase = _chase_pressure(momentum)
    mexh = _momentum_exhaustion(momentum)
    age = _move_age(momentum)
    start_active = _momentum_start_active(momentum) or _structure_start_active(structure)
    extended = _structure_extended(structure)

    danger = 0.0
    danger += max(0.0, late - 20.0) * 0.46
    danger += max(0.0, exhaustion - 50.0) * 0.22
    danger += max(0.0, chase - 50.0) * 0.24
    danger += max(0.0, age - 62.0) * 0.18
    danger += max(0.0, 48.0 - continuation) * 0.20
    probability += danger

    # Fresh start reduces reversal noise only when late/chase evidence is not dangerous.
    if start_active and early >= 50 and fresh >= 60 and chase <= 50 and mexh <= 48 and not extended:
        probability -= 14.0
    elif early >= 40 and fresh >= 58 and chase <= 55 and mexh <= 55 and not extended:
        probability -= 8.0
    elif fresh >= 65 and chase <= 45 and mexh <= 45 and continuation >= 58:
        probability -= 6.0

    return clamp(probability, 0.0, 100.0)


def cap_continuation_for_late_risk(
    continuation_probability: float,
    *,
    exhaustion_probability: float,
    late_entry_synergy: float,
    early_start_synergy: float,
    momentum: MomentumSnapshot,
    structure: StructureSnapshot,
) -> float:
    """Cap continuation when late-entry risk is high, while allowing fresh starts."""
    continuation = _num(continuation_probability, 0.0)
    exhaustion = _num(exhaustion_probability, 0.0)
    late = _num(late_entry_synergy, 0.0)
    early = _num(early_start_synergy, 0.0)
    chase = _chase_pressure(momentum)
    age = _move_age(momentum)
    extended = _structure_extended(structure)

    cap = 100.0
    if extended or late >= 70 or chase >= 75 or exhaustion >= 78 or age >= 82:
        cap = 38.0
    elif late >= 55 or chase >= 65 or exhaustion >= 68 or age >= 72:
        cap = 48.0
    elif late >= 42 or chase >= 55 or exhaustion >= 58 or age >= 64:
        cap = 58.0
    elif late >= 30 or chase >= 48 or exhaustion >= 50 or age >= 58:
        cap = 68.0

    # A genuine early start can loosen only the soft caps, not hard extension.
    if not extended and early >= 55 and chase <= 55 and exhaustion <= 58 and age <= 62:
        cap = max(cap, 72.0)
    elif not extended and early >= 45 and chase <= 60 and exhaustion <= 62:
        cap = max(cap, 66.0)

    return clamp(min(continuation, cap), 0.0, 100.0)


# =============================================================================
# Probabilities
# =============================================================================

def classify_weakness_level(reversal_probability: float, exhaustion_probability: float) -> str:
    """Classify weakness level."""
    rev = _num(reversal_probability, 0.0)
    exh = _num(exhaustion_probability, 0.0)

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
    early_start_synergy: float = 0.0,
) -> float:
    """Calculate continuation probability from aligned components and fresh-start evidence."""
    structure_score = _num(getattr(structure, "structure_score", 50.0), 50.0)
    momentum_score = _num(getattr(momentum, "momentum_score", 50.0), 50.0)
    continuation_score = _num(getattr(momentum, "continuation_score", 50.0), 50.0)
    survival_score = _num(getattr(liquidity, "breakout_survival_score", 50.0), 50.0)
    context_score = _num(getattr(context, "context_score", 50.0), 50.0)
    early = _num(early_start_synergy, 0.0)

    probability = (
        structure_score * 0.21
        + momentum_score * 0.26
        + continuation_score * 0.19
        + survival_score * 0.14
        + context_score * 0.13
        + early * 0.07
    )

    probability -= _num(reversal_risk, 0.0) * 0.24

    return clamp(probability, 0.0, 100.0)


def calculate_exhaustion_probability(
    sensor: SensorSnapshot,
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
) -> tuple[float, list[str]]:
    """Calculate move exhaustion probability with start-aware reduction."""
    score = 0.0
    reasons: list[str] = []

    if _structure_extended(structure):
        score += 34.0
        reasons.append("EXH_LATE_STRUCTURE")

    acceleration = _num(getattr(momentum, "acceleration_score", 50.0), 50.0)
    weakness = _num(getattr(momentum, "weakness_score", 0.0), 0.0)
    mexh = _momentum_exhaustion(momentum)
    age = _move_age(momentum)
    chase = _chase_pressure(momentum)
    fresh = _fresh_momentum(momentum)
    start_active = _momentum_start_active(momentum) or _structure_start_active(structure)

    if acceleration <= 42:
        score += 18.0
        reasons.append("EXH_ACCELERATION_FADING")

    if weakness >= 55:
        score += 20.0
        reasons.append("EXH_WEAKNESS_VISIBLE")

    if mexh >= 60:
        score += 22.0
        reasons.append("EXH_MOMENTUM_EXHAUSTION_HIGH")
    elif mexh >= 45:
        score += 12.0
        reasons.append("EXH_MOMENTUM_EXHAUSTION_MEDIUM")

    if age >= 72:
        score += 20.0
        reasons.append("EXH_MOVE_AGE_LATE")
    elif age >= 62:
        score += 10.0
        reasons.append("EXH_MOVE_AGE_WARNING")

    if chase >= 68:
        score += 16.0
        reasons.append("EXH_CHASE_PRESSURE_HIGH")
    elif chase >= 55:
        score += 8.0
        reasons.append("EXH_CHASE_PRESSURE_MEDIUM")

    if _num(getattr(liquidity, "wick_rejection_score", 0.0), 0.0) >= 55:
        score += 16.0
        reasons.append("EXH_WICK_REJECTION")

    rsi = safe_float(getattr(sensor, "rsi", None), None)
    if rsi is not None and (rsi >= 76 or rsi <= 24):
        score += 12.0
        reasons.append("EXH_RSI_EXTREME")

    if start_active and fresh >= 58 and mexh < 55 and chase < 60 and age < 65:
        score -= 14.0
        reasons.append("EXH_REDUCED_BY_FRESH_START")

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
    early_start_synergy, early_reasons = calculate_early_start_synergy(
        structure=structure,
        momentum=momentum,
        liquidity=liquidity,
        context=context,
    )
    late_entry_synergy, synergy_reasons = calculate_late_entry_synergy(
        structure=structure,
        momentum=momentum,
        liquidity=liquidity,
    )

    for codes in [
        sensor_reasons,
        structure_reasons,
        momentum_reasons,
        liquidity_reasons,
        context_reasons,
        exhaustion_reasons,
        early_reasons,
        synergy_reasons,
    ]:
        reason_codes.extend(codes)

    chase = _chase_pressure(momentum)
    fresh = _fresh_momentum(momentum)
    mexh = _momentum_exhaustion(momentum)
    age = _move_age(momentum)
    start_pressure = _momentum_start_score(momentum)
    momentum_start_active = _momentum_start_active(momentum)
    structure_start_active = _structure_start_active(structure)
    structure_start_score = _structure_start_score(structure)
    structure_extended = _structure_extended(structure)

    reversal_score = (
        sensor_risk * 0.16
        + structure_risk * 0.16
        + momentum_risk * 0.27
        + liquidity_risk * 0.19
        + context_risk * 0.09
        + late_entry_synergy * 0.09
        - early_start_synergy * 0.08
    )
    reversal_score = clamp(reversal_score, 0.0, 100.0)

    base_reversal_probability = clamp(
        reversal_score * 0.68
        + exhaustion_probability * 0.18
        + late_entry_synergy * 0.12
        - early_start_synergy * 0.10,
        0.0,
        100.0,
    )

    continuation_probability = calculate_continuation_probability(
        structure=structure,
        momentum=momentum,
        liquidity=liquidity,
        context=context,
        reversal_risk=base_reversal_probability,
        early_start_synergy=early_start_synergy,
    )

    # Fresh-start boost before the late cap. This makes the engine a hunter,
    # not merely a post-move reversal detector.
    if early_start_synergy >= 55 and fresh >= 58 and chase <= 55 and mexh <= 58 and not structure_extended:
        continuation_probability += 10.0
        reason_codes.append("EARLY_START_BOOSTS_CONTINUATION")
    elif early_start_synergy >= 42 and not structure_extended:
        continuation_probability += 5.0
        reason_codes.append("EARLY_START_SUPPORTS_CONTINUATION")

    continuation_probability = clamp(continuation_probability, 0.0, 100.0)

    reversal_probability = adjust_reversal_probability_nonlinear(
        base_probability=base_reversal_probability,
        continuation_probability=continuation_probability,
        exhaustion_probability=exhaustion_probability,
        late_entry_synergy=late_entry_synergy,
        early_start_synergy=early_start_synergy,
        momentum=momentum,
        structure=structure,
    )

    continuation_probability = cap_continuation_for_late_risk(
        continuation_probability,
        exhaustion_probability=exhaustion_probability,
        late_entry_synergy=late_entry_synergy,
        early_start_synergy=early_start_synergy,
        momentum=momentum,
        structure=structure,
    )

    # If nonlinear reversal risk jumped, continuation should reflect it.
    continuation_probability = clamp(
        continuation_probability - max(0.0, reversal_probability - base_reversal_probability) * 0.25,
        0.0,
        100.0,
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

    if early_start_synergy >= 55:
        reason_codes.append("EARLY_START_SYNERGY_HIGH")
    elif early_start_synergy >= 35:
        reason_codes.append("EARLY_START_SYNERGY_MEDIUM")
    else:
        reason_codes.append("EARLY_START_SYNERGY_LOW")

    if late_entry_synergy >= 55:
        reason_codes.append("LATE_ENTRY_SYNERGY_HIGH")
    elif late_entry_synergy >= 35:
        reason_codes.append("LATE_ENTRY_SYNERGY_MEDIUM")
    else:
        reason_codes.append("LATE_ENTRY_SYNERGY_LOW")

    if chase >= 60:
        reason_codes.append("CHASE_PRESSURE_REVERSAL_RISK")

    if structure_extended:
        reason_codes.append("EXTENDED_MOVE_REVERSAL_RISK")

    if (momentum_start_active or structure_start_active) and fresh >= 58 and chase <= 55 and mexh <= 55:
        reason_codes.append("FRESH_START_REDUCES_REVERSAL_NOISE")

    symbol = (
        safe_str(getattr(sensor, "symbol", ""))
        or safe_str(getattr(structure, "symbol", ""))
        or safe_str(getattr(liquidity, "symbol", ""))
    )

    return make_reversal_snapshot(
        symbol=symbol,
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
            "early_start_synergy": early_start_synergy,
            "late_entry_synergy": late_entry_synergy,
            "base_reversal_probability": base_reversal_probability,
            "chase_pressure": chase,
            "fresh_momentum_score": fresh,
            "momentum_exhaustion_score": mexh,
            "move_age_score": age,
            "start_pressure_score": start_pressure,
            "momentum_start_active": momentum_start_active,
            "structure_start_active": structure_start_active,
            "structure_start_score": structure_start_score,
            "structure_extended": structure_extended,
            "early_reasons": early_reasons,
            "synergy_reasons": synergy_reasons,
            "sensor_created_at": getattr(sensor, "created_at", None),
            "structure_created_at": getattr(structure, "created_at", None),
            "momentum_created_at": getattr(momentum, "created_at", None),
            "liquidity_created_at": getattr(liquidity, "created_at", None),
            "context_created_at": getattr(context, "created_at", None),
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

    raw = snapshot.get("raw") or {}
    if not isinstance(raw, Mapping):
        errors.append("invalid_raw")
    else:
        for key in ["early_start_synergy", "late_entry_synergy", "chase_pressure", "fresh_momentum_score", "move_age_score"]:
            if key in raw:
                value = safe_float(raw.get(key), None)
                if value is None or not (0.0 <= value <= 100.0):
                    errors.append(f"invalid_raw_{key}")

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
    "calculate_early_start_synergy",
    "calculate_late_entry_synergy",
    "adjust_reversal_probability_nonlinear",
    "cap_continuation_for_late_risk",
    "classify_weakness_level",
    "calculate_continuation_probability",
    "calculate_exhaustion_probability",
    "build_reversal_snapshot",
    "validate_reversal_snapshot",
]
