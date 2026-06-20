from __future__ import annotations

"""
AI Memory / Learning Ledger for AI Movement Hunter.

This module is the persistent learning backbone of the bot.

Responsibilities:
- Store full metadata for REAL and GHOST decisions.
- Store setup/activation/exit snapshots.
- Track MFE/MAE and TP/SL quality.
- Learn per symbol + direction behavior.
- Learn per indicator range behavior.
- Compare REAL vs GHOST outcomes.
- Maintain adaptive AI module weights.
- Maintain state memory for market regimes.
- Maintain time/session risk memory.
- Provide fast cached summaries for Telegram commands.
- Never import bot.py or trading/exchange modules.

Important architecture rule:
This module does not generate signals and does not place trades.
It only records, learns, audits, and returns memory profiles.
"""

import math
import statistics
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from config import CORE_DATA_FILES, COMMAND_CACHE_TTL_SECONDS
from data_store import (
    load_dict,
    save_json,
    backup_file,
    prune_records,
    cache_get,
    cache_set,
    now_ts,
)
from diagnostics import safe, record_error, info, warning


AI_MEMORY_FILE = CORE_DATA_FILES.get("ai_memory")
AI_WEIGHTS_FILE = CORE_DATA_FILES.get("ai_weights")
MARKET_CACHE_FILE = CORE_DATA_FILES.get("market_cache")

MAX_MEMORY_RECORDS = 50000
MAX_STATE_RECORDS = 2000
MAX_RECENT_EVENTS = 500


# -----------------------------
# Helpers
# -----------------------------

def _ts() -> int:
    return int(time.time())


def _day_key(ts: Optional[int] = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts or _ts()))


def _hour_key(ts: Optional[int] = None) -> str:
    return time.strftime("%H", time.localtime(ts or _ts()))


def _session_key(ts: Optional[int] = None) -> str:
    h = int(_hour_key(ts))
    if 0 <= h < 6:
        return "ASIA_LATE"
    if 6 <= h < 12:
        return "EUROPE_MORNING"
    if 12 <= h < 17:
        return "US_PREMARKET_EUROPE"
    if 17 <= h < 22:
        return "US_ACTIVE"
    return "US_LATE"


def _symbol_direction(symbol: str, direction: str) -> str:
    return f"{str(symbol).upper()}::{str(direction).upper()}"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def _round(value: Any, ndigits: int = 6) -> float:
    return round(_safe_float(value), ndigits)


def _range_bucket(value: Any, step: int = 5, min_v: int = 0, max_v: int = 100) -> str:
    v = _safe_float(value, 0)
    v = max(min_v, min(max_v, v))
    lo = int(v // step) * step
    hi = lo + step
    return f"{lo}-{hi}"


def _signed_bucket(value: Any, step: float = 0.25, limit: float = 5.0) -> str:
    v = _safe_float(value, 0)
    v = max(-limit, min(limit, v))
    lo = math.floor(v / step) * step
    hi = lo + step
    return f"{lo:.2f}:{hi:.2f}"


def _result_group(result: str) -> str:
    r = str(result or "").upper()
    if r.startswith("TP"):
        return "TP"
    if r == "SL":
        return "SL"
    if r in {"BE", "BREAKEVEN"}:
        return "BE"
    if r in {"CANCELLED", "CANCELED"}:
        return "CANCELLED"
    if r in {"NO_MOVE", "TIMEOUT"}:
        return "NO_MOVE"
    if r in {"REVERSE", "REVERSAL"}:
        return "REVERSAL"
    return r or "OPEN"


def _is_win(result: str) -> bool:
    return _result_group(result) == "TP"


def _is_loss(result: str) -> bool:
    return _result_group(result) == "SL"


def _deep_get(d: Dict[str, Any], path: str, default: Any = None) -> Any:
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
    return default if cur is None else cur


def _extract_indicator_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize indicator metadata. Keeps exact values when present.
    Supports both flat and nested feature dictionaries.
    """
    snapshot = snapshot or {}
    indicators = snapshot.get("indicators", snapshot)

    return {
        "rsi": _round(indicators.get("rsi", indicators.get("RSI", 50)), 4),
        "macd": _round(indicators.get("macd", 0), 8),
        "macd_signal": _round(indicators.get("macd_signal", 0), 8),
        "macd_hist": _round(indicators.get("macd_hist", indicators.get("macd_histogram", 0)), 8),
        "macd_slope": _round(indicators.get("macd_slope", 0), 8),
        "adx": _round(indicators.get("adx", 0), 4),
        "atr": _round(indicators.get("atr", 0), 8),
        "ema_20": _round(indicators.get("ema_20", indicators.get("ema20", 0)), 8),
        "ema_50": _round(indicators.get("ema_50", indicators.get("ema50", 0)), 8),
        "ema_200": _round(indicators.get("ema_200", indicators.get("ema200", 0)), 8),
        "ema_state": str(indicators.get("ema_state", "UNKNOWN")),
        "vwap": _round(indicators.get("vwap", 0), 8),
        "vwap_state": str(indicators.get("vwap_state", "UNKNOWN")),
        "vwap_distance": _round(indicators.get("vwap_distance", 0), 8),
        "volume": _round(indicators.get("volume", 0), 8),
        "volume_z": _round(indicators.get("volume_z", 0), 4),
        "power_2": _round(indicators.get("power_2", indicators.get("buy_sell_power_2", 0)), 6),
        "power_3": _round(indicators.get("power_3", indicators.get("buy_sell_power_3", 0)), 6),
        "buy_power": _round(indicators.get("buy_power", 0), 6),
        "sell_power": _round(indicators.get("sell_power", 0), 6),
        "candle_quality": _round(indicators.get("candle_quality", 0), 4),
        "fresh_momentum": _round(indicators.get("fresh_momentum", 0), 4),
        "early_momentum": _round(indicators.get("early_momentum", 0), 4),
        "compression": _round(indicators.get("compression", 0), 4),
        "expansion": _round(indicators.get("expansion", 0), 4),
    }


def _extract_context(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = snapshot or {}
    ctx = snapshot.get("context", snapshot)
    return {
        "market_mode": str(ctx.get("market_mode", "UNKNOWN")),
        "btc_trend": str(ctx.get("btc_trend", "UNKNOWN")),
        "btc_bias": str(ctx.get("btc_bias", "UNKNOWN")),
        "btc_dominance": _round(ctx.get("btc_dominance", 0), 4),
        "fear_greed": _round(ctx.get("fear_greed", 50), 2),
        "altseason": _round(ctx.get("altseason", 0), 4),
        "market_breadth_bullish": _round(ctx.get("market_breadth_bullish", 0), 4),
        "market_breadth_bearish": _round(ctx.get("market_breadth_bearish", 0), 4),
        "leader_influence": _round(ctx.get("leader_influence", 0), 4),
        "leader_symbol": str(ctx.get("leader_symbol", "")),
        "correlation_group": str(ctx.get("correlation_group", "UNKNOWN")),
        "session": str(ctx.get("session", _session_key())),
        "hour": str(ctx.get("hour", _hour_key())),
    }


def _extract_structure(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = snapshot or {}
    st = snapshot.get("structure", snapshot)
    return {
        "support_near": _round(st.get("support_near", 0), 8),
        "resistance_near": _round(st.get("resistance_near", 0), 8),
        "supply_zone_distance": _round(st.get("supply_zone_distance", 0), 8),
        "demand_zone_distance": _round(st.get("demand_zone_distance", 0), 8),
        "sr_distance": _round(st.get("sr_distance", 0), 8),
        "swing_high": _round(st.get("swing_high", 0), 8),
        "swing_low": _round(st.get("swing_low", 0), 8),
        "breakout_state": str(st.get("breakout_state", "UNKNOWN")),
        "fake_breakout_risk": _round(st.get("fake_breakout_risk", 0), 4),
        "trap_risk": _round(st.get("trap_risk", 0), 4),
        "liquidity_risk": _round(st.get("liquidity_risk", 0), 4),
        "reversal_probability": _round(st.get("reversal_probability", 0), 4),
        "movement_phase": str(st.get("movement_phase", "UNKNOWN")),
    }


def _default_weights() -> Dict[str, float]:
    return {
        "fresh_momentum": 1.00,
        "entry_quality": 1.00,
        "trend_context": 1.00,
        "trap_filter": 1.00,
        "liquidity_filter": 1.00,
        "reversal_filter": 1.00,
        "coin_behavior": 1.00,
        "state_memory": 1.00,
        "sr_behavior": 1.00,
        "time_risk": 1.00,
        "btc_leader": 1.00,
        "confidence_boundary": 1.00,
        "correlation_exposure": 1.00,
        "tp_memory": 1.00,
        "sl_memory": 1.00,
    }


def _empty_state() -> Dict[str, Any]:
    return {
        "version": 2,
        "created_at": _ts(),
        "updated_at": _ts(),
        "records": {},
        "open_records": {},
        "recent_events": [],
        "coin_direction": {},
        "indicator_ranges": {},
        "tp_sl_memory": {},
        "mfe_mae_memory": {},
        "state_memory": {},
        "time_memory": {},
        "leader_laggard": {},
        "cross_coin_groups": {},
        "self_audit": {
            "last_run": 0,
            "runs": 0,
            "real_vs_ghost": {},
            "module_performance": {},
            "bad_decision_patterns": [],
            "good_decision_patterns": [],
        },
        "weights": _default_weights(),
        "cache": {},
    }


@safe(default={})
def load_memory() -> Dict[str, Any]:
    st = load_dict(AI_MEMORY_FILE)
    if not st:
        st = _empty_state()
        save_json(AI_MEMORY_FILE, st)
    # Migration safety
    for k, v in _empty_state().items():
        st.setdefault(k, v)
    st.setdefault("weights", _default_weights())
    for k, v in _default_weights().items():
        st["weights"].setdefault(k, v)
    return st


@safe(default=False)
def save_memory(state: Dict[str, Any], make_backup: bool = False) -> bool:
    state["updated_at"] = _ts()
    records = state.get("records", {})
    if isinstance(records, dict) and len(records) > MAX_MEMORY_RECORDS:
        # Keep newest records by updated/created time
        items = sorted(records.items(), key=lambda kv: kv[1].get("updated_at", kv[1].get("created_at", 0)))
        state["records"] = dict(items[-MAX_MEMORY_RECORDS:])
    if isinstance(state.get("recent_events"), list):
        state["recent_events"] = state["recent_events"][-MAX_RECENT_EVENTS:]
    return save_json(AI_MEMORY_FILE, state, make_backup=make_backup)


@safe(default="")
def backup_memory() -> str:
    return backup_file(AI_MEMORY_FILE, suffix="ai_memory")


# -----------------------------
# Data models
# -----------------------------

@dataclass
class AIRecord:
    id: str
    symbol: str
    direction: str
    decision: str
    status: str
    setup_time: int
    activation_time: Optional[int] = None
    exit_time: Optional[int] = None
    entry_price: float = 0.0
    exit_price: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    sl: float = 0.0
    result: str = "OPEN"
    pnl: float = 0.0
    mfe: float = 0.0
    mae: float = 0.0
    max_profit_pct: float = 0.0
    max_adverse_pct: float = 0.0
    setup_snapshot: Dict[str, Any] = field(default_factory=dict)
    activation_snapshot: Dict[str, Any] = field(default_factory=dict)
    exit_snapshot: Dict[str, Any] = field(default_factory=dict)
    indicators: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    structure: Dict[str, Any] = field(default_factory=dict)
    ai_confidence: float = 0.0
    ai_reason: str = ""
    modules: Dict[str, Any] = field(default_factory=dict)
    quality: Dict[str, Any] = field(default_factory=dict)
    telegram_message_id: Optional[int] = None
    reply_chat_id: Optional[int] = None
    created_at: int = field(default_factory=_ts)
    updated_at: int = field(default_factory=_ts)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# -----------------------------
# Record lifecycle
# -----------------------------

@safe(default="")
def create_record(
    symbol: str,
    direction: str,
    decision: str,
    setup_snapshot: Optional[Dict[str, Any]] = None,
    entry_price: float = 0.0,
    tp1: float = 0.0,
    tp2: float = 0.0,
    sl: float = 0.0,
    ai_confidence: float = 0.0,
    ai_reason: str = "",
    modules: Optional[Dict[str, Any]] = None,
    telegram_message_id: Optional[int] = None,
    reply_chat_id: Optional[int] = None,
    record_id: Optional[str] = None,
) -> str:
    """
    Create SETUP/REAL/GHOST memory record.
    decision examples: SETUP, REAL, GHOST, REJECT, WAIT.
    """
    st = load_memory()
    rid = record_id or f"{int(time.time())}_{symbol}_{direction}_{uuid.uuid4().hex[:10]}"
    snap = setup_snapshot or {}

    rec = AIRecord(
        id=rid,
        symbol=str(symbol).upper(),
        direction=str(direction).upper(),
        decision=str(decision).upper(),
        status="OPEN" if str(decision).upper() in {"REAL", "GHOST", "SETUP"} else "CLOSED",
        setup_time=_ts(),
        entry_price=_round(entry_price),
        tp1=_round(tp1),
        tp2=_round(tp2),
        sl=_round(sl),
        setup_snapshot=snap,
        indicators=_extract_indicator_snapshot(snap),
        context=_extract_context(snap),
        structure=_extract_structure(snap),
        ai_confidence=_round(ai_confidence, 4),
        ai_reason=str(ai_reason or ""),
        modules=modules or {},
        telegram_message_id=telegram_message_id,
        reply_chat_id=reply_chat_id,
    ).to_dict()

    st["records"][rid] = rec
    if rec["status"] == "OPEN":
        st["open_records"][rid] = {
            "id": rid,
            "symbol": rec["symbol"],
            "direction": rec["direction"],
            "decision": rec["decision"],
            "created_at": rec["created_at"],
        }

    _append_event(st, "CREATE_RECORD", rid, rec)
    _update_profiles_on_create(st, rec)
    save_memory(st, make_backup=False)
    return rid


@safe(default=False)
def activate_record(
    record_id: str,
    activation_snapshot: Optional[Dict[str, Any]] = None,
    entry_price: Optional[float] = None,
    tp1: Optional[float] = None,
    tp2: Optional[float] = None,
    sl: Optional[float] = None,
    decision: Optional[str] = None,
) -> bool:
    st = load_memory()
    rec = st.get("records", {}).get(record_id)
    if not rec:
        return False

    rec["activation_time"] = _ts()
    rec["activation_snapshot"] = activation_snapshot or {}
    rec["status"] = "OPEN"
    if decision:
        rec["decision"] = str(decision).upper()
    if entry_price is not None:
        rec["entry_price"] = _round(entry_price)
    if tp1 is not None:
        rec["tp1"] = _round(tp1)
    if tp2 is not None:
        rec["tp2"] = _round(tp2)
    if sl is not None:
        rec["sl"] = _round(sl)

    # Prefer activation exact indicators when available
    if activation_snapshot:
        rec["activation_indicators"] = _extract_indicator_snapshot(activation_snapshot)
        rec["activation_context"] = _extract_context(activation_snapshot)
        rec["activation_structure"] = _extract_structure(activation_snapshot)

    rec["updated_at"] = _ts()
    st["records"][record_id] = rec
    st["open_records"][record_id] = {
        "id": record_id,
        "symbol": rec["symbol"],
        "direction": rec["direction"],
        "decision": rec["decision"],
        "created_at": rec.get("created_at", _ts()),
        "activation_time": rec["activation_time"],
    }
    _append_event(st, "ACTIVATE_RECORD", record_id, rec)
    save_memory(st)
    return True


@safe(default=False)
def update_excursion(
    record_id: str,
    current_price: float,
    high_price: Optional[float] = None,
    low_price: Optional[float] = None,
) -> bool:
    """
    Update MFE/MAE for open record.
    For LONG:
      favorable = high - entry
      adverse = entry - low
    For SHORT:
      favorable = entry - low
      adverse = high - entry
    """
    st = load_memory()
    rec = st.get("records", {}).get(record_id)
    if not rec:
        return False
    entry = _safe_float(rec.get("entry_price"), 0)
    if entry <= 0:
        return False

    high = _safe_float(high_price if high_price is not None else current_price)
    low = _safe_float(low_price if low_price is not None else current_price)
    direction = str(rec.get("direction", "")).upper()

    if direction == "LONG":
        favorable = max(0.0, high - entry)
        adverse = max(0.0, entry - low)
    else:
        favorable = max(0.0, entry - low)
        adverse = max(0.0, high - entry)

    mfe = max(_safe_float(rec.get("mfe")), favorable)
    mae = max(_safe_float(rec.get("mae")), adverse)
    rec["mfe"] = _round(mfe)
    rec["mae"] = _round(mae)
    rec["max_profit_pct"] = _round((mfe / entry) * 100, 6)
    rec["max_adverse_pct"] = _round((mae / entry) * 100, 6)
    rec["updated_at"] = _ts()

    st["records"][record_id] = rec
    save_memory(st)
    return True


@safe(default=False)
def close_record(
    record_id: str,
    result: str,
    exit_price: float = 0.0,
    pnl: float = 0.0,
    exit_snapshot: Optional[Dict[str, Any]] = None,
    final_mfe: Optional[float] = None,
    final_mae: Optional[float] = None,
) -> bool:
    st = load_memory()
    rec = st.get("records", {}).get(record_id)
    if not rec:
        return False

    rec["exit_time"] = _ts()
    rec["exit_price"] = _round(exit_price)
    rec["result"] = str(result).upper()
    rec["pnl"] = _round(pnl)
    rec["exit_snapshot"] = exit_snapshot or {}
    rec["status"] = "CLOSED"
    if final_mfe is not None:
        rec["mfe"] = max(_safe_float(rec.get("mfe")), _safe_float(final_mfe))
    if final_mae is not None:
        rec["mae"] = max(_safe_float(rec.get("mae")), _safe_float(final_mae))

    # final quality analysis
    rec["quality"] = evaluate_decision_quality(rec)
    rec["updated_at"] = _ts()

    st["records"][record_id] = rec
    st.get("open_records", {}).pop(record_id, None)

    _append_event(st, "CLOSE_RECORD", record_id, rec)
    _learn_from_closed_record(st, rec)
    _prune_state_memory(st)
    save_memory(st, make_backup=True)
    return True


@safe(default=False)
def cancel_record(record_id: str, reason: str = "") -> bool:
    return close_record(record_id, "CANCELLED", pnl=0.0, exit_snapshot={"cancel_reason": reason})


# -----------------------------
# Learning internals
# -----------------------------

def _append_event(st: Dict[str, Any], event_type: str, record_id: str, rec: Dict[str, Any]) -> None:
    st.setdefault("recent_events", []).append({
        "ts": _ts(),
        "event": event_type,
        "record_id": record_id,
        "symbol": rec.get("symbol"),
        "direction": rec.get("direction"),
        "decision": rec.get("decision"),
        "result": rec.get("result"),
    })
    st["recent_events"] = st["recent_events"][-MAX_RECENT_EVENTS:]


def _profile_template() -> Dict[str, Any]:
    return {
        "total": 0,
        "real": 0,
        "ghost": 0,
        "tp": 0,
        "sl": 0,
        "be": 0,
        "cancelled": 0,
        "win_rate": 0.0,
        "avg_mfe": 0.0,
        "avg_mae": 0.0,
        "avg_max_profit_pct": 0.0,
        "avg_max_adverse_pct": 0.0,
        "tp_reach": {"tp1": 0, "tp2": 0},
        "indicator_ranges": {},
        "last_updated": _ts(),
        "strictness": 1.0,
        "notes": [],
    }


def _range_stats_template() -> Dict[str, Any]:
    return {
        "total": 0,
        "tp": 0,
        "sl": 0,
        "ghost_tp": 0,
        "ghost_sl": 0,
        "real_tp": 0,
        "real_sl": 0,
        "avg_mfe": 0.0,
        "avg_mae": 0.0,
        "score": 0.0,
        "last_updated": _ts(),
    }


def _update_profiles_on_create(st: Dict[str, Any], rec: Dict[str, Any]) -> None:
    key = _symbol_direction(rec.get("symbol", ""), rec.get("direction", ""))
    profile = st.setdefault("coin_direction", {}).setdefault(key, _profile_template())
    profile["total"] += 1
    if rec.get("decision") == "REAL":
        profile["real"] += 1
    if rec.get("decision") == "GHOST":
        profile["ghost"] += 1
    profile["last_updated"] = _ts()


def _learn_from_closed_record(st: Dict[str, Any], rec: Dict[str, Any]) -> None:
    _learn_coin_direction(st, rec)
    _learn_indicator_ranges(st, rec)
    _learn_tp_sl(st, rec)
    _learn_mfe_mae(st, rec)
    _learn_state(st, rec)
    _learn_time_session(st, rec)
    _learn_leader_laggard(st, rec)
    _run_light_self_audit(st)


def _learn_coin_direction(st: Dict[str, Any], rec: Dict[str, Any]) -> None:
    key = _symbol_direction(rec.get("symbol", ""), rec.get("direction", ""))
    profile = st.setdefault("coin_direction", {}).setdefault(key, _profile_template())
    result = _result_group(rec.get("result", ""))
    decision = rec.get("decision", "")

    if result == "TP":
        profile["tp"] += 1
    elif result == "SL":
        profile["sl"] += 1
    elif result == "BE":
        profile["be"] += 1
    elif result == "CANCELLED":
        profile["cancelled"] += 1

    if decision == "REAL":
        profile["real"] = max(profile.get("real", 0), 0)
    if decision == "GHOST":
        profile["ghost"] = max(profile.get("ghost", 0), 0)

    wins = profile.get("tp", 0)
    losses = profile.get("sl", 0)
    profile["win_rate"] = round(wins / max(1, wins + losses) * 100, 2)

    # Strictness starts after two stop losses for this coin+direction.
    if losses >= 2:
        profile["strictness"] = min(1.8, 1.0 + (losses - 1) * 0.10)
    else:
        profile["strictness"] = 1.0

    _running_average(profile, "avg_mfe", _safe_float(rec.get("mfe")), profile.get("tp", 0) + profile.get("sl", 0))
    _running_average(profile, "avg_mae", _safe_float(rec.get("mae")), profile.get("tp", 0) + profile.get("sl", 0))
    _running_average(profile, "avg_max_profit_pct", _safe_float(rec.get("max_profit_pct")), profile.get("tp", 0) + profile.get("sl", 0))
    _running_average(profile, "avg_max_adverse_pct", _safe_float(rec.get("max_adverse_pct")), profile.get("tp", 0) + profile.get("sl", 0))
    profile["last_updated"] = _ts()


def _running_average(container: Dict[str, Any], key: str, value: float, n: int) -> None:
    n = max(1, int(n))
    old = _safe_float(container.get(key))
    container[key] = round(((old * max(0, n - 1)) + value) / n, 8)


def _learn_indicator_ranges(st: Dict[str, Any], rec: Dict[str, Any]) -> None:
    symbol = str(rec.get("symbol", "")).upper()
    direction = str(rec.get("direction", "")).upper()
    decision = str(rec.get("decision", "")).upper()
    result = _result_group(rec.get("result", ""))
    ind = rec.get("activation_indicators") or rec.get("indicators") or {}

    key = _symbol_direction(symbol, direction)
    root = st.setdefault("indicator_ranges", {}).setdefault(key, {})

    buckets = {
        "rsi": _range_bucket(ind.get("rsi"), step=5),
        "adx": _range_bucket(ind.get("adx"), step=5, max_v=60),
        "macd_hist": _signed_bucket(ind.get("macd_hist"), step=0.0005, limit=0.05),
        "macd_slope": _signed_bucket(ind.get("macd_slope"), step=0.0005, limit=0.05),
        "vwap_distance": _signed_bucket(ind.get("vwap_distance"), step=0.1, limit=5.0),
        "power_2": _signed_bucket(ind.get("power_2"), step=0.1, limit=10.0),
        "power_3": _signed_bucket(ind.get("power_3"), step=0.1, limit=10.0),
        "fresh_momentum": _range_bucket(_safe_float(ind.get("fresh_momentum")) * 100, step=10, max_v=100),
        "candle_quality": _range_bucket(_safe_float(ind.get("candle_quality")) * 100, step=10, max_v=100),
    }

    for name, bucket in buckets.items():
        node = root.setdefault(name, {}).setdefault(bucket, _range_stats_template())
        node["total"] += 1
        if result == "TP":
            node["tp"] += 1
        if result == "SL":
            node["sl"] += 1
        if decision == "GHOST" and result == "TP":
            node["ghost_tp"] += 1
        if decision == "GHOST" and result == "SL":
            node["ghost_sl"] += 1
        if decision == "REAL" and result == "TP":
            node["real_tp"] += 1
        if decision == "REAL" and result == "SL":
            node["real_sl"] += 1

        _running_average(node, "avg_mfe", _safe_float(rec.get("mfe")), node["total"])
        _running_average(node, "avg_mae", _safe_float(rec.get("mae")), node["total"])

        # Simple score: TP positive, SL negative, ghost TP means missed opportunity
        node["score"] = round((node["tp"] - node["sl"] + node["ghost_tp"] * 0.35 - node["ghost_sl"] * 0.15) / max(1, node["total"]), 4)
        node["last_updated"] = _ts()


def _learn_tp_sl(st: Dict[str, Any], rec: Dict[str, Any]) -> None:
    key = _symbol_direction(rec.get("symbol", ""), rec.get("direction", ""))
    mem = st.setdefault("tp_sl_memory", {}).setdefault(key, {
        "samples": 0,
        "tp_hits": 0,
        "sl_hits": 0,
        "avg_tp1_distance_pct": 0.0,
        "avg_tp2_distance_pct": 0.0,
        "avg_sl_distance_pct": 0.0,
        "avg_reachable_profit_pct": 0.0,
        "avg_adverse_before_win_pct": 0.0,
        "tp_too_far_count": 0,
        "sl_too_close_count": 0,
        "last_updated": _ts(),
    })

    entry = _safe_float(rec.get("entry_price"))
    if entry <= 0:
        return
    direction = str(rec.get("direction", "")).upper()
    tp1 = _safe_float(rec.get("tp1"))
    tp2 = _safe_float(rec.get("tp2"))
    sl = _safe_float(rec.get("sl"))

    if direction == "LONG":
        tp1_pct = (tp1 - entry) / entry * 100 if tp1 else 0
        tp2_pct = (tp2 - entry) / entry * 100 if tp2 else 0
        sl_pct = (entry - sl) / entry * 100 if sl else 0
    else:
        tp1_pct = (entry - tp1) / entry * 100 if tp1 else 0
        tp2_pct = (entry - tp2) / entry * 100 if tp2 else 0
        sl_pct = (sl - entry) / entry * 100 if sl else 0

    mem["samples"] += 1
    n = mem["samples"]
    _running_average(mem, "avg_tp1_distance_pct", tp1_pct, n)
    _running_average(mem, "avg_tp2_distance_pct", tp2_pct, n)
    _running_average(mem, "avg_sl_distance_pct", sl_pct, n)
    _running_average(mem, "avg_reachable_profit_pct", _safe_float(rec.get("max_profit_pct")), n)
    _running_average(mem, "avg_adverse_before_win_pct", _safe_float(rec.get("max_adverse_pct")), n)

    result = _result_group(rec.get("result"))
    if result == "TP":
        mem["tp_hits"] += 1
    if result == "SL":
        mem["sl_hits"] += 1
        if _safe_float(rec.get("max_profit_pct")) >= max(0.2, tp1_pct * 0.65):
            mem["tp_too_far_count"] += 1
        if _safe_float(rec.get("max_adverse_pct")) < sl_pct * 0.35:
            mem["sl_too_close_count"] += 1

    mem["last_updated"] = _ts()


def _learn_mfe_mae(st: Dict[str, Any], rec: Dict[str, Any]) -> None:
    key = _symbol_direction(rec.get("symbol", ""), rec.get("direction", ""))
    mem = st.setdefault("mfe_mae_memory", {}).setdefault(key, {
        "samples": 0,
        "wins": 0,
        "losses": 0,
        "mfe_values": [],
        "mae_values": [],
        "profit_pct_values": [],
        "adverse_pct_values": [],
        "median_mfe": 0.0,
        "median_mae": 0.0,
        "median_profit_pct": 0.0,
        "median_adverse_pct": 0.0,
        "last_updated": _ts(),
    })
    mem["samples"] += 1
    if _is_win(rec.get("result")):
        mem["wins"] += 1
    if _is_loss(rec.get("result")):
        mem["losses"] += 1

    for k, source in [
        ("mfe_values", "mfe"),
        ("mae_values", "mae"),
        ("profit_pct_values", "max_profit_pct"),
        ("adverse_pct_values", "max_adverse_pct"),
    ]:
        arr = mem.setdefault(k, [])
        arr.append(_round(rec.get(source), 8))
        mem[k] = arr[-500:]

    for arr_key, med_key in [
        ("mfe_values", "median_mfe"),
        ("mae_values", "median_mae"),
        ("profit_pct_values", "median_profit_pct"),
        ("adverse_pct_values", "median_adverse_pct"),
    ]:
        vals = [_safe_float(x) for x in mem.get(arr_key, [])]
        mem[med_key] = round(statistics.median(vals), 8) if vals else 0.0

    mem["last_updated"] = _ts()


def _state_key_from_record(rec: Dict[str, Any]) -> str:
    ctx = rec.get("activation_context") or rec.get("context") or {}
    stc = rec.get("activation_structure") or rec.get("structure") or {}
    ind = rec.get("activation_indicators") or rec.get("indicators") or {}
    parts = [
        str(ctx.get("market_mode", "UNKNOWN")),
        str(ctx.get("btc_bias", "UNKNOWN")),
        str(ctx.get("correlation_group", "UNKNOWN")),
        f"ADX{_range_bucket(ind.get('adx'), 10, 0, 60)}",
        f"RSI{_range_bucket(ind.get('rsi'), 10)}",
        str(stc.get("movement_phase", "UNKNOWN")),
        f"TRAP{_range_bucket(_safe_float(stc.get('trap_risk'))*100, 20)}",
    ]
    return "|".join(parts)


def _learn_state(st: Dict[str, Any], rec: Dict[str, Any]) -> None:
    skey = _state_key_from_record(rec)
    mem = st.setdefault("state_memory", {}).setdefault(skey, {
        "samples": 0,
        "tp": 0,
        "sl": 0,
        "real_tp": 0,
        "real_sl": 0,
        "ghost_tp": 0,
        "ghost_sl": 0,
        "avg_confidence": 0.0,
        "avg_mfe": 0.0,
        "avg_mae": 0.0,
        "symbols": {},
        "last_updated": _ts(),
    })
    mem["samples"] += 1
    result = _result_group(rec.get("result"))
    decision = str(rec.get("decision", "")).upper()
    if result == "TP":
        mem["tp"] += 1
    if result == "SL":
        mem["sl"] += 1
    if decision == "REAL" and result == "TP":
        mem["real_tp"] += 1
    if decision == "REAL" and result == "SL":
        mem["real_sl"] += 1
    if decision == "GHOST" and result == "TP":
        mem["ghost_tp"] += 1
    if decision == "GHOST" and result == "SL":
        mem["ghost_sl"] += 1
    _running_average(mem, "avg_confidence", _safe_float(rec.get("ai_confidence")), mem["samples"])
    _running_average(mem, "avg_mfe", _safe_float(rec.get("mfe")), mem["samples"])
    _running_average(mem, "avg_mae", _safe_float(rec.get("mae")), mem["samples"])
    sym = str(rec.get("symbol", "")).upper()
    mem.setdefault("symbols", {})[sym] = mem.setdefault("symbols", {}).get(sym, 0) + 1
    mem["last_updated"] = _ts()


def _learn_time_session(st: Dict[str, Any], rec: Dict[str, Any]) -> None:
    ts = rec.get("activation_time") or rec.get("setup_time") or _ts()
    session = _session_key(ts)
    hour = _hour_key(ts)
    key = f"{session}|{hour}|{rec.get('direction')}"
    mem = st.setdefault("time_memory", {}).setdefault(key, {
        "samples": 0,
        "tp": 0,
        "sl": 0,
        "risk_score": 0.0,
        "last_updated": _ts(),
    })
    mem["samples"] += 1
    if _is_win(rec.get("result")):
        mem["tp"] += 1
    if _is_loss(rec.get("result")):
        mem["sl"] += 1
    mem["risk_score"] = round(mem["sl"] / max(1, mem["tp"] + mem["sl"]), 4)
    mem["last_updated"] = _ts()


def _learn_leader_laggard(st: Dict[str, Any], rec: Dict[str, Any]) -> None:
    ctx = rec.get("activation_context") or rec.get("context") or {}
    leader = str(ctx.get("leader_symbol", "")).upper()
    if not leader:
        return
    key = f"{leader}->{rec.get('symbol')}::{rec.get('direction')}"
    mem = st.setdefault("leader_laggard", {}).setdefault(key, {
        "samples": 0,
        "tp": 0,
        "sl": 0,
        "avg_leader_influence": 0.0,
        "score": 0.0,
        "last_updated": _ts(),
    })
    mem["samples"] += 1
    if _is_win(rec.get("result")):
        mem["tp"] += 1
    if _is_loss(rec.get("result")):
        mem["sl"] += 1
    _running_average(mem, "avg_leader_influence", _safe_float(ctx.get("leader_influence")), mem["samples"])
    mem["score"] = round((mem["tp"] - mem["sl"]) / max(1, mem["samples"]), 4)
    mem["last_updated"] = _ts()


def _prune_state_memory(st: Dict[str, Any]) -> None:
    sm = st.get("state_memory", {})
    if len(sm) <= MAX_STATE_RECORDS:
        return
    items = sorted(sm.items(), key=lambda kv: kv[1].get("last_updated", 0))
    st["state_memory"] = dict(items[-MAX_STATE_RECORDS:])


# -----------------------------
# Self audit / meta learning
# -----------------------------

@safe(default={})
def run_self_audit(force: bool = False) -> Dict[str, Any]:
    st = load_memory()
    result = _run_light_self_audit(st, force=force)
    save_memory(st, make_backup=True)
    return result


def _run_light_self_audit(st: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
    audit = st.setdefault("self_audit", {})
    last = int(audit.get("last_run", 0))
    if not force and _ts() - last < 300:
        return {"status": "SKIPPED_RECENT", "last_run": last}

    records = list(st.get("records", {}).values())
    closed = [r for r in records if r.get("status") == "CLOSED"]
    real = [r for r in closed if r.get("decision") == "REAL"]
    ghost = [r for r in closed if r.get("decision") == "GHOST"]

    real_tp = [r for r in real if _is_win(r.get("result"))]
    real_sl = [r for r in real if _is_loss(r.get("result"))]
    ghost_tp = [r for r in ghost if _is_win(r.get("result"))]
    ghost_sl = [r for r in ghost if _is_loss(r.get("result"))]

    weights = st.setdefault("weights", _default_weights())
    changes = {}

    # If ghost wins more than real, AI was too conservative or picked wrong candidates.
    ghost_wr = len(ghost_tp) / max(1, len(ghost_tp) + len(ghost_sl))
    real_wr = len(real_tp) / max(1, len(real_tp) + len(real_sl))

    def adjust(name: str, delta: float, lo: float = 0.70, hi: float = 1.45) -> None:
        old = _safe_float(weights.get(name, 1.0), 1.0)
        new = max(lo, min(hi, old + delta))
        if abs(new - old) >= 0.0001:
            weights[name] = round(new, 4)
            changes[name] = {"old": old, "new": new, "delta": round(delta, 4)}

    if len(real) + len(ghost) >= 20:
        if ghost_wr > real_wr + 0.10 and len(ghost_tp) >= 5:
            adjust("confidence_boundary", -0.02)
            adjust("fresh_momentum", 0.02)
            adjust("entry_quality", 0.015)
        if real_wr < 0.45 and len(real_sl) >= 5:
            adjust("trap_filter", 0.02)
            adjust("liquidity_filter", 0.015)
            adjust("reversal_filter", 0.015)
            adjust("coin_behavior", 0.01)
        if real_wr > 0.60 and len(real_tp) >= 8:
            adjust("confidence_boundary", 0.01)
            adjust("entry_quality", 0.01)

    module_perf = _module_performance(closed)
    audit["module_performance"] = module_perf
    audit["real_vs_ghost"] = {
        "real": len(real),
        "ghost": len(ghost),
        "real_tp": len(real_tp),
        "real_sl": len(real_sl),
        "ghost_tp": len(ghost_tp),
        "ghost_sl": len(ghost_sl),
        "real_wr": round(real_wr * 100, 2),
        "ghost_wr": round(ghost_wr * 100, 2),
    }
    audit["last_run"] = _ts()
    audit["runs"] = int(audit.get("runs", 0)) + 1
    audit["last_changes"] = changes
    st["weights"] = weights
    save_json(AI_WEIGHTS_FILE, weights, make_backup=True)
    return {"status": "UPDATED", "changes": changes, **audit["real_vs_ghost"]}


def _module_performance(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    perf: Dict[str, Any] = {}
    for rec in records[-5000:]:
        modules = rec.get("modules") or {}
        result = _result_group(rec.get("result"))
        for name, value in modules.items():
            node = perf.setdefault(name, {"support": 0, "reject": 0, "tp": 0, "sl": 0, "score": 0.0})
            # value can be bool, number or dict
            supported = False
            rejected = False
            if isinstance(value, dict):
                supported = _safe_float(value.get("score", value.get("value", 0))) > 0
                rejected = _safe_float(value.get("score", value.get("value", 0))) < 0
            elif isinstance(value, bool):
                supported = value
            else:
                supported = _safe_float(value) > 0
                rejected = _safe_float(value) < 0

            if supported:
                node["support"] += 1
                if result == "TP":
                    node["tp"] += 1
                if result == "SL":
                    node["sl"] += 1
            if rejected:
                node["reject"] += 1
        for node in perf.values():
            node["score"] = round((node["tp"] - node["sl"]) / max(1, node["support"]), 4)
    return perf


@safe(default={})
def evaluate_decision_quality(rec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Judge whether AI decision/TP/SL/entry was likely good after final result.
    This is not a trading signal. It is post-trade learning metadata.
    """
    result = _result_group(rec.get("result"))
    max_profit = _safe_float(rec.get("max_profit_pct"))
    max_adverse = _safe_float(rec.get("max_adverse_pct"))
    confidence = _safe_float(rec.get("ai_confidence"))
    decision = str(rec.get("decision", "")).upper()

    quality = {
        "decision_good": False,
        "entry_good": False,
        "tp_quality": "UNKNOWN",
        "sl_quality": "UNKNOWN",
        "reason": [],
    }

    if result == "TP":
        quality["decision_good"] = True
        quality["entry_good"] = max_adverse < max(0.8, max_profit * 0.8)
        quality["reason"].append("result_tp")
    elif result == "SL":
        quality["decision_good"] = False
        if max_profit >= 0.25:
            quality["entry_good"] = True
            quality["tp_quality"] = "POSSIBLY_TOO_FAR_OR_EXIT_LATE"
            quality["reason"].append("moved_profit_before_sl")
        else:
            quality["entry_good"] = False
            quality["reason"].append("direct_or_weak_sl")
    elif result in {"BE", "NO_MOVE"}:
        quality["decision_good"] = confidence < 0.55 or decision == "GHOST"
        quality["reason"].append("no_clear_move")

    # TP/SL distance check
    entry = _safe_float(rec.get("entry_price"))
    tp1 = _safe_float(rec.get("tp1"))
    sl = _safe_float(rec.get("sl"))
    if entry > 0 and tp1 > 0:
        expected = abs(tp1 - entry) / entry * 100
        if max_profit > 0 and expected > max_profit * 1.4 and result == "SL":
            quality["tp_quality"] = "TOO_FAR"
    if entry > 0 and sl > 0:
        sl_dist = abs(entry - sl) / entry * 100
        if result == "SL" and max_adverse < sl_dist * 0.4:
            quality["sl_quality"] = "POSSIBLY_TOO_CLOSE_OR_NOISY"
        elif result == "SL":
            quality["sl_quality"] = "HIT_VALID_RISK"

    return quality


# -----------------------------
# Public profile/query API
# -----------------------------

@safe(default={})
def learning_profile(symbol: str, direction: str) -> Dict[str, Any]:
    st = load_memory()
    key = _symbol_direction(symbol, direction)
    return {
        "coin_direction": st.get("coin_direction", {}).get(key, {}),
        "indicator_ranges": st.get("indicator_ranges", {}).get(key, {}),
        "tp_sl_memory": st.get("tp_sl_memory", {}).get(key, {}),
        "mfe_mae_memory": st.get("mfe_mae_memory", {}).get(key, {}),
    }


@safe(default={})
def state_profile_from_snapshot(snapshot: Dict[str, Any], symbol: str = "", direction: str = "") -> Dict[str, Any]:
    fake = {
        "symbol": symbol,
        "direction": direction,
        "context": _extract_context(snapshot),
        "structure": _extract_structure(snapshot),
        "indicators": _extract_indicator_snapshot(snapshot),
    }
    skey = _state_key_from_record(fake)
    st = load_memory()
    return {
        "state_key": skey,
        "profile": st.get("state_memory", {}).get(skey, {}),
    }


@safe(default={})
def get_adaptive_weights() -> Dict[str, float]:
    st = load_memory()
    return st.get("weights", _default_weights())


@safe(default={})
def get_coin_strictness(symbol: str, direction: str) -> Dict[str, Any]:
    profile = learning_profile(symbol, direction).get("coin_direction", {})
    return {
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "strictness": _safe_float(profile.get("strictness", 1.0), 1.0),
        "sl": int(profile.get("sl", 0) or 0),
        "tp": int(profile.get("tp", 0) or 0),
        "win_rate": _safe_float(profile.get("win_rate", 0)),
    }


@safe(default={})
def recommend_tp_sl_context(symbol: str, direction: str) -> Dict[str, Any]:
    prof = learning_profile(symbol, direction)
    tp = prof.get("tp_sl_memory", {})
    mfe = prof.get("mfe_mae_memory", {})
    return {
        "avg_tp1_distance_pct": _safe_float(tp.get("avg_tp1_distance_pct")),
        "avg_tp2_distance_pct": _safe_float(tp.get("avg_tp2_distance_pct")),
        "avg_sl_distance_pct": _safe_float(tp.get("avg_sl_distance_pct")),
        "avg_reachable_profit_pct": _safe_float(tp.get("avg_reachable_profit_pct")),
        "median_profit_pct": _safe_float(mfe.get("median_profit_pct")),
        "median_adverse_pct": _safe_float(mfe.get("median_adverse_pct")),
        "tp_too_far_count": int(tp.get("tp_too_far_count", 0) or 0),
        "sl_too_close_count": int(tp.get("sl_too_close_count", 0) or 0),
        "samples": int(tp.get("samples", 0) or 0),
    }


@safe(default={})
def summary(use_cache: bool = True) -> Dict[str, Any]:
    if use_cache:
        cached = cache_get("market_cache", "ai_memory_summary", COMMAND_CACHE_TTL_SECONDS)
        if cached:
            return cached

    st = load_memory()
    records = list(st.get("records", {}).values())
    real = [r for r in records if r.get("decision") == "REAL"]
    ghost = [r for r in records if r.get("decision") == "GHOST"]
    open_records = list(st.get("open_records", {}).values())

    real_tp = sum(1 for r in real if _is_win(r.get("result")))
    real_sl = sum(1 for r in real if _is_loss(r.get("result")))
    ghost_tp = sum(1 for r in ghost if _is_win(r.get("result")))
    ghost_sl = sum(1 for r in ghost if _is_loss(r.get("result")))

    coin_profiles = st.get("coin_direction", {})
    best = []
    worst = []
    for key, p in coin_profiles.items():
        total = int(p.get("tp", 0)) + int(p.get("sl", 0))
        if total < 2:
            continue
        item = {
            "key": key,
            "wr": _safe_float(p.get("win_rate")),
            "tp": int(p.get("tp", 0)),
            "sl": int(p.get("sl", 0)),
            "strictness": _safe_float(p.get("strictness", 1.0)),
        }
        best.append(item)
        worst.append(item)

    best = sorted(best, key=lambda x: (x["wr"], x["tp"] - x["sl"]), reverse=True)[:10]
    worst = sorted(worst, key=lambda x: (x["wr"], -(x["sl"])), reverse=False)[:10]

    res = {
        "records": len(records),
        "open": len(open_records),
        "real": len(real),
        "ghost": len(ghost),
        "real_tp": real_tp,
        "real_sl": real_sl,
        "real_wr": round(real_tp / max(1, real_tp + real_sl) * 100, 2),
        "ghost_tp": ghost_tp,
        "ghost_sl": ghost_sl,
        "ghost_wr": round(ghost_tp / max(1, ghost_tp + ghost_sl) * 100, 2),
        "weights": st.get("weights", {}),
        "self_audit": st.get("self_audit", {}),
        "best": best,
        "worst": worst,
        "updated_at": st.get("updated_at"),
    }
    cache_set("market_cache", "ai_memory_summary", res)
    return res


@safe(default="")
def summary_fa() -> str:
    s = summary(use_cache=False)
    lines = [
        "🧠 حافظه و یادگیری AI",
        f"کل رکوردها: {s.get('records', 0)} | باز: {s.get('open', 0)}",
        f"Real: {s.get('real', 0)} | TP:{s.get('real_tp', 0)} SL:{s.get('real_sl', 0)} | WR:{s.get('real_wr', 0)}%",
        f"Ghost: {s.get('ghost', 0)} | TP:{s.get('ghost_tp', 0)} SL:{s.get('ghost_sl', 0)} | WR:{s.get('ghost_wr', 0)}%",
    ]
    best = s.get("best", [])[:3]
    worst = s.get("worst", [])[:3]
    if best:
        lines.append("بهترین‌ها: " + "، ".join(f"{x['key']} {x['wr']}%" for x in best))
    if worst:
        lines.append("پرریسک‌ها: " + "، ".join(f"{x['key']} {x['wr']}%" for x in worst))
    return "\n".join(lines)


@safe(default=[])
def open_records() -> List[Dict[str, Any]]:
    st = load_memory()
    return list(st.get("open_records", {}).values())


@safe(default={})
def get_record(record_id: str) -> Dict[str, Any]:
    return load_memory().get("records", {}).get(record_id, {})


@safe(default=[])
def recent_records(limit: int = 20, decision: Optional[str] = None) -> List[Dict[str, Any]]:
    records = list(load_memory().get("records", {}).values())
    if decision:
        records = [r for r in records if r.get("decision") == decision.upper()]
    records = sorted(records, key=lambda r: r.get("updated_at", r.get("created_at", 0)), reverse=True)
    return records[:limit]


@safe(default=True)
def initialize_memory_files() -> bool:
    st = load_memory()
    save_memory(st)
    weights = st.get("weights", _default_weights())
    save_json(AI_WEIGHTS_FILE, weights)
    return True
