"""
candidate_selector.py
Level 4 / 1H Smart Scalp Bot

Candidate selector for Level 4 REAL execution.

Architecture lock:
- Selects the best / highest expected-profit candidate from already-built AIDecision objects.
- Does not fetch market data, calculate indicators, place orders, monitor positions,
  write JSON state, or build Telegram text.
- Does not call Toobit or real_trade_manager.py.
- Keeps AI Brain as the per-candidate REAL/GHOST/REJECT decision maker.
- This layer only arbitrates between multiple candidates so only the strongest
  opportunity can remain REAL.
- Allowed project imports: constants.py, utils.py, models.py only.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Optional, Sequence

from constants import MODE_GHOST, MODE_REAL, MODE_REJECT, SYSTEM_VERSION
from models import AIDecision, TPSLPlan
from utils import clamp, normalize_direction, normalize_symbol, safe_float, safe_int, safe_str, utc_now_iso


CANDIDATE_SELECTOR_VERSION: str = SYSTEM_VERSION


DEFAULT_SELECTOR_CONFIG: dict[str, Any] = {
    # Only the best candidate should remain REAL by default. The strategy manager
    # and real_trade_manager still enforce actual slot limits later.
    "max_real_candidates": 1,

    # Minimum quality needed for a candidate to stay REAL after cross-candidate selection.
    "min_selected_score": 76.0,
    "min_selected_confidence": 70.0,
    "min_selected_net_profit": 0.10,
    "min_selected_profit_percent": 0.12,
    "min_selected_expected_move_percent": 0.18,
    "min_selected_rr": 0.75,
    "max_selected_late_risk": 55.0,
    "max_selected_reversal": 55.0,
    "max_selected_trap": 58.0,
    "max_selected_exhaustion": 58.0,

    # Ranking weights. Final rank is not the same as AI score; it prefers
    # high-quality + high expected net profit without accepting late/chasing risk.
    "weight_ai_score": 0.28,
    "weight_confidence": 0.20,
    "weight_profit_quality": 0.16,
    "weight_relative_profit_quality": 0.12,
    "weight_rr_quality": 0.10,
    "weight_timing_quality": 0.12,
    "weight_safety_quality": 0.10,
    "weight_learning_quality": 0.10,
}


# =============================================================================
# Safe helpers
# =============================================================================

def _num(value: Any, default: float = 0.0) -> float:
    """Return safe float while preserving a valid real 0.0."""
    parsed = safe_float(value, None)
    if parsed is None:
        return float(default)
    return float(parsed)


def _cfg(config: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    merged = dict(DEFAULT_SELECTOR_CONFIG)
    if isinstance(config, Mapping):
        merged.update(dict(config))
    return merged


def _cfg_float(config: Mapping[str, Any], key: str, default: float) -> float:
    return _num(config.get(key), default)


def _cfg_int(config: Mapping[str, Any], key: str, default: int) -> int:
    value = safe_int(config.get(key), default)
    return int(default if value is None else value)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _decision_metadata(decision: AIDecision) -> Mapping[str, Any]:
    return _as_mapping(getattr(decision, "metadata", {}))


def _component_scores(decision: AIDecision) -> Mapping[str, Any]:
    return _as_mapping(_decision_metadata(decision).get("component_scores"))


def _timing_snapshot(decision: AIDecision) -> Mapping[str, Any]:
    return _as_mapping(_decision_metadata(decision).get("timing_snapshot"))


def _reversal_snapshot(decision: AIDecision) -> Mapping[str, Any]:
    return _as_mapping(_decision_metadata(decision).get("reversal_snapshot"))


def _timing_raw(decision: AIDecision) -> Mapping[str, Any]:
    return _as_mapping(_timing_snapshot(decision).get("raw"))


def _tp_sl_plan(decision: AIDecision) -> Optional[TPSLPlan]:
    plan = getattr(decision, "tp_sl", None)
    return plan if isinstance(plan, TPSLPlan) else None


def _metadata_num(decision: AIDecision, key: str, default: float = 0.0) -> float:
    """Read a flattened numeric value from ai_brain metadata while preserving 0.0."""
    metadata = _decision_metadata(decision)
    if key in metadata:
        return _num(metadata.get(key), default)
    learning = _as_mapping(metadata.get("learning_features"))
    if key in learning:
        return _num(learning.get(key), default)
    return float(default)


def _metadata_bool(decision: AIDecision, key: str, default: bool = False) -> bool:
    metadata = _decision_metadata(decision)
    if key in metadata:
        return bool(metadata.get(key))
    learning = _as_mapping(metadata.get("learning_features"))
    if key in learning:
        return bool(learning.get(key))
    return bool(default)


# =============================================================================
# Feature extraction
# =============================================================================

def expected_net_profit(decision: AIDecision) -> float:
    """Return TP1 expected net profit estimate in USDT. Prefer ai_brain metadata."""
    metadata = _decision_metadata(decision)
    if "expected_net_profit" in metadata or "expected_net_profit" in _as_mapping(metadata.get("learning_features")):
        return _metadata_num(decision, "expected_net_profit", 0.0)
    plan = _tp_sl_plan(decision)
    if plan is None:
        return 0.0
    return _num(plan.tp1_net_profit_estimate, 0.0)


def entry_price(decision: AIDecision) -> float:
    """Return entry price from decision/metadata/TP plan."""
    entry = _num(getattr(decision, "entry", 0.0), 0.0)
    if entry > 0:
        return entry
    entry = _metadata_num(decision, "entry", 0.0)
    if entry > 0:
        return entry
    plan = _tp_sl_plan(decision)
    return _num(getattr(plan, "entry", 0.0), 0.0) if plan is not None else 0.0


def tp1_price(decision: AIDecision) -> float:
    metadata = _decision_metadata(decision)
    learning = _as_mapping(metadata.get("learning_features"))
    if "tp1" in learning:
        return _num(learning.get("tp1"), 0.0)
    plan = _tp_sl_plan(decision)
    return _num(getattr(plan, "tp1", 0.0), 0.0) if plan is not None else 0.0


def expected_move_percent(decision: AIDecision) -> float:
    """Absolute TP1 move percent from entry to TP1. Useful across different coin prices."""
    metadata_value = _metadata_num(decision, "expected_move_percent", -1.0)
    if metadata_value >= 0:
        return metadata_value
    entry = entry_price(decision)
    tp1 = tp1_price(decision)
    if entry <= 0 or tp1 <= 0:
        return 0.0
    return abs(tp1 - entry) / entry * 100.0


def profit_percent(decision: AIDecision) -> float:
    """Approximate net profit percent versus margin/entry context when available."""
    direct = _metadata_num(decision, "profit_percent", -1.0)
    if direct >= 0:
        return direct
    learning = _as_mapping(_decision_metadata(decision).get("learning_features"))
    margin = _num(_as_mapping(_decision_metadata(decision).get("runtime")).get("margin_usdt"), 0.0)
    if margin <= 0:
        margin = _num(learning.get("margin_usdt"), 0.0)
    net = expected_net_profit(decision)
    if margin > 0 and net > 0:
        return net / margin * 100.0
    return expected_move_percent(decision)


def reward_risk(decision: AIDecision) -> float:
    """Return TP1 RR from metadata or TP/SL plan."""
    metadata = _decision_metadata(decision)
    if "rr" in metadata or "rr" in _as_mapping(metadata.get("learning_features")):
        return _metadata_num(decision, "rr", 0.0)
    plan = _tp_sl_plan(decision)
    if plan is None:
        return 0.0
    return _num(plan.rr, 0.0)


def timing_score(decision: AIDecision) -> float:
    snap = _timing_snapshot(decision)
    if snap:
        return _num(snap.get("timing_score"), _num(_component_scores(decision).get("timing"), 50.0))
    return _num(_component_scores(decision).get("timing"), 50.0)


def late_risk_score(decision: AIDecision) -> float:
    if "late_risk_score" in _decision_metadata(decision):
        return _metadata_num(decision, "late_risk_score", 0.0)
    return _num(_timing_snapshot(decision).get("late_risk_score"), 0.0)


def fresh_momentum_score(decision: AIDecision) -> float:
    if "fresh_momentum_score" in _decision_metadata(decision):
        return _metadata_num(decision, "fresh_momentum_score", 50.0)
    return _num(_timing_raw(decision).get("fresh_momentum_score"), 50.0)


def exhaustion_score(decision: AIDecision) -> float:
    if "exhaustion_score" in _decision_metadata(decision):
        return _metadata_num(decision, "exhaustion_score", 0.0)
    timing_exhaustion = _num(_timing_raw(decision).get("exhaustion_score"), 0.0)
    reversal_exhaustion = _num(_reversal_snapshot(decision).get("exhaustion_probability"), 0.0)
    return max(timing_exhaustion, reversal_exhaustion)


def reversal_probability(decision: AIDecision) -> float:
    if "reversal_probability" in _decision_metadata(decision):
        return _metadata_num(decision, "reversal_probability", 0.0)
    return _num(_reversal_snapshot(decision).get("reversal_probability"), 0.0)


def continuation_probability(decision: AIDecision) -> float:
    if "continuation_probability" in _decision_metadata(decision):
        return _metadata_num(decision, "continuation_probability", 50.0)
    return _num(_reversal_snapshot(decision).get("continuation_probability"), 50.0)


def trap_risk_score(decision: AIDecision) -> float:
    # Prefer component liquidity score if raw liquidity trap was not separately stored.
    metadata = _decision_metadata(decision)
    if "trap_risk_score" in metadata:
        return _num(metadata.get("trap_risk_score"), 0.0)

    # ai_brain metadata currently stores component score, where higher liquidity score
    # means safer. Convert it back to a risk approximation for ranking.
    liquidity_quality = _num(_component_scores(decision).get("liquidity"), 50.0)
    return clamp(100.0 - liquidity_quality, 0.0, 100.0)


def has_wait_flag(decision: AIDecision) -> bool:
    if "wait_for_better_entry" in _decision_metadata(decision):
        return _metadata_bool(decision, "wait_for_better_entry", False)
    return bool(_timing_snapshot(decision).get("wait_for_better_entry"))


# =============================================================================
# Ranking / eligibility
# =============================================================================

def profit_quality(decision: AIDecision) -> float:
    """Map expected net profit to 0-100 without letting tiny profit rank high."""
    net = expected_net_profit(decision)
    if net <= 0:
        return 0.0
    if net < 0.10:
        return clamp(net / 0.10 * 35.0, 0.0, 35.0)
    if net < 0.20:
        return 55.0 + (net - 0.10) / 0.10 * 15.0
    if net < 0.40:
        return 70.0 + (net - 0.20) / 0.20 * 18.0
    return 92.0 + min(8.0, (net - 0.40) * 10.0)


def relative_profit_quality(decision: AIDecision) -> float:
    """Score relative profitability so lower-price/volatile coins are not ignored."""
    move_pct = expected_move_percent(decision)
    profit_pct = profit_percent(decision)
    # Blend pure price movement with margin-normalized profit when available.
    rel = max(move_pct, profit_pct * 0.60)
    if rel <= 0:
        return 0.0
    if rel < 0.10:
        return clamp(rel / 0.10 * 30.0, 0.0, 30.0)
    if rel < 0.25:
        return 45.0 + (rel - 0.10) / 0.15 * 25.0
    if rel < 0.50:
        return 70.0 + (rel - 0.25) / 0.25 * 18.0
    return 88.0 + min(12.0, (rel - 0.50) * 12.0)


def learning_quality(decision: AIDecision) -> float:
    """Use optional Pattern/Coin learning metadata without importing learning modules."""
    metadata = _decision_metadata(decision)
    learning = _as_mapping(metadata.get("learning_features"))
    candidates = [
        metadata.get("learning_score"),
        metadata.get("coin_learning_score"),
        metadata.get("pattern_memory_score"),
        metadata.get("pattern_success_rate"),
        metadata.get("similar_setup_success_rate"),
        metadata.get("historical_win_rate"),
        learning.get("learning_score"),
        learning.get("coin_learning_score"),
        learning.get("pattern_memory_score"),
        learning.get("pattern_success_rate"),
        learning.get("similar_setup_success_rate"),
        learning.get("historical_win_rate"),
    ]
    values = [_num(v, -1.0) for v in candidates if v is not None]
    values = [v * 100.0 if 0.0 <= v <= 1.0 else v for v in values if v >= 0.0]
    if not values:
        return 50.0
    return clamp(sum(values) / len(values), 0.0, 100.0)


def rr_quality(decision: AIDecision) -> float:
    rr = reward_risk(decision)
    if rr <= 0:
        return 0.0
    if rr < 0.75:
        return clamp(rr / 0.75 * 45.0, 0.0, 45.0)
    if rr < 1.10:
        return 60.0 + (rr - 0.75) / 0.35 * 20.0
    if rr < 1.60:
        return 80.0 + (rr - 1.10) / 0.50 * 15.0
    return 95.0


def timing_quality(decision: AIDecision) -> float:
    timing = timing_score(decision)
    late = late_risk_score(decision)
    fresh = fresh_momentum_score(decision)
    exhaustion = exhaustion_score(decision)
    wait_penalty = 18.0 if has_wait_flag(decision) else 0.0
    score = timing * 0.55 + fresh * 0.25 + (100.0 - late) * 0.12 + (100.0 - exhaustion) * 0.08 - wait_penalty
    return clamp(score, 0.0, 100.0)


def safety_quality(decision: AIDecision) -> float:
    late = late_risk_score(decision)
    trap = trap_risk_score(decision)
    rev = reversal_probability(decision)
    exhaustion = exhaustion_score(decision)
    continuation = continuation_probability(decision)
    score = (
        (100.0 - late) * 0.24
        + (100.0 - trap) * 0.22
        + (100.0 - rev) * 0.24
        + (100.0 - exhaustion) * 0.18
        + continuation * 0.12
    )
    return clamp(score, 0.0, 100.0)


def selector_rank_score(decision: AIDecision, config: Optional[Mapping[str, Any]] = None) -> float:
    """Rank candidate by quality + expected profitability + safety."""
    cfg = _cfg(config)
    score = (
        _num(decision.score, 0.0) * _cfg_float(cfg, "weight_ai_score", 0.28)
        + _num(decision.confidence, 0.0) * _cfg_float(cfg, "weight_confidence", 0.20)
        + profit_quality(decision) * _cfg_float(cfg, "weight_profit_quality", 0.16)
        + relative_profit_quality(decision) * _cfg_float(cfg, "weight_relative_profit_quality", 0.12)
        + rr_quality(decision) * _cfg_float(cfg, "weight_rr_quality", 0.10)
        + timing_quality(decision) * _cfg_float(cfg, "weight_timing_quality", 0.12)
        + safety_quality(decision) * _cfg_float(cfg, "weight_safety_quality", 0.10)
        + learning_quality(decision) * _cfg_float(cfg, "weight_learning_quality", 0.10)
    )

    # Strong anti-chase penalty. A late high-profit target should not beat a clean setup.
    if has_wait_flag(decision):
        score -= 10.0
    if late_risk_score(decision) >= 60:
        score -= 10.0
    if exhaustion_score(decision) >= 60:
        score -= 8.0
    if reversal_probability(decision) >= 60:
        score -= 8.0
    if continuation_probability(decision) < 45:
        score -= 7.0

    return clamp(score, 0.0, 100.0)


def real_eligibility_blocks(decision: AIDecision, config: Optional[Mapping[str, Any]] = None) -> list[str]:
    """Return reasons why this candidate cannot remain REAL after selection."""
    cfg = _cfg(config)
    reasons: list[str] = []

    if getattr(decision, "mode", "") != MODE_REAL:
        reasons.append("SELECTOR_NOT_REAL_FROM_AI")

    if _num(decision.score, 0.0) < _cfg_float(cfg, "min_selected_score", 76.0):
        reasons.append("SELECTOR_SCORE_LOW")
    if _num(decision.confidence, 0.0) < _cfg_float(cfg, "min_selected_confidence", 70.0):
        reasons.append("SELECTOR_CONFIDENCE_LOW")
    if expected_net_profit(decision) < _cfg_float(cfg, "min_selected_net_profit", 0.10):
        reasons.append("SELECTOR_NET_PROFIT_LOW")
    # Require either a reasonable price-move percentage or a margin-normalized profit percent.
    # This prevents selecting a high-dollar but tiny/weak move over a cleaner coin setup.
    min_move_pct = _cfg_float(cfg, "min_selected_expected_move_percent", 0.18)
    min_profit_pct = _cfg_float(cfg, "min_selected_profit_percent", 0.12)
    if expected_move_percent(decision) < min_move_pct and profit_percent(decision) < min_profit_pct:
        reasons.append("SELECTOR_RELATIVE_PROFIT_LOW")
    if reward_risk(decision) < _cfg_float(cfg, "min_selected_rr", 0.75):
        reasons.append("SELECTOR_RR_LOW")
    if late_risk_score(decision) > _cfg_float(cfg, "max_selected_late_risk", 55.0):
        reasons.append("SELECTOR_LATE_RISK_HIGH")
    if reversal_probability(decision) > _cfg_float(cfg, "max_selected_reversal", 55.0):
        reasons.append("SELECTOR_REVERSAL_HIGH")
    if trap_risk_score(decision) > _cfg_float(cfg, "max_selected_trap", 58.0):
        reasons.append("SELECTOR_TRAP_HIGH")
    if exhaustion_score(decision) > _cfg_float(cfg, "max_selected_exhaustion", 58.0):
        reasons.append("SELECTOR_EXHAUSTION_HIGH")
    if has_wait_flag(decision):
        reasons.append("SELECTOR_WAIT_FOR_BETTER_ENTRY")

    # If AI Brain already blocked REAL and downgraded it to GHOST, do not resurrect it.
    for reason in list(getattr(decision, "reason_codes", []) or []):
        reason_text = safe_str(reason).upper()
        if reason_text.startswith("REAL_BLOCK_"):
            reasons.append("SELECTOR_AI_REAL_BLOCK_PRESENT")
            break

    return _dedupe(reasons)


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = safe_str(item).upper()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _with_selector_metadata(decision: AIDecision, *, mode: Optional[str] = None, reason_codes: Optional[list[str]] = None, selected: bool = False, selector_rank: float = 0.0, selector_reason: str = "") -> AIDecision:
    """Return a copied AIDecision with selector metadata/reasons attached."""
    new_mode = safe_str(mode or decision.mode).upper()
    metadata = dict(getattr(decision, "metadata", {}) or {})
    metadata.update({
        "selector_version": CANDIDATE_SELECTOR_VERSION,
        "selector_checked_at": utc_now_iso(),
        "selector_selected_for_real": bool(selected),
        "selector_rank_score": selector_rank,
        "selector_reason": selector_reason,
        "selector_metrics": {
            "expected_net_profit": expected_net_profit(decision),
            "expected_move_percent": expected_move_percent(decision),
            "profit_percent": profit_percent(decision),
            "rr": reward_risk(decision),
            "timing_score": timing_score(decision),
            "late_risk_score": late_risk_score(decision),
            "fresh_momentum_score": fresh_momentum_score(decision),
            "exhaustion_score": exhaustion_score(decision),
            "reversal_probability": reversal_probability(decision),
            "continuation_probability": continuation_probability(decision),
            "trap_risk_score": trap_risk_score(decision),
            "profit_quality": profit_quality(decision),
            "relative_profit_quality": relative_profit_quality(decision),
            "rr_quality": rr_quality(decision),
            "learning_quality": learning_quality(decision),
            "timing_quality": timing_quality(decision),
            "safety_quality": safety_quality(decision),
        },
    })

    reasons = list(getattr(decision, "reason_codes", []) or [])
    if reason_codes:
        reasons.extend(reason_codes)
    reasons = _dedupe(reasons)

    return replace(decision, mode=new_mode, reason_codes=reasons, metadata=metadata)


# =============================================================================
# Public selection API
# =============================================================================

def select_best_real_candidates(
    decisions: Sequence[AIDecision],
    *,
    max_real: Optional[int] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> list[AIDecision]:
    """Keep only the best REAL candidates and downgrade other REALs to GHOST.

    The chosen REAL is not simply the highest AI score. It must be eligible and
    rank well on expected net profit, RR, timing/freshness, low late risk,
    low reversal risk, low trap risk, and confidence.
    """
    cfg = _cfg(config)
    allowed_real = max(0, safe_int(max_real, _cfg_int(cfg, "max_real_candidates", 1)) or 0)

    original = list(decisions or [])
    if not original:
        return []

    ranked: list[tuple[float, int, AIDecision, list[str]]] = []
    for idx, decision in enumerate(original):
        rank = selector_rank_score(decision, cfg)
        blocks = real_eligibility_blocks(decision, cfg)
        ranked.append((rank, idx, decision, blocks))

    eligible = [(rank, idx, decision) for rank, idx, decision, blocks in ranked if not blocks]
    eligible.sort(
        key=lambda item: (
            item[0],
            _num(item[2].score, 0.0),
            _num(item[2].confidence, 0.0),
            relative_profit_quality(item[2]),
            learning_quality(item[2]),
            expected_net_profit(item[2]),
        ),
        reverse=True,
    )

    selected_ids: set[int] = set()
    for _, idx, _ in eligible[:allowed_real]:
        selected_ids.add(idx)

    out: list[AIDecision] = []
    for rank, idx, decision, blocks in ranked:
        if idx in selected_ids:
            out.append(_with_selector_metadata(
                decision,
                mode=MODE_REAL,
                reason_codes=["SELECTOR_SELECTED_BEST_REAL"],
                selected=True,
                selector_rank=rank,
                selector_reason="BEST_REAL_BY_SCORE_PROFIT_AND_SAFETY",
            ))
            continue

        if decision.mode == MODE_REAL:
            # Candidate was REAL alone, but lost the cross-candidate selection or failed
            # selector eligibility. Keep it as GHOST for learning instead of executing.
            reasons = blocks or ["SELECTOR_BETTER_REAL_EXISTS"]
            out.append(_with_selector_metadata(
                decision,
                mode=MODE_GHOST,
                reason_codes=["SELECTOR_DOWNGRADED_REAL_TO_GHOST", *reasons],
                selected=False,
                selector_rank=rank,
                selector_reason="NOT_SELECTED_FOR_REAL",
            ))
        else:
            out.append(_with_selector_metadata(
                decision,
                mode=decision.mode,
                reason_codes=["SELECTOR_NOT_REAL_CANDIDATE"] if decision.mode != MODE_REJECT else ["SELECTOR_REJECT_PRESERVED"],
                selected=False,
                selector_rank=rank,
                selector_reason="MODE_PRESERVED",
            ))

    # Return in execution priority order: selected REAL first, then best ghosts, then rejects.
    out.sort(
        key=lambda d: (
            2 if d.mode == MODE_REAL else 1 if d.mode == MODE_GHOST else 0,
            _num(_decision_metadata(d).get("selector_rank_score"), 0.0),
            _num(d.score, 0.0),
            _num(d.confidence, 0.0),
        ),
        reverse=True,
    )
    return out


def select_single_best_real(decisions: Sequence[AIDecision], *, config: Optional[Mapping[str, Any]] = None) -> Optional[AIDecision]:
    """Return the single selected REAL candidate, if any."""
    selected = select_best_real_candidates(decisions, max_real=1, config=config)
    for decision in selected:
        if decision.mode == MODE_REAL and bool(_decision_metadata(decision).get("selector_selected_for_real")):
            return decision
    return None


def summarize_selection(decisions: Sequence[AIDecision]) -> dict[str, Any]:
    """Return lightweight summary for logs/tests/UI callers."""
    items = list(decisions or [])
    return {
        "system_version": SYSTEM_VERSION,
        "selector_version": CANDIDATE_SELECTOR_VERSION,
        "created_at": utc_now_iso(),
        "total": len(items),
        "real": sum(1 for d in items if d.mode == MODE_REAL),
        "ghost": sum(1 for d in items if d.mode == MODE_GHOST),
        "reject": sum(1 for d in items if d.mode == MODE_REJECT),
        "selected_real": [
            {
                "symbol": normalize_symbol(d.symbol),
                "direction": normalize_direction(d.direction),
                "rank": _num(_decision_metadata(d).get("selector_rank_score"), 0.0),
                "score": _num(d.score, 0.0),
                "confidence": _num(d.confidence, 0.0),
                "expected_net_profit": expected_net_profit(d),
                "expected_move_percent": expected_move_percent(d),
                "profit_percent": profit_percent(d),
                "rr": reward_risk(d),
                "learning_quality": learning_quality(d),
            }
            for d in items
            if d.mode == MODE_REAL and bool(_decision_metadata(d).get("selector_selected_for_real"))
        ],
    }


def validate_selection(decisions: Sequence[AIDecision], *, max_real: int = 1) -> dict[str, Any]:
    """Validate selector output."""
    errors: list[str] = []
    items = list(decisions or [])
    real_count = sum(1 for d in items if d.mode == MODE_REAL)
    if real_count > max(0, safe_int(max_real, 1) or 1):
        errors.append("TOO_MANY_REAL_SELECTED")
    for decision in items:
        if not normalize_symbol(decision.symbol):
            errors.append("MISSING_SYMBOL")
        if normalize_direction(decision.direction) not in {"LONG", "SHORT"}:
            errors.append("INVALID_DIRECTION")
        if decision.mode not in {MODE_REAL, MODE_GHOST, MODE_REJECT}:
            errors.append("INVALID_MODE")
        if not (0.0 <= _num(decision.score, -1.0) <= 100.0):
            errors.append("INVALID_SCORE")
        if not (0.0 <= _num(decision.confidence, -1.0) <= 100.0):
            errors.append("INVALID_CONFIDENCE")
    return {
        "valid": not errors,
        "errors": _dedupe(errors),
        **summarize_selection(items),
    }


__all__ = [
    "CANDIDATE_SELECTOR_VERSION",
    "DEFAULT_SELECTOR_CONFIG",
    "expected_net_profit",
    "entry_price",
    "tp1_price",
    "expected_move_percent",
    "profit_percent",
    "reward_risk",
    "timing_score",
    "late_risk_score",
    "fresh_momentum_score",
    "exhaustion_score",
    "reversal_probability",
    "continuation_probability",
    "trap_risk_score",
    "profit_quality",
    "relative_profit_quality",
    "learning_quality",
    "rr_quality",
    "timing_quality",
    "safety_quality",
    "selector_rank_score",
    "real_eligibility_blocks",
    "select_best_real_candidates",
    "select_single_best_real",
    "summarize_selection",
    "validate_selection",
]
