# -*- coding: utf-8 -*-
"""
coin_learning.py

AI Coin Learning Engine for the crypto futures bot.

Purpose:
- Persist per-coin + per-direction learning across code updates.
- Learn from REAL and GHOST signals separately, with REAL weighted higher.
- Store compact technical snapshots at signal/result time.
- Build Coin Personality / Direction Archive.
- Provide Smart TP memory and adaptive extra-strength requirements directly
  consumed by analysis.py.

Compatibility:
- Keeps the old public function names used by the bot:
    build_signal_snapshot
    record_signal
    update_signal_result
    get_smart_tp_suggestion
    should_require_extra_strength
    format_learning_summary
    format_coin_behavior
    format_smart_stats
    get_tp_sl_v2_profile
    find_similar_patterns
    get_similarity_adjustment
    register_tp_sl_v2_result
"""

import math
import time
from typing import Dict, Any, Optional, List, Tuple

from data_store import load_json, save_json

LEARNING_FILE = "coin_learning.json"
MAX_SIGNALS_STORED = 20000
MAX_EVENTS_PER_BUCKET = 240
MAX_PATTERNS_PER_BUCKET = 80
REAL_WEIGHT = 1.0
GHOST_WEIGHT = 0.45

# Time-Weighted Learning (Mode B)
# Recent market behavior should affect scalping decisions more than old data.
# 0-7d: full weight, 8-30d: medium, 31-90d: low, 90d+: archive/weak.
TIME_WEIGHT_RECENT_DAYS = 7
TIME_WEIGHT_MEDIUM_DAYS = 30
TIME_WEIGHT_OLD_DAYS = 90
TIME_WEIGHT_RECENT = 1.0
TIME_WEIGHT_MEDIUM = 0.70
TIME_WEIGHT_OLD = 0.40
TIME_WEIGHT_ARCHIVE = 0.20
MIN_TP_MEMORY_WINS = 3
TP_SL_V2_VERSION = 1

# Historical Similarity Engine
# Soft decision layer: compares the current technical snapshot with prior
# TP/SL snapshots for the same coin+direction. It never hard-blocks signals by
# itself; scanner/slot_manager can use the returned adjustment for ranking.
SIMILARITY_MIN_SAMPLES = 4
SIMILARITY_MAX_MATCHES = 60
SIMILARITY_MIN_SCORE = 0.58
SIMILARITY_REAL_WEIGHT = 1.0
SIMILARITY_GHOST_WEIGHT = 0.55


def _now() -> int:
    return int(time.time())


def _key(symbol: str, direction: str) -> str:
    return f"{str(symbol).upper()}:{str(direction).upper()}"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _norm_result(result: Any) -> str:
    r = str(result or "").upper().strip()
    if r in {"TP", "TP1", "TAKE_PROFIT", "TAKEPROFIT", "EARLY_PROFIT", "AI_EXIT_PROFIT", "DYNAMIC_PROFIT", "PROFIT_PROTECT"}:
        return "TP1"
    if r == "TP2":
        return "TP2"
    if r in {"SL", "STOP", "STOP_LOSS", "STOPLOSS"}:
        return "SL"
    return r or "UNKNOWN"


def _norm_source(source: Any = None, signal_type: Any = None) -> str:
    """Normalize learning source as REAL or GHOST.

    Important compatibility fix:
    older Ghost records often arrived with source="auto_signal_gate" and
    signal_type="GHOST".  The old implementation preferred source first, so
    those records were incorrectly counted as REAL.  Prefer any explicit Ghost
    marker from either field and also recognize Ghost-only gate sources.
    """
    src = str(source or "").upper().strip()
    typ = str(signal_type or "").upper().strip()
    combined = f"{typ} {src}".strip()
    ghost_markers = {
        "GHOST", "SHADOW", "PAPER_GHOST", "GHOST_ONLY", "AI_SETUP_GHOST",
        "AUTO_SIGNAL_GATE", "SLOT_FULL", "AI_NOT_REAL", "SIGNAL_NOT_ACTIVE",
        "MOVE_NOT_FRESH", "BOT_TRADING_DISABLED", "BAD_DIRECTION",
        "MISSING_TRADE_LEVELS",
    }
    if any(marker in combined for marker in ghost_markers):
        return "GHOST"
    return "REAL"


def _source_weight(source: str) -> float:
    return GHOST_WEIGHT if str(source).upper() == "GHOST" else REAL_WEIGHT


def _time_decay_weight(ts: Any, now: Optional[int] = None) -> float:
    """Return recency weight for Mode B time-weighted learning.

    This keeps the bot adaptive for 5M-15M scalping:
    fresh outcomes matter most, 30-90 day data still helps, and very old
    outcomes remain archive evidence with weak influence.
    """
    event_ts = _safe_int(ts, 0)
    if event_ts <= 0:
        return TIME_WEIGHT_RECENT
    current = int(now or _now())
    age_days = max(0.0, (current - event_ts) / 86400.0)
    if age_days <= TIME_WEIGHT_RECENT_DAYS:
        return TIME_WEIGHT_RECENT
    if age_days <= TIME_WEIGHT_MEDIUM_DAYS:
        return TIME_WEIGHT_MEDIUM
    if age_days <= TIME_WEIGHT_OLD_DAYS:
        return TIME_WEIGHT_OLD
    return TIME_WEIGHT_ARCHIVE


def _learning_weight(source: str, ts: Any = None) -> float:
    return _source_weight(source) * _time_decay_weight(ts)


def _bucket_time_weighted_outcomes(b: Dict[str, Any]) -> Dict[str, float]:
    """Recalculate current TP/SL influence from bucket events with recency decay.

    Stored cumulative fields are preserved for counters/history, but decisions
    should prefer this live time-weighted view when enough event data exists.
    """
    events = b.get("events", []) if isinstance(b, dict) else []
    if not isinstance(events, list) or not events:
        return {"tp": _safe_float(b.get("weighted_tp") if isinstance(b, dict) else 0), "sl": _safe_float(b.get("weighted_sl") if isinstance(b, dict) else 0), "total": 0.0, "move_sum": _safe_float(b.get("weighted_move_sum") if isinstance(b, dict) else 0)}
    tp = sl = move_sum = move_w = 0.0
    now = _now()
    for ev in events[-MAX_EVENTS_PER_BUCKET:]:
        if not isinstance(ev, dict):
            continue
        r = _norm_result(ev.get("result"))
        if r not in {"TP1", "TP2", "SL"}:
            continue
        w = _source_weight(_norm_source(ev.get("source"))) * _time_decay_weight(ev.get("ts"), now)
        if r in {"TP1", "TP2"}:
            tp += w
        elif r == "SL":
            sl += w
        if ev.get("move_percent") is not None:
            move_sum += _safe_float(ev.get("move_percent"), 0.0) * w
            move_w += w
    return {"tp": tp, "sl": sl, "total": tp + sl, "move_sum": move_sum, "move_w": move_w}


def _default_state() -> Dict[str, Any]:
    return {
        "version": 3,
        "signals": {},
        "by_coin_direction": {},
        "coin_archive": {},
        "updated_at": 0,
    }


def _state() -> Dict[str, Any]:
    s = load_json(LEARNING_FILE, _default_state())
    if not isinstance(s, dict):
        s = _default_state()

    # Migration from old shape {'signals': {}, 'by_coin_direction': {}, 'ghost': {}}
    s.setdefault("version", 3)
    s.setdefault("signals", {})
    s.setdefault("by_coin_direction", {})
    s.setdefault("coin_archive", {})
    s.setdefault("updated_at", 0)

    if not isinstance(s.get("signals"), dict):
        s["signals"] = {}
    if not isinstance(s.get("by_coin_direction"), dict):
        s["by_coin_direction"] = {}
    if not isinstance(s.get("coin_archive"), dict):
        s["coin_archive"] = {}
    return s


def _empty_bucket(symbol: str, direction: str) -> Dict[str, Any]:
    return {
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "real_total": 0,
        "ghost_total": 0,
        "real_tp": 0,
        "real_sl": 0,
        "ghost_tp": 0,
        "ghost_sl": 0,
        "tp1": 0,
        "tp2": 0,
        "sl": 0,
        "weighted_tp": 0.0,
        "weighted_sl": 0.0,
        "move_sum": 0.0,
        "weighted_move_sum": 0.0,
        "tp_distance_sum": 0.0,
        "tp_distance_weighted_sum": 0.0,
        "tp_distance_weight": 0.0,
        "atr_tp_ratio_sum": 0.0,
        "atr_tp_ratio_weight": 0.0,

        # TP/SL v2 memory: all values are per coin + direction.
        # These fields feed smart TP, SL survival, fake-breakout awareness,
        # and coin personality without changing old public function names.
        "tp1_reach_atr_sum": 0.0,
        "tp1_reach_atr_weight": 0.0,
        "tp2_reach_atr_sum": 0.0,
        "tp2_reach_atr_weight": 0.0,
        "sl_distance_atr_sum": 0.0,
        "sl_distance_atr_weight": 0.0,
        "max_favorable_atr_sum": 0.0,
        "max_favorable_atr_weight": 0.0,
        "max_adverse_atr_sum": 0.0,
        "max_adverse_atr_weight": 0.0,
        "fake_breakouts": 0,
        "clean_breakouts": 0,
        "bounces": 0,
        "sr_memory": {},
        "tp_sl_v2": {"version": TP_SL_V2_VERSION, "samples": 0},
        "events": [],
        "tp_patterns": [],
        "sl_patterns": [],
        "indicator_stats": {},
        "market_stats": {},
        "entry_quality_stats": {},
        "condition_stats": {},
        "time_stats": {},
        "dynamic_profit_stats": {"profit_exits": 0, "good_exits": 0, "premature_exits": 0, "reasons": {}},
        "meta_learning": {},
        "early_5m_tp": 0.0,
        "early_5m_sl": 0.0,
        "multi_tf_tp": 0.0,
        "multi_tf_sl": 0.0,
        "late_entry_tp": 0.0,
        "late_entry_sl": 0.0,
        "rr_filter_count": 0,
        "behavior": "UNKNOWN",
        "personality": "UNKNOWN",
        "confidence": 0,
        "risk_bias": 0,
        "last_updated": 0,
    }


def _ensure_bucket_fields(b: Dict[str, Any], symbol: str, direction: str) -> None:
    defaults = _empty_bucket(symbol, direction)
    for k, v in defaults.items():
        b.setdefault(k, v)
    if not isinstance(b.get("events"), list):
        b["events"] = []
    if not isinstance(b.get("tp_patterns"), list):
        b["tp_patterns"] = []
    if not isinstance(b.get("sl_patterns"), list):
        b["sl_patterns"] = []
    if not isinstance(b.get("indicator_stats"), dict):
        b["indicator_stats"] = {}
    if not isinstance(b.get("market_stats"), dict):
        b["market_stats"] = {}
    if not isinstance(b.get("entry_quality_stats"), dict):
        b["entry_quality_stats"] = {}
    if not isinstance(b.get("condition_stats"), dict):
        b["condition_stats"] = {}
    if not isinstance(b.get("time_stats"), dict):
        b["time_stats"] = {}
    if not isinstance(b.get("dynamic_profit_stats"), dict):
        b["dynamic_profit_stats"] = {"profit_exits": 0, "good_exits": 0, "premature_exits": 0, "reasons": {}}
    if not isinstance(b.get("meta_learning"), dict):
        b["meta_learning"] = {}


def _bucket(s: Dict[str, Any], symbol: str, direction: str) -> Dict[str, Any]:
    k = _key(symbol, direction)
    b = s["by_coin_direction"].setdefault(k, _empty_bucket(symbol, direction))
    _ensure_bucket_fields(b, symbol, direction)
    return b


def _coin_row(s: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    sym = str(symbol).upper()
    row = s["coin_archive"].setdefault(sym, {
        "symbol": sym,
        "long": {"tp": 0, "sl": 0},
        "short": {"tp": 0, "sl": 0},
        "behavior": "UNKNOWN",
        "best_direction": "UNKNOWN",
        "confidence": 0,
        "last_updated": 0,
    })
    row.setdefault("long", {"tp": 0, "sl": 0})
    row.setdefault("short", {"tp": 0, "sl": 0})
    return row


def _compact_snapshot(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    keys = [
        "symbol", "direction", "price", "entry", "score", "long_score", "short_score",
        "rsi", "rsi_5m", "rsi_slope_15m", "macd", "macd_signal", "macd_hist",
        "macd_hist_slope_15m", "macd_hist_accel_15m", "adx", "adx_slope_15m", "atr",
        "ema20", "ema50", "ema200", "ema_structure_15m", "vwap", "vwap_status", "vwap_distance_pct",
        "volume_ratio_15m", "power2_buy", "power2_sell", "power3_buy", "power3_sell", "buy_power", "sell_power",
        "market_regime", "market_mode", "btc_bias", "support", "resistance", "timeframe_core", "entry_timing_tf",
        "risk_level", "risk_reward", "confirmations", "freshness", "entry_mode",
        "early_5m_trigger", "multi_tf_alignment", "late_entry", "pump_dump_chase",
        "early_5m_active", "early_5m_score", "multi_tf_active", "late_entry_flag",
        "rr_filter", "reject_reason", "valid_gate",
        "prediction_score", "expected_move_atr", "reversal_risk_score", "move_state", "trap_risk", "move_phase", "freshness_score", "move_done_pct", "movement_type", "ai_score",
        "result_source", "move_percent", "exit_price", "exit_reason", "early_profit_exit",
    ]
    out = {k: snapshot.get(k) for k in keys if snapshot.get(k) is not None}
    if isinstance(snapshot.get("market_context"), dict):
        out["market_context"] = snapshot.get("market_context")
    if isinstance(snapshot.get("trends"), dict):
        out["trends"] = snapshot.get("trends")
    for nested_key in [
        "prediction_layer", "state_awareness", "candle_behavior", "liquidity_trap",
        "relative_strength", "sr_levels", "dynamic_profit_protection", "trade_management",
    ]:
        if isinstance(snapshot.get(nested_key), dict):
            out[nested_key] = snapshot.get(nested_key)

    # Promote common nested values for fast conditional learning.
    pred = out.get("prediction_layer") if isinstance(out.get("prediction_layer"), dict) else {}
    state = out.get("state_awareness") if isinstance(out.get("state_awareness"), dict) else pred.get("state", {}) if isinstance(pred.get("state"), dict) else {}
    liq = out.get("liquidity_trap") if isinstance(out.get("liquidity_trap"), dict) else pred.get("liquidity_trap", {}) if isinstance(pred.get("liquidity_trap"), dict) else {}
    rel = out.get("relative_strength") if isinstance(out.get("relative_strength"), dict) else pred.get("relative_strength", {}) if isinstance(pred.get("relative_strength"), dict) else {}
    if pred:
        out.setdefault("prediction_score", pred.get("prediction_score"))
        out.setdefault("expected_move_atr", pred.get("expected_move_atr"))
        out.setdefault("reversal_risk_score", pred.get("reversal_risk_score"))
    if state:
        out.setdefault("move_state", state.get("move_state"))
        out.setdefault("reversal_risk_score", state.get("reversal_risk_score"))
    if liq:
        out.setdefault("trap_risk", liq.get("trap_risk"))
        out.setdefault("fake_break_risk", liq.get("fake_break_risk"))
    if rel:
        out.setdefault("relative_status", rel.get("relative_status"))
    return out


def build_signal_snapshot(symbol: str, direction: str, technical_snapshot: Optional[Dict] = None, market_context: Optional[Dict] = None) -> Dict:
    snap = dict(technical_snapshot or {})
    market = dict(market_context or {})
    # Normalize market keys into the snapshot so later modules do not have to
    # dig into nested objects.
    if "market_regime" not in snap and market.get("market_regime") is not None:
        snap["market_regime"] = market.get("market_regime")
    if "btc_bias" not in snap and market.get("btc_bias") is not None:
        snap["btc_bias"] = market.get("btc_bias")
    snap.update({
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "market_context": market,
        "snapshot_at": _now(),
    })
    return snap


def _update_avg_stat(stats: Dict[str, Any], name: str, value: Any, weight: float, result: str) -> None:
    val = _safe_float(value, None)
    if val is None:
        return
    row = stats.setdefault(name, {"tp_sum": 0.0, "tp_w": 0.0, "sl_sum": 0.0, "sl_w": 0.0})
    if result in {"TP1", "TP2"}:
        row["tp_sum"] = _safe_float(row.get("tp_sum")) + val * weight
        row["tp_w"] = _safe_float(row.get("tp_w")) + weight
    elif result == "SL":
        row["sl_sum"] = _safe_float(row.get("sl_sum")) + val * weight
        row["sl_w"] = _safe_float(row.get("sl_w")) + weight


def _update_market_stat(stats: Dict[str, Any], name: str, value: Any, weight: float, result: str) -> None:
    if value is None:
        return
    key = f"{name}:{str(value).upper()}"
    row = stats.setdefault(key, {"tp_w": 0.0, "sl_w": 0.0})
    if result in {"TP1", "TP2"}:
        row["tp_w"] = _safe_float(row.get("tp_w")) + weight
    elif result == "SL":
        row["sl_w"] = _safe_float(row.get("sl_w")) + weight



def _extract_direction_pack(value: Any, direction: str) -> Dict[str, Any]:
    """Return direction-specific metadata from nested analysis packs."""
    if not isinstance(value, dict):
        return {}
    direction = str(direction or "").upper().strip()
    if direction and isinstance(value.get(direction), dict):
        return value.get(direction) or {}
    return value


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "active"}


def _fast_entry_flags(snap: Dict[str, Any], direction: str) -> Dict[str, Any]:
    """Normalize Early-5M / Multi-TF / Late-Entry metadata for learning."""
    direction = str(direction or snap.get("direction") or "").upper().strip()
    early = _extract_direction_pack(snap.get("early_5m_trigger"), direction)
    align = _extract_direction_pack(snap.get("multi_tf_alignment"), direction)
    late = _extract_direction_pack(snap.get("late_entry"), direction)

    early_active = _boolish(early.get("active") if isinstance(early, dict) else snap.get("early_5m_active"))
    early_score = _safe_float((early or {}).get("score") if isinstance(early, dict) else snap.get("early_5m_score"), 0.0)
    multi_active = _boolish(align.get("active") if isinstance(align, dict) else snap.get("multi_tf_active"))
    multi_score = _safe_float((align or {}).get("score") if isinstance(align, dict) else 0.0, 0.0)
    late_active = _boolish(late.get("late") if isinstance(late, dict) else snap.get("late_entry_flag"))
    rr_filter = str(snap.get("entry_mode") or "").upper() == "RR_FILTER" or _boolish(snap.get("rr_filter"))

    return {
        "early_5m_active": early_active,
        "early_5m_score": early_score,
        "multi_tf_active": multi_active,
        "multi_tf_score": multi_score,
        "late_entry": late_active,
        "rr_filter": rr_filter,
    }


def _update_binary_quality_stat(stats: Dict[str, Any], name: str, active: bool, weight: float, result: str) -> None:
    """Track TP/SL outcomes for boolean setup features like Early Trigger."""
    row = stats.setdefault(name, {"tp_w": 0.0, "sl_w": 0.0, "total_w": 0.0})
    if not active:
        return
    row["total_w"] = _safe_float(row.get("total_w")) + weight
    if result in {"TP1", "TP2"}:
        row["tp_w"] = _safe_float(row.get("tp_w")) + weight
    elif result == "SL":
        row["sl_w"] = _safe_float(row.get("sl_w")) + weight

def _bucketed_number(value: Any, step: float, default: str = "NA", min_value: float = None, max_value: float = None) -> str:
    """Return a stable text bucket such as 20-24 for conditional learning."""
    v = _safe_float(value, None)
    if v is None:
        return default
    if min_value is not None:
        v = max(min_value, v)
    if max_value is not None:
        v = min(max_value, v)
    base = math.floor(v / step) * step
    hi = base + step
    if float(step).is_integer():
        return f"{int(base)}-{int(hi)}"
    return f"{round(base, 3)}-{round(hi, 3)}"


def _update_condition_stat(stats: Dict[str, Any], key: str, weight: float, result: str, move_percent: Any = None) -> None:
    if not key:
        return
    row = stats.setdefault(key, {"tp_w": 0.0, "sl_w": 0.0, "total_w": 0.0, "move_sum": 0.0, "move_w": 0.0})
    row["total_w"] = _safe_float(row.get("total_w")) + weight
    if result in {"TP1", "TP2"}:
        row["tp_w"] = _safe_float(row.get("tp_w")) + weight
    elif result == "SL":
        row["sl_w"] = _safe_float(row.get("sl_w")) + weight
    mp = _safe_float(move_percent, None)
    if mp is not None:
        row["move_sum"] = _safe_float(row.get("move_sum")) + mp * weight
        row["move_w"] = _safe_float(row.get("move_w")) + weight


def _learn_condition_matrix(b: Dict[str, Any], snap: Dict[str, Any], weight: float, result: str, move_percent: Any = None) -> None:
    """Learn coin+direction+condition behavior instead of broad labels."""
    stats = b.setdefault("condition_stats", {})
    direction = str(snap.get("direction") or b.get("direction") or "").upper()

    conditions = [
        ("ADX", _bucketed_number(snap.get("adx"), 4, min_value=0, max_value=80)),
        ("RSI", _bucketed_number(snap.get("rsi"), 5, min_value=0, max_value=100)),
        ("RSI_SLOPE", _bucketed_number(snap.get("rsi_slope_15m"), 1.0, min_value=-10, max_value=10)),
        ("MACD_HIST_ACCEL", _bucketed_number(snap.get("macd_hist_accel_15m"), 0.0005, min_value=-0.01, max_value=0.01)),
        ("ADX_SLOPE", _bucketed_number(snap.get("adx_slope_15m"), 1.0, min_value=-10, max_value=10)),
        ("VWAP", str(snap.get("vwap_status") or "NA").upper()),
        ("EMA", str(snap.get("ema_structure_15m") or "NA").upper()),
        ("STATE", str(snap.get("move_state") or ((snap.get("state_awareness") or {}).get("move_state") if isinstance(snap.get("state_awareness"), dict) else None) or "NA").upper()),
        ("MOVE_PHASE", str(snap.get("move_phase") or "NA").upper()),
        ("FRESHNESS", _bucketed_number(snap.get("freshness_score"), 10, min_value=0, max_value=100)),
        ("MOVE_DONE", _bucketed_number(snap.get("move_done_pct"), 10, min_value=0, max_value=100)),
        ("TRAP", str(snap.get("trap_risk") or ((snap.get("liquidity_trap") or {}).get("trap_risk") if isinstance(snap.get("liquidity_trap"), dict) else None) or "NA").upper()),
        ("PRED", _bucketed_number(snap.get("prediction_score"), 10, min_value=0, max_value=100)),
        ("REVERSAL", _bucketed_number(snap.get("reversal_risk_score"), 10, min_value=0, max_value=100)),
        ("MARKET", str(snap.get("market_regime") or snap.get("market_mode") or "NA").upper()),
        ("BTC", str(snap.get("btc_bias") or "NA").upper()),
        ("REL", str(snap.get("relative_status") or ((snap.get("relative_strength") or {}).get("relative_status") if isinstance(snap.get("relative_strength"), dict) else "NA")).upper()),
    ]
    for name, val in conditions:
        _update_condition_stat(stats, f"{direction}:{name}:{val}", weight, result, move_percent)

    # Pair conditions catch examples like DOGE SHORT + ADX 18-22 + VWAP status.
    adx_bin = _bucketed_number(snap.get("adx"), 4, min_value=0, max_value=80)
    vwap = str(snap.get("vwap_status") or "NA").upper()
    state = str(snap.get("move_state") or ((snap.get("state_awareness") or {}).get("move_state") if isinstance(snap.get("state_awareness"), dict) else "NA")).upper()
    trap = str(snap.get("trap_risk") or ((snap.get("liquidity_trap") or {}).get("trap_risk") if isinstance(snap.get("liquidity_trap"), dict) else "NA")).upper()
    for key in [f"{direction}:ADX_VWAP:{adx_bin}:{vwap}", f"{direction}:STATE_TRAP:{state}:{trap}"]:
        _update_condition_stat(stats, key, weight, result, move_percent)


def _learn_time_behavior(b: Dict[str, Any], snap: Dict[str, Any], weight: float, result: str, move_percent: Any = None) -> None:
    ts = _safe_int(snap.get("snapshot_at") or snap.get("ts") or time.time(), _now())
    hour = time.gmtime(ts).tm_hour
    stats = b.setdefault("time_stats", {})
    _update_condition_stat(stats, f"UTC_HOUR:{hour:02d}", weight, result, move_percent)


def _learn_meta_layer(b: Dict[str, Any], snap: Dict[str, Any], weight: float, result: str, move_percent: Any = None) -> None:
    """Meta learning: track which internal layers were helpful under outcomes."""
    meta = b.setdefault("meta_learning", {})
    pred = _safe_float(snap.get("prediction_score"), None)
    rev = _safe_float(snap.get("reversal_risk_score"), None)
    early = _safe_float(snap.get("early_5m_score"), None)
    trap = str(snap.get("trap_risk") or "").upper()
    state = str(snap.get("move_state") or "").upper()
    candle = snap.get("candle_behavior") if isinstance(snap.get("candle_behavior"), dict) else {}

    layer_flags = {
        "prediction_high": pred is not None and pred >= 70,
        "prediction_low": pred is not None and pred <= 45,
        "reversal_high": rev is not None and rev >= 70,
        "early_5m_strong": early is not None and early >= 7,
        "trap_high": trap == "HIGH",
        "state_late": state == "LATE_OR_EXHAUSTION",
        "state_early_momentum": state == "EARLY_MOMENTUM",
        "candle_strong_close": bool(candle.get("strong_close_up") or candle.get("strong_close_down")),
        "candle_rejection": bool(candle.get("upper_rejection") or candle.get("lower_rejection")),
    }
    for name, active in layer_flags.items():
        if active:
            _update_condition_stat(meta, name, weight, result, move_percent)


def _weighted_win_rate(b: Dict[str, Any]) -> float:
    tw = _bucket_time_weighted_outcomes(b)
    tp = _safe_float(tw.get("tp"))
    sl = _safe_float(tw.get("sl"))
    total = tp + sl
    return tp / total if total > 0 else 0.0


def _closed_weight(b: Dict[str, Any]) -> float:
    tw = _bucket_time_weighted_outcomes(b)
    total = _safe_float(tw.get("total"))
    if total > 0:
        return total
    return _safe_float(b.get("weighted_tp")) + _safe_float(b.get("weighted_sl"))


def _weighted_avg(b: Dict[str, Any], sum_key: str, weight_key: str, default: float = 0.0) -> float:
    w = _safe_float(b.get(weight_key))
    if w <= 0:
        return default
    return _safe_float(b.get(sum_key)) / max(w, 1e-9)


def _add_weighted(b: Dict[str, Any], sum_key: str, weight_key: str, value: Any, weight: float) -> None:
    val = _safe_float(value, None)
    if val is None:
        return
    b[sum_key] = _safe_float(b.get(sum_key)) + val * weight
    b[weight_key] = _safe_float(b.get(weight_key)) + weight


def _tp_sl_v2_snapshot_metrics(sig: Dict[str, Any], snap: Dict[str, Any], result: str, exit_price: Any, weight: float, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    entry = _safe_float(sig.get("entry") or sig.get("price") or snap.get("entry") or snap.get("price"), 0.0)
    atr = _safe_float(snap.get("atr") or kwargs.get("atr"), 0.0)
    if atr <= 0 and entry > 0:
        atr = entry * 0.0015
    atr = max(atr, 1e-12)

    tp1 = _safe_float(kwargs.get("tp1") or sig.get("tp1") or snap.get("tp1"), 0.0)
    tp2 = _safe_float(kwargs.get("tp2") or sig.get("tp2") or snap.get("tp2"), 0.0)
    sl = _safe_float(kwargs.get("stop_loss") or kwargs.get("sl") or sig.get("stop_loss") or snap.get("stop_loss"), 0.0)

    out = {"entry": entry, "atr": atr}
    if entry > 0 and tp1 > 0:
        out["tp1_atr"] = abs(tp1 - entry) / atr
    if entry > 0 and tp2 > 0:
        out["tp2_atr"] = abs(tp2 - entry) / atr
    if entry > 0 and sl > 0:
        out["sl_atr"] = abs(entry - sl) / atr

    if kwargs.get("max_favorable") is not None:
        out["max_favorable_atr"] = abs(_safe_float(kwargs.get("max_favorable"))) / atr
    elif exit_price and entry and result in {"TP1", "TP2"}:
        out["max_favorable_atr"] = abs(_safe_float(exit_price) - entry) / atr

    if kwargs.get("max_adverse") is not None:
        out["max_adverse_atr"] = abs(_safe_float(kwargs.get("max_adverse"))) / atr
    elif exit_price and entry and result == "SL":
        out["max_adverse_atr"] = abs(_safe_float(exit_price) - entry) / atr

    return out


def _recompute_bucket_behavior(b: Dict[str, Any]) -> None:
    total_w = _closed_weight(b)
    wr = _weighted_win_rate(b)
    tw_outcomes = _bucket_time_weighted_outcomes(b)
    if _safe_float(tw_outcomes.get("move_w")) > 0:
        avg_move = _safe_float(tw_outcomes.get("move_sum")) / max(_safe_float(tw_outcomes.get("move_w")), 1e-9)
    else:
        avg_move = _safe_float(b.get("weighted_move_sum")) / max(total_w, 1e-9) if total_w else 0.0

    if total_w < 3:
        behavior = "UNKNOWN"
    elif wr >= 0.68:
        behavior = "GOOD"
    elif wr >= 0.56:
        behavior = "NORMAL"
    elif wr >= 0.45:
        behavior = "WEAK"
    else:
        behavior = "BAD"

    avg_fav = _weighted_avg(b, "max_favorable_atr_sum", "max_favorable_atr_weight", 0.0)
    avg_adv = _weighted_avg(b, "max_adverse_atr_sum", "max_adverse_atr_weight", 0.0)
    fake = int(b.get("fake_breakouts", 0) or 0)
    clean = int(b.get("clean_breakouts", 0) or 0)

    late_tp = _safe_float(b.get("late_entry_tp"))
    late_sl = _safe_float(b.get("late_entry_sl"))
    late_total = late_tp + late_sl
    early_tp = _safe_float(b.get("early_5m_tp"))
    early_sl = _safe_float(b.get("early_5m_sl"))
    early_total = early_tp + early_sl
    early_wr = early_tp / max(early_total, 1e-9) if early_total else 0.0
    late_wr = late_tp / max(late_total, 1e-9) if late_total else 0.0

    if behavior == "GOOD" and avg_fav >= 1.4:
        personality = "CLEAN_RUNNER"
    elif early_total >= 3 and early_wr >= 0.62:
        personality = "EARLY_TRIGGER_FRIENDLY"
    elif late_total >= 3 and late_wr <= 0.42:
        personality = "LATE_ENTRY_RISK"
    elif avg_adv >= 1.45 and total_w >= 4:
        personality = "WICKY"
    elif fake >= max(3, clean + 2):
        personality = "FAKE_BREAK_RISK"
    elif behavior in {"WEAK", "BAD"}:
        personality = "RISKY_DIRECTION"
    elif total_w >= 5 and (abs(avg_move) < 0.05 or avg_fav < 0.75):
        personality = "LOW_REACH"
    else:
        personality = "NORMAL"

    risk_bias = 0
    if behavior == "BAD":
        risk_bias = 3
    elif behavior == "WEAK":
        risk_bias = 2
    elif behavior == "NORMAL":
        risk_bias = 1 if avg_move < 0 else 0

    b["behavior"] = behavior
    b["personality"] = personality
    b["confidence"] = min(100, int(total_w * 12))
    b["risk_bias"] = risk_bias


def _refresh_coin_archive(s: Dict[str, Any], symbol: str) -> None:
    row = _coin_row(s, symbol)
    sym = str(symbol).upper()
    long_b = s.get("by_coin_direction", {}).get(f"{sym}:LONG", {})
    short_b = s.get("by_coin_direction", {}).get(f"{sym}:SHORT", {})
    row["long"] = {"tp": int(long_b.get("tp1", 0)) + int(long_b.get("tp2", 0)), "sl": int(long_b.get("sl", 0))}
    row["short"] = {"tp": int(short_b.get("tp1", 0)) + int(short_b.get("tp2", 0)), "sl": int(short_b.get("sl", 0))}

    l_total = row["long"]["tp"] + row["long"]["sl"]
    s_total = row["short"]["tp"] + row["short"]["sl"]
    l_wr = row["long"]["tp"] / max(l_total, 1) if l_total else 0
    s_wr = row["short"]["tp"] / max(s_total, 1) if s_total else 0

    if l_total + s_total < 5:
        row["behavior"] = "UNKNOWN"
        row["best_direction"] = "UNKNOWN"
    else:
        if l_wr >= s_wr + 0.12 and l_total >= 3:
            row["behavior"] = "LONG_BIASED"
            row["best_direction"] = "LONG"
        elif s_wr >= l_wr + 0.12 and s_total >= 3:
            row["behavior"] = "SHORT_BIASED"
            row["best_direction"] = "SHORT"
        elif max(l_wr, s_wr) >= 0.58:
            row["behavior"] = "TRADEABLE"
            row["best_direction"] = "BALANCED"
        else:
            row["behavior"] = "CHOPPY_OR_RISKY"
            row["best_direction"] = "UNKNOWN"
    row["confidence"] = min(100, (l_total + s_total) * 8)
    row["last_updated"] = _now()


def _trim_signals(s: Dict[str, Any]) -> None:
    signals = s.get("signals", {})
    if not isinstance(signals, dict) or len(signals) <= MAX_SIGNALS_STORED:
        return
    items = sorted(signals.items(), key=lambda kv: _safe_int(kv[1].get("recorded_at"), 0))
    for sid, _ in items[:max(0, len(items) - MAX_SIGNALS_STORED)]:
        signals.pop(sid, None)


def record_signal(signal: Dict, signal_type: str = "REAL") -> bool:
    s = _state()
    if not isinstance(signal, dict):
        return False
    sid = signal.get("signal_id") or signal.get("id") or f"{signal.get('symbol')}_{_now()}"
    source = _norm_source(signal.get("source"), signal_type)
    item = dict(signal)
    item["signal_id"] = sid
    item["signal_type"] = source
    item["recorded_at"] = _now()
    item["snapshot"] = _compact_snapshot(item.get("snapshot") if isinstance(item.get("snapshot"), dict) else item)
    s["signals"][sid] = item

    b = _bucket(s, item.get("symbol"), item.get("direction"))
    if source == "GHOST":
        b["ghost_total"] = int(b.get("ghost_total", 0)) + 1
    else:
        b["real_total"] = int(b.get("real_total", 0)) + 1
    b["last_updated"] = _now()
    _refresh_coin_archive(s, item.get("symbol"))
    _trim_signals(s)
    s["updated_at"] = _now()
    save_json(LEARNING_FILE, s)
    return True


def update_signal_result(signal_id: str, result: str, exit_price: float = None, move_percent: float = None, snapshot: Optional[Dict[str, Any]] = None, source: str = None, **kwargs) -> bool:
    s = _state()
    sid = str(signal_id or "")
    sig = s.get("signals", {}).get(sid)

    # If signal was not recorded before (some old paths), create a minimal
    # signal from snapshot so learning is not silently lost.
    if not isinstance(sig, dict):
        snap = snapshot if isinstance(snapshot, dict) else {}
        if not snap.get("symbol") or not snap.get("direction"):
            return False
        sig = {
            "signal_id": sid or f"{snap.get('symbol')}_{snap.get('direction')}_{_now()}",
            "symbol": snap.get("symbol"),
            "direction": snap.get("direction"),
            "entry": snap.get("entry") or snap.get("price"),
            "price": snap.get("price") or snap.get("entry"),
            "signal_type": _norm_source(source or snap.get("result_source")),
            "snapshot": _compact_snapshot(snap),
            "recorded_at": _now(),
        }
        sid = sig["signal_id"]
        s["signals"][sid] = sig

    source_norm = _norm_source(source or sig.get("signal_type") or (snapshot or {}).get("result_source"))
    r = _norm_result(result)
    ts = _now()
    snap = _compact_snapshot(snapshot if isinstance(snapshot, dict) else sig.get("snapshot", {}))
    # Store cumulative counters, but apply Mode-B recency weight to learning
    # influence so fresh market behavior matters most.
    weight = _learning_weight(source_norm, sig.get("recorded_at") or (snapshot or {}).get("snapshot_at") or ts)

    sig.update({
        "result": r,
        "exit_price": exit_price,
        "move_percent": move_percent,
        "closed_at": ts,
        "signal_type": source_norm,
        "result_snapshot": snap,
    })

    b = _bucket(s, sig.get("symbol"), sig.get("direction"))
    fast_flags = _fast_entry_flags(snap, sig.get("direction"))
    event = {
        "ts": ts,
        "result": r,
        "source": source_norm,
        "exit_price": exit_price,
        "move_percent": move_percent,
        "snapshot": snap,
        "entry_quality": fast_flags,
        "learning_weight": weight,
        "time_weight": _time_decay_weight(ts),
    }
    b.setdefault("events", []).append(event)
    b["events"] = b["events"][-MAX_EVENTS_PER_BUCKET:]

    if r in {"TP1", "TP2"}:
        if r == "TP1":
            b["tp1"] = int(b.get("tp1", 0)) + 1
        else:
            b["tp2"] = int(b.get("tp2", 0)) + 1
        if source_norm == "GHOST":
            b["ghost_tp"] = int(b.get("ghost_tp", 0)) + 1
        else:
            b["real_tp"] = int(b.get("real_tp", 0)) + 1
        b["weighted_tp"] = _safe_float(b.get("weighted_tp")) + weight
        b.setdefault("tp_patterns", []).append(event)
        b["tp_patterns"] = b["tp_patterns"][-MAX_PATTERNS_PER_BUCKET:]
    elif r == "SL":
        b["sl"] = int(b.get("sl", 0)) + 1
        if source_norm == "GHOST":
            b["ghost_sl"] = int(b.get("ghost_sl", 0)) + 1
        else:
            b["real_sl"] = int(b.get("real_sl", 0)) + 1
        b["weighted_sl"] = _safe_float(b.get("weighted_sl")) + weight
        b.setdefault("sl_patterns", []).append(event)
        b["sl_patterns"] = b["sl_patterns"][-MAX_PATTERNS_PER_BUCKET:]

    # Learn whether fast-entry features actually lead to TP or SL.
    try:
        if fast_flags.get("early_5m_active"):
            if r in {"TP1", "TP2"}:
                b["early_5m_tp"] = _safe_float(b.get("early_5m_tp")) + weight
            elif r == "SL":
                b["early_5m_sl"] = _safe_float(b.get("early_5m_sl")) + weight
        if fast_flags.get("multi_tf_active"):
            if r in {"TP1", "TP2"}:
                b["multi_tf_tp"] = _safe_float(b.get("multi_tf_tp")) + weight
            elif r == "SL":
                b["multi_tf_sl"] = _safe_float(b.get("multi_tf_sl")) + weight
        if fast_flags.get("late_entry"):
            if r in {"TP1", "TP2"}:
                b["late_entry_tp"] = _safe_float(b.get("late_entry_tp")) + weight
            elif r == "SL":
                b["late_entry_sl"] = _safe_float(b.get("late_entry_sl")) + weight
        if fast_flags.get("rr_filter"):
            b["rr_filter_count"] = int(b.get("rr_filter_count", 0) or 0) + 1

        _update_binary_quality_stat(b["entry_quality_stats"], "early_5m_active", fast_flags.get("early_5m_active"), weight, r)
        _update_binary_quality_stat(b["entry_quality_stats"], "multi_tf_active", fast_flags.get("multi_tf_active"), weight, r)
        _update_binary_quality_stat(b["entry_quality_stats"], "late_entry", fast_flags.get("late_entry"), weight, r)
    except Exception:
        pass

    mp = _safe_float(move_percent, 0.0)
    b["move_sum"] = _safe_float(b.get("move_sum")) + mp
    b["weighted_move_sum"] = _safe_float(b.get("weighted_move_sum")) + mp * weight

    try:
        entry = _safe_float(sig.get("entry") or sig.get("price") or snap.get("entry") or snap.get("price"), 0.0)
        ex = _safe_float(exit_price, 0.0)
        atr = _safe_float(snap.get("atr"), 0.0)
        if r in {"TP1", "TP2"} and ex > 0 and entry > 0:
            dist = abs(ex - entry)
            b["tp_distance_sum"] = _safe_float(b.get("tp_distance_sum")) + dist
            b["tp_distance_weighted_sum"] = _safe_float(b.get("tp_distance_weighted_sum")) + dist * weight
            b["tp_distance_weight"] = _safe_float(b.get("tp_distance_weight")) + weight
            if atr > 0:
                b["atr_tp_ratio_sum"] = _safe_float(b.get("atr_tp_ratio_sum")) + (dist / atr) * weight
                b["atr_tp_ratio_weight"] = _safe_float(b.get("atr_tp_ratio_weight")) + weight
    except Exception:
        pass

    # TP/SL v2 coordinated learning.
    # This records how far this coin+direction usually moves before TP/SL,
    # how much adverse wick it needs to survive, and SR/fake-breakout behavior.
    try:
        m = _tp_sl_v2_snapshot_metrics(sig, snap, r, exit_price, weight, kwargs)
        if m.get("tp1_atr") is not None:
            _add_weighted(b, "tp1_reach_atr_sum", "tp1_reach_atr_weight", m.get("tp1_atr"), weight)
        if m.get("tp2_atr") is not None:
            _add_weighted(b, "tp2_reach_atr_sum", "tp2_reach_atr_weight", m.get("tp2_atr"), weight)
        if m.get("sl_atr") is not None:
            _add_weighted(b, "sl_distance_atr_sum", "sl_distance_atr_weight", m.get("sl_atr"), weight)
        if m.get("max_favorable_atr") is not None:
            _add_weighted(b, "max_favorable_atr_sum", "max_favorable_atr_weight", m.get("max_favorable_atr"), weight)
        if m.get("max_adverse_atr") is not None:
            _add_weighted(b, "max_adverse_atr_sum", "max_adverse_atr_weight", m.get("max_adverse_atr"), weight)

        sr_event = str(kwargs.get("sr_event") or snap.get("sr_event") or "").upper()
        if kwargs.get("fake_breakout") is True or sr_event in {"FAKE_BREAK", "FAKE_BREAKOUT"}:
            b["fake_breakouts"] = int(b.get("fake_breakouts", 0)) + 1
        elif sr_event in {"CLEAN_BREAK", "BREAKOUT"}:
            b["clean_breakouts"] = int(b.get("clean_breakouts", 0)) + 1
        elif sr_event in {"BOUNCE", "SR_BOUNCE"}:
            b["bounces"] = int(b.get("bounces", 0)) + 1

        b["tp_sl_v2"] = {
            "version": TP_SL_V2_VERSION,
            "samples": int(b.get("tp1", 0)) + int(b.get("tp2", 0)) + int(b.get("sl", 0)),
            "avg_tp1_atr": round(_weighted_avg(b, "tp1_reach_atr_sum", "tp1_reach_atr_weight", 0.0), 4),
            "avg_tp2_atr": round(_weighted_avg(b, "tp2_reach_atr_sum", "tp2_reach_atr_weight", 0.0), 4),
            "avg_sl_atr": round(_weighted_avg(b, "sl_distance_atr_sum", "sl_distance_atr_weight", 0.0), 4),
            "avg_max_favorable_atr": round(_weighted_avg(b, "max_favorable_atr_sum", "max_favorable_atr_weight", 0.0), 4),
            "avg_max_adverse_atr": round(_weighted_avg(b, "max_adverse_atr_sum", "max_adverse_atr_weight", 0.0), 4),
            "fake_breakouts": int(b.get("fake_breakouts", 0)),
            "clean_breakouts": int(b.get("clean_breakouts", 0)),
            "bounces": int(b.get("bounces", 0)),
        }
    except Exception:
        pass

    # Conditional AI learning: every coin+direction learns its own indicator,
    # state, trap, time, prediction, and meta-layer outcomes.
    try:
        _learn_condition_matrix(b, snap, weight, r, move_percent)
        _learn_time_behavior(b, snap, weight, r, move_percent)
        _learn_meta_layer(b, snap, weight, r, move_percent)
    except Exception:
        pass

    for name in [
        "rsi", "rsi_5m", "rsi_slope_15m", "macd_hist", "macd_hist_slope_15m",
        "macd_hist_accel_15m", "adx", "adx_slope_15m", "vwap_distance_pct",
        "volume_ratio_15m", "prediction_score", "expected_move_atr", "reversal_risk_score",
        "power2_buy", "power2_sell", "power3_buy", "power3_sell", "buy_power", "sell_power"
    ]:
        _update_avg_stat(b["indicator_stats"], name, snap.get(name), weight, r)
    for name in ["market_regime", "market_mode", "btc_bias", "vwap_status", "move_state", "trap_risk", "relative_status", "ema_structure_15m"]:
        _update_market_stat(b["market_stats"], name, snap.get(name), weight, r)

    _recompute_bucket_behavior(b)
    b["last_updated"] = ts
    _refresh_coin_archive(s, sig.get("symbol"))
    _trim_signals(s)
    s["updated_at"] = ts
    save_json(LEARNING_FILE, s)
    return True


def _avg_tp_distance(b: Dict[str, Any]) -> float:
    w = _safe_float(b.get("tp_distance_weight"))
    if w > 0:
        return _safe_float(b.get("tp_distance_weighted_sum")) / max(w, 1e-9)
    wins = int(b.get("tp1", 0)) + int(b.get("tp2", 0))
    if wins > 0:
        return _safe_float(b.get("tp_distance_sum")) / max(wins, 1)
    return 0.0


def get_smart_tp_suggestion(symbol: str, direction: str, snapshot: Optional[Dict] = None) -> Dict:
    s = _state()
    b = s.get("by_coin_direction", {}).get(_key(symbol, direction), {})
    wins = int(b.get("tp1", 0)) + int(b.get("tp2", 0))
    weighted_tp = _safe_float(b.get("weighted_tp"))
    if wins < MIN_TP_MEMORY_WINS and weighted_tp < 2.0:
        return {}

    price = _safe_float((snapshot or {}).get("price") or (snapshot or {}).get("entry"), 0.0)
    atr = _safe_float((snapshot or {}).get("atr"), 0.0)
    avg_dist = _avg_tp_distance(b)
    avg_tp1_atr = _weighted_avg(b, "tp1_reach_atr_sum", "tp1_reach_atr_weight", 0.0)
    avg_tp2_atr = _weighted_avg(b, "tp2_reach_atr_sum", "tp2_reach_atr_weight", 0.0)
    avg_fav_atr = _weighted_avg(b, "max_favorable_atr_sum", "max_favorable_atr_weight", 0.0)
    if price <= 0:
        return {}

    if atr > 0 and avg_tp1_atr > 0:
        avg_dist = atr * avg_tp1_atr
    elif atr > 0 and avg_fav_atr > 0:
        avg_dist = atr * max(0.55, min(1.8, avg_fav_atr * 0.72))

    # Prefer learned average distance, but keep it bounded by ATR/price so TP
    # does not become absurdly tiny or too far for fast trades.
    min_dist = max(price * 0.0015, atr * 0.45 if atr > 0 else 0.0)
    max_dist = max(price * 0.012, atr * 2.2 if atr > 0 else 0.0)
    if avg_dist <= 0 and atr > 0:
        ratio_w = _safe_float(b.get("atr_tp_ratio_weight"))
        ratio = _safe_float(b.get("atr_tp_ratio_sum")) / max(ratio_w, 1e-9) if ratio_w else 0.0
        avg_dist = atr * max(0.55, min(1.8, ratio or 0.9))
    if avg_dist <= 0:
        return {}

    dist1 = max(min_dist, min(max_dist, avg_dist * 0.90))
    if atr > 0 and avg_tp2_atr > 0:
        dist2 = max(dist1 * 1.35, min(max_dist * 1.8, atr * avg_tp2_atr))
    else:
        dist2 = max(dist1 * 1.35, min(max_dist * 1.6, avg_dist * 1.55))
    confidence = "high" if _safe_float(b.get("confidence")) >= 60 else "medium"

    if str(direction).upper() == "LONG":
        return {"tp1": price + dist1, "tp2": price + dist2, "confidence": confidence, "source": "AI_TP_MEMORY"}
    return {"tp1": price - dist1, "tp2": price - dist2, "confidence": confidence, "source": "AI_TP_MEMORY"}


def _similar_sl_pressure(b: Dict[str, Any], snapshot: Optional[Dict[str, Any]]) -> int:
    if not isinstance(snapshot, dict):
        return 0
    sl_patterns = b.get("sl_patterns", [])[-30:]
    if not sl_patterns:
        return 0
    pressure = 0
    rsi = _safe_float(snapshot.get("rsi"), None)
    adx = _safe_float(snapshot.get("adx"), None)
    vwap = snapshot.get("vwap_status")
    market = snapshot.get("market_regime") or snapshot.get("market_mode")
    for ev in sl_patterns:
        sp = ev.get("snapshot", {}) if isinstance(ev, dict) else {}
        score = 0
        if rsi is not None and sp.get("rsi") is not None and abs(_safe_float(sp.get("rsi")) - rsi) <= 5:
            score += 1
        if adx is not None and sp.get("adx") is not None and abs(_safe_float(sp.get("adx")) - adx) <= 6:
            score += 1
        if vwap and sp.get("vwap_status") == vwap:
            score += 1
        if market and (sp.get("market_regime") == market or sp.get("market_mode") == market):
            score += 1
        if score >= 3:
            pressure += 1
    return min(3, pressure)



# ---------------------------------------------------------------------------
# Historical Similarity Engine
# ---------------------------------------------------------------------------
# This is the active use of stored snapshots:
# current snapshot -> compare with past result snapshots -> TP/SL probability,
# average move/MFE/MAE, and a soft adjustment for AI ranking/strictness.
def _extract_nested_value(snapshot: Dict[str, Any], key: str, default: Any = None) -> Any:
    if not isinstance(snapshot, dict):
        return default
    if snapshot.get(key) is not None:
        return snapshot.get(key)
    for nest in ("prediction_layer", "state_awareness", "liquidity_trap", "relative_strength", "candle_behavior", "market_context"):
        obj = snapshot.get(nest)
        if isinstance(obj, dict) and obj.get(key) is not None:
            return obj.get(key)
    return default


def _similarity_numeric(a: Any, b: Any, tolerance: float) -> float:
    av = _safe_float(a, None)
    bv = _safe_float(b, None)
    if av is None or bv is None:
        return 0.0
    tolerance = max(float(tolerance or 1.0), 1e-9)
    return max(0.0, 1.0 - min(1.0, abs(av - bv) / tolerance))


def _similarity_category(a: Any, b: Any) -> float:
    if a is None or b is None:
        return 0.0
    return 1.0 if str(a).upper() == str(b).upper() else 0.0


def _snapshot_similarity(current: Dict[str, Any], past: Dict[str, Any]) -> float:
    """Weighted similarity between two compact technical snapshots.

    The tolerance values are intentionally broad; this is not exact matching.
    Goal: find historically similar market/indicator states without making AI
    too strict in the early data phase.
    """
    if not isinstance(current, dict) or not isinstance(past, dict):
        return 0.0

    numeric_specs = [
        ("rsi", 8.0, 1.10),
        ("rsi_5m", 10.0, 0.65),
        ("rsi_slope_15m", 3.0, 0.75),
        ("adx", 8.0, 1.00),
        ("adx_slope_15m", 3.0, 0.75),
        ("macd_hist", 0.0015, 0.80),
        ("macd_hist_slope_15m", 0.0015, 0.80),
        ("macd_hist_accel_15m", 0.0015, 0.90),
        ("vwap_distance_pct", 0.45, 0.95),
        ("volume_ratio_15m", 0.75, 0.65),
        ("power2_buy", 18.0, 0.75),
        ("power2_sell", 18.0, 0.75),
        ("power3_buy", 16.0, 0.85),
        ("power3_sell", 16.0, 0.85),
        ("buy_power", 14.0, 0.55),
        ("sell_power", 14.0, 0.55),
        ("prediction_score", 18.0, 0.95),
        ("reversal_risk_score", 20.0, 0.90),
        ("expected_move_atr", 0.70, 0.65),
    ]
    category_specs = [
        ("vwap_status", 0.90),
        ("ema_structure_15m", 0.80),
        ("market_regime", 0.85),
        ("market_mode", 0.65),
        ("btc_bias", 0.90),
        ("move_state", 1.00),
        ("trap_risk", 0.95),
        ("relative_status", 0.55),
        ("timeframe_core", 0.25),
        ("entry_timing_tf", 0.25),
    ]

    total_w = 0.0
    score = 0.0
    for key, tolerance, weight in numeric_specs:
        c = _extract_nested_value(current, key)
        p = _extract_nested_value(past, key)
        if c is None or p is None:
            continue
        total_w += weight
        score += _similarity_numeric(c, p, tolerance) * weight

    for key, weight in category_specs:
        c = _extract_nested_value(current, key)
        p = _extract_nested_value(past, key)
        if c is None or p is None:
            continue
        total_w += weight
        score += _similarity_category(c, p) * weight

    # Direction-specific nested packs: early trigger / late entry are important
    # but should not dominate the whole comparison.
    try:
        direction = str(current.get("direction") or past.get("direction") or "").upper()
        c_flags = _fast_entry_flags(current, direction)
        p_flags = _fast_entry_flags(past, direction)
        for key, weight in [("early_5m_active", 0.55), ("multi_tf_active", 0.45), ("late_entry", 0.70), ("rr_filter", 0.40)]:
            total_w += weight
            score += (1.0 if bool(c_flags.get(key)) == bool(p_flags.get(key)) else 0.0) * weight
    except Exception:
        pass

    if total_w <= 0:
        return 0.0
    return round(max(0.0, min(1.0, score / total_w)), 4)


def _event_similarity_weight(event: Dict[str, Any], similarity: float) -> float:
    src = _norm_source(event.get("source"))
    base = SIMILARITY_GHOST_WEIGHT if src == "GHOST" else SIMILARITY_REAL_WEIGHT
    recency = _time_decay_weight(event.get("ts"))
    # More similar and more recent cases matter disproportionately more than
    # barely similar / stale cases, but keep every old event as weak archive data.
    return base * recency * max(0.05, min(1.0, similarity)) ** 2


def _event_move_atr(event: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    snap = event.get("snapshot", {}) if isinstance(event, dict) else {}
    if not isinstance(snap, dict):
        return None, None
    atr = _safe_float(snap.get("atr"), 0.0)
    entry = _safe_float(snap.get("entry") or snap.get("price"), 0.0)
    if atr <= 0 and entry > 0:
        atr = entry * 0.0015
    if atr <= 0:
        return None, None
    fav = _safe_float(event.get("max_favorable") or snap.get("max_favorable") or snap.get("max_favorable_atr"), None)
    adv = _safe_float(event.get("max_adverse") or snap.get("max_adverse") or snap.get("max_adverse_atr"), None)
    return fav, adv


def find_similar_patterns(symbol: str, direction: str, snapshot: Optional[Dict[str, Any]] = None, limit: int = SIMILARITY_MAX_MATCHES, min_similarity: float = SIMILARITY_MIN_SCORE) -> Dict[str, Any]:
    """Find historically similar TP/SL snapshots for symbol+direction.

    Read-only and safe for scanner/slot_manager. It uses stored bucket events
    and returns a compact profile:
      - match_count / confidence
      - TP/SL weighted win-rate
      - average move and MFE/MAE when available
      - soft adjustment score for AI ranking
    """
    current = _compact_snapshot(build_signal_snapshot(symbol, direction, snapshot or {}, (snapshot or {}).get("market_context") if isinstance(snapshot, dict) else None))
    s = _state()
    b = s.get("by_coin_direction", {}).get(_key(symbol, direction), {})
    if not isinstance(b, dict) or not b:
        return {"available": False, "reason": "NO_BUCKET", "match_count": 0, "confidence": 0, "adjustment": 0.0, "source": "historical_similarity"}

    events = b.get("events", [])
    if not isinstance(events, list) or not events:
        return {"available": False, "reason": "NO_EVENTS", "match_count": 0, "confidence": 0, "adjustment": 0.0, "source": "historical_similarity"}

    matches: List[Dict[str, Any]] = []
    for ev in events[-MAX_EVENTS_PER_BUCKET:]:
        if not isinstance(ev, dict):
            continue
        result = _norm_result(ev.get("result"))
        if result not in {"TP1", "TP2", "SL"}:
            continue
        ps = ev.get("snapshot", {})
        if not isinstance(ps, dict):
            continue
        sim = _snapshot_similarity(current, ps)
        if sim >= float(min_similarity):
            row = {
                "similarity": sim,
                "result": result,
                "source": _norm_source(ev.get("source")),
                "move_percent": _safe_float(ev.get("move_percent"), 0.0),
                "ts": _safe_int(ev.get("ts"), 0),
                "weight": _event_similarity_weight(ev, sim),
            }
            fav, adv = _event_move_atr(ev)
            if fav is not None:
                row["max_favorable_atr"] = fav
            if adv is not None:
                row["max_adverse_atr"] = adv
            matches.append(row)

    matches.sort(key=lambda x: (x.get("similarity", 0), x.get("ts", 0)), reverse=True)
    matches = matches[:max(1, int(limit or SIMILARITY_MAX_MATCHES))]

    if not matches:
        return {"available": False, "reason": "NO_SIMILAR_MATCH", "match_count": 0, "confidence": 0, "adjustment": 0.0, "source": "historical_similarity"}

    tp_w = sl_w = total_w = 0.0
    move_sum = move_w = 0.0
    fav_sum = fav_w = 0.0
    adv_sum = adv_w = 0.0
    for m in matches:
        w = _safe_float(m.get("weight"), 0.0)
        if w <= 0:
            continue
        total_w += w
        if m.get("result") in {"TP1", "TP2"}:
            tp_w += w
        elif m.get("result") == "SL":
            sl_w += w
        if m.get("move_percent") is not None:
            move_sum += _safe_float(m.get("move_percent"), 0.0) * w
            move_w += w
        if m.get("max_favorable_atr") is not None:
            fav_sum += _safe_float(m.get("max_favorable_atr"), 0.0) * w
            fav_w += w
        if m.get("max_adverse_atr") is not None:
            adv_sum += _safe_float(m.get("max_adverse_atr"), 0.0) * w
            adv_w += w

    wr = tp_w / max(tp_w + sl_w, 1e-9)
    avg_similarity = sum(_safe_float(m.get("similarity")) for m in matches) / max(len(matches), 1)
    confidence = min(100, int(len(matches) * 10 + total_w * 8 + avg_similarity * 18))

    # Soft rank effect only. Low sample profiles are deliberately weak.
    if len(matches) < SIMILARITY_MIN_SAMPLES:
        adjustment = 0.0
        verdict = "LOW_DATA"
    else:
        adjustment = (wr - 0.50) * 18.0
        if wr >= 0.68:
            verdict = "FAVORABLE"
        elif wr <= 0.42:
            verdict = "RISKY"
        else:
            verdict = "MIXED"
        # If similar cases moved profitably but not far, avoid over-bonus.
        avg_move = move_sum / max(move_w, 1e-9) if move_w else 0.0
        if wr >= 0.60 and avg_move < 0.10:
            adjustment -= 1.5
        adjustment = max(-8.0, min(8.0, adjustment))

    return {
        "available": len(matches) >= SIMILARITY_MIN_SAMPLES,
        "source": "historical_similarity",
        "match_count": len(matches),
        "weighted_samples": round(total_w, 3),
        "confidence": confidence,
        "win_rate": round(wr * 100.0, 1),
        "tp_weight": round(tp_w, 3),
        "sl_weight": round(sl_w, 3),
        "avg_similarity": round(avg_similarity, 4),
        "avg_move_percent": round(move_sum / max(move_w, 1e-9), 4) if move_w else None,
        "avg_max_favorable_atr": round(fav_sum / max(fav_w, 1e-9), 4) if fav_w else None,
        "avg_max_adverse_atr": round(adv_sum / max(adv_w, 1e-9), 4) if adv_w else None,
        "adjustment": round(adjustment, 3),
        "verdict": verdict,
        "top_matches": matches[:8],
    }


def get_similarity_adjustment(symbol: str, direction: str, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Public helper for scanner/slot_manager.

    Returns the same profile plus explicit rank/strictness fields.
    """
    profile = find_similar_patterns(symbol, direction, snapshot)
    adj = _safe_float(profile.get("adjustment"), 0.0)
    profile["rank_adjustment"] = adj
    profile["extra_strength_score"] = 0 if adj >= 0 else min(6, int(abs(adj) // 2 + 1))
    profile["extra_confirmations"] = 1 if profile.get("available") and profile.get("verdict") == "RISKY" and _safe_float(profile.get("win_rate"), 50.0) <= 40 else 0
    return profile


def should_require_extra_strength(symbol: str, direction: str, snapshot: Optional[Dict] = None) -> Dict:
    s = _state()
    b = s.get("by_coin_direction", {}).get(_key(symbol, direction), {})
    if not b:
        return {"required": False, "extra_score": 0, "extra_confirmations": 0}

    tw_outcomes = _bucket_time_weighted_outcomes(b)
    tp = _safe_float(tw_outcomes.get("tp"), _safe_float(b.get("weighted_tp")))
    sl = _safe_float(tw_outcomes.get("sl"), _safe_float(b.get("weighted_sl")))
    total = tp + sl
    wr = tp / max(total, 1e-9) if total else 0.0
    risk_bias = _safe_int(b.get("risk_bias"), 0)
    similar_sl = _similar_sl_pressure(b, snapshot)
    fast_flags = _fast_entry_flags(snapshot or {}, direction)

    extra_score = 0
    extra_conf = 0
    reasons: List[str] = []

    if total >= 3 and wr < 0.45:
        extra_score += 4
        extra_conf += 1
        reasons.append("وین‌ریت ضعیف در این کوین/جهت")
    elif total >= 5 and wr < 0.55:
        extra_score += 2
        reasons.append("عملکرد متوسط رو به ضعیف در این کوین/جهت")

    if risk_bias >= 2:
        extra_score += min(5, risk_bias * 2)
        extra_conf += 1
        reasons.append(f"رفتار AI={b.get('behavior','UNKNOWN')}")

    if similar_sl:
        extra_score += similar_sl * 2
        extra_conf += 1 if similar_sl >= 2 else 0
        reasons.append("شباهت به الگوهای قبلی SL")

    # Conditional weakness learned per coin+direction+condition.
    try:
        cond_stats = b.get("condition_stats", {}) if isinstance(b.get("condition_stats"), dict) else {}
        adx_bin = _bucketed_number((snapshot or {}).get("adx"), 4, min_value=0, max_value=80)
        vwap = str((snapshot or {}).get("vwap_status") or "NA").upper()
        d = str(direction or "").upper()
        weak_keys = [f"{d}:ADX:{adx_bin}", f"{d}:ADX_VWAP:{adx_bin}:{vwap}"]
        weak_hits = 0
        for wk in weak_keys:
            row = cond_stats.get(wk, {})
            tpw = _safe_float(row.get("tp_w"))
            slw = _safe_float(row.get("sl_w"))
            if tpw + slw >= 3 and slw > tpw:
                weak_hits += 1
        if weak_hits:
            extra_score += min(4, weak_hits * 2)
            reasons.append("شرط مشابه قبلاً SL بیشتری داده")
    except Exception:
        pass

    # Fast-entry learning: reward/penalize based on this coin+direction history.
    early_tp = _safe_float(b.get("early_5m_tp"))
    early_sl = _safe_float(b.get("early_5m_sl"))
    early_total = early_tp + early_sl
    early_wr = early_tp / max(early_total, 1e-9) if early_total else 0.0
    late_tp = _safe_float(b.get("late_entry_tp"))
    late_sl = _safe_float(b.get("late_entry_sl"))
    late_total = late_tp + late_sl
    late_wr = late_tp / max(late_total, 1e-9) if late_total else 0.0

    if fast_flags.get("early_5m_active") and early_total >= 5 and early_wr < 0.45:
        extra_score += 3
        reasons.append("Early 5M قبلاً ضعیف بوده")
    if fast_flags.get("late_entry") and late_total >= 3 and late_wr < 0.50:
        extra_score += 5
        extra_conf += 1
        reasons.append("Late Entry قبلاً پرریسک بوده")

    # Historical similarity: compare the current snapshot with prior TP/SL
    # snapshots for this exact coin+direction. This is a soft learning layer;
    # it increases required strength only when similar past cases were risky.
    try:
        sim_profile = get_similarity_adjustment(symbol, direction, snapshot or {})
        if sim_profile.get("available"):
            wr_sim = _safe_float(sim_profile.get("win_rate"), 50.0)
            adj_sim = _safe_float(sim_profile.get("rank_adjustment"), 0.0)
            if wr_sim <= 42 or adj_sim <= -4:
                extra_score += min(5, int(abs(adj_sim)) if adj_sim < 0 else 3)
                extra_conf += 1 if wr_sim <= 38 else 0
                reasons.append("شباهت تاریخی به نمونه‌های پرریسک")
            elif wr_sim >= 68 and adj_sim >= 3:
                # Favorable similarity should reduce only the added strictness,
                # never bypass base technical/risk rules.
                extra_score = max(0, extra_score - 2)
                reasons.append("شباهت تاریخی مثبت")
    except Exception:
        pass

    # Keep this module adaptive but not over-restrictive. coin_risk.py already
    # handles hard daily/long-term risk. This layer focuses on learned quality.
    extra_score = min(10, extra_score)
    extra_conf = min(2, extra_conf)
    required = extra_score > 0 or extra_conf > 0
    reason = "AI Learning: " + "، ".join(reasons[:3]) if reasons else None
    return {"required": required, "extra_score": extra_score, "extra_confirmations": extra_conf, "reason": reason}


def get_tp_sl_v2_profile(symbol: str, direction: str, snapshot: Optional[Dict] = None) -> Dict[str, Any]:
    """Return compact TP/SL v2 profile for analysis/scanner.

    Safe read-only helper. If there is not enough data, it returns neutral
    values so old behavior remains unchanged.
    """
    s = _state()
    b = s.get("by_coin_direction", {}).get(_key(symbol, direction), {})
    if not isinstance(b, dict) or not b:
        return {
            "available": False,
            "confidence": 0,
            "personality": "UNKNOWN",
            "tp1_atr": None,
            "tp2_atr": None,
            "sl_atr": None,
            "fake_break_rate": 0.0,
            "source": "coin_learning_v2",
        }

    samples = int(b.get("tp1", 0)) + int(b.get("tp2", 0)) + int(b.get("sl", 0))
    fake = int(b.get("fake_breakouts", 0) or 0)
    clean = int(b.get("clean_breakouts", 0) or 0)
    bounces = int(b.get("bounces", 0) or 0)
    sr_total = max(1, fake + clean + bounces)

    return {
        "available": samples >= 3,
        "confidence": int(b.get("confidence", 0) or 0),
        "samples": samples,
        "behavior": b.get("behavior", "UNKNOWN"),
        "personality": b.get("personality", "UNKNOWN"),
        "tp1_atr": _weighted_avg(b, "tp1_reach_atr_sum", "tp1_reach_atr_weight", None),
        "tp2_atr": _weighted_avg(b, "tp2_reach_atr_sum", "tp2_reach_atr_weight", None),
        "sl_atr": _weighted_avg(b, "sl_distance_atr_sum", "sl_distance_atr_weight", None),
        "max_favorable_atr": _weighted_avg(b, "max_favorable_atr_sum", "max_favorable_atr_weight", None),
        "max_adverse_atr": _weighted_avg(b, "max_adverse_atr_sum", "max_adverse_atr_weight", None),
        "fake_break_rate": round(fake / sr_total, 4),
        "early_5m_win_rate": round(_safe_float(b.get("early_5m_tp")) / max(_safe_float(b.get("early_5m_tp")) + _safe_float(b.get("early_5m_sl")), 1e-9), 4) if (_safe_float(b.get("early_5m_tp")) + _safe_float(b.get("early_5m_sl"))) > 0 else None,
        "late_entry_win_rate": round(_safe_float(b.get("late_entry_tp")) / max(_safe_float(b.get("late_entry_tp")) + _safe_float(b.get("late_entry_sl")), 1e-9), 4) if (_safe_float(b.get("late_entry_tp")) + _safe_float(b.get("late_entry_sl"))) > 0 else None,
        "entry_quality_stats": b.get("entry_quality_stats", {}),
        "condition_stats": b.get("condition_stats", {}),
        "time_stats": b.get("time_stats", {}),
        "meta_learning": b.get("meta_learning", {}),
        "dynamic_profit_stats": b.get("dynamic_profit_stats", {}),
        "similarity_profile": get_similarity_adjustment(symbol, direction, snapshot or {}) if snapshot else {"available": False, "source": "historical_similarity"},
        "clean_breakouts": clean,
        "bounces": bounces,
        "source": "coin_learning_v2",
    }


def register_tp_sl_v2_result(symbol: str, direction: str, result: str, entry: float = None, stop_loss: float = None, tp1: float = None, tp2: float = None, snapshot: Optional[Dict[str, Any]] = None, source: str = "REAL", max_favorable: float = None, max_adverse: float = None, sr_event: str = None, fake_breakout: bool = None, signal_id: str = None, **kwargs) -> bool:
    """Compatibility hook for real_trade_manager/ghost_signals.

    It routes TP/SL v2 learning through update_signal_result so all existing
    learning summaries, Smart TP memory, and behavior logic stay consistent.
    """
    snap = dict(snapshot or {})
    snap.setdefault("symbol", str(symbol).upper())
    snap.setdefault("direction", str(direction).upper())
    if entry is not None:
        snap.setdefault("entry", entry)
        snap.setdefault("price", entry)
    if stop_loss is not None:
        snap.setdefault("stop_loss", stop_loss)
    if tp1 is not None:
        snap.setdefault("tp1", tp1)
    if tp2 is not None:
        snap.setdefault("tp2", tp2)
    if sr_event is not None:
        snap.setdefault("sr_event", sr_event)

    sid = signal_id or snap.get("signal_id") or f"{str(symbol).upper()}_{str(direction).upper()}_{_now()}"
    exit_price = kwargs.get("exit_price")
    if exit_price is None:
        if _norm_result(result) == "SL":
            exit_price = stop_loss
        elif _norm_result(result) == "TP2":
            exit_price = tp2
        else:
            exit_price = tp1

    return update_signal_result(
        sid,
        result,
        exit_price=exit_price,
        move_percent=kwargs.get("move_percent"),
        snapshot=snap,
        source=source,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        max_favorable=max_favorable,
        max_adverse=max_adverse,
        sr_event=sr_event,
        fake_breakout=fake_breakout,
    )



def register_dynamic_profit_exit(symbol: str, direction: str, entry: float, exit_price: float, snapshot: Optional[Dict[str, Any]] = None, reason: str = None, source: str = "REAL", signal_id: str = None, max_favorable: float = None, max_adverse: float = None, **kwargs) -> bool:
    """Special high-priority Trade Management AI learning hook.

    Use when the manager exits a LONG or SHORT early while already in profit
    because continuation probability dropped and reversal risk rose. It is
    counted as a TP-side win, but the reason is stored separately so Meta
    Learning can later detect whether exits were good or premature.
    """
    snap = dict(snapshot or {})
    snap.setdefault("symbol", str(symbol).upper())
    snap.setdefault("direction", str(direction).upper())
    snap.setdefault("entry", entry)
    snap.setdefault("price", entry)
    snap["exit_reason"] = reason or "DYNAMIC_PROFIT_PROTECTION"
    snap["early_profit_exit"] = True
    snap["dynamic_profit_protection"] = {
        "enabled": True,
        "reason": snap["exit_reason"],
        "exit_price": exit_price,
        "applies_to": "LONG_AND_SHORT",
    }

    # Move percent is signed in trade direction.
    e = _safe_float(entry, 0.0)
    x = _safe_float(exit_price, 0.0)
    if e > 0 and x > 0:
        if str(direction).upper() == "SHORT":
            move_percent = ((e - x) / e) * 100.0
        else:
            move_percent = ((x - e) / e) * 100.0
    else:
        move_percent = kwargs.get("move_percent")

    ok = update_signal_result(
        signal_id or snap.get("signal_id") or f"{str(symbol).upper()}_{str(direction).upper()}_AI_PROFIT_{_now()}",
        "EARLY_PROFIT",
        exit_price=exit_price,
        move_percent=move_percent,
        snapshot=snap,
        source=source,
        max_favorable=max_favorable,
        max_adverse=max_adverse,
    )

    # Store a compact reason counter per coin+direction.
    try:
        s = _state()
        b = _bucket(s, symbol, direction)
        dp = b.setdefault("dynamic_profit_stats", {"profit_exits": 0, "good_exits": 0, "premature_exits": 0, "reasons": {}})
        dp["profit_exits"] = int(dp.get("profit_exits", 0) or 0) + 1
        rs = str(reason or "DYNAMIC_PROFIT_PROTECTION").upper()[:80]
        dp.setdefault("reasons", {})
        dp["reasons"][rs] = int(dp["reasons"].get(rs, 0) or 0) + 1
        b["last_updated"] = _now()
        s["updated_at"] = _now()
        save_json(LEARNING_FILE, s)
    except Exception:
        pass
    return bool(ok)


def _display_learning_counts(s: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """Display-safe REAL/GHOST counts from the actual signals table.

    Bucket totals are useful for long-term learning, but they can drift after
    migrations or older Ghost records that were misclassified.  Reports should
    show the same accounting basis as ai_memory.py: registered signals from
    signals{}, closed TP/SL from each signal result, and pending/no-result as
    registered minus closed.
    """
    s = s or _state()
    signals = s.get("signals", {}) if isinstance(s.get("signals"), dict) else {}
    out = {
        "real": 0, "ghost": 0,
        "real_tp": 0, "real_sl": 0,
        "ghost_tp": 0, "ghost_sl": 0,
    }
    for item in signals.values():
        if not isinstance(item, dict):
            continue
        src = _norm_source(
            item.get("source") or item.get("result_source"),
            item.get("signal_type") or item.get("type"),
        )
        res = _norm_result(item.get("result"))
        if src == "GHOST":
            out["ghost"] += 1
            if res in {"TP1", "TP2"}:
                out["ghost_tp"] += 1
            elif res == "SL":
                out["ghost_sl"] += 1
        else:
            out["real"] += 1
            if res in {"TP1", "TP2"}:
                out["real_tp"] += 1
            elif res == "SL":
                out["real_sl"] += 1
    out["tp"] = out["real_tp"] + out["ghost_tp"]
    out["sl"] = out["real_sl"] + out["ghost_sl"]
    out["real_closed"] = out["real_tp"] + out["real_sl"]
    out["ghost_closed"] = out["ghost_tp"] + out["ghost_sl"]
    out["real_pending"] = max(0, out["real"] - out["real_closed"])
    out["ghost_pending"] = max(0, out["ghost"] - out["ghost_closed"])
    return out


def format_learning_summary() -> str:
    s = _state()
    rows = list(s.get("by_coin_direction", {}).values())
    c = _display_learning_counts(s)
    tp = int(c.get("tp", 0))
    sl = int(c.get("sl", 0))
    wr = round(tp / max(tp + sl, 1) * 100, 1) if (tp + sl) else 0
    real_wr = round(c["real_tp"] / max(c["real_tp"] + c["real_sl"], 1) * 100, 1) if (c["real_tp"] + c["real_sl"]) else 0
    ghost_wr = round(c["ghost_tp"] / max(c["ghost_tp"] + c["ghost_sl"], 1) * 100, 1) if (c["ghost_tp"] + c["ghost_sl"]) else 0
    good = len([r for r in rows if r.get("behavior") == "GOOD"])
    bad = len([r for r in rows if r.get("behavior") == "BAD"])
    dyn = sum(int((r.get("dynamic_profit_stats") or {}).get("profit_exits", 0) or 0) for r in rows)
    return (
        "🧠 خلاصه یادگیری\n"
        f"Real ثبت‌شده: {c['real']} | بسته‌شده: {c['real_closed']} | بی‌نتیجه/باز: {c['real_pending']}\n"
        f"Real TP/SL: TP:{c['real_tp']} | SL:{c['real_sl']} | WR:{real_wr}%\n"
        f"Ghost ثبت‌شده در Learning: {c['ghost']} | بسته‌شده: {c['ghost_closed']} | بی‌نتیجه/باز: {c['ghost_pending']}\n"
        f"Ghost TP/SL: TP:{c['ghost_tp']} | SL:{c['ghost_sl']} | WR:{ghost_wr}%\n"
        f"کل نتیجه‌دار: TP:{tp} | SL:{sl} | WR:{wr}%\n"
        f"خروج سود AI: {dyn}\n"
        f"رفتار خوب: {good} | رفتار بد: {bad}"
    )


def format_coin_behavior(symbol: str = None) -> str:
    s = _state()
    rows = list(s.get("by_coin_direction", {}).values())
    if symbol:
        rows = [r for r in rows if str(r.get("symbol", "")).upper() == str(symbol).upper()]
    if not rows:
        return "رفتار کوین هنوز داده کافی ندارد."
    rows.sort(key=lambda r: (_safe_int(r.get("confidence")), _safe_float(r.get("weighted_tp")) - _safe_float(r.get("weighted_sl"))), reverse=True)
    lines = ["🧠 رفتار کوین‌ها"]
    for r in rows[:20]:
        tp = int(r.get("tp1", 0)) + int(r.get("tp2", 0))
        sl = int(r.get("sl", 0))
        wr = round(tp / max(tp + sl, 1) * 100, 1) if tp + sl else 0
        lines.append(
            f"{r.get('symbol')} {r.get('direction')} | TP:{tp} SL:{sl} WR:{wr}% | "
            f"رفتار:{r.get('behavior','UNKNOWN')} | شخصیت:{r.get('personality','UNKNOWN')} | اعتماد:{r.get('confidence',0)} | "
            f"E5M:{round(_safe_float(r.get('early_5m_tp')) / max(_safe_float(r.get('early_5m_tp')) + _safe_float(r.get('early_5m_sl')), 1e-9) * 100, 1) if (_safe_float(r.get('early_5m_tp')) + _safe_float(r.get('early_5m_sl'))) > 0 else '-'}%"
        )
    return "\n".join(lines)


def format_smart_stats() -> str:
    return format_learning_summary()
