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
    "real_min_score": 76.0,
    "real_min_confidence": 70.0,
    "ghost_min_score": 55.0,
    "reject_below_score": 45.0,
    "max_trap_risk_for_real": 62.0,
    "max_reversal_probability_for_real": 58.0,
    "max_late_risk_for_real": 62.0,
    "min_timing_score_for_real": 58.0,
    "min_structure_score_for_real": 55.0,
    "min_momentum_score_for_real": 58.0,
    "min_context_score_for_real": 38.0,
    "tp_sl_required_for_real": True,
    "soft_ghost_when_trade_off": True,
}


def _ai_config() -> Mapping[str, Any]:
    """Return AI config from constants if available, otherwise safe fallback."""
    return getattr(constants, "AI_DECISION_CONFIG", DEFAULT_AI_CONFIG)


def _cfg_float(key: str, default: float) -> float:
    return safe_float(_ai_config().get(key), default) or default


def _cfg_bool(key: str, default: bool) -> bool:
    return safe_bool(_ai_config().get(key), default)


# =============================================================================
# Scoring helpers
# =============================================================================

def direction_valid(direction: str) -> bool:
    return normalize_direction(direction) in {DIRECTION_LONG, DIRECTION_SHORT}


def score_structure(structure: StructureSnapshot) -> tuple[float, list[str]]:
    score = safe_float(structure.structure_score, 0.0) or 0.0
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
    score = safe_float(momentum.momentum_score, 0.0) or 0.0
    reasons: list[str] = []
    if score >= 72:
        reasons.append("AI_MOMENTUM_STRONG")
    elif score >= 58:
        reasons.append("AI_MOMENTUM_OK")
    else:
        reasons.append("AI_MOMENTUM_WEAK")
    if safe_float(momentum.weakness_score, 0.0) >= 60:
        reasons.append("AI_MOMENTUM_WEAKNESS_VISIBLE")
    return clamp(score, 0.0, 100.0), reasons


def score_liquidity(liquidity: LiquiditySnapshot) -> tuple[float, list[str]]:
    trap = safe_float(liquidity.trap_risk_score, 0.0) or 0.0
    survival = safe_float(liquidity.breakout_survival_score, 50.0) or 50.0
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
    score = safe_float(context.context_score, 50.0) or 50.0
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

    reversal_prob = safe_float(reversal_snapshot.get("reversal_probability"), 50.0) or 50.0
    exhaustion_prob = safe_float(reversal_snapshot.get("exhaustion_probability"), 50.0) or 50.0
    continuation_prob = safe_float(reversal_snapshot.get("continuation_probability"), 50.0) or 50.0

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

    return clamp(score, 0.0, 100.0), reasons


def score_timing(timing_snapshot: Optional[Mapping[str, Any]]) -> tuple[float, list[str]]:
    if not timing_snapshot:
        return 55.0, ["AI_TIMING_MISSING"]

    score = safe_float(timing_snapshot.get("timing_score"), 50.0) or 50.0
    quality = safe_str(timing_snapshot.get("entry_quality")).upper()
    wait = bool(timing_snapshot.get("wait_for_better_entry"))

    reasons: list[str] = []
    if quality in {"EXCELLENT", "GOOD"}:
        reasons.append("AI_TIMING_GOOD")
    elif quality == "ACCEPTABLE":
        reasons.append("AI_TIMING_ACCEPTABLE")
    else:
        reasons.append("AI_TIMING_WEAK")

    if wait:
        reasons.append("AI_TIMING_WAIT_SUGGESTED")

    return clamp(score, 0.0, 100.0), reasons


def score_tp_sl(plan: Optional[TPSLPlan], quantity: float = 0.0) -> tuple[float, list[str]]:
    if plan is None:
        return 0.0, ["AI_TP_SL_MISSING"]

    valid, errors = validate_tp_sl_plan(plan, quantity=quantity)
    if not valid:
        return 25.0, ["AI_TP_SL_INVALID", *errors]

    score = 55.0
    rr = safe_float(plan.rr, 0.0) or 0.0
    net = safe_float(plan.tp1_net_profit_estimate, 0.0) or 0.0

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
    """Weighted final score."""
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
        total += (safe_float(parts.get(key), 0.0) or 0.0) * weight
        wsum += weight
    if wsum <= 0:
        return 0.0
    return clamp(total / wsum, 0.0, 100.0)


def confidence_from_score(score: float, parts: Mapping[str, float]) -> float:
    """
    Estimate confidence from score and consistency.

    If components are highly inconsistent, confidence is reduced.
    """
    values = [safe_float(v, 0.0) or 0.0 for v in parts.values()]
    if not values:
        return 0.0

    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    spread_penalty = min(18.0, variance ** 0.5 * 0.35)

    confidence = (safe_float(score, 0.0) or 0.0) - spread_penalty
    return clamp(confidence, 0.0, 100.0)


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

    if tp_sl is None:
        reasons.append("TP_SL_MISSING")
    elif _cfg_bool("tp_sl_required_for_real", True):
        if not tp_sl.valid:
            reasons.append("TP_SL_INVALID")

    if safe_float(liquidity.trap_risk_score, 0.0) >= 82:
        reasons.append("EXTREME_TRAP_RISK")

    if liquidity.likely_trap and safe_float(liquidity.fake_break_risk, 0.0) >= 75:
        reasons.append("LIKELY_FAKE_BREAK_TRAP")

    if reversal_snapshot:
        if safe_float(reversal_snapshot.get("reversal_probability"), 0.0) >= 82:
            reasons.append("EXTREME_REVERSAL_PROBABILITY")

    if timing_snapshot:
        if safe_float(timing_snapshot.get("late_risk_score"), 0.0) >= 85:
            reasons.append("EXTREME_LATE_ENTRY_RISK")

    if safe_float(momentum.momentum_score, 0.0) < 35 and safe_float(structure.structure_score, 0.0) < 40:
        reasons.append("STRUCTURE_AND_MOMENTUM_TOO_WEAK")

    return reasons


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
    reasons: list[str] = []

    if hard_rejects:
        return MODE_REJECT, hard_rejects

    real_min_score = _cfg_float("real_min_score", 76.0)
    real_min_conf = _cfg_float("real_min_confidence", 70.0)
    ghost_min_score = _cfg_float("ghost_min_score", 55.0)

    trap = safe_float(liquidity.trap_risk_score, 0.0) or 0.0
    rev = safe_float((reversal_snapshot or {}).get("reversal_probability"), 0.0) or 0.0
    timing_score = safe_float((timing_snapshot or {}).get("timing_score"), 50.0) or 50.0
    late = safe_float((timing_snapshot or {}).get("late_risk_score"), 0.0) or 0.0

    real_allowed = True
    if trap > _cfg_float("max_trap_risk_for_real", 62.0):
        real_allowed = False
        reasons.append("REAL_BLOCK_TRAP_RISK")
    if rev > _cfg_float("max_reversal_probability_for_real", 58.0):
        real_allowed = False
        reasons.append("REAL_BLOCK_REVERSAL_RISK")
    if late > _cfg_float("max_late_risk_for_real", 62.0):
        real_allowed = False
        reasons.append("REAL_BLOCK_LATE_RISK")
    if timing_score < _cfg_float("min_timing_score_for_real", 58.0):
        real_allowed = False
        reasons.append("REAL_BLOCK_TIMING_LOW")
    if safe_float(structure.structure_score, 0.0) < _cfg_float("min_structure_score_for_real", 55.0):
        real_allowed = False
        reasons.append("REAL_BLOCK_STRUCTURE_LOW")
    if safe_float(momentum.momentum_score, 0.0) < _cfg_float("min_momentum_score_for_real", 58.0):
        real_allowed = False
        reasons.append("REAL_BLOCK_MOMENTUM_LOW")
    if safe_float(context.context_score, 50.0) < _cfg_float("min_context_score_for_real", 38.0):
        real_allowed = False
        reasons.append("REAL_BLOCK_CONTEXT_LOW")

    if final_score >= real_min_score and confidence >= real_min_conf and real_allowed:
        return MODE_REAL, ["AI_MODE_REAL"]

    if final_score >= ghost_min_score:
        if reasons:
            return MODE_GHOST, ["AI_MODE_GHOST_INSTEAD_OF_REAL", *reasons]
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
        margin = safe_float(runtime.get("margin_usdt"), 0.0) or 0.0
        lev = safe_float(runtime.get("leverage"), 1.0) or 1.0
        if tp_sl.entry > 0:
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

    final_score = combine_final_score(parts)
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

    entry = safe_float(sensor.price, 0.0) or (tp_sl.entry if tp_sl else 0.0)

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
            "component_scores": parts,
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

    weakness = safe_float(momentum.weakness_score, 0.0) or 0.0
    trap = safe_float(liquidity.trap_risk_score, 0.0) or 0.0
    rev = safe_float((reversal_snapshot or {}).get("reversal_probability"), 0.0) or 0.0
    progress = safe_float(progress_to_tp1, 0.0) or 0.0

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
        # Before TP1, do not be too nervous: require progress and weakness.
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
    if not (0.0 <= safe_float(decision.score, -1.0) <= 100.0):
        errors.append("INVALID_SCORE")
    if not (0.0 <= safe_float(decision.confidence, -1.0) <= 100.0):
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
    "hard_reject_reasons",
    "choose_mode",
    "make_reject_decision",
    "build_ai_decision",
    "evaluate_open_position",
    "validate_ai_decision",
]
