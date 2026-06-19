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

DEFAULT_STATE = {
    "version": 3,
    "settings": {
        "enabled": True,
        "learning_enabled": True,
        "soft_mode": True,
        "daily_report_enabled": True,
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
    s.setdefault("version", 3)
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
    s["version"] = 3
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
    if r in {"TP", "TP1", "TAKE_PROFIT", "TAKEPROFIT"}:
        return "TP1"
    if r == "TP2":
        return "TP2"
    if r in {"SL", "STOP", "STOP_LOSS", "STOPLOSS"}:
        return "SL"
    return r


def _norm_type(item: Dict[str, Any]) -> str:
    t = str(item.get("signal_type") or item.get("type") or item.get("source") or "REAL").upper()
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
    """Return display-safe AI counters from real stored learning/Ghost data."""
    s = _state()
    sm = s.get("summary", {})
    lc = _learning_counts()
    gc = _ghost_counts()
    bs = _learning_bucket_stats()

    real = int(lc.get("real", 0))
    ghost_learning = int(lc.get("ghost", 0))
    ghost_file_total = int(gc.get("open", 0)) + int(gc.get("closed", 0))
    ghost_total = max(ghost_learning, ghost_file_total, _safe_int(sm.get("total_ghost_signals")))
    total_signals = real if real > 0 else _safe_int(sm.get("total_signals"))

    real_tp = int(lc.get("real_tp", 0))
    real_sl = int(lc.get("real_sl", 0))
    ghost_tp = max(int(lc.get("ghost_tp", 0)), int(gc.get("tp", 0)))
    ghost_sl = max(int(lc.get("ghost_sl", 0)), int(gc.get("sl", 0)))
    total_tp = real_tp + ghost_tp
    total_sl = real_sl + ghost_sl
    closed = total_tp + total_sl
    confidence = _confidence_label(closed, int(bs.get("learned_directions", 0)), int(bs.get("learned_coins", 0)))

    return {
        "total_signals": total_signals,
        "total_ghost_signals": ghost_total,
        "real_learning": real,
        "ghost_learning": ghost_learning,
        "ghost_open": int(gc.get("open", 0)),
        "ghost_closed": int(gc.get("closed", 0)),
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
    if kwargs.get("total_tp") is not None:
        increments["total_real_tp"] += _safe_int(kwargs.get("total_tp"))
    if kwargs.get("total_sl") is not None:
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
        mm["history"] = mm["history"][-240:]

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
    mm["history"] = mm["history"][-240:]
    _save_state(s)
    return mm


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

    # Keep last 45 daily reports.
    keys = sorted(s["daily_reports"].keys())
    for old in keys[:-45]:
        s["daily_reports"].pop(old, None)
    _save_state(s)
    return row


def _refresh_health_in_state(s: Dict[str, Any]) -> Dict[str, Any]:
    counts = get_ai_summary_counts()
    h = s.setdefault("health", {})
    h.update({
        "confidence": counts.get("confidence", "LOW_DATA"),
        "learned_coin_directions": counts.get("learned_coin_directions", 0),
        "learned_coins": counts.get("learned_coins", 0),
        "closed_results": counts.get("closed_results", 0),
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
        f"{_format_last_market()}"
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
