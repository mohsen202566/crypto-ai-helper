# -*- coding: utf-8 -*-
"""
scanner.py

AI Movement Hunter scanner for the crypto futures bot.

Purpose:
- Scan configured symbols using analysis.analyze_symbol.
- analysis.py is now treated as an AI Movement Hunter result provider:
    classic technical indicators are sensor data only.
    classic score/signal authority is disabled.
- Scanner never creates a classic signal and never approves by EMA/MACD/ADX score.
- Scanner only accepts AI-approved movement entries:
    AI direction, movement freshness, move phase, trap/liquidity, risk, learning,
    rotation, and slot context decide REAL / GHOST / REJECT.
- Preserve all public function names used by bot.py and real-trade modules.
- Save unused/rejected movement candidates as Ghost signals when safe.

This keeps the public function names used by bot.py:
    scan_market
    scan_for_auto_signals
    get_best_signal
    get_top_signals
    scan_market_overview
    scan_symbols_for_signals
    find_best_signal
    find_top_signals
"""

import time
import logging
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger("crypto-bot")

from analysis import analyze_symbol, add_indicators, get_klines, ema_direction

try:
    from config import SCAN_SYMBOLS, AUTO_DIRECT_SCORE_MIN
except Exception:
    SCAN_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    AUTO_DIRECT_SCORE_MIN = 82

try:
    from slot_manager import get_free_slots, is_symbol_direction_active, select_best_candidates
except Exception:
    get_free_slots = None
    is_symbol_direction_active = None
    select_best_candidates = None

try:
    from ghost_signals import create_ghost_signal
except Exception:
    create_ghost_signal = None

try:
    from coin_rotation import sort_symbols_by_rotation, get_symbol_rotation_score
except Exception:
    sort_symbols_by_rotation = None
    get_symbol_rotation_score = None

try:
    from coin_risk import get_direction_risk_state
except Exception:
    get_direction_risk_state = None

try:
    from coin_learning import should_require_extra_strength, get_smart_tp_suggestion, get_similarity_adjustment
except Exception:
    should_require_extra_strength = None
    get_smart_tp_suggestion = None
    get_similarity_adjustment = None

try:
    from sr_learning import get_liquidity_trap_profile, suggest_liquidity_aware_buffer
except Exception:
    get_liquidity_trap_profile = None
    suggest_liquidity_aware_buffer = None

try:
    from coin_learning import get_meta_layer_weights
except Exception:
    get_meta_layer_weights = None


SCAN_DELAY_SECONDS = 0.05
MAX_SCAN_RESULTS = 10
MIN_SCANNER_SCORE = int(AUTO_DIRECT_SCORE_MIN or 82)


def normalize_symbol(symbol):
    s = str(symbol).upper().strip()
    return s if s.endswith("USDT") else f"{s}USDT"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def get_scan_symbols():
    symbols = list(dict.fromkeys([normalize_symbol(x) for x in SCAN_SYMBOLS if str(x).strip()]))
    if sort_symbols_by_rotation:
        try:
            rotated = sort_symbols_by_rotation(symbols)
            if isinstance(rotated, list) and rotated:
                return rotated
        except Exception:
            pass
    return symbols


def _base_reject_reason(r: Dict[str, Any]) -> str:
    """Final real-trade safety gate for AI Movement Hunter results.

    Important:
    - scanner.py does NOT validate a classic technical score anymore.
    - score, when present, is treated as AI movement score from analysis.py.
    - this gate only protects the real-order path from incomplete/non-real data.
    """
    if not isinstance(r, dict):
        return "ANALYSIS_NOT_DICT"

    ai_decision = r.get("ai_decision") if isinstance(r.get("ai_decision"), dict) else {}
    hunter = r.get("ai_movement_hunter") if isinstance(r.get("ai_movement_hunter"), dict) else {}

    if r.get("status") != "ACTIVE":
        return f"AI_NOT_REAL:{r.get('status')}:{r.get('entry_mode')}"
    if not bool(r.get("entry_confirmed")):
        return "AI_ENTRY_NOT_CONFIRMED"
    if r.get("direction") not in ["LONG", "SHORT"]:
        return f"BAD_DIRECTION:{r.get('direction')}"

    # Only AI REAL decisions are allowed to reach real-trade selection.
    decision = str(ai_decision.get("decision") or hunter.get("decision") or "REAL").upper()
    if decision not in {"REAL", "ACTIVE", "ENTRY"}:
        return f"AI_DECISION_NOT_REAL:{decision}"

    if bool(ai_decision.get("classic_signal_disabled") is False):
        return "CLASSIC_SIGNAL_AUTHORITY_NOT_DISABLED"

    if r.get("entry") is None:
        return "NO_ENTRY"
    if r.get("stop_loss") is None:
        return "NO_STOP_LOSS"
    if r.get("tp1") is None:
        return "NO_TP1"
    return "OK"


def _base_valid_signal(r: Dict[str, Any]) -> bool:
    return _base_reject_reason(r) == "OK"


def _can_store_rejected_as_ghost(r: Dict[str, Any]) -> bool:
    """Store safe AI movement candidates as Ghost for learning only.

    Ghost storage is allowed only when TP/SL/entry are complete. It never opens
    real trades and never sends a real auto-signal.
    """
    if not isinstance(r, dict):
        return False

    direction = r.get("direction") if r.get("direction") in ["LONG", "SHORT"] else r.get("candidate_direction")
    if direction not in ["LONG", "SHORT"]:
        return False
    if r.get("entry") is None or r.get("stop_loss") is None or r.get("tp1") is None:
        return False

    ai_decision = r.get("ai_decision") if isinstance(r.get("ai_decision"), dict) else {}
    hunter = r.get("ai_movement_hunter") if isinstance(r.get("ai_movement_hunter"), dict) else {}
    decision = str(ai_decision.get("decision") or hunter.get("decision") or "").upper()
    if decision in {"GHOST", "SETUP", "WATCH"}:
        return True

    # score is AI movement score, not classic score.
    return _safe_int(r.get("score"), 0) >= max(60, MIN_SCANNER_SCORE - 18)


def _build_scanner_snapshot(r: Dict[str, Any]) -> Dict[str, Any]:
    snap = r.get("snapshot") if isinstance(r.get("snapshot"), dict) else {}
    out = dict(snap)
    for key in [
        "symbol", "direction", "entry", "price", "score", "long_score", "short_score",
        "risk_level", "risk_reward", "confirmations", "freshness", "rsi", "rsi_5m",
        "adx", "macd", "macd_signal", "macd_hist", "power2_buy", "power2_sell",
        "power3_buy", "power3_sell", "buy_power", "sell_power", "atr",
        "market_mode", "market_regime", "coin_behavior", "btc_bias",
        "support", "resistance", "vwap_status", "vwap_distance_pct", "entry_mode",
        "timeframe_core", "entry_timing_tf", "rsi_slope_15m", "adx_slope_15m",
        "macd_hist_accel_15m", "macd_hist_slope_15m", "prediction_score",
        "reversal_risk_score", "expected_move_pct", "trap_risk", "time_risk",
        "time_risk_score", "liquidity_risk_score", "relative_status", "move_state",
        "ai_decision", "ai_movement_hunter", "technical_sensors", "candidate_direction",
        "classic_signal_disabled", "classic_score_disabled",
    ]:
        if key not in out and r.get(key) is not None:
            out[key] = r.get(key)
    return out


def _get_rotation_score(symbol: str, direction: str = None, snapshot: Dict[str, Any] = None) -> float:
    if not get_symbol_rotation_score:
        return 50.0
    try:
        try:
            val = get_symbol_rotation_score(symbol, direction=direction, snapshot=snapshot)
        except TypeError:
            try:
                val = get_symbol_rotation_score(symbol, direction)
            except TypeError:
                val = get_symbol_rotation_score(symbol)
        if isinstance(val, dict):
            for k in ["score", "rotation_score", "priority", "rank_score"]:
                if val.get(k) is not None:
                    return _safe_float(val.get(k), 50.0)
            return 50.0
        return _safe_float(val, 50.0)
    except Exception:
        return 50.0


def _risk_adjustment(symbol: str, direction: str, snapshot: Dict[str, Any] = None) -> Tuple[Dict[str, Any], float, int, List[str]]:
    if not get_direction_risk_state:
        return {}, 0.0, 0, []
    try:
        try:
            state = get_direction_risk_state(symbol, direction, snapshot=snapshot)
        except TypeError:
            state = get_direction_risk_state(symbol, direction)
    except Exception:
        return {}, 0.0, 0, []

    strict = _safe_int(state.get("strictness_level"), 0)
    risk_score = _safe_float(state.get("risk_score"), 0.0)
    sl_count = _safe_int(state.get("sl_count"), 0)

    # Stronger final selection penalty. analysis.py already blocks weak entries;
    # scanner.py decides which approved signal deserves the real slot. Repeated
    # SLs and high risk must matter more than raw technical score here.
    penalty = min(30.0, strict * 5.0 + risk_score * 0.14 + max(0, sl_count - 1) * 3.0)
    extra_conf = min(4, max(0, strict))

    reasons = []
    if strict > 0:
        reasons.append(f"AI Risk strict={strict}")
    if sl_count >= 2:
        reasons.append(f"AI Risk SL count={sl_count}")
    if risk_score >= 40:
        reasons.append(f"AI Risk score={int(risk_score)}")
    if state.get("recommend_reduce"):
        penalty += 5.0
        extra_conf = min(4, extra_conf + 1)
        reasons.append("AI Risk recommend_reduce")

    return state if isinstance(state, dict) else {}, min(34.0, penalty), extra_conf, reasons


def _learning_adjustment(symbol: str, direction: str, snapshot: Dict[str, Any]) -> Tuple[Dict[str, Any], float, int, List[str]]:
    if not should_require_extra_strength:
        return {}, 0.0, 0, []
    try:
        extra = should_require_extra_strength(symbol, direction, snapshot=snapshot)
    except TypeError:
        try:
            extra = should_require_extra_strength(symbol, direction, snapshot)
        except Exception:
            extra = {}
    except Exception:
        extra = {}

    if not isinstance(extra, dict):
        extra = {}
    extra_score = _safe_float(extra.get("extra_score"), 0.0)
    extra_conf = _safe_int(extra.get("extra_confirmations"), 0)
    reasons = []
    if extra.get("required") or extra_score or extra_conf:
        if extra.get("reason"):
            reasons.append(str(extra.get("reason")))
        else:
            reasons.append("AI Learning extra strength")
    return extra, min(18.0, max(0.0, extra_score)), min(3, max(0, extra_conf)), reasons


def _movement_rank_value(r: Dict[str, Any]) -> float:
    """Rank by AI movement quality, not classic technical score.

    r['score'] is expected to be AI Movement Hunter score from analysis.py.
    """
    score = _safe_float(r.get("score"), 0.0)
    conf = _safe_float(r.get("confirmations"), 0.0)
    rr = _safe_float(r.get("risk_reward"), 0.0)

    ai = r.get("ai_decision") if isinstance(r.get("ai_decision"), dict) else {}
    hunter = r.get("ai_movement_hunter") if isinstance(r.get("ai_movement_hunter"), dict) else {}
    phase = str(ai.get("move_phase") or hunter.get("move_phase") or r.get("move_state") or "").upper()
    trap = str(ai.get("trap_risk") or hunter.get("trap_risk") or r.get("trap_risk") or "").upper()
    fresh_label = str(ai.get("move_freshness") or hunter.get("move_freshness") or r.get("freshness") or "").upper()

    phase_bonus = {
        "START": 10.0,
        "EARLY": 8.0,
        "ENTRY": 8.0,
        "SETUP": 2.0,
        "MID": -6.0,
        "EXHAUSTION": -30.0,
        "RANGE_AFTER_MOVE": -35.0,
        "RANGE": -20.0,
    }.get(phase, 0.0)
    trap_penalty = {"HIGH": 25.0, "MEDIUM": 7.0, "LOW": 0.0}.get(trap, 0.0)
    fresh_bonus = {"HIGH": 7.0, "MEDIUM": 2.0, "LOW": -8.0}.get(fresh_label, 0.0)
    risk_bonus = {"LOW": 4.0, "MEDIUM": 1.0, "HIGH": -5.0}.get(str(r.get("risk_level") or "").upper(), 0.0)

    return score + conf * 1.2 + rr * 2.0 + phase_bonus + fresh_bonus + risk_bonus - trap_penalty


# Backward-compatible alias for older helper references inside this file.
def _classic_rank_value(r: Dict[str, Any]) -> float:
    return _movement_rank_value(r)


def _extract_nested_number(snapshot: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    """Read a numeric value from snapshot or known nested AI layer dicts."""
    if not isinstance(snapshot, dict):
        return float(default)
    for k in keys:
        if snapshot.get(k) is not None:
            return _safe_float(snapshot.get(k), default)
    for nest in ("prediction_layer", "liquidity_trap", "state_awareness", "candle_behavior", "ai_layers"):
        obj = snapshot.get(nest)
        if isinstance(obj, dict):
            for k in keys:
                if obj.get(k) is not None:
                    return _safe_float(obj.get(k), default)
    return float(default)


def _prediction_adjustment(snapshot: Dict[str, Any]) -> Tuple[float, List[str]]:
    """Soft prediction bonus/penalty. It never hard-blocks; it only changes rank."""
    pred = _extract_nested_number(snapshot, "prediction_score", "early_momentum_score", default=50.0)
    reversal = _extract_nested_number(snapshot, "reversal_risk_score", "reversal_risk", default=50.0)
    expected = _extract_nested_number(snapshot, "expected_move_pct", "expected_move", "expected_move_atr", default=0.0)
    adx_slope = _extract_nested_number(snapshot, "adx_slope_15m", "adx_slope", default=0.0)
    rsi_slope = _extract_nested_number(snapshot, "rsi_slope_15m", "rsi_slope", default=0.0)
    macd_accel = _extract_nested_number(snapshot, "macd_hist_accel_15m", "macd_acceleration", default=0.0)

    adj = 0.0
    adj += max(-6.0, min(8.0, (pred - 50.0) / 7.0))
    adj -= max(-4.0, min(7.0, (reversal - 50.0) / 9.0))
    adj += max(0.0, min(4.0, expected * 1.2))
    adj += max(-2.0, min(2.0, adx_slope * 0.25))
    adj += max(-2.0, min(2.0, rsi_slope * 0.18))
    adj += max(-2.0, min(2.5, macd_accel * 500.0))

    reasons = []
    if pred >= 65:
        reasons.append(f"Prediction strong={round(pred,1)}")
    if reversal >= 65:
        reasons.append(f"Reversal risk={round(reversal,1)}")
    return round(max(-12.0, min(14.0, adj)), 4), reasons


def _liquidity_adjustment(symbol: str, direction: str, snapshot: Dict[str, Any]) -> Tuple[Dict[str, Any], float, List[str]]:
    """Soft liquidity/trap/stop-hunt rank adjustment from sr_learning and snapshot."""
    profile = {}
    if get_liquidity_trap_profile:
        try:
            price = snapshot.get("entry") or snapshot.get("price")
            profile = get_liquidity_trap_profile(symbol, direction=direction, price=price)
        except Exception:
            profile = {}

    snap_trap = str(snapshot.get("trap_risk") or ((snapshot.get("liquidity_trap") or {}).get("trap_risk") if isinstance(snapshot.get("liquidity_trap"), dict) else "") or "").upper()
    risk_score = _safe_float(profile.get("trap_risk_score"), 0.0) if isinstance(profile, dict) else 0.0
    if snap_trap == "HIGH":
        risk_score = max(risk_score, 70.0)
    elif snap_trap == "MEDIUM":
        risk_score = max(risk_score, 40.0)

    penalty = max(0.0, min(10.0, risk_score / 10.0))
    reasons = []
    if risk_score >= 45:
        reasons.append(f"Liquidity/Trap risk={int(risk_score)}")
    return profile if isinstance(profile, dict) else {}, round(penalty, 4), reasons


def _time_risk_adjustment(snapshot: Dict[str, Any]) -> Tuple[float, List[str]]:
    """Soft penalty for dangerous hours/regime shifts learned elsewhere."""
    tr = snapshot.get("time_risk")
    score = _extract_nested_number(snapshot, "time_risk_score", default=0.0)
    if isinstance(tr, dict):
        score = max(score, _safe_float(tr.get("risk_score"), 0.0))
        label = str(tr.get("risk") or tr.get("level") or "").upper()
    else:
        label = str(tr or "").upper()

    if label == "HIGH":
        score = max(score, 70.0)
    elif label == "MEDIUM":
        score = max(score, 40.0)

    penalty = max(0.0, min(7.0, score / 14.0))
    return round(penalty, 4), ([f"Time risk={int(score)}"] if score >= 40 else [])




def _similarity_adjustment(symbol: str, direction: str, snapshot: Dict[str, Any]) -> Tuple[Dict[str, Any], float, List[str]]:
    """Historical Similarity Engine layer.

    Uses coin_learning.get_similarity_adjustment when available. This is a soft
    decision layer only: similar profitable historical snapshots can improve the
    final AI rank, while similar weak/SL-heavy snapshots reduce priority or send
    the signal to Ghost through the normal scanner approval path.
    """
    if not get_similarity_adjustment:
        return {}, 0.0, []
    try:
        try:
            sim = get_similarity_adjustment(symbol, direction, snapshot=snapshot)
        except TypeError:
            sim = get_similarity_adjustment(symbol, direction, snapshot)
    except Exception:
        return {}, 0.0, []

    if not isinstance(sim, dict):
        return {}, 0.0, []

    # Accept several possible field names to stay compatible with future
    # coin_learning.py versions.
    adj = None
    for key in ("rank_adjustment", "score_adjustment", "adjustment", "ai_adjustment"):
        if sim.get(key) is not None:
            adj = _safe_float(sim.get(key), 0.0)
            break
    if adj is None:
        wr = _safe_float(sim.get("win_rate"), None)
        matches = _safe_int(sim.get("matches") or sim.get("sample_count") or sim.get("similar_count"), 0)
        confidence = _safe_float(sim.get("confidence"), 0.0)
        if wr is None:
            adj = 0.0
        else:
            # Conservative fallback: no strong effect unless there are enough
            # similar examples. Ghost has lower weight inside coin_learning.
            match_factor = min(1.0, max(0.0, matches / 20.0))
            conf_factor = min(1.0, max(0.25, confidence / 100.0))
            adj = (wr - 55.0) / 7.5 * match_factor * conf_factor

    adj = max(-8.0, min(8.0, _safe_float(adj, 0.0)))

    reasons: List[str] = []
    matches = _safe_int(sim.get("matches") or sim.get("sample_count") or sim.get("similar_count"), 0)
    wr_val = sim.get("win_rate")
    risk = str(sim.get("risk") or sim.get("pattern_risk") or "").upper()
    label = str(sim.get("label") or sim.get("pattern_label") or sim.get("status") or "").upper()

    if matches:
        if wr_val is not None:
            reasons.append(f"Similarity {matches}x WR={round(_safe_float(wr_val),1)} adj={round(adj,1)}")
        else:
            reasons.append(f"Similarity {matches}x adj={round(adj,1)}")
    elif abs(adj) >= 1.0:
        reasons.append(f"Similarity adj={round(adj,1)}")
    if risk in {"HIGH", "BAD", "WEAK"}:
        reasons.append(f"Similarity risk={risk}")
    if label and label not in {"UNKNOWN", "NA", "NONE"} and len(reasons) < 3:
        reasons.append(f"Similarity {label}")

    return sim, round(adj, 4), reasons
def _meta_weights(symbol: str, direction: str, snapshot: Dict[str, Any]) -> Dict[str, float]:
    """Best-effort adaptive weights. Neutral defaults keep compatibility."""
    weights = {
        "prediction": 1.0,
        "risk": 1.0,
        "learning": 1.0,
        "similarity": 1.0,
        "liquidity": 1.0,
        "time": 1.0,
        "rotation": 1.0,
    }
    if not get_meta_layer_weights:
        return weights
    try:
        meta = get_meta_layer_weights(symbol, direction=direction, snapshot=snapshot)
    except TypeError:
        try:
            meta = get_meta_layer_weights(symbol, direction, snapshot)
        except Exception:
            meta = {}
    except Exception:
        meta = {}
    if isinstance(meta, dict):
        src = meta.get("weights") if isinstance(meta.get("weights"), dict) else meta
        for k in list(weights.keys()):
            if src.get(k) is not None:
                weights[k] = max(0.4, min(1.8, _safe_float(src.get(k), 1.0)))
    return weights


def apply_ai_scanner_decision(r: Dict[str, Any]) -> Dict[str, Any]:
    """Attach final scanner selection fields to an AI Movement Hunter result.

    analysis.py already decides setup/entry/direction/freshness/trap. Scanner
    only adds portfolio layers: risk, learning, similarity, rotation, slots.
    It never converts a classic technical condition into a real signal.
    """
    r = dict(r)
    symbol = normalize_symbol(r.get("symbol"))
    direction = str(r.get("direction") or "").upper()
    snapshot = _build_scanner_snapshot(r)

    base_score = _safe_float(r.get("score"), 0.0)
    base_rank = _classic_rank_value(r)
    base_min = max(MIN_SCANNER_SCORE, _safe_int(r.get("min_score"), MIN_SCANNER_SCORE))
    base_req_conf = _safe_int(r.get("required_confirmations"), 0)
    actual_conf = _safe_int(r.get("confirmations"), 0)

    risk_state, risk_penalty, risk_extra_conf, risk_reasons = _risk_adjustment(symbol, direction, snapshot)
    learning_state, learning_penalty, learning_extra_conf, learning_reasons = _learning_adjustment(symbol, direction, snapshot)
    similarity_state, similarity_adj, similarity_reasons = _similarity_adjustment(symbol, direction, snapshot)
    rotation_score = _get_rotation_score(symbol, direction, snapshot)
    prediction_adj, prediction_reasons = _prediction_adjustment(snapshot)
    liquidity_state, liquidity_penalty, liquidity_reasons = _liquidity_adjustment(symbol, direction, snapshot)
    time_penalty, time_reasons = _time_risk_adjustment(snapshot)
    meta_w = _meta_weights(symbol, direction, snapshot)

    # Rotation is the portfolio priority layer. 50 is neutral; weak coins get
    # meaningful de-prioritization, strong learned coins get a modest boost.
    rotation_adj = max(-10.0, min(8.0, (rotation_score - 50.0) / 5.0)) * meta_w.get('rotation', 1.0)

    weighted_risk_penalty = risk_penalty * meta_w.get('risk', 1.0)
    weighted_learning_penalty = learning_penalty * meta_w.get('learning', 1.0)
    weighted_similarity_adj = similarity_adj * meta_w.get('similarity', 1.0)
    weighted_liquidity_penalty = liquidity_penalty * meta_w.get('liquidity', 1.0)
    weighted_time_penalty = time_penalty * meta_w.get('time', 1.0)
    weighted_prediction_adj = prediction_adj * meta_w.get('prediction', 1.0)

    final_score = base_score - weighted_risk_penalty - weighted_learning_penalty - weighted_liquidity_penalty - weighted_time_penalty + rotation_adj + weighted_prediction_adj + weighted_similarity_adj
    required_score = base_min + weighted_learning_penalty + min(6.0, weighted_risk_penalty * 0.35) + min(4.0, weighted_liquidity_penalty * 0.35) + min(3.0, weighted_time_penalty * 0.35)
    required_confirmations = base_req_conf + risk_extra_conf + learning_extra_conf

    approved = True
    reject_reasons: List[str] = []

    ai_decision = r.get("ai_decision") if isinstance(r.get("ai_decision"), dict) else {}
    hunter = r.get("ai_movement_hunter") if isinstance(r.get("ai_movement_hunter"), dict) else {}
    decision_kind = str(ai_decision.get("decision") or hunter.get("decision") or "REAL").upper()
    move_phase = str(ai_decision.get("move_phase") or hunter.get("move_phase") or snapshot.get("move_state") or "").upper()
    trap_risk = str(ai_decision.get("trap_risk") or hunter.get("trap_risk") or snapshot.get("trap_risk") or "").upper()

    if decision_kind not in {"REAL", "ACTIVE", "ENTRY"}:
        approved = False
        reject_reasons.append(f"AI Movement decision is not REAL: {decision_kind}")
    if move_phase in {"EXHAUSTION", "RANGE_AFTER_MOVE", "RANGE"}:
        approved = False
        reject_reasons.append(f"AI Movement phase blocks real entry: {move_phase}")
    if trap_risk == "HIGH":
        approved = False
        reject_reasons.append("AI Movement trap risk HIGH: ghost/reject")

    if final_score < required_score:
        approved = False
        reject_reasons.append(f"AI final score {round(final_score,1)} < required {round(required_score,1)}")
    if required_confirmations and actual_conf < required_confirmations:
        approved = False
        reject_reasons.append(f"confirmations {actual_conf} < required {required_confirmations}")

    # Hard safety blocks for the final real-slot selector. These are symmetric
    # for LONG/SHORT and only affect high-risk coin+direction candidates.
    risk_score_value = _safe_float(risk_state.get("risk_score"), 0.0) if isinstance(risk_state, dict) else 0.0
    sl_count_value = _safe_int(risk_state.get("sl_count"), 0) if isinstance(risk_state, dict) else 0
    if risk_score_value >= 90 and final_score < 96:
        approved = False
        reject_reasons.append("AI scanner severe risk: ghost instead of real")
    elif risk_score_value >= 75 and final_score < 92:
        approved = False
        reject_reasons.append("AI scanner high risk: ghost instead of real")
    if sl_count_value >= 3 and final_score < 94:
        approved = False
        reject_reasons.append("AI scanner repeated same-day SL: ghost instead of real")

    # Do not fully erase high-quality analysis results; if rejected, they can
    # become Ghost signals for learning instead of Telegram/live entry.
    # final_score already includes rotation/prediction/similarity and penalties.
    # Do not add those positive layers a second time here; otherwise AI rank
    # can over-prioritize noisy boosts and ignore risk/liquidity penalties.
    ai_rank = _movement_rank_value(r) + (final_score - base_score)

    r["symbol"] = symbol
    r["snapshot"] = snapshot
    r["coin_risk"] = risk_state
    r["ai_learning"] = learning_state
    r["similarity_learning"] = similarity_state
    r["liquidity_trap"] = liquidity_state
    r["rotation_score"] = rotation_score
    r["meta_weights"] = meta_w
    r["ai_scanner"] = {
        "approved": approved,
        "base_score": round(base_score, 4),
        "final_score": round(final_score, 4),
        "required_score": round(required_score, 4),
        "base_rank": round(base_rank, 4),
        "ai_final_rank": round(ai_rank, 4),
        "risk_penalty": round(weighted_risk_penalty, 4),
        "learning_penalty": round(weighted_learning_penalty, 4),
        "similarity_adjustment": round(weighted_similarity_adj, 4),
        "liquidity_penalty": round(weighted_liquidity_penalty, 4),
        "time_penalty": round(weighted_time_penalty, 4),
        "prediction_adjustment": round(weighted_prediction_adj, 4),
        "rotation_adjustment": round(rotation_adj, 4),
        "required_confirmations": required_confirmations,
        "actual_confirmations": actual_conf,
        "reasons": risk_reasons + learning_reasons + similarity_reasons + prediction_reasons + liquidity_reasons + time_reasons + reject_reasons,
    }
    r["ai_final_score"] = round(final_score, 4)
    r["ai_final_rank"] = round(ai_rank, 4)
    r["ai_scanner_approved"] = approved

    # Optional learned TP suggestion is attached but does not force TP changes
    # unless later analysis/order code explicitly chooses to use it.
    if get_smart_tp_suggestion:
        try:
            smart_tp = get_smart_tp_suggestion(symbol, direction, snapshot=snapshot)
            if isinstance(smart_tp, dict) and smart_tp:
                r["smart_tp_suggestion"] = smart_tp
        except Exception:
            pass

    return r


def is_valid_signal(r):
    if not _base_valid_signal(r):
        return False
    enriched = apply_ai_scanner_decision(r)
    return bool(enriched.get("ai_scanner_approved"))


def signal_rank_value(r):
    try:
        return _safe_float(r.get("ai_final_rank"), _movement_rank_value(r))
    except Exception:
        return _movement_rank_value(r)


def should_skip_duplicate(r):
    if not is_symbol_direction_active:
        return False
    try:
        return bool(is_symbol_direction_active(r.get("symbol"), r.get("direction")))
    except Exception:
        return False


def save_as_ghost(r, reason="SLOT_FULL"):
    if not create_ghost_signal:
        return False
    try:
        rr = dict(r)
        snap = _build_scanner_snapshot(rr)
        if rr.get("ai_scanner"):
            snap["ai_scanner"] = rr.get("ai_scanner")
        if rr.get("similarity_learning"):
            snap["similarity_learning"] = rr.get("similarity_learning")
        if rr.get("liquidity_trap"):
            snap["liquidity_trap"] = rr.get("liquidity_trap")
        if rr.get("meta_weights"):
            snap["meta_weights"] = rr.get("meta_weights")
        create_ghost_signal(
            rr.get("symbol"),
            rr.get("direction"),
            rr.get("entry"),
            rr.get("stop_loss"),
            rr.get("tp1"),
            rr.get("tp2"),
            rr.get("score"),
            snap,
            "scanner",
            reason,
        )
        return True
    except Exception:
        return False


def scan_market(symbols: Optional[List[str]] = None, max_results: int = MAX_SCAN_RESULTS, allow_ghost: bool = True):
    symbols = symbols or get_scan_symbols()
    valid: List[Dict[str, Any]] = []
    ai_rejected: List[Dict[str, Any]] = []
    no_trade = 0
    errors = 0
    ghost_count = 0

    for sym in symbols:
        try:
            res = analyze_symbol(normalize_symbol(sym))
            reject_reason = _base_reject_reason(res)
            if reject_reason != "OK":
                no_trade += 1
                if allow_ghost and _can_store_rejected_as_ghost(res):
                    rr = dict(res)
                    rr["scanner_reject_reason"] = reject_reason
                    if save_as_ghost(rr, f"BASE_REJECTED:{reject_reason}"):
                        ghost_count += 1
                        logger.info(
                            f"scanner candidate saved as ghost: {rr.get('symbol')} {rr.get('direction')} | BASE_REJECTED:{reject_reason}"
                        )
                continue

            enriched = apply_ai_scanner_decision(res)
            if should_skip_duplicate(enriched):
                continue

            if enriched.get("ai_scanner_approved"):
                valid.append(enriched)
            else:
                ai_rejected.append(enriched)
                if allow_ghost and save_as_ghost(enriched, "AI_SCANNER_REJECTED"):
                    ghost_count += 1
        except Exception as e:
            errors += 1
            logger.warning(f"scanner analyze error: {normalize_symbol(sym)} | {str(e)[:160]}")
        time.sleep(SCAN_DELAY_SECONDS)

    valid.sort(key=signal_rank_value, reverse=True)
    logger.info(
        f"scanner scan complete: scanned={len(symbols)} valid={len(valid)} ai_rejected={len(ai_rejected)} "
        f"no_trade={no_trade} errors={errors} ghosts={ghost_count}"
    )
    return {
        "signals": valid[:max_results],
        "all_valid_signals": valid,
        "ai_rejected_signals": ai_rejected,
        "scanned": len(symbols),
        "no_trade_count": no_trade,
        "error_count": errors,
        "ghost_count": ghost_count,
        "timestamp": int(time.time()),
    }


def get_available_slots():
    if get_free_slots is None:
        return 1
    try:
        return max(0, int(get_free_slots()))
    except Exception:
        return 1


def scan_for_auto_signals(symbols: Optional[List[str]] = None, max_results: int = MAX_SCAN_RESULTS, allow_ghost: bool = True):
    sr = scan_market(symbols, max_results, allow_ghost)
    valid = sr.get("all_valid_signals", [])
    if not valid:
        sr["signals"] = []
        sr["mode"] = "NO_SIGNAL"
        logger.info(
            f"auto scan result: NO_SIGNAL | scanned={sr.get('scanned')} no_trade={sr.get('no_trade_count')} "
            f"errors={sr.get('error_count')} ghosts={sr.get('ghost_count')}"
        )
        return sr

    free = get_available_slots()
    sr["free_slots"] = free

    if free <= 0:
        gc = int(sr.get("ghost_count", 0) or 0)
        if allow_ghost:
            for sig in valid:
                if save_as_ghost(sig, "SLOT_FULL"):
                    gc += 1
        sr["signals"] = []
        sr["ghost_count"] = gc
        sr["mode"] = "GHOST_ONLY"
        logger.info(f"auto scan result: GHOST_ONLY | free_slots={free} ghosts={gc}")
        return sr

    candidates = valid
    if select_best_candidates:
        try:
            selected = select_best_candidates(valid, min(max_results, free))
            if isinstance(selected, list) and selected:
                candidates = selected
        except Exception:
            candidates = valid

    # Final scanner authority: even if slot_manager returns candidates, sort by
    # AI final rank so learning/risk/rotation determine final priority.
    candidates = sorted(candidates, key=signal_rank_value, reverse=True)
    sr["signals"] = candidates[: min(max_results, free)]
    sr["mode"] = "ACTIVE_SIGNALS"
    logger.info(f"auto scan result: ACTIVE_SIGNALS | count={len(sr['signals'])} free_slots={free}")
    return sr


def get_best_signal(symbols=None):
    r = scan_for_auto_signals(symbols, 1, False)
    return (r.get("signals") or [None])[0]


def get_top_signals(symbols=None, limit=5):
    return scan_for_auto_signals(symbols, limit, False).get("signals", [])[:limit]


def quick_market_bias(symbol):
    """Fast overview helper: only 1H + 15M, no full signal analysis.
    This keeps Telegram market overview from timing out.
    """
    df_1h = add_indicators(get_klines(normalize_symbol(symbol), "1h", limit=260))
    df_15m = add_indicators(get_klines(normalize_symbol(symbol), "15m", limit=260))
    t1 = ema_direction(df_1h)
    t15 = ema_direction(df_15m)
    l15 = df_15m.iloc[-1]
    score = 50
    if t1 == "bullish":
        score += 15
    if t15 == "bullish":
        score += 15
    if t1 == "bearish":
        score -= 15
    if t15 == "bearish":
        score -= 15
    if l15["close"] > l15["vwap"]:
        score += 5
    else:
        score -= 5
    if l15["macd"] > l15["macd_signal"]:
        score += 5
    else:
        score -= 5
    if l15["rsi"] >= 52:
        score += 5
    elif l15["rsi"] <= 48:
        score -= 5
    if t1 == "bullish" and t15 == "bullish":
        bias = "bullish"
    elif t1 == "bearish" and t15 == "bearish":
        bias = "bearish"
    else:
        bias = "neutral"
    return {
        "symbol": normalize_symbol(symbol),
        "bias": bias,
        "direction": "OVERVIEW",
        "score": max(0, min(100, int(score))),
        "trend_1h": t1,
        "trend_15m": t15,
    }


def scan_market_overview(symbols=None, limit=40):
    symbols = (symbols or get_scan_symbols())[:limit]
    bullish = bearish = neutral = errors = 0
    details = []
    error_details = []
    for sym in symbols:
        try:
            r = quick_market_bias(sym)
            bias = r.get("bias")
            if bias == "bullish":
                bullish += 1
            elif bias == "bearish":
                bearish += 1
            else:
                neutral += 1
            details.append(r)
        except Exception as e:
            errors += 1
            if len(error_details) < 5:
                error_details.append({"symbol": normalize_symbol(sym), "error": str(e)[:160]})
        time.sleep(SCAN_DELAY_SECONDS)
    total = max(bullish + bearish + neutral, 1)
    bp = round(bullish / total * 100, 1)
    sp = round(bearish / total * 100, 1)
    np = round(neutral / total * 100, 1)
    if bp >= 50:
        mb = "bullish"
        summary = "بازار بیشتر صعودی است"
    elif sp >= 50:
        mb = "bearish"
        summary = "بازار بیشتر نزولی است"
    elif np >= 45:
        mb = "neutral"
        summary = "بازار بیشتر رنج یا نامشخص است"
    elif bp > sp:
        mb = "slightly_bullish"
        summary = "بازار کمی تمایل صعودی دارد"
    elif sp > bp:
        mb = "slightly_bearish"
        summary = "بازار کمی تمایل نزولی دارد"
    else:
        mb = "neutral"
        summary = "بازار جهت مشخصی ندارد"
    return {
        "market_bias": mb,
        "summary": summary,
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "errors": errors,
        "error_details": error_details,
        "bullish_pct": bp,
        "bearish_pct": sp,
        "neutral_pct": np,
        "details": details,
        "scanned": len(symbols),
        "timestamp": int(time.time()),
    }


def scan_symbols_for_signals(symbols=None, max_results=MAX_SCAN_RESULTS):
    return scan_for_auto_signals(symbols, max_results, True).get("signals", [])


def find_best_signal(symbols=None):
    return get_best_signal(symbols)


def find_top_signals(symbols=None, limit=5):
    return get_top_signals(symbols, limit)
