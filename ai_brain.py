"""
ai_brain.py
Level 4 / 1H Smart Scalp Bot

Final decision brain for Level 4.

Architecture lock:
- Combines already-built analysis snapshots and TP/SL plan into final AIDecision.
- Owns final REAL / GHOST / REJECT decision for new opportunities.
- Does not fetch market data, calculate indicators directly, place orders,
  write JSON state, monitor positions, or build Telegram text.
- REAL execution still belongs to real_trade_manager.py.
- Position creation still belongs to bot.py / position_manager.py flow.
- Allowed project imports:
  constants.py, utils.py, models.py, strategy_manager.py, tp_sl_engine.py only.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

import constants
from constants import DIRECTION_LONG, DIRECTION_SHORT, MODE_GHOST, MODE_REAL, MODE_REJECT, SYSTEM_VERSION
from models import (
    AIDecision,
    LiquiditySnapshot,
    MarketContextSnapshot,
    MomentumSnapshot,
    MonitorDecision,
    SensorSnapshot,
    StructureSnapshot,
    TPSLPlan,
)
from strategy_manager import execution_mode_for_new_decision, get_trade_runtime_config, is_real_trading_enabled
from tp_sl_engine import validate_tp_sl_plan
from utils import clamp, normalize_direction, normalize_symbol, safe_bool, safe_float, safe_str, utc_now_iso


AI_BRAIN_VERSION: str = SYSTEM_VERSION


DEFAULT_AI_CONFIG: dict[str, Any] = {
    # Level 4 / 1H Smart Scalp should prefer fewer but cleaner REAL entries.
    "real_min_score": 78.0,
    "real_min_confidence": 72.0,
    "ghost_min_score": 55.0,
    "reject_below_score": 45.0,
    "max_trap_risk_for_real": 58.0,
    "max_reversal_probability_for_real": 55.0,
    "max_late_risk_for_real": 55.0,
    "min_timing_score_for_real": 62.0,
    "min_structure_score_for_real": 58.0,
    "min_momentum_score_for_real": 60.0,
    "min_context_score_for_real": 38.0,
    "max_exhaustion_for_real": 58.0,
    "min_fresh_momentum_for_real": 45.0,
    "max_weakness_for_real": 58.0,
    "min_continuation_probability_for_real": 48.0,
    "tp_sl_required_for_real": True,
    "soft_ghost_when_trade_off": True,
}


# =============================================================================
# Safe numeric helpers
# =============================================================================

def _num(value: Any, default: float = 0.0) -> float:
    """Return safe float while preserving a valid real 0.0.

    Important: never use `safe_float(... ) or default` for decision values here.
    A real 0.0 reversal/late/fresh value must remain 0.0, not become 50.0.
    """
    parsed = safe_float(value, None)
    if parsed is None:
        return float(default)
    return float(parsed)


def _ai_config() -> Mapping[str, Any]:
    """Return AI config from constants if available, otherwise safe fallback."""
    config = getattr(constants, "AI_DECISION_CONFIG", DEFAULT_AI_CONFIG)
    return config if isinstance(config, Mapping) else DEFAULT_AI_CONFIG


def _cfg_float(key: str, default: float) -> float:
    return _num(_ai_config().get(key), default)


def _cfg_bool(key: str, default: bool) -> bool:
    return safe_bool(_ai_config().get(key), default)


def _mapping_value(data: Optional[Mapping[str, Any]], key: str, default: float = 0.0) -> float:
    if not isinstance(data, Mapping):
        return float(default)
    return _num(data.get(key), default)


def _raw_mapping_value(data: Optional[Mapping[str, Any]], key: str, default: float = 0.0) -> float:
    if not isinstance(data, Mapping):
        return float(default)
    raw = data.get("raw")
    if not isinstance(raw, Mapping):
        return float(default)
    return _num(raw.get(key), default)


def _momentum_raw_value(momentum: MomentumSnapshot, key: str, default: float = 0.0) -> float:
    raw = getattr(momentum, "raw", None)
    if not isinstance(raw, Mapping):
        return float(default)
    return _num(raw.get(key), default)


# =============================================================================
# Scoring helpers
# =============================================================================

def direction_valid(direction: str) -> bool:
    return normalize_direction(direction) in {DIRECTION_LONG, DIRECTION_SHORT}


def score_structure(structure: StructureSnapshot) -> tuple[float, list[str]]:
    score = _num(structure.structure_score, 0.0)
    reasons: list[str] = []
    if score >= 70:
        reasons.append("AI_STRUCTURE_STRONG")
    elif score >= 55:
        reasons.append("AI_STRUCTURE_OK")
    else:
        reasons.append("AI_STRUCTURE_WEAK")
    if structure.is_late_move:
        reasons.append("AI_STRUCTURE_LATE_RISK")
    if structure.is_range:
        reasons.append("AI_STRUCTURE_RANGE")
    return clamp(score, 0.0, 100.0), reasons


def score_momentum(momentum: MomentumSnapshot) -> tuple[float, list[str]]:
    score = _num(momentum.momentum_score, 0.0)
    reasons: list[str] = []
    if score >= 72:
        reasons.append("AI_MOMENTUM_STRONG")
    elif score >= 58:
        reasons.append("AI_MOMENTUM_OK")
    else:
        reasons.append("AI_MOMENTUM_WEAK")

    weakness = _num(momentum.weakness_score, 0.0)
    fresh = _momentum_raw_value(momentum, "fresh_momentum_score", 50.0)
    exhaustion = _momentum_raw_value(momentum, "exhaustion_score", 0.0)

    if weakness >= 60:
        reasons.append("AI_MOMENTUM_WEAKNESS_VISIBLE")
    if fresh <= 42:
        reasons.append("AI_FRESH_MOMENTUM_WEAK")
    elif fresh >= 65:
        reasons.append("AI_FRESH_MOMENTUM_OK")
    if exhaustion >= 60:
        reasons.append("AI_MOMENTUM_EXHAUSTED")

    return clamp(score, 0.0, 100.0), reasons


def score_liquidity(liquidity: LiquiditySnapshot) -> tuple[float, list[str]]:
    trap = _num(liquidity.trap_risk_score, 0.0)
    survival = _num(liquidity.breakout_survival_score, 50.0)
    score = (100.0 - trap) * 0.65 + survival * 0.35
    reasons: list[str] = []
    if trap >= 70 or liquidity.likely_trap:
        reasons.append("AI_LIQUIDITY_TRAP_HIGH")
    elif trap >= 50:
        reasons.append("AI_LIQUIDITY_TRAP_MEDIUM")
    else:
        reasons.append("AI_LIQUIDITY_ACCEPTABLE")
    if liquidity.stop_hunt_detected:
        reasons.append("AI_STOP_HUNT_DETECTED")
    return clamp(score, 0.0, 100.0), reasons


def score_context(context: MarketContextSnapshot) -> tuple[float, list[str]]:
    score = _num(context.context_score, 50.0)
    reasons: list[str] = []
    if context.aligned_with_direction:
        reasons.append("AI_CONTEXT_ALIGNED")
    elif score <= 40:
        reasons.append("AI_CONTEXT_AGAINST")
    else:
        reasons.append("AI_CONTEXT_NEUTRAL")
    if context.choppy:
        reasons.append("AI_CONTEXT_CHOPPY")
    return clamp(score, 0.0, 100.0), reasons


def score_reversal(reversal_snapshot: Optional[Mapping[str, Any]]) -> tuple[float, list[str]]:
    """Higher returned score means safer from reversal."""
    if not reversal_snapshot:
        return 55.0, ["AI_REVERSAL_MISSING"]

    reversal_prob = _mapping_value(reversal_snapshot, "reversal_probability", 50.0)
    exhaustion_prob = _mapping_value(reversal_snapshot, "exhaustion_probability", 50.0)
    continuation_prob = _mapping_value(reversal_snapshot, "continuation_probability", 50.0)

    risk = reversal_prob * 0.60 + exhaustion_prob * 0.25 + max(0.0, 50.0 - continuation_prob) * 0.15
    score = 100.0 - risk

    reasons: list[str] = []
    if reversal_prob >= 70:
        reasons.append("AI_REVERSAL_HIGH")
    elif reversal_prob >= 55:
        reasons.append("AI_REVERSAL_MEDIUM")
    else:
        reasons.append("AI_REVERSAL_LOW")

    if exhaustion_prob >= 70:
        reasons.append("AI_EXHAUSTION_HIGH")
    elif exhaustion_prob >= 55:
        reasons.append("AI_EXHAUSTION_MEDIUM")

    if continuation_prob < 45:
        reasons.append("AI_CONTINUATION_WEAK")

    return clamp(score, 0.0, 100.0), reasons


def score_timing(timing_snapshot: Optional[Mapping[str, Any]]) -> tuple[float, list[str]]:
    if not timing_snapshot:
        return 55.0, ["AI_TIMING_MISSING"]

    score = _mapping_value(timing_snapshot, "timing_score", 50.0)
    quality = safe_str(timing_snapshot.get("entry_quality")).upper()
    wait = bool(timing_snapshot.get("wait_for_better_entry"))
    late = _mapping_value(timing_snapshot, "late_risk_score", 0.0)
    fresh = _raw_mapping_value(timing_snapshot, "fresh_momentum_score", 50.0)
    exhaustion = _raw_mapping_value(timing_snapshot, "exhaustion_score", 0.0)

    reasons: list[str] = []
    if quality in {"EXCELLENT", "GOOD"}:
        reasons.append("AI_TIMING_GOOD")
    elif quality == "ACCEPTABLE":
        reasons.append("AI_TIMING_ACCEPTABLE")
    else:
        reasons.append("AI_TIMING_WEAK")

    if wait:
        reasons.append("AI_TIMING_WAIT_SUGGESTED")
    if late >= 55:
        reasons.append("AI_TIMING_LATE_RISK")
    if fresh <= 42:
        reasons.append("AI_TIMING_FRESH_WEAK")
    if exhaustion >= 58:
        reasons.append("AI_TIMING_EXHAUSTED")

    return clamp(score, 0.0, 100.0), reasons


def score_tp_sl(plan: Optional[TPSLPlan], quantity: float = 0.0) -> tuple[float, list[str]]:
    if plan is None:
        return 0.0, ["AI_TP_SL_MISSING"]

    valid, errors = validate_tp_sl_plan(plan, quantity=quantity)
    if not valid:
        return 25.0, ["AI_TP_SL_INVALID", *errors]

    score = 55.0
    rr = _num(plan.rr, 0.0)
    net = _num(plan.tp1_net_profit_estimate, 0.0)

    if rr >= 1.1:
        score += 18.0
    elif rr >= 0.8:
        score += 10.0
    else:
        score -= 15.0

    if net >= 0.20:
        score += 12.0
    elif net >= 0.10:
        score += 6.0
    elif net > 0:
        score -= 10.0
    else:
        score -= 18.0

    return clamp(score, 0.0, 100.0), ["AI_TP_SL_VALID"]


def combine_final_score(parts: Mapping[str, float]) -> float:
    """Weighted final score for Level 4 quality, not raw signal frequency."""
    weights = {
        "structure": 0.17,
        "momentum": 0.20,
        "liquidity": 0.17,
        "context": 0.10,
        "reversal": 0.14,
        "timing": 0.14,
        "tp_sl": 0.08,
    }
    total = 0.0
    wsum = 0.0
    for key, weight in weights.items():
        total += _num(parts.get(key), 0.0) * weight
        wsum += weight
    if wsum <= 0:
        return 0.0
    return clamp(total / wsum, 0.0, 100.0)


def confidence_from_score(score: float, parts: Mapping[str, float]) -> float:
    """Estimate confidence from final score and component consistency."""
    values = [_num(v, 0.0) for v in parts.values()]
    if not values:
        return 0.0

    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    spread_penalty = min(18.0, variance ** 0.5 * 0.35)

    confidence = _num(score, 0.0) - spread_penalty
    return clamp(confidence, 0.0, 100.0)


def adjusted_score_for_entry_quality(
    *,
    final_score: float,
    liquidity: LiquiditySnapshot,
    reversal_snapshot: Optional[Mapping[str, Any]],
    timing_snapshot: Optional[Mapping[str, Any]],
    momentum: MomentumSnapshot,
) -> tuple[float, list[str]]:
    """Apply soft score penalties for late/chasing/reversal conditions.

    This does not make the final mode directly; it prevents late pump-chasing from
    retaining a high final score just because older structure/context still look good.
    """
    score = _num(final_score, 0.0)
    reasons: list[str] = []

    trap = _num(liquidity.trap_risk_score, 0.0)
    rev = _mapping_value(reversal_snapshot, "reversal_probability", 0.0)
    rev_exhaustion = _mapping_value(reversal_snapshot, "exhaustion_probability", 0.0)
    continuation = _mapping_value(reversal_snapshot, "continuation_probability", 50.0)
    late = _mapping_value(timing_snapshot, "late_risk_score", 0.0)
    wait = bool(timing_snapshot.get("wait_for_better_entry")) if isinstance(timing_snapshot, Mapping) else False
    timing_quality = safe_str(timing_snapshot.get("entry_quality") if isinstance(timing_snapshot, Mapping) else "").upper()
    timing_exhaustion = _raw_mapping_value(timing_snapshot, "exhaustion_score", 0.0)
    timing_fresh = _raw_mapping_value(timing_snapshot, "fresh_momentum_score", 50.0)
    weakness = _num(momentum.weakness_score, 0.0)
    momentum_exhaustion = _momentum_raw_value(momentum, "exhaustion_score", timing_exhaustion)
    fresh = min(timing_fresh, _momentum_raw_value(momentum, "fresh_momentum_score", timing_fresh))

    if wait:
        score -= 10.0
        reasons.append("SCORE_PENALTY_WAIT_FOR_BETTER_ENTRY")
    if timing_quality in {"WEAK", "BAD"}:
        score -= 8.0
        reasons.append("SCORE_PENALTY_WEAK_TIMING")
    if late >= 55:
        score -= (late - 50.0) * 0.22
        reasons.append("SCORE_PENALTY_LATE_ENTRY")
    if max(timing_exhaustion, momentum_exhaustion, rev_exhaustion) >= 55:
        score -= (max(timing_exhaustion, momentum_exhaustion, rev_exhaustion) - 50.0) * 0.18
        reasons.append("SCORE_PENALTY_EXHAUSTION")
    if rev >= 55:
        score -= (rev - 50.0) * 0.16
        reasons.append("SCORE_PENALTY_REVERSAL")
    if continuation < 45:
        score -= (45.0 - continuation) * 0.18
        reasons.append("SCORE_PENALTY_CONTINUATION_WEAK")
    if weakness >= 58:
        score -= (weakness - 52.0) * 0.16
        reasons.append("SCORE_PENALTY_WEAKNESS")
    if trap >= 58:
        score -= (trap - 52.0) * 0.14
        reasons.append("SCORE_PENALTY_TRAP")
    if fresh <= 42 and late >= 45:
        score -= 9.0
        reasons.append("SCORE_PENALTY_NOT_FRESH_AND_LATE")

    return clamp(score, 0.0, 100.0), reasons


# =============================================================================
# Decision rules
# =============================================================================

def hard_reject_reasons(
    *,
    direction: str,
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
    context: MarketContextSnapshot,
    tp_sl: Optional[TPSLPlan],
    reversal_snapshot: Optional[Mapping[str, Any]],
    timing_snapshot: Optional[Mapping[str, Any]],
) -> list[str]:
    """Return hard reject reasons. Keep hard blocks limited to obvious danger."""
    reasons: list[str] = []

    if not direction_valid(direction):
        reasons.append("INVALID_DIRECTION")

    # No TP/SL plan means this opportunity cannot be managed safely.
    if tp_sl is None:
        reasons.append("TP_SL_MISSING")
    elif _cfg_bool("tp_sl_required_for_real", True) and not tp_sl.valid:
        reasons.append("TP_SL_INVALID")

    if _num(liquidity.trap_risk_score, 0.0) >= 82:
        reasons.append("EXTREME_TRAP_RISK")

    if liquidity.likely_trap and _num(liquidity.fake_break_risk, 0.0) >= 75:
        reasons.append("LIKELY_FAKE_BREAK_TRAP")

    if reversal_snapshot:
        if _mapping_value(reversal_snapshot, "reversal_probability", 0.0) >= 82:
            reasons.append("EXTREME_REVERSAL_PROBABILITY")
        if _mapping_value(reversal_snapshot, "exhaustion_probability", 0.0) >= 85:
            reasons.append("EXTREME_EXHAUSTION_PROBABILITY")

    if timing_snapshot:
        if _mapping_value(timing_snapshot, "late_risk_score", 0.0) >= 85:
            reasons.append("EXTREME_LATE_ENTRY_RISK")
        if bool(timing_snapshot.get("wait_for_better_entry")) and _mapping_value(timing_snapshot, "late_risk_score", 0.0) >= 75:
            reasons.append("WAIT_AND_VERY_LATE_ENTRY")

    if _num(momentum.momentum_score, 0.0) < 35 and _num(structure.structure_score, 0.0) < 40:
        reasons.append("STRUCTURE_AND_MOMENTUM_TOO_WEAK")

    return reasons


def _real_quality_block_reasons(
    *,
    liquidity: LiquiditySnapshot,
    reversal_snapshot: Optional[Mapping[str, Any]],
    timing_snapshot: Optional[Mapping[str, Any]],
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    context: MarketContextSnapshot,
) -> list[str]:
    """Reasons that block REAL but still allow GHOST learning when score is enough."""
    reasons: list[str] = []

    trap = _num(liquidity.trap_risk_score, 0.0)
    rev = _mapping_value(reversal_snapshot, "reversal_probability", 0.0)
    rev_exhaustion = _mapping_value(reversal_snapshot, "exhaustion_probability", 0.0)
    continuation = _mapping_value(reversal_snapshot, "continuation_probability", 50.0)

    timing_score = _mapping_value(timing_snapshot, "timing_score", 50.0)
    late = _mapping_value(timing_snapshot, "late_risk_score", 0.0)
    wait = bool(timing_snapshot.get("wait_for_better_entry")) if isinstance(timing_snapshot, Mapping) else False
    quality = safe_str(timing_snapshot.get("entry_quality") if isinstance(timing_snapshot, Mapping) else "").upper()
    timing_fresh = _raw_mapping_value(timing_snapshot, "fresh_momentum_score", 50.0)
    timing_exhaustion = _raw_mapping_value(timing_snapshot, "exhaustion_score", 0.0)
    move_age = _raw_mapping_value(timing_snapshot, "move_age_score", 50.0)

    fresh = min(timing_fresh, _momentum_raw_value(momentum, "fresh_momentum_score", timing_fresh))
    exhaustion = max(timing_exhaustion, _momentum_raw_value(momentum, "exhaustion_score", timing_exhaustion), rev_exhaustion)
    weakness = _num(momentum.weakness_score, 0.0)

    if trap > _cfg_float("max_trap_risk_for_real", 58.0):
        reasons.append("REAL_BLOCK_TRAP_RISK")
    if rev > _cfg_float("max_reversal_probability_for_real", 55.0):
        reasons.append("REAL_BLOCK_REVERSAL_RISK")
    if late > _cfg_float("max_late_risk_for_real", 55.0):
        reasons.append("REAL_BLOCK_LATE_RISK")
    if timing_score < _cfg_float("min_timing_score_for_real", 62.0):
        reasons.append("REAL_BLOCK_TIMING_LOW")
    if _num(structure.structure_score, 0.0) < _cfg_float("min_structure_score_for_real", 58.0):
        reasons.append("REAL_BLOCK_STRUCTURE_LOW")
    if _num(momentum.momentum_score, 0.0) < _cfg_float("min_momentum_score_for_real", 60.0):
        reasons.append("REAL_BLOCK_MOMENTUM_LOW")
    if _num(context.context_score, 50.0) < _cfg_float("min_context_score_for_real", 38.0):
        reasons.append("REAL_BLOCK_CONTEXT_LOW")
    if wait:
        reasons.append("REAL_BLOCK_WAIT_FOR_BETTER_ENTRY")
    if quality in {"WEAK", "BAD"}:
        reasons.append("REAL_BLOCK_WEAK_TIMING_QUALITY")
    if exhaustion > _cfg_float("max_exhaustion_for_real", 58.0):
        reasons.append("REAL_BLOCK_EXHAUSTION")
    if fresh < _cfg_float("min_fresh_momentum_for_real", 45.0) and late >= 40:
        reasons.append("REAL_BLOCK_NOT_FRESH")
    if weakness > _cfg_float("max_weakness_for_real", 58.0):
        reasons.append("REAL_BLOCK_WEAKNESS")
    if continuation < _cfg_float("min_continuation_probability_for_real", 48.0):
        reasons.append("REAL_BLOCK_CONTINUATION_LOW")

    # Anti-chase rule: do not REAL a direction after the move is already consumed.
    if structure.is_late_move and (late >= 45 or exhaustion >= 50 or fresh <= 50):
        reasons.append("REAL_BLOCK_STRUCTURE_LATE_CHASE")
    if move_age >= 70 and (late >= 45 or exhaustion >= 50):
        reasons.append("REAL_BLOCK_MOVE_AGE_LATE")
    if late >= 50 and exhaustion >= 50:
        reasons.append("REAL_BLOCK_LATE_AND_EXHAUSTED")
    if late >= 45 and weakness >= 55:
        reasons.append("REAL_BLOCK_LATE_WITH_WEAKNESS")
    if rev >= 50 and continuation < 45:
        reasons.append("REAL_BLOCK_REVERSAL_OVER_CONTINUATION")
    if liquidity.likely_trap and trap >= 50:
        reasons.append("REAL_BLOCK_LIKELY_TRAP")

    # De-duplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for reason in reasons:
        if reason not in seen:
            out.append(reason)
            seen.add(reason)
    return out


def choose_mode(
    *,
    final_score: float,
    confidence: float,
    hard_rejects: list[str],
    liquidity: LiquiditySnapshot,
    reversal_snapshot: Optional[Mapping[str, Any]],
    timing_snapshot: Optional[Mapping[str, Any]],
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    context: MarketContextSnapshot,
    trade_state: Optional[Mapping[str, Any]] = None,
) -> tuple[str, list[str]]:
    """Choose REAL/GHOST/REJECT before trade-off downgrade."""
    if hard_rejects:
        return MODE_REJECT, hard_rejects

    real_min_score = _cfg_float("real_min_score", 78.0)
    real_min_conf = _cfg_float("real_min_confidence", 72.0)
    ghost_min_score = _cfg_float("ghost_min_score", 55.0)

    real_blocks = _real_quality_block_reasons(
        liquidity=liquidity,
        reversal_snapshot=reversal_snapshot,
        timing_snapshot=timing_snapshot,
        structure=structure,
        momentum=momentum,
        context=context,
    )

    if final_score >= real_min_score and confidence >= real_min_conf and not real_blocks:
        return MODE_REAL, ["AI_MODE_REAL"]

    if final_score >= ghost_min_score:
        if real_blocks:
            return MODE_GHOST, ["AI_MODE_GHOST_INSTEAD_OF_REAL", *real_blocks]
        return MODE_GHOST, ["AI_MODE_GHOST_SCORE"]

    if final_score < _cfg_float("reject_below_score", 45.0):
        return MODE_REJECT, ["AI_REJECT_SCORE_TOO_LOW"]

    return MODE_GHOST, ["AI_MODE_GHOST_BORDERLINE"]


# =============================================================================
# Public builders
# =============================================================================

def make_reject_decision(symbol: str, direction: str, reason: str, metadata: Optional[Mapping[str, Any]] = None) -> AIDecision:
    """Create standard reject decision."""
    d = normalize_direction(direction)
    return AIDecision(
        symbol=normalize_symbol(symbol),
        direction=d,
        mode=MODE_REJECT,
        score=0.0,
        confidence=0.0,
        entry=0.0,
        tp_sl=None,
        reason_codes=[safe_str(reason, "REJECT")],
        reject_reason=safe_str(reason, "REJECT"),
        metadata=dict(metadata or {}),
    )


def build_ai_decision(
    *,
    symbol: str,
    direction: str,
    sensor: SensorSnapshot,
    structure: StructureSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
    context: MarketContextSnapshot,
    tp_sl: Optional[TPSLPlan],
    reversal_snapshot: Optional[Mapping[str, Any]] = None,
    timing_snapshot: Optional[Mapping[str, Any]] = None,
    trade_state: Optional[Mapping[str, Any]] = None,
) -> AIDecision:
    """
    Build final AI decision for a new opportunity.

    This is the only module that decides REAL/GHOST/REJECT, but it does not
    execute trades or store records.
    """
    normalized_symbol = normalize_symbol(symbol)
    d = normalize_direction(direction)

    if not normalized_symbol or not direction_valid(d):
        return make_reject_decision(normalized_symbol, d, "INVALID_SYMBOL_OR_DIRECTION")

    runtime = get_trade_runtime_config(trade_state)

    quantity = 0.0
    if tp_sl is not None:
        margin = _num(runtime.get("margin_usdt"), 0.0)
        lev = _num(runtime.get("leverage"), 1.0)
        if _num(tp_sl.entry, 0.0) > 0:
            quantity = (margin * lev) / tp_sl.entry

    reason_codes: list[str] = []

    structure_score, structure_reasons = score_structure(structure)
    momentum_score, momentum_reasons = score_momentum(momentum)
    liquidity_score, liquidity_reasons = score_liquidity(liquidity)
    context_score, context_reasons = score_context(context)
    reversal_score, reversal_reasons = score_reversal(reversal_snapshot)
    timing_score, timing_reasons = score_timing(timing_snapshot)
    tp_sl_score, tp_sl_reasons = score_tp_sl(tp_sl, quantity=quantity)

    reason_codes.extend(structure_reasons)
    reason_codes.extend(momentum_reasons)
    reason_codes.extend(liquidity_reasons)
    reason_codes.extend(context_reasons)
    reason_codes.extend(reversal_reasons)
    reason_codes.extend(timing_reasons)
    reason_codes.extend(tp_sl_reasons)

    parts = {
        "structure": structure_score,
        "momentum": momentum_score,
        "liquidity": liquidity_score,
        "context": context_score,
        "reversal": reversal_score,
        "timing": timing_score,
        "tp_sl": tp_sl_score,
    }

    raw_final_score = combine_final_score(parts)
    final_score, score_penalty_reasons = adjusted_score_for_entry_quality(
        final_score=raw_final_score,
        liquidity=liquidity,
        reversal_snapshot=reversal_snapshot,
        timing_snapshot=timing_snapshot,
        momentum=momentum,
    )
    reason_codes.extend(score_penalty_reasons)

    confidence = confidence_from_score(final_score, parts)

    rejects = hard_reject_reasons(
        direction=d,
        structure=structure,
        momentum=momentum,
        liquidity=liquidity,
        context=context,
        tp_sl=tp_sl,
        reversal_snapshot=reversal_snapshot,
        timing_snapshot=timing_snapshot,
    )

    mode, mode_reasons = choose_mode(
        final_score=final_score,
        confidence=confidence,
        hard_rejects=rejects,
        liquidity=liquidity,
        reversal_snapshot=reversal_snapshot,
        timing_snapshot=timing_snapshot,
        structure=structure,
        momentum=momentum,
        context=context,
        trade_state=trade_state,
    )
    reason_codes.extend(mode_reasons)

    executable_mode = execution_mode_for_new_decision(mode, trade_state)
    if mode == MODE_REAL and executable_mode == MODE_GHOST:
        reason_codes.append("TRADE_OFF_REAL_DOWNGRADED_TO_GHOST")
    mode = executable_mode

    reject_reason = ""
    if mode == MODE_REJECT:
        reject_reason = ",".join(mode_reasons or rejects or ["AI_REJECT"])

    entry = _num(sensor.price, 0.0)
    if entry <= 0 and tp_sl is not None:
        entry = _num(tp_sl.entry, 0.0)

    # Flatten the most important decision features into metadata so downstream
    # components such as candidate_selector.py, learning_memory.py, and future
    # Pattern Memory do not need to guess or reverse-engineer values from nested
    # snapshots. Keep 0.0 values intact by using _num(), never `or default`.
    trap_risk_score = _num(liquidity.trap_risk_score, 0.0)
    fake_break_risk = _num(getattr(liquidity, "fake_break_risk", 0.0), 0.0)
    breakout_survival_score = _num(getattr(liquidity, "breakout_survival_score", 50.0), 50.0)

    late_risk_score = _mapping_value(timing_snapshot, "late_risk_score", 0.0)
    timing_quality = safe_str(timing_snapshot.get("entry_quality") if isinstance(timing_snapshot, Mapping) else "").upper()
    wait_for_better_entry = bool(timing_snapshot.get("wait_for_better_entry")) if isinstance(timing_snapshot, Mapping) else False
    move_age_score = _raw_mapping_value(timing_snapshot, "move_age_score", 50.0)

    timing_fresh = _raw_mapping_value(timing_snapshot, "fresh_momentum_score", 50.0)
    timing_exhaustion = _raw_mapping_value(timing_snapshot, "exhaustion_score", 0.0)
    momentum_fresh = _momentum_raw_value(momentum, "fresh_momentum_score", timing_fresh)
    momentum_exhaustion = _momentum_raw_value(momentum, "exhaustion_score", timing_exhaustion)

    fresh_momentum_score = min(timing_fresh, momentum_fresh)
    exhaustion_score = max(
        timing_exhaustion,
        momentum_exhaustion,
        _mapping_value(reversal_snapshot, "exhaustion_probability", 0.0),
    )
    weakness_score = _num(momentum.weakness_score, 0.0)
    reversal_probability = _mapping_value(reversal_snapshot, "reversal_probability", 0.0)
    continuation_probability = _mapping_value(reversal_snapshot, "continuation_probability", 50.0)
    exhaustion_probability = _mapping_value(reversal_snapshot, "exhaustion_probability", 0.0)

    expected_net_profit = _num(getattr(tp_sl, "tp1_net_profit_estimate", 0.0), 0.0) if tp_sl is not None else 0.0
    rr = _num(getattr(tp_sl, "rr", 0.0), 0.0) if tp_sl is not None else 0.0

    learning_features = {
        "symbol": normalized_symbol,
        "direction": d,
        "level": 4,
        "entry": entry,
        "mode": mode,
        "score": final_score,
        "confidence": confidence,
        "structure_score": structure_score,
        "momentum_score": momentum_score,
        "liquidity_score": liquidity_score,
        "context_score": context_score,
        "reversal_score": reversal_score,
        "timing_score": timing_score,
        "tp_sl_score": tp_sl_score,
        "trap_risk_score": trap_risk_score,
        "fake_break_risk": fake_break_risk,
        "breakout_survival_score": breakout_survival_score,
        "late_risk_score": late_risk_score,
        "fresh_momentum_score": fresh_momentum_score,
        "exhaustion_score": exhaustion_score,
        "weakness_score": weakness_score,
        "reversal_probability": reversal_probability,
        "continuation_probability": continuation_probability,
        "exhaustion_probability": exhaustion_probability,
        "wait_for_better_entry": wait_for_better_entry,
        "entry_quality": timing_quality,
        "move_age_score": move_age_score,
        "expected_net_profit": expected_net_profit,
        "rr": rr,
        "tp1": _num(getattr(tp_sl, "tp1", 0.0), 0.0) if tp_sl is not None else 0.0,
        "tp2": _num(getattr(tp_sl, "tp2", 0.0), 0.0) if tp_sl is not None else 0.0,
        "sl": _num(getattr(tp_sl, "sl", 0.0), 0.0) if tp_sl is not None else 0.0,
    }

    return AIDecision(
        symbol=normalized_symbol,
        direction=d,
        mode=mode,
        score=final_score,
        confidence=confidence,
        entry=entry,
        tp_sl=tp_sl,
        reason_codes=reason_codes,
        reject_reason=reject_reason,
        metadata={
            "system_version": SYSTEM_VERSION,
            "created_at": utc_now_iso(),
            "trap_risk_score": trap_risk_score,
            "fake_break_risk": fake_break_risk,
            "breakout_survival_score": breakout_survival_score,
            "late_risk_score": late_risk_score,
            "fresh_momentum_score": fresh_momentum_score,
            "exhaustion_score": exhaustion_score,
            "weakness_score": weakness_score,
            "reversal_probability": reversal_probability,
            "continuation_probability": continuation_probability,
            "exhaustion_probability": exhaustion_probability,
            "wait_for_better_entry": wait_for_better_entry,
            "entry_quality": timing_quality,
            "move_age_score": move_age_score,
            "expected_net_profit": expected_net_profit,
            "rr": rr,
            "learning_features": learning_features,
            "component_scores": parts,
            "raw_final_score": raw_final_score,
            "adjusted_final_score": final_score,
            "runtime": runtime,
            "reversal_snapshot": dict(reversal_snapshot or {}),
            "timing_snapshot": dict(timing_snapshot or {}),
            "quantity_estimate": quantity,
            "trade_enabled": is_real_trading_enabled(trade_state),
        },
    )


def evaluate_open_position(
    *,
    position_direction: str,
    sensor: SensorSnapshot,
    momentum: MomentumSnapshot,
    liquidity: LiquiditySnapshot,
    reversal_snapshot: Optional[Mapping[str, Any]] = None,
    progress_to_tp1: float = 0.0,
    after_tp1: bool = False,
) -> MonitorDecision:
    """
    Lightweight AI monitor decision for open positions.

    Does not close anything. position_monitor decides and real_trade_manager verifies.
    """
    d = normalize_direction(position_direction)
    reasons: list[str] = []

    weakness = _num(momentum.weakness_score, 0.0)
    trap = _num(liquidity.trap_risk_score, 0.0)
    rev = _mapping_value(reversal_snapshot, "reversal_probability", 0.0)
    progress = _num(progress_to_tp1, 0.0)

    should_close = False
    action = "HOLD"
    confidence = 0.0

    if after_tp1:
        if weakness >= 60 or rev >= 62 or trap >= 70:
            should_close = True
            action = "CLOSE_RUNNER"
            confidence = clamp(max(weakness, rev, trap), 0.0, 100.0)
            reasons.append("AI_CLOSE_RUNNER_WEAKNESS")
    else:
        # Before TP1, do not be too nervous: require progress and confirmed weakness.
        if progress >= 0.70 and (weakness >= 65 or rev >= 68 or trap >= 75):
            should_close = True
            action = "AI_EXIT"
            confidence = clamp(max(weakness, rev, trap), 0.0, 100.0)
            reasons.append("AI_EXIT_BEFORE_TP1_CONFIRMED_WEAKNESS")

    if not reasons:
        reasons.append("AI_HOLD")

    return MonitorDecision(
        action=action,
        should_close=should_close,
        should_partial_close=False,
        should_protect_sl=after_tp1 or progress >= 1.0,
        close_reason=",".join(reasons) if should_close else "",
        confidence=confidence,
        progress_to_tp1=progress,
        weakness_confirmations=1 if should_close else 0,
        emergency=False,
        reason_codes=reasons,
        metadata={
            "weakness_score": weakness,
            "trap_risk_score": trap,
            "reversal_probability": rev,
            "direction": d,
        },
    )


def validate_ai_decision(decision: AIDecision) -> dict[str, Any]:
    """Lightweight validation for AIDecision output."""
    errors: list[str] = []

    if decision.system_version != SYSTEM_VERSION:
        errors.append("INVALID_SYSTEM_VERSION")
    if not normalize_symbol(decision.symbol):
        errors.append("MISSING_SYMBOL")
    if decision.direction not in {DIRECTION_LONG, DIRECTION_SHORT}:
        errors.append("INVALID_DIRECTION")
    if decision.mode not in {MODE_REAL, MODE_GHOST, MODE_REJECT}:
        errors.append("INVALID_MODE")
    if not (0.0 <= _num(decision.score, -1.0) <= 100.0):
        errors.append("INVALID_SCORE")
    if not (0.0 <= _num(decision.confidence, -1.0) <= 100.0):
        errors.append("INVALID_CONFIDENCE")
    if decision.mode != MODE_REJECT and decision.entry <= 0:
        errors.append("INVALID_ENTRY")

    return {
        "valid": not errors,
        "errors": errors,
        "symbol": decision.symbol,
        "direction": decision.direction,
        "mode": decision.mode,
        "score": decision.score,
        "confidence": decision.confidence,
    }


__all__ = [
    "AI_BRAIN_VERSION",
    "DEFAULT_AI_CONFIG",
    "direction_valid",
    "score_structure",
    "score_momentum",
    "score_liquidity",
    "score_context",
    "score_reversal",
    "score_timing",
    "score_tp_sl",
    "combine_final_score",
    "confidence_from_score",
    "adjusted_score_for_entry_quality",
    "hard_reject_reasons",
    "choose_mode",
    "make_reject_decision",
    "build_ai_decision",
    "evaluate_open_position",
    "validate_ai_decision",
]
