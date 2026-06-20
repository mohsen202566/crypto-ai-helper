# -*- coding: utf-8 -*-
"""
ai_memory.py

Central AI memory/status layer for the crypto futures bot.

Purpose:
- Persist AI settings across restarts/updates.
- Provide display-safe AI status from real stored data, not drifting counters.
- Keep Daily Report memory, Market Mode memory, Coin Rotation summary,
  learned coin counts, Real/Ghost TP/SL split, and AI confidence.

Compatibility:
- Keeps old public function names used by bot.py and other modules:
    get_ai_settings
    update_ai_summary
    get_ai_summary_counts
    format_ai_status
- Adds optional helpers for richer AI reports without breaking old code.
"""

import time
from typing import Dict, Any, List, Optional, Tuple

from data_store import load_json, save_json

AI_MEMORY_FILE = "ai_memory.json"
COIN_LEARNING_FILE = "coin_learning.json"
GHOST_FILE = "ghost_signals.json"

VERSION = 5
MAX_MARKET_HISTORY = 20000
MAX_DAILY_REPORTS = 90
MAX_MOVEMENT_HISTORY = 50000
MAX_MOVEMENT_RECENT = 200

DEFAULT_STATE = {
    "version": VERSION,
    "settings": {
        "enabled": True,
        "learning_enabled": True,
        "soft_mode": True,
        "daily_report_enabled": True,
        "movement_hunter_enabled": True,
    },
    "summary": {
        "total_signals": 0,
        "total_ghost_signals": 0,
        "total_real_tp": 0,
        "total_real_sl": 0,
        "total_ghost_tp": 0,
        "total_ghost_sl": 0,
        "last_update": None,
    },
    "market_memory": {
        "last_mode": "UNKNOWN",
        "last_btc_bias": "UNKNOWN",
        "history": [],
        "mode_counts": {},
        "btc_bias_counts": {},
        "last_update": None,
    },
    "movement_hunter": {
        "enabled": True,
        "last_decision": {},
        "history": [],
        "decision_counts": {},
        "phase_counts": {},
        "movement_type_counts": {},
        "direction_counts": {},
        "symbol_counts": {},
        "real_count": 0,
        "ghost_count": 0,
        "reject_count": 0,
        "setup_count": 0,
        "entry_count": 0,
        "fresh_count": 0,
        "late_count": 0,
        "trap_high_count": 0,
        "last_update": None,
    },
    "daily_reports": {},
    "last_rotation_summary": {},
    "health": {
        "confidence": "LOW_DATA",
        "learned_coin_directions": 0,
        "learned_coins": 0,
        "closed_results": 0,
        "last_update": None,
    },
}


def _now() -> int:
    return int(time.time())


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _deepcopy_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    # Avoid importing copy for this small config structure.
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _deepcopy_dict(v)
        elif isinstance(v, list):
            out[k] = list(v)
        else:
            out[k] = v
    return out


def _state() -> Dict[str, Any]:
    s = load_json(AI_MEMORY_FILE, _deepcopy_dict(DEFAULT_STATE))
    if not isinstance(s, dict):
        s = _deepcopy_dict(DEFAULT_STATE)

    # Migration / field repair for old ai_memory.json shape.
    s.setdefault("version", VERSION)
    s.setdefault("settings", {})
    for k, v in DEFAULT_STATE["settings"].items():
        s["settings"].setdefault(k, v)

    s.setdefault("summary", {})
    for k, v in DEFAULT_STATE["summary"].items():
        s["summary"].setdefault(k, v)

    s.setdefault("market_memory", {})
    for k, v in DEFAULT_STATE["market_memory"].items():
        if k in {"history"}:
            s["market_memory"].setdefault(k, [])
            if not isinstance(s["market_memory"].get(k), list):
                s["market_memory"][k] = []
        elif k in {"mode_counts", "btc_bias_counts"}:
            s["market_memory"].setdefault(k, {})
            if not isinstance(s["market_memory"].get(k), dict):
                s["market_memory"][k] = {}
        else:
            s["market_memory"].setdefault(k, v)

    s.setdefault("movement_hunter", {})
    if not isinstance(s.get("movement_hunter"), dict):
        s["movement_hunter"] = {}
    for k, v in DEFAULT_STATE["movement_hunter"].items():
        if k == "history":
            s["movement_hunter"].setdefault(k, [])
            if not isinstance(s["movement_hunter"].get(k), list):
                s["movement_hunter"][k] = []
        elif k in {"last_decision", "decision_counts", "phase_counts", "movement_type_counts", "direction_counts", "symbol_counts"}:
            s["movement_hunter"].setdefault(k, {})
            if not isinstance(s["movement_hunter"].get(k), dict):
                s["movement_hunter"][k] = {}
        else:
            s["movement_hunter"].setdefault(k, v)

    s.setdefault("daily_reports", {})
    if not isinstance(s.get("daily_reports"), dict):
        s["daily_reports"] = {}

    s.setdefault("last_rotation_summary", {})
    if not isinstance(s.get("last_rotation_summary"), dict):
        s["last_rotation_summary"] = {}

    s.setdefault("health", {})
    for k, v in DEFAULT_STATE["health"].items():
        s["health"].setdefault(k, v)
    return s


def _save_state(s: Dict[str, Any]) -> Dict[str, Any]:
    s["version"] = VERSION
    save_json(AI_MEMORY_FILE, s)
    return s


# -------------------- settings --------------------

def get_ai_settings() -> Dict[str, Any]:
    return dict(_state().get("settings", {}))


def set_ai_enabled(enabled: bool) -> Dict[str, Any]:
    s = _state()
    s.setdefault("settings", {})["enabled"] = bool(enabled)
    s["summary"]["last_update"] = _now()
    _save_state(s)
    return get_ai_settings()


def set_ai_learning_enabled(enabled: bool) -> Dict[str, Any]:
    s = _state()
    s.setdefault("settings", {})["learning_enabled"] = bool(enabled)
    s["summary"]["last_update"] = _now()
    _save_state(s)
    return get_ai_settings()


def set_daily_report_enabled(enabled: bool) -> Dict[str, Any]:
    s = _state()
    s.setdefault("settings", {})["daily_report_enabled"] = bool(enabled)
    s["summary"]["last_update"] = _now()
    _save_state(s)
    return get_ai_settings()


# -------------------- raw data readers --------------------

def _learning_state() -> Dict[str, Any]:
    data = load_json(COIN_LEARNING_FILE, {"signals": {}, "by_coin_direction": {}, "coin_archive": {}})
    return data if isinstance(data, dict) else {"signals": {}, "by_coin_direction": {}, "coin_archive": {}}


def _ghost_state() -> Dict[str, Any]:
    data = load_json(GHOST_FILE, {"open": {}, "closed": []})
    return data if isinstance(data, dict) else {"open": {}, "closed": []}


def _iter_learning_signals() -> List[Dict[str, Any]]:
    signals = _learning_state().get("signals", {})
    if not isinstance(signals, dict):
        return []
    return [x for x in signals.values() if isinstance(x, dict)]


def _norm_result(result: Any) -> str:
    r = str(result or "").upper().strip()
    if r in {
        "TP", "TP1", "TAKE_PROFIT", "TAKEPROFIT",
        "EARLY_PROFIT", "AI_EXIT_PROFIT", "DYNAMIC_PROFIT",
        "PROFIT_PROTECT", "PROFIT_PROTECTION"
    }:
        return "TP1"
    if r == "TP2":
        return "TP2"
    if r in {"SL", "STOP", "STOP_LOSS", "STOPLOSS"}:
        return "SL"
    return r


def _norm_type(item: Dict[str, Any]) -> str:
    """Normalize stored signal source.

    Prefer explicit result_source/source fields so Ghost and Real stats do not
    mix when older records use different key names.
    """
    t = str(
        item.get("result_source")
        or item.get("signal_type")
        or item.get("type")
        or item.get("source")
        or "REAL"
    ).upper()
    return "GHOST" if "GHOST" in t or "SHADOW" in t else "REAL"


# -------------------- counters / summaries --------------------

def _learning_counts() -> Dict[str, int]:
    real = ghost = real_tp = real_sl = ghost_tp = ghost_sl = 0
    for item in _iter_learning_signals():
        src = _norm_type(item)
        result = _norm_result(item.get("result"))
        if src == "GHOST":
            ghost += 1
            if result in {"TP1", "TP2"}:
                ghost_tp += 1
            elif result == "SL":
                ghost_sl += 1
        else:
            real += 1
            if result in {"TP1", "TP2"}:
                real_tp += 1
            elif result == "SL":
                real_sl += 1
    return {
        "real": real,
        "ghost": ghost,
        "real_tp": real_tp,
        "real_sl": real_sl,
        "ghost_tp": ghost_tp,
        "ghost_sl": ghost_sl,
        "tp": real_tp + ghost_tp,
        "sl": real_sl + ghost_sl,
    }


def _ghost_counts() -> Dict[str, int]:
    data = _ghost_state()
    open_map = data.get("open", {}) if isinstance(data.get("open", {}), dict) else {}
    closed = data.get("closed", []) if isinstance(data.get("closed", []), list) else []
    tp = 0
    sl = 0
    for item in closed:
        if not isinstance(item, dict):
            continue
        r = _norm_result(item.get("result"))
        if r in {"TP1", "TP2"}:
            tp += 1
        elif r == "SL":
            sl += 1
    return {"open": len(open_map), "closed": len(closed), "tp": tp, "sl": sl}


def _learning_bucket_stats() -> Dict[str, Any]:
    data = _learning_state()
    buckets = data.get("by_coin_direction", {}) if isinstance(data.get("by_coin_direction"), dict) else {}
    archive = data.get("coin_archive", {}) if isinstance(data.get("coin_archive"), dict) else {}
    rows = [b for b in buckets.values() if isinstance(b, dict)]
    learned_dirs = len([r for r in rows if _safe_int(r.get("tp1")) + _safe_int(r.get("tp2")) + _safe_int(r.get("sl")) > 0])
    learned_coins = len([r for r in archive.values() if isinstance(r, dict) and _safe_int(r.get("confidence")) > 0])
    good = len([r for r in rows if str(r.get("behavior", "")).upper() == "GOOD"])
    bad = len([r for r in rows if str(r.get("behavior", "")).upper() == "BAD"])
    weak = len([r for r in rows if str(r.get("behavior", "")).upper() == "WEAK"])
    return {"learned_directions": learned_dirs, "learned_coins": learned_coins, "good": good, "bad": bad, "weak": weak}


def _win_rate(tp: int, sl: int) -> float:
    total = int(tp) + int(sl)
    return round(int(tp) / max(total, 1) * 100, 1) if total else 0.0


def _confidence_label(closed: int, learned_dirs: int, learned_coins: int) -> str:
    if closed >= 120 and learned_dirs >= 20 and learned_coins >= 10:
        return "HIGH"
    if closed >= 40 and learned_dirs >= 8:
        return "MEDIUM"
    if closed >= 10:
        return "LOW_MEDIUM"
    return "LOW_DATA"


def get_ai_summary_counts() -> Dict[str, Any]:
    """Return consistent AI counters from one source per concept.

    Final display rule:
    - Real counts/results come only from coin_learning.json.
    - Ghost open/closed/results come only from ghost_signals.json.
    - ai_memory.summary is only a legacy backup and must not inflate display
      numbers when real files are available.
    This prevents mixed/drifting values like Real=867 in one line and
    Real=989 in another, or Ghost totals that do not equal open+closed.
    """
    lc = _learning_counts()
    gc = _ghost_counts()
    bs = _learning_bucket_stats()

    real = int(lc.get("real", 0))

    # Ghost display is based only on the Ghost file because it is the source
    # of truth for open/closed shadow signals.  Do not use max(...) with
    # legacy counters; that caused inconsistent reports.
    ghost_open = int(gc.get("open", 0))
    ghost_closed = int(gc.get("closed", 0))
    ghost_total = ghost_open + ghost_closed

    real_tp = int(lc.get("real_tp", 0))
    real_sl = int(lc.get("real_sl", 0))
    ghost_tp = int(gc.get("tp", 0))
    ghost_sl = int(gc.get("sl", 0))

    total_tp = real_tp + ghost_tp
    total_sl = real_sl + ghost_sl
    closed = total_tp + total_sl
    confidence = _confidence_label(closed, int(bs.get("learned_directions", 0)), int(bs.get("learned_coins", 0)))

    return {
        "total_signals": real,
        "total_ghost_signals": ghost_total,
        "real_learning": real,
        "ghost_learning": ghost_total,
        "ghost_open": ghost_open,
        "ghost_closed": ghost_closed,
        "real_tp": real_tp,
        "real_sl": real_sl,
        "ghost_tp": ghost_tp,
        "ghost_sl": ghost_sl,
        "tp": total_tp,
        "sl": total_sl,
        "closed_results": closed,
        "win_rate": _win_rate(total_tp, total_sl),
        "real_win_rate": _win_rate(real_tp, real_sl),
        "ghost_win_rate": _win_rate(ghost_tp, ghost_sl),
        "learned_coin_directions": int(bs.get("learned_directions", 0)),
        "learned_coins": int(bs.get("learned_coins", 0)),
        "good_behaviors": int(bs.get("good", 0)),
        "weak_behaviors": int(bs.get("weak", 0)),
        "bad_behaviors": int(bs.get("bad", 0)),
        "confidence": confidence,
    }

# -------------------- update hooks --------------------

def update_ai_summary(
    total_signals: int = 0,
    total_ghost_signals: int = 0,
    total_real_tp: int = 0,
    total_real_sl: int = 0,
    total_ghost_tp: int = 0,
    total_ghost_sl: int = 0,
    **kwargs,
) -> Dict[str, Any]:
    """Increment lightweight counters.

    Display functions still prefer actual coin_learning/ghost files, so these
    counters are backup/compatibility only and will not drift the main status.
    """
    s = _state()
    sm = s.setdefault("summary", {})
    increments = {
        "total_signals": total_signals,
        "total_ghost_signals": total_ghost_signals,
        "total_real_tp": total_real_tp,
        "total_real_sl": total_real_sl,
        "total_ghost_tp": total_ghost_tp,
        "total_ghost_sl": total_ghost_sl,
    }
    # Accept flexible kwargs from old/new modules, e.g. total_tp, total_sl.
    for k in list(increments.keys()):
        if kwargs.get(k) is not None:
            increments[k] += _safe_int(kwargs.get(k))
    # Backward compatibility: older modules may send generic total_tp/total_sl.
    # Route them by source when possible so Ghost results do not pollute Real stats.
    generic_source = str(kwargs.get("source") or kwargs.get("result_source") or "REAL").upper()
    if kwargs.get("total_tp") is not None:
        if "GHOST" in generic_source or "SHADOW" in generic_source:
            increments["total_ghost_tp"] += _safe_int(kwargs.get("total_tp"))
        else:
            increments["total_real_tp"] += _safe_int(kwargs.get("total_tp"))
    if kwargs.get("total_sl") is not None:
        if "GHOST" in generic_source or "SHADOW" in generic_source:
            increments["total_ghost_sl"] += _safe_int(kwargs.get("total_sl"))
        else:
            increments["total_real_sl"] += _safe_int(kwargs.get("total_sl"))

    for k, v in increments.items():
        sm[k] = _safe_int(sm.get(k)) + _safe_int(v)

    # New compatibility path:
    # analysis.py / scanner.py may call update_ai_summary(...) with BTC Lead
    # or Market Mode values in kwargs. Older update_ai_summary ignored these
    # fields, so "وضعیت AI" kept showing BTC: UNKNOWN / Market: UNKNOWN even
    # when analysis.py calculated them correctly. Store them here as market
    # memory without forcing callers to import update_market_memory directly.
    market_mode = (
        kwargs.get("market_mode")
        or kwargs.get("market_regime")
        or kwargs.get("last_mode")
    )
    btc_bias = (
        kwargs.get("btc_bias")
        or kwargs.get("btc_lead_bias")
        or kwargs.get("last_btc_bias")
    )
    snapshot = kwargs.get("market_context") or kwargs.get("snapshot") or {}
    if isinstance(snapshot, dict):
        if market_mode is None:
            market_mode = snapshot.get("market_mode") or snapshot.get("market_regime")
        if btc_bias is None:
            btc_bias = snapshot.get("btc_bias")
    else:
        snapshot = {}

    # Movement Hunter compatibility: callers can pass final AI movement fields
    # without importing record_movement_decision directly. This records only
    # metadata and does not trigger Telegram or real trading.
    movement_decision = (
        kwargs.get("movement_decision")
        or kwargs.get("ai_decision")
        or kwargs.get("final_decision")
        or kwargs.get("decision")
    )
    if movement_decision is not None:
        try:
            _record_movement_decision_in_state(
                s,
                symbol=kwargs.get("symbol") or snapshot.get("symbol"),
                direction=kwargs.get("direction") or snapshot.get("direction"),
                decision=movement_decision,
                move_phase=kwargs.get("move_phase") or kwargs.get("move_state") or snapshot.get("move_phase") or snapshot.get("move_state"),
                movement_type=kwargs.get("movement_type") or kwargs.get("move_type") or snapshot.get("movement_type") or snapshot.get("move_type"),
                confidence=kwargs.get("confidence") or kwargs.get("ai_confidence") or snapshot.get("confidence") or snapshot.get("prediction_score"),
                snapshot=snapshot,
                source=str(kwargs.get("source") or "update_ai_summary"),
            )
        except Exception:
            pass

    if market_mode is not None or btc_bias is not None:
        mm = s.setdefault("market_memory", {})
        mode = str(market_mode or mm.get("last_mode") or "UNKNOWN").upper()
        btc = str(btc_bias or mm.get("last_btc_bias") or "UNKNOWN").upper()
        ts = _now()
        mm["last_mode"] = mode
        mm["last_btc_bias"] = btc
        mm["last_update"] = ts
        mm.setdefault("mode_counts", {})[mode] = _safe_int(mm.setdefault("mode_counts", {}).get(mode)) + 1
        mm.setdefault("btc_bias_counts", {})[btc] = _safe_int(mm.setdefault("btc_bias_counts", {}).get(btc)) + 1
        mm.setdefault("history", []).append({
            "ts": ts,
            "market_mode": mode,
            "btc_bias": btc,
            "source": str(kwargs.get("source") or "update_ai_summary"),
        })
        mm["history"] = mm["history"][-MAX_MARKET_HISTORY:]

    sm["last_update"] = _now()
    _refresh_health_in_state(s)
    _save_state(s)
    return sm


def update_market_memory(market_mode: str = None, btc_bias: str = None, snapshot: Optional[Dict[str, Any]] = None, source: str = "scanner") -> Dict[str, Any]:
    s = _state()
    mm = s.setdefault("market_memory", {})
    snap = snapshot if isinstance(snapshot, dict) else {}
    mode = str(market_mode or snap.get("market_mode") or snap.get("market_regime") or "UNKNOWN").upper()
    btc = str(btc_bias or snap.get("btc_bias") or "UNKNOWN").upper()
    ts = _now()

    mm["last_mode"] = mode
    mm["last_btc_bias"] = btc
    mm["last_update"] = ts
    mm.setdefault("mode_counts", {})[mode] = _safe_int(mm.setdefault("mode_counts", {}).get(mode)) + 1
    mm.setdefault("btc_bias_counts", {})[btc] = _safe_int(mm.setdefault("btc_bias_counts", {}).get(btc)) + 1
    event = {"ts": ts, "market_mode": mode, "btc_bias": btc, "source": source}
    mm.setdefault("history", []).append(event)
    mm["history"] = mm["history"][-MAX_MARKET_HISTORY:]
    _save_state(s)
    return mm



# -------------------- AI Movement Hunter memory --------------------

def _norm_decision(value: Any) -> str:
    d = str(value or "").upper().strip()
    if d in {"REAL", "REAL_SIGNAL", "ACTIVE", "ENTRY", "ENTRY_ACTIVE", "ACTIVATED"}:
        return "REAL"
    if d in {"GHOST", "SHADOW", "GHOST_ONLY", "PAPER_ONLY"}:
        return "GHOST"
    if d in {"REJECT", "REJECTED", "NO_TRADE", "NONE", "NO_SIGNAL"}:
        return "REJECT"
    if d in {"SETUP", "WATCH", "WATCHLIST", "CANDIDATE"}:
        return "SETUP"
    return d or "UNKNOWN"


def _norm_phase(value: Any) -> str:
    p = str(value or "").upper().strip()
    aliases = {
        "START": "START", "FRESH": "START", "NEW": "START",
        "EARLY": "EARLY", "EARLY_MOMENTUM": "EARLY",
        "MID": "MID", "MID_MOVE": "MID", "MIDDLE": "MID",
        "EXHAUSTION": "EXHAUSTION", "LATE": "EXHAUSTION", "LATE_OR_EXHAUSTION": "EXHAUSTION",
        "RANGE": "RANGE", "RANGE_AFTER_MOVE": "RANGE_AFTER_MOVE", "POST_MOVE_RANGE": "RANGE_AFTER_MOVE",
    }
    return aliases.get(p, p or "UNKNOWN")


def _movement_field(snapshot: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    if not isinstance(snapshot, dict):
        return default
    for k in keys:
        if snapshot.get(k) is not None:
            return snapshot.get(k)
    for nest in ("movement_hunter", "ai_movement", "ai_decision", "prediction_layer", "state_awareness", "state", "liquidity_trap"):
        obj = snapshot.get(nest)
        if isinstance(obj, dict):
            for k in keys:
                if obj.get(k) is not None:
                    return obj.get(k)
    return default


def _record_movement_decision_in_state(
    s: Dict[str, Any],
    symbol: str = None,
    direction: str = None,
    decision: str = None,
    move_phase: str = None,
    movement_type: str = None,
    confidence: Any = None,
    snapshot: Optional[Dict[str, Any]] = None,
    source: str = "movement_hunter",
    **kwargs,
) -> Dict[str, Any]:
    mh = s.setdefault("movement_hunter", {})
    snap = snapshot if isinstance(snapshot, dict) else {}
    sym = str(symbol or _movement_field(snap, "symbol", default="UNKNOWN") or "UNKNOWN").upper().strip()
    direct = str(direction or _movement_field(snap, "direction", default="NONE") or "NONE").upper().strip()
    dec = _norm_decision(decision or _movement_field(snap, "decision", "ai_decision", "final_decision", default=None) or kwargs.get("ai_decision"))
    phase = _norm_phase(move_phase or _movement_field(snap, "move_phase", "movement_phase", "move_state", "state", default=None) or kwargs.get("move_state"))
    mtype = str(movement_type or _movement_field(snap, "movement_type", "move_type", "setup_type", default="UNKNOWN") or "UNKNOWN").upper().strip()
    trap = str(_movement_field(snap, "trap_risk", "liquidity_trap_risk", default=kwargs.get("trap_risk") or "UNKNOWN") or "UNKNOWN").upper()
    freshness = str(_movement_field(snap, "freshness", "move_freshness", "fresh_momentum", default=kwargs.get("freshness") or "UNKNOWN") or "UNKNOWN").upper()
    score = _safe_float(confidence if confidence is not None else _movement_field(snap, "confidence", "ai_confidence", "movement_score", "prediction_score", default=0.0), 0.0)
    ts = _now()
    event = {
        "ts": ts,
        "symbol": sym,
        "direction": direct,
        "decision": dec,
        "move_phase": phase,
        "movement_type": mtype,
        "freshness": freshness,
        "trap_risk": trap,
        "confidence": score,
        "source": str(source or "movement_hunter"),
    }
    mh["last_decision"] = event
    mh["last_update"] = ts
    mh.setdefault("history", []).append(event)
    mh["history"] = mh["history"][-MAX_MOVEMENT_HISTORY:]

    def inc_map(name: str, key: str):
        mp = mh.setdefault(name, {})
        mp[key] = _safe_int(mp.get(key)) + 1

    inc_map("decision_counts", dec)
    inc_map("phase_counts", phase)
    inc_map("movement_type_counts", mtype)
    inc_map("direction_counts", direct)
    inc_map("symbol_counts", sym)

    if dec == "REAL":
        mh["real_count"] = _safe_int(mh.get("real_count")) + 1
        mh["entry_count"] = _safe_int(mh.get("entry_count")) + 1
    elif dec == "GHOST":
        mh["ghost_count"] = _safe_int(mh.get("ghost_count")) + 1
    elif dec == "REJECT":
        mh["reject_count"] = _safe_int(mh.get("reject_count")) + 1
    elif dec == "SETUP":
        mh["setup_count"] = _safe_int(mh.get("setup_count")) + 1

    if phase in {"START", "EARLY"}:
        mh["fresh_count"] = _safe_int(mh.get("fresh_count")) + 1
    if phase in {"MID", "EXHAUSTION", "RANGE_AFTER_MOVE"}:
        mh["late_count"] = _safe_int(mh.get("late_count")) + 1
    if trap == "HIGH":
        mh["trap_high_count"] = _safe_int(mh.get("trap_high_count")) + 1
    return event


def record_movement_decision(
    symbol: str = None,
    direction: str = None,
    decision: str = None,
    move_phase: str = None,
    movement_type: str = None,
    confidence: Any = None,
    snapshot: Optional[Dict[str, Any]] = None,
    source: str = "movement_hunter",
    **kwargs,
) -> Dict[str, Any]:
    """Persist one AI Movement Hunter decision without touching trade execution.

    This is a compatibility-safe memory hook. analysis.py/scanner.py/bot.py can
    call it for SETUP, ENTRY/REAL, GHOST, and REJECT decisions. It records how
    the AI judged move freshness/phase/trap context so later learning can see
    whether the bot hunted the start of a move or chased an exhausted move.
    """
    s = _state()
    event = _record_movement_decision_in_state(
        s,
        symbol=symbol,
        direction=direction,
        decision=decision,
        move_phase=move_phase,
        movement_type=movement_type,
        confidence=confidence,
        snapshot=snapshot,
        source=source,
        **kwargs,
    )
    _refresh_health_in_state(s)
    _save_state(s)
    return event

def update_movement_memory(*args, **kwargs) -> Dict[str, Any]:
    """Alias kept for future modules."""
    return record_movement_decision(*args, **kwargs)


def get_movement_hunter_summary() -> Dict[str, Any]:
    mh = _state().get("movement_hunter", {})
    hist = mh.get("history", []) if isinstance(mh.get("history"), list) else []
    recent = hist[-MAX_MOVEMENT_RECENT:]
    fresh = len([x for x in recent if _norm_phase(x.get("move_phase")) in {"START", "EARLY"}])
    late = len([x for x in recent if _norm_phase(x.get("move_phase")) in {"MID", "EXHAUSTION", "RANGE_AFTER_MOVE"}])
    real = len([x for x in recent if _norm_decision(x.get("decision")) == "REAL"])
    ghost = len([x for x in recent if _norm_decision(x.get("decision")) == "GHOST"])
    reject = len([x for x in recent if _norm_decision(x.get("decision")) == "REJECT"])
    total = max(len(recent), 1)
    return {
        "enabled": bool(mh.get("enabled", True)),
        "last_decision": mh.get("last_decision", {}),
        "total_events": len(hist),
        "recent_events": len(recent),
        "recent_fresh": fresh,
        "recent_late": late,
        "recent_real": real,
        "recent_ghost": ghost,
        "recent_reject": reject,
        "recent_fresh_pct": round(fresh / total * 100, 1) if recent else 0.0,
        "decision_counts": mh.get("decision_counts", {}),
        "phase_counts": mh.get("phase_counts", {}),
        "movement_type_counts": mh.get("movement_type_counts", {}),
        "real_count": _safe_int(mh.get("real_count")),
        "ghost_count": _safe_int(mh.get("ghost_count")),
        "reject_count": _safe_int(mh.get("reject_count")),
        "setup_count": _safe_int(mh.get("setup_count")),
        "entry_count": _safe_int(mh.get("entry_count")),
        "fresh_count": _safe_int(mh.get("fresh_count")),
        "late_count": _safe_int(mh.get("late_count")),
        "trap_high_count": _safe_int(mh.get("trap_high_count")),
    }


def format_movement_hunter_status() -> str:
    m = get_movement_hunter_summary()
    last = m.get("last_decision", {}) if isinstance(m.get("last_decision"), dict) else {}
    last_line = "آخرین شکار: -"
    if last:
        last_line = f"آخرین شکار: {last.get('symbol','?')} {last.get('direction','?')} | {last.get('decision','?')} | فاز:{last.get('move_phase','?')}"
    return (
        "🎯 AI Movement Hunter\n"
        f"فعال: {'بله' if m.get('enabled') else 'خیر'}\n"
        f"رویدادها: {m.get('total_events', 0)} | اخیر: {m.get('recent_events', 0)}\n"
        f"Fresh اخیر: {m.get('recent_fresh', 0)} ({m.get('recent_fresh_pct', 0)}٪) | Late/Exhaustion اخیر: {m.get('recent_late', 0)}\n"
        f"REAL/GHOST/REJECT اخیر: {m.get('recent_real', 0)}/{m.get('recent_ghost', 0)}/{m.get('recent_reject', 0)}\n"
        f"{last_line}"
    )

def save_rotation_summary(summary: Optional[Dict[str, Any]] = None, best: Optional[List[Dict[str, Any]]] = None, worst: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    s = _state()
    row = {
        "updated_at": _now(),
        "summary": summary or {},
        "best": best or [],
        "worst": worst or [],
    }
    s["last_rotation_summary"] = row
    _save_state(s)
    return row


def update_daily_report_memory(date: str = None, report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    s = _state()
    d = date or _today()
    counts = get_ai_summary_counts()
    row = dict(report or {})
    row.setdefault("date", d)
    row.setdefault("counts", counts)
    row["updated_at"] = _now()
    s.setdefault("daily_reports", {})[d] = row

    # Keep enough daily reports for the 90-day time-weighted learning window.
    keys = sorted(s["daily_reports"].keys())
    for old in keys[:-MAX_DAILY_REPORTS]:
        s["daily_reports"].pop(old, None)
    _save_state(s)
    return row


def _refresh_health_in_state(s: Dict[str, Any]) -> Dict[str, Any]:
    counts = get_ai_summary_counts()
    h = s.setdefault("health", {})
    movement = get_movement_hunter_summary() if "get_movement_hunter_summary" in globals() else {}
    h.update({
        "confidence": counts.get("confidence", "LOW_DATA"),
        "learned_coin_directions": counts.get("learned_coin_directions", 0),
        "learned_coins": counts.get("learned_coins", 0),
        "closed_results": counts.get("closed_results", 0),
        "movement_events": movement.get("total_events", 0),
        "movement_recent_fresh_pct": movement.get("recent_fresh_pct", 0),
        "last_update": _now(),
    })
    return h


def refresh_ai_health() -> Dict[str, Any]:
    s = _state()
    h = _refresh_health_in_state(s)
    _save_state(s)
    return h


# -------------------- formatters --------------------

def _format_last_market() -> str:
    mm = _state().get("market_memory", {})
    mode = mm.get("last_mode", "UNKNOWN")
    btc = mm.get("last_btc_bias", "UNKNOWN")
    return f"حالت بازار: {mode} | BTC: {btc}"


def _format_rotation_short() -> str:
    rot = _state().get("last_rotation_summary", {})
    best = rot.get("best", []) if isinstance(rot, dict) else []
    worst = rot.get("worst", []) if isinstance(rot, dict) else []
    def names(rows):
        out = []
        for r in rows[:3]:
            if isinstance(r, dict):
                out.append(str(r.get("symbol") or r.get("coin") or "?"))
            else:
                out.append(str(r))
        return ", ".join(out) if out else "-"
    return f"Rotation بهتر: {names(best)}\nRotation ضعیف: {names(worst)}"



def _format_movement_short() -> str:
    try:
        m = get_movement_hunter_summary()
        last = m.get("last_decision", {}) if isinstance(m.get("last_decision"), dict) else {}
        last_txt = "-"
        if last:
            last_txt = f"{last.get('symbol','?')} {last.get('direction','?')} {last.get('decision','?')} فاز:{last.get('move_phase','?')}"
        return (
            f"Movement Hunter: رویداد {m.get('total_events', 0)} | Fresh اخیر {m.get('recent_fresh', 0)} "
            f"({m.get('recent_fresh_pct', 0)}٪) | Late اخیر {m.get('recent_late', 0)}\n"
            f"آخرین: {last_txt}"
        )
    except Exception:
        return "Movement Hunter: نامشخص"

def format_ai_status() -> str:
    s = _state()
    st = s.get("settings", {})
    sm = get_ai_summary_counts()
    return (
        "🤖 وضعیت AI\n"
        f"فعال: {'بله' if st.get('enabled') else 'خیر'}\n"
        f"یادگیری: {'بله' if st.get('learning_enabled') else 'خیر'}\n"
        f"گزارش روزانه: {'بله' if st.get('daily_report_enabled') else 'خیر'}\n"
        f"اعتماد AI: {sm.get('confidence')}\n"
        f"سیگنال واقعی ثبت‌شده: {sm.get('total_signals', 0)} | TP:{sm.get('real_tp', 0)} SL:{sm.get('real_sl', 0)} | WR:{sm.get('real_win_rate', 0)}%\n"
        f"Ghost: {sm.get('total_ghost_signals', 0)} | باز:{sm.get('ghost_open', 0)} بسته:{sm.get('ghost_closed', 0)} | TP:{sm.get('ghost_tp', 0)} SL:{sm.get('ghost_sl', 0)}\n"
        f"کل TP/SL: TP:{sm.get('tp', 0)} SL:{sm.get('sl', 0)} | WR:{sm.get('win_rate', 0)}%\n"
        f"کوین‌های یادگرفته‌شده: {sm.get('learned_coins', 0)} | جهت‌های یادگرفته‌شده: {sm.get('learned_coin_directions', 0)}\n"
        f"رفتارها: خوب {sm.get('good_behaviors', 0)} | ضعیف {sm.get('weak_behaviors', 0)} | بد {sm.get('bad_behaviors', 0)}\n"
        f"{_format_last_market()}\n"
        f"{_format_movement_short()}"
    )


def format_ai_daily_report(date: str = None) -> str:
    d = date or _today()
    row = update_daily_report_memory(d)
    c = row.get("counts", {})
    return (
        f"📅 گزارش روزانه AI - {d}\n"
        f"Real: {c.get('real_learning', 0)} | Ghost: {c.get('total_ghost_signals', 0)}\n"
        f"Real TP/SL: {c.get('real_tp', 0)}/{c.get('real_sl', 0)} | WR:{c.get('real_win_rate', 0)}%\n"
        f"Ghost TP/SL: {c.get('ghost_tp', 0)}/{c.get('ghost_sl', 0)} | WR:{c.get('ghost_win_rate', 0)}%\n"
        f"Confidence: {c.get('confidence')} | Learned Coins: {c.get('learned_coins', 0)}"
    )


def format_ai_memory_summary() -> str:
    return format_ai_status() + "\n" + _format_rotation_short()

# -------------------- compatibility aliases --------------------

def format_learning_summary() -> str:
    """Compatibility helper used by bot.py in some deployments."""
    return format_ai_memory_summary()


def ai_status_text() -> str:
    return format_ai_status()


def enable_ai() -> Dict[str, Any]:
    return set_ai_enabled(True)


def disable_ai() -> Dict[str, Any]:
    return set_ai_enabled(False)


def enable_ai_learning() -> Dict[str, Any]:
    return set_ai_learning_enabled(True)


def disable_ai_learning() -> Dict[str, Any]:
    return set_ai_learning_enabled(False)


# Movement Hunter compatibility aliases
def movement_hunter_status_text() -> str:
    return format_movement_hunter_status()

def ai_movement_status_text() -> str:
    return format_movement_hunter_status()
