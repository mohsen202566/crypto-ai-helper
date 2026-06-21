from __future__ import annotations

"""
AI Movement Hunter.

This is the top decision layer of the bot.

Important:
- Classic indicators are only raw sensors.
- This module does not fetch candles directly.
- This module does not send Telegram messages.
- This module does not place trades.
- It decides REAL / GHOST / WAIT / REJECT. SETUP/ENTRY_ACTIVATION are kept only as legacy constants for compatibility and are not produced by this module.
- It prepares smart TP/SL suggestions, confidence, module scores, and metadata.
- It records decisions into ai_memory when requested.

Expected input:
candidate = {
    "symbol": "BTCUSDT",
    "price": 100.0,
    "features": multi_timeframe_features output from analysis.py,
    "structure": multi_timeframe_structure output from market_structure.py,
    "market_context": context from market_sentiment.py,
    "existing_setup_id": optional,
    "slot_state": {"free_slots": 1, "max_positions": 10, ...},
}

Output:
{
    "decision": "REAL|GHOST|SETUP|WAIT|ENTRY_ACTIVATION|REJECT",
    "direction": "LONG|SHORT|NEUTRAL",
    "confidence": 0..1,
    "priority": 0..1,
    "entry": price,
    "tp1": price,
    "tp2": price,
    "sl": price,
    "reason": short internal reason,
    "modules": {...},
    "metadata": {...}
}
"""

import math
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

from diagnostics import safe, record_error
from config import ISOLATED_MARGIN_ONLY
import ai_memory
import coin_learning
import coin_risk
import coin_rotation
import sr_learning
import trend_analysis


DECISION_REAL = "REAL"
DECISION_GHOST = "GHOST"
DECISION_SETUP = "SETUP"
DECISION_WAIT = "WAIT"
DECISION_ENTRY = "ENTRY_ACTIVATION"
DECISION_REJECT = "REJECT"

LONG = "LONG"
SHORT = "SHORT"
NEUTRAL = "NEUTRAL"


def _ts() -> int:
    return int(time.time())


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _get_tf(features: Dict[str, Any], tf: str) -> Dict[str, Any]:
    tfs = features.get("timeframes", features or {})
    return tfs.get(tf) or tfs.get(tf.lower()) or tfs.get(tf.upper()) or {}


def _ind(tf_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return tf_snapshot.get("indicators", tf_snapshot or {})


def _struct_tf(structure: Dict[str, Any], tf: str) -> Dict[str, Any]:
    tfs = structure.get("timeframes", structure or {})
    snap = tfs.get(tf) or tfs.get(tf.lower()) or tfs.get(tf.upper()) or {}
    return snap.get("structure", snap or {})


def _direction_sign(direction: str) -> int:
    if direction == LONG:
        return 1
    if direction == SHORT:
        return -1
    return 0


def _opposite(direction: str) -> str:
    return SHORT if direction == LONG else LONG if direction == SHORT else NEUTRAL


@dataclass
class ModuleScore:
    score: float
    weight: float
    reason: str = ""
    risk: str = ""

    def weighted(self) -> float:
        return self.score * self.weight

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AIDecision:
    decision: str
    symbol: str
    direction: str
    confidence: float
    priority: float
    entry: float
    tp1: float
    tp2: float
    sl: float
    reason: str
    modules: Dict[str, Any]
    metadata: Dict[str, Any]
    record_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# -----------------------------
# Core direction and module scoring
# -----------------------------

@safe(default={})
def decide(candidate: Dict[str, Any], record: bool = False) -> Dict[str, Any]:
    symbol = str(candidate.get("symbol", "")).upper()
    features = candidate.get("features", {}) or {}
    structure = candidate.get("structure", {}) or {}
    market_context = candidate.get("market_context", {}) or {}
    slot_state = candidate.get("slot_state", {}) or {}

    price = _safe_float(candidate.get("price") or _latest_price(features))
    if not symbol or price <= 0:
        return _reject(symbol, "NEUTRAL", "missing_symbol_or_price", candidate)

    trend = trend_analysis.analyze_trend(features)
    direction = _select_direction(features, trend, market_context)
    if direction == NEUTRAL:
        return _reject(symbol, direction, "direction_neutral", candidate)

    modules = _score_modules(symbol, direction, price, features, structure, market_context, candidate)
    confidence = _confidence_from_modules(modules)
    priority = _priority_from_modules(modules, confidence, slot_state)

    tp_sl = smart_tp_sl(symbol, direction, price, features, structure, market_context, confidence)

    decision, reason = _final_decision(symbol, direction, confidence, priority, modules, slot_state, candidate)

    metadata = _build_metadata(symbol, direction, features, structure, market_context, trend, candidate, tp_sl)
    out = AIDecision(
        decision=decision,
        symbol=symbol,
        direction=direction,
        confidence=round(confidence, 4),
        priority=round(priority, 4),
        entry=round(price, 8),
        tp1=tp_sl["tp1"],
        tp2=tp_sl["tp2"],
        sl=tp_sl["sl"],
        reason=reason,
        modules={k: v.to_dict() if isinstance(v, ModuleScore) else v for k, v in modules.items()},
        metadata=metadata,
    )

    result = out.to_dict()
    if record and decision in {DECISION_SETUP, DECISION_REAL, DECISION_GHOST, DECISION_ENTRY}:
        rid = ai_memory.create_record(
            symbol=symbol,
            direction=direction,
            decision=decision if decision != DECISION_ENTRY else DECISION_REAL,
            setup_snapshot=metadata,
            entry_price=price,
            tp1=tp_sl["tp1"],
            tp2=tp_sl["tp2"],
            sl=tp_sl["sl"],
            ai_confidence=confidence,
            ai_reason=reason,
            modules=result["modules"],
            telegram_message_id=candidate.get("telegram_message_id"),
            reply_chat_id=candidate.get("reply_chat_id"),
            record_id=candidate.get("record_id"),
        )
        result["record_id"] = rid
    return result


def _latest_price(features: Dict[str, Any]) -> float:
    for tf in ["5m", "5M", "15m", "15M"]:
        snap = _get_tf(features, tf)
        ind = _ind(snap)
        if ind.get("close"):
            return _safe_float(ind.get("close"))
    return 0.0


def _select_direction(features: Dict[str, Any], trend: Dict[str, Any], market_context: Dict[str, Any]) -> str:
    scores = {LONG: 0.0, SHORT: 0.0}
    tdir = str(trend.get("direction", NEUTRAL)).upper()
    tscore = _safe_float(trend.get("trend_score"))
    if tdir == LONG:
        scores[LONG] += abs(tscore) * 0.9
    elif tdir == SHORT:
        scores[SHORT] += abs(tscore) * 0.9

    for tf, weight in [("5m", 1.20), ("15m", 0.90), ("30m", 0.55), ("1h", 0.35), ("4h", 0.20), ("5M", 1.20), ("15M", 0.90), ("30M", 0.55), ("1H", 0.35), ("4H", 0.20)]:
        snap = _get_tf(features, tf)
        if not snap:
            continue
        ind = _ind(snap)
        hint = str(ind.get("direction_hint", NEUTRAL)).upper()
        if hint == LONG:
            scores[LONG] += weight
        elif hint == SHORT:
            scores[SHORT] += weight

        fm = _safe_float(ind.get("fresh_momentum"))
        p2 = _safe_float(ind.get("power_2"))
        hist = _safe_float(ind.get("macd_hist"))
        micro = fm + p2 / 100 + (1 if hist > 0 else -1 if hist < 0 else 0) * 0.15
        if micro > 0.18:
            scores[LONG] += weight * min(0.5, micro)
        elif micro < -0.18:
            scores[SHORT] += weight * min(0.5, abs(micro))

    market_mode = str(market_context.get("market_mode", "RANGE")).upper()
    btc_bias = str(market_context.get("btc_bias", "NEUTRAL")).upper()
    if market_mode == "BULLISH":
        scores[LONG] += 0.25
    elif market_mode == "BEARISH":
        scores[SHORT] += 0.25
    if "BULLISH" in btc_bias:
        scores[LONG] += 0.20
    elif "BEARISH" in btc_bias:
        scores[SHORT] += 0.20

    diff = scores[LONG] - scores[SHORT]
    if diff > 0.35:
        return LONG
    if diff < -0.35:
        return SHORT
    return NEUTRAL


def _score_modules(symbol: str, direction: str, price: float, features: Dict[str, Any], structure: Dict[str, Any], market_context: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, ModuleScore]:
    weights = ai_memory.get_adaptive_weights()
    modules: Dict[str, ModuleScore] = {}

    modules["fresh_momentum"] = _score_fresh_momentum(direction, features, weights.get("fresh_momentum", 1.0))
    modules["entry_quality"] = _score_entry_quality(direction, features, weights.get("entry_quality", 1.0))
    modules["trend_context"] = _score_trend_context(direction, features, weights.get("trend_context", 1.0))
    modules["trap_filter"] = _score_trap(direction, structure, weights.get("trap_filter", 1.0))
    modules["liquidity_filter"] = _score_liquidity(direction, structure, weights.get("liquidity_filter", 1.0))
    modules["reversal_filter"] = _score_reversal(direction, structure, weights.get("reversal_filter", 1.0))
    modules["btc_leader"] = _score_btc_leader(direction, market_context, weights.get("btc_leader", 1.0))
    modules["confidence_boundary"] = _score_confidence_boundary(features, structure, market_context, weights.get("confidence_boundary", 1.0))

    # Learning modules
    learning = coin_learning.guidance_for_snapshot(symbol, direction, _merge_snapshot(features, structure, market_context))
    modules["coin_behavior"] = ModuleScore(_clamp(0.5 + _safe_float(learning.get("soft_score")), 0, 1), weights.get("coin_behavior", 1.0), "coin_learning", ",".join(learning.get("risk_notes", [])[:3]))

    risk = coin_risk.evaluate(symbol, direction, _merge_snapshot(features, structure, market_context))
    modules["coin_risk"] = ModuleScore(_clamp(1.0 - _safe_float(risk.get("risk_score")), 0, 1), 1.0, "coin_risk", ",".join(risk.get("warnings", [])[:3]))

    st5 = _struct_tf(structure, "5m") or _struct_tf(structure, "5M")
    sr = sr_learning.guidance(symbol, direction, st5)
    modules["sr_behavior"] = ModuleScore(_clamp(0.5 + _safe_float(sr.get("soft_score")), 0, 1), weights.get("sr_behavior", 1.0), "sr_learning", ",".join(sr.get("risks", [])[:3]))

    state_profile = ai_memory.state_profile_from_snapshot(_merge_snapshot(features, structure, market_context), symbol, direction)
    modules["state_memory"] = _score_state_memory(state_profile, weights.get("state_memory", 1.0))

    modules["time_risk"] = _score_time_risk(symbol, direction, weights.get("time_risk", 1.0))
    modules["correlation_exposure"] = _score_correlation_exposure(symbol, candidate.get("slot_state", {}), weights.get("correlation_exposure", 1.0))

    return modules


def _score_fresh_momentum(direction: str, features: Dict[str, Any], weight: float) -> ModuleScore:
    vals = []
    for tf, mult in [("5m", 1.0), ("5M", 1.0), ("15m", 0.75), ("15M", 0.75)]:
        ind = _ind(_get_tf(features, tf))
        if not ind:
            continue
        fm = _safe_float(ind.get("fresh_momentum"))
        em = _safe_float(ind.get("early_momentum"))
        p2 = _safe_float(ind.get("power_2")) / 100
        signed = (fm * 0.45 + em * 0.35 + p2 * 0.20) * _direction_sign(direction)
        vals.append(signed * mult)
    raw = sum(vals) / max(1, len(vals))
    score = _clamp(0.5 + raw * 0.75)
    return ModuleScore(score, weight, "fresh_momentum")


def _score_entry_quality(direction: str, features: Dict[str, Any], weight: float) -> ModuleScore:
    readiness = trend_analysis.entry_readiness(features, direction)
    base = _safe_float(readiness.get("entry_readiness"))
    ind5 = _ind(_get_tf(features, "5m") or _get_tf(features, "5M"))
    candle_q = _safe_float(ind5.get("candle_quality"))
    adx = _safe_float(ind5.get("adx"))
    adx_bonus = 0.10 if adx >= 20 else -0.10
    score = _clamp(0.5 + base * 0.5 + (candle_q - 0.5) * 0.25 + adx_bonus)
    risk = ",".join(readiness.get("risks", [])[:3])
    return ModuleScore(score, weight, "entry_readiness", risk)


def _score_trend_context(direction: str, features: Dict[str, Any], weight: float) -> ModuleScore:
    trend = trend_analysis.analyze_trend(features)
    tscore = _safe_float(trend.get("trend_score"))
    aligned = tscore * _direction_sign(direction)
    conflict = _safe_float(trend.get("conflict"))
    score = _clamp(0.5 + aligned * 0.55 - conflict * 0.25)
    return ModuleScore(score, weight, "multi_tf_trend", "conflict" if conflict > 0.35 else "")


def _score_trap(direction: str, structure: Dict[str, Any], weight: float) -> ModuleScore:
    risks = []
    vals = []
    for tf, mult in [("5m", 1.0), ("5M", 1.0), ("15m", 0.75), ("15M", 0.75)]:
        st = _struct_tf(structure, tf)
        if not st:
            continue
        trap = _safe_float(st.get("trap_risk"))
        fake = _safe_float(st.get("fake_breakout_risk"))
        vals.append(max(trap, fake) * mult)
        if max(trap, fake) > 0.65:
            risks.append(f"{tf}_trap")
    risk = sum(vals) / max(1, len(vals))
    return ModuleScore(_clamp(1 - risk), weight, "trap_detection", ",".join(risks[:3]))


def _score_liquidity(direction: str, structure: Dict[str, Any], weight: float) -> ModuleScore:
    vals = []
    for tf, mult in [("5m", 1.0), ("15m", 0.75), ("5M", 1.0), ("15M", 0.75)]:
        st = _struct_tf(structure, tf)
        if st:
            vals.append(_safe_float(st.get("liquidity_risk")) * mult)
    risk = sum(vals) / max(1, len(vals))
    return ModuleScore(_clamp(1 - risk * 0.85), weight, "liquidity_risk")


def _score_reversal(direction: str, structure: Dict[str, Any], weight: float) -> ModuleScore:
    vals = []
    reasons = []
    for tf, mult in [("5m", 1.0), ("15m", 0.80), ("30m", 0.55), ("5M", 1.0), ("15M", 0.80), ("30M", 0.55)]:
        st = _struct_tf(structure, tf)
        if not st:
            continue
        rev = _safe_float(st.get("reversal_probability"))
        phase = str(st.get("movement_phase", ""))
        if phase == "EXHAUSTION":
            rev = max(rev, 0.75)
            reasons.append(f"{tf}_exhaustion")
        vals.append(rev * mult)
    risk = sum(vals) / max(1, len(vals))
    return ModuleScore(_clamp(1 - risk), weight, "reversal_probability", ",".join(reasons[:3]))


def _score_btc_leader(direction: str, market_context: Dict[str, Any], weight: float) -> ModuleScore:
    btc_score = _safe_float(market_context.get("btc_score"))
    leader = _safe_float(market_context.get("leader_influence"))
    market_score = _safe_float(market_context.get("market_score"))
    aligned = (btc_score * 0.45 + leader * 0.35 + market_score * 0.20) * _direction_sign(direction)
    score = _clamp(0.5 + aligned * 0.65)
    return ModuleScore(score, weight, "btc_leader_market")


def _score_confidence_boundary(features: Dict[str, Any], structure: Dict[str, Any], market_context: Dict[str, Any], weight: float) -> ModuleScore:
    """
    Known vs unknown market state.
    Lower score means AI should prefer GHOST/WAIT instead of REAL.
    """
    missing = 0
    total = 0
    for tf in ["5m", "15m", "30m", "1h", "4h", "5M", "15M", "30M", "1H", "4H"]:
        snap = _get_tf(features, tf)
        if snap:
            total += 1
            if not snap.get("ok", True):
                missing += 1
    total = max(1, total)
    data_quality = 1 - missing / total

    market_known = 0.0 if market_context.get("market_mode") in {None, "", "UNKNOWN"} else 1.0
    st5 = _struct_tf(structure, "5m") or _struct_tf(structure, "5M")
    phase_known = 0.0 if st5.get("movement_phase") in {None, "", "UNKNOWN"} else 1.0
    score = data_quality * 0.55 + market_known * 0.20 + phase_known * 0.25
    return ModuleScore(_clamp(score), weight, "confidence_boundary", "unknown_state" if score < 0.6 else "")


def _score_state_memory(state_profile: Dict[str, Any], weight: float) -> ModuleScore:
    prof = state_profile.get("profile", {}) if isinstance(state_profile, dict) else {}
    samples = int(prof.get("samples", 0) or 0)
    if samples < 3:
        return ModuleScore(0.5, weight, "state_memory_insufficient")
    tp = int(prof.get("tp", 0) or 0)
    sl = int(prof.get("sl", 0) or 0)
    wr = tp / max(1, tp + sl)
    return ModuleScore(_clamp(wr), weight, "state_memory")


def _score_time_risk(symbol: str, direction: str, weight: float) -> ModuleScore:
    # Detailed time memory is stored inside ai_memory; public access is intentionally compact.
    # Return neutral now. Later can query a public ai_memory time profile.
    return ModuleScore(0.5, weight, "time_risk_neutral")


def _score_correlation_exposure(symbol: str, slot_state: Dict[str, Any], weight: float) -> ModuleScore:
    open_positions = slot_state.get("open_positions", []) if isinstance(slot_state, dict) else []
    ranked = coin_rotation.rank_candidates([{"symbol": symbol, "direction": LONG, "priority": 0.5}], open_positions=open_positions, limit=1)
    penalty = _safe_float(ranked[0].get("exposure_penalty")) if ranked else 0.0
    score = _clamp(1 - penalty / 30)
    return ModuleScore(score, weight, "correlation_exposure", "exposure" if penalty > 0 else "")


def _confidence_from_modules(modules: Dict[str, ModuleScore]) -> float:
    total_w = 0.0
    total = 0.0
    for name, m in modules.items():
        if not isinstance(m, ModuleScore):
            continue
        w = max(0.01, _safe_float(m.weight, 1.0))
        total_w += w
        total += _safe_float(m.score) * w
    return _clamp(total / max(0.01, total_w))


def _priority_from_modules(modules: Dict[str, ModuleScore], confidence: float, slot_state: Dict[str, Any]) -> float:
    momentum = _safe_float(modules.get("fresh_momentum", ModuleScore(0.5,1)).score)
    entry = _safe_float(modules.get("entry_quality", ModuleScore(0.5,1)).score)
    trap = _safe_float(modules.get("trap_filter", ModuleScore(0.5,1)).score)
    priority = confidence * 0.50 + momentum * 0.25 + entry * 0.20 + trap * 0.05
    return _clamp(priority)


# -----------------------------
# Decision and TP/SL
# -----------------------------

def _final_decision(symbol: str, direction: str, confidence: float, priority: float, modules: Dict[str, ModuleScore], slot_state: Dict[str, Any], candidate: Dict[str, Any]) -> Tuple[str, str]:
    free_slots = int(slot_state.get("free_slots", 0) or 0) if isinstance(slot_state, dict) else 0
    ai_enabled = bool(candidate.get("ai_enabled", True))
    learning_only = bool(candidate.get("learning_only", False))
    existing_setup_id = candidate.get("existing_setup_id")

    if not ai_enabled:
        return DECISION_REJECT, "ai_disabled"

    hard_risks = []
    for name in ["trap_filter", "liquidity_filter", "reversal_filter", "confidence_boundary"]:
        m = modules.get(name)
        if isinstance(m, ModuleScore) and m.score < 0.25:
            hard_risks.append(name)

    if len(hard_risks) >= 2 and confidence < 0.62:
        return DECISION_REJECT, "multiple_high_risks:" + ",".join(hard_risks)

    entry_score = modules.get("entry_quality", ModuleScore(0.5, 1)).score
    momentum = modules.get("fresh_momentum", ModuleScore(0.5, 1)).score

    # Final architecture: no SETUP / no waiting activation.
    # AI Movement Hunter must directly route candidates to REAL, GHOST, WAIT, or REJECT.
    # REAL = strong enough now and slot available.
    # GHOST = good/learnable but not safe enough for real, slot is full, learning-only mode, or uncertainty is high.
    if existing_setup_id and confidence >= 0.58 and entry_score >= 0.56 and momentum >= 0.55:
        if free_slots > 0 and confidence >= 0.64 and not learning_only:
            return DECISION_REAL, "legacy_setup_converted_to_real"
        return DECISION_GHOST, "legacy_setup_converted_to_ghost"

    strong_now = confidence >= 0.64 and entry_score >= 0.55 and momentum >= 0.54
    decent_learnable = confidence >= 0.54 and (entry_score >= 0.52 or momentum >= 0.52)

    if strong_now:
        if free_slots > 0 and not learning_only:
            return DECISION_REAL, "real_allowed_ai_movement_ready"
        return DECISION_GHOST, "ghost_slot_full_or_learning"

    if decent_learnable:
        return DECISION_GHOST, "ghost_ai_not_strong_enough_for_real"

    if confidence >= 0.48:
        return DECISION_WAIT, "wait_low_edge"

    return DECISION_REJECT, "low_confidence"


@safe(default={})
def smart_tp_sl(symbol: str, direction: str, entry: float, features: Dict[str, Any], structure: Dict[str, Any], market_context: Dict[str, Any], confidence: float = 0.5) -> Dict[str, Any]:
    ind5 = _ind(_get_tf(features, "5m") or _get_tf(features, "5M"))
    atr = _safe_float(ind5.get("atr"))
    if atr <= 0:
        atr = max(entry * 0.0035, 1e-8)

    mem = ai_memory.recommend_tp_sl_context(symbol, direction)
    avg_reach = _safe_float(mem.get("avg_reachable_profit_pct"))
    median_profit = _safe_float(mem.get("median_profit_pct"))

    # Scalp default: TP1 close, TP2 moderate, SL controlled.
    tp1_atr = 0.85
    tp2_atr = 1.55
    sl_atr = 1.05

    if confidence > 0.78:
        tp1_atr += 0.10
        tp2_atr += 0.20
    if confidence < 0.62:
        tp1_atr -= 0.10
        tp2_atr -= 0.20
        sl_atr += 0.05

    if avg_reach > 0:
        # If learned reachable movement is smaller, bring TP closer softly.
        learned_move = entry * (avg_reach / 100)
        if learned_move > 0:
            tp1_atr = max(0.55, min(tp1_atr, learned_move / atr * 0.85))
            tp2_atr = max(tp1_atr + 0.25, min(tp2_atr, learned_move / atr * 1.25))

    st5 = _struct_tf(structure, "5m") or _struct_tf(structure, "5M")
    reversal = _safe_float(st5.get("reversal_probability"))
    trap = _safe_float(st5.get("trap_risk"))
    if reversal > 0.65 or trap > 0.65:
        tp1_atr *= 0.85
        tp2_atr *= 0.80
        sl_atr *= 0.95

    sign = _direction_sign(direction)
    tp1 = entry + sign * atr * tp1_atr
    tp2 = entry + sign * atr * tp2_atr
    sl = entry - sign * atr * sl_atr

    return {
        "entry": round(entry, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "sl": round(sl, 8),
        "atr": round(atr, 8),
        "tp1_atr": round(tp1_atr, 4),
        "tp2_atr": round(tp2_atr, 4),
        "sl_atr": round(sl_atr, 4),
        "source": "ai_smart_scalp_memory",
    }


# -----------------------------
# Metadata and batch selection
# -----------------------------

def _merge_snapshot(features: Dict[str, Any], structure: Dict[str, Any], market_context: Dict[str, Any]) -> Dict[str, Any]:
    ind5 = _ind(_get_tf(features, "5m") or _get_tf(features, "5M"))
    st5 = _struct_tf(structure, "5m") or _struct_tf(structure, "5M")
    return {
        "indicators": ind5,
        "structure": st5,
        "context": market_context,
        "timeframes": features.get("timeframes", {}),
        "structure_timeframes": structure.get("timeframes", {}),
    }


def _build_metadata(symbol: str, direction: str, features: Dict[str, Any], structure: Dict[str, Any], market_context: Dict[str, Any], trend: Dict[str, Any], candidate: Dict[str, Any], tp_sl: Dict[str, Any]) -> Dict[str, Any]:
    merged = _merge_snapshot(features, structure, market_context)
    merged.update({
        "symbol": symbol,
        "direction": direction,
        "trend": trend,
        "tp_sl": tp_sl,
        "slot_state": candidate.get("slot_state", {}),
        "created_at": _ts(),
        "ai_architecture": "AI_MOVEMENT_HUNTER",
        "market_maker_layer": "NOT_USED",
    })
    return merged


def _reject(symbol: str, direction: str, reason: str, candidate: Dict[str, Any]) -> Dict[str, Any]:
    return AIDecision(
        decision=DECISION_REJECT,
        symbol=str(symbol).upper(),
        direction=direction,
        confidence=0.0,
        priority=0.0,
        entry=_safe_float(candidate.get("price")),
        tp1=0.0,
        tp2=0.0,
        sl=0.0,
        reason=reason,
        modules={},
        metadata={"candidate": candidate, "created_at": _ts()},
    ).to_dict()


@safe(default=[])
def rank_decisions(decisions: List[Dict[str, Any]], open_positions: Optional[List[Dict[str, Any]]] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    candidates = []
    for d in decisions:
        if d.get("decision") in {DECISION_REAL, DECISION_GHOST, DECISION_ENTRY, DECISION_SETUP}:
            candidates.append({
                "symbol": d.get("symbol"),
                "direction": d.get("direction"),
                "priority": d.get("priority", d.get("confidence", 0)),
                "_decision": d,
            })
    ranked = coin_rotation.rank_candidates(candidates, open_positions=open_positions or [], limit=limit)
    return [r["_decision"] | {"final_priority": r.get("final_priority"), "correlation_group": r.get("correlation_group"), "exposure_penalty": r.get("exposure_penalty")} for r in ranked]


@safe(default={})
def choose_best_candidate(candidates: List[Dict[str, Any]], slot_state: Optional[Dict[str, Any]] = None, record: bool = False) -> Dict[str, Any]:
    slot_state = slot_state or {}
    decisions = []
    for c in candidates:
        cc = dict(c)
        cc.setdefault("slot_state", slot_state)
        decisions.append(decide(cc, record=record))
    ranked = rank_decisions(decisions, open_positions=slot_state.get("open_positions", []), limit=1)
    if ranked:
        return ranked[0]
    return {"decision": DECISION_WAIT, "reason": "no_rankable_candidate"}


@safe(default="")
def format_decision_fa(decision: Dict[str, Any]) -> str:
    """
    Short Persian internal-friendly summary. Final Telegram formatting can be done in bot.py.
    """
    d = decision.get("decision", "")
    symbol = decision.get("symbol", "")
    direction = "لانگ" if decision.get("direction") == LONG else "شورت" if decision.get("direction") == SHORT else "خنثی"
    conf = round(_safe_float(decision.get("confidence")) * 100, 1)
    entry = decision.get("entry", 0)
    tp1 = decision.get("tp1", 0)
    tp2 = decision.get("tp2", 0)
    sl = decision.get("sl", 0)

    if d == DECISION_REJECT:
        return f"❌ رد شد\nارز: {symbol}\nدلیل: {decision.get('reason','')}"
    if d == DECISION_WAIT:
        return f"⏳ منتظر تایید ورود\nارز: {symbol}\nجهت: {direction}\nاعتماد: {conf}%"
    if d == DECISION_SETUP:
        return f"👻 سیگنال مخفی / ستاپ قدیمی حذف شد\nارز: {symbol}\nجهت: {direction}\nاعتماد: {conf}%"
    if d in {DECISION_REAL, DECISION_ENTRY}:
        return f"✅ سیگنال فعال\nارز: {symbol}\nجهت: {direction}\nورود: {entry}\nTP1: {tp1}\nTP2: {tp2}\nSL: {sl}\nاعتماد: {conf}%"
    if d == DECISION_GHOST:
        return f"👻 سیگنال مخفی\nارز: {symbol}\nجهت: {direction}\nورود فرضی: {entry}\nاعتماد: {conf}%"
    return f"{symbol} {direction} {conf}%"
