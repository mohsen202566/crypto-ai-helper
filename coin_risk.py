# -*- coding: utf-8 -*-
"""
coin_risk.py

AI Coin Risk / Direction / Condition Memory Engine

Purpose:
- Keep DAILY risk memory for fast same-day strictness.
- Keep LONG-TERM archive so the bot does not forget coin behavior after midnight.
- Track REAL and GHOST results separately, but use both for learning.
- Learn risk per coin + direction + condition, not one universal rule.
- Return compact risk state used by analysis.py without hard-blocking by itself.

Design rule:
- Risk memory is per coin AND per direction.
- Similar conditions, e.g. DOGE SHORT + ADX 18-22 + VWAP_AGAINST, get their own soft strictness.
- After repeated SLs, strictness rises gradually.
- TP results reduce risk gradually, but do not erase history.
- Data persists across code updates.
"""

import time
import math
from typing import Dict, Any, Optional, List

from data_store import load_json, save_json

try:
    from config import DAILY_SL_STRICTNESS_START, MAX_DAILY_STRICTNESS_LEVEL
except Exception:
    DAILY_SL_STRICTNESS_START = 3
    MAX_DAILY_STRICTNESS_LEVEL = 5

RISK_FILE = "coin_risk.json"
DAY_SECONDS = 86400
MAX_RECENT_EVENTS = 20000
VERSION = 4


def _now() -> int:
    return int(time.time())


def _day_key(ts: Optional[int] = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts or _now()))


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


def _bucket_number(value: Any, step: float, default: str = "NA", min_value: float = None, max_value: float = None) -> str:
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
    return f"{round(base, 4)}-{round(hi, 4)}"


def _empty_counts() -> Dict[str, Any]:
    return {
        "tp": 0,
        "tp1": 0,
        "tp2": 0,
        "sl": 0,
        "real_tp": 0,
        "real_sl": 0,
        "ghost_tp": 0,
        "ghost_sl": 0,
    }


def _empty_bucket(symbol: str, direction: str) -> Dict[str, Any]:
    row = {
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "risk_score": 0,
        "strictness_level": 0,
        "condition_stats": {},
        "time_stats": {},
        "last_result": None,
        "last_updated": 0,
    }
    row.update(_empty_counts())
    return row


def _default_state() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "days": {},
        "archive": {},
        "recent_events": [],
        "updated_at": 0,
    }


def _state() -> Dict[str, Any]:
    s = load_json(RISK_FILE, _default_state())
    if not isinstance(s, dict):
        s = _default_state()
    s.setdefault("version", VERSION)
    s.setdefault("days", {})
    s.setdefault("archive", {})
    s.setdefault("recent_events", [])
    s.setdefault("updated_at", 0)
    if not isinstance(s.get("days"), dict):
        s["days"] = {}
    if not isinstance(s.get("archive"), dict):
        s["archive"] = {}
    if not isinstance(s.get("recent_events"), list):
        s["recent_events"] = []
    return s


def _ensure_bucket_fields(row: Dict[str, Any], symbol: str, direction: str) -> None:
    defaults = _empty_bucket(symbol, direction)
    for k, v in defaults.items():
        row.setdefault(k, v)
    if not isinstance(row.get("condition_stats"), dict):
        row["condition_stats"] = {}
    if not isinstance(row.get("time_stats"), dict):
        row["time_stats"] = {}


def _get_day_row(s: Dict[str, Any], symbol: str, direction: str, ts: Optional[int] = None) -> Dict[str, Any]:
    day = _day_key(ts)
    d = s["days"].setdefault(day, {})
    k = _key(symbol, direction)
    row = d.setdefault(k, _empty_bucket(symbol, direction))
    _ensure_bucket_fields(row, symbol, direction)
    return row


def _get_archive_row(s: Dict[str, Any], symbol: str, direction: str) -> Dict[str, Any]:
    k = _key(symbol, direction)
    row = s["archive"].setdefault(k, _empty_bucket(symbol, direction))
    row.setdefault("first_seen", _now())
    row.setdefault("recent_7d", _empty_counts())
    row.setdefault("recent_30d", _empty_counts())
    row.setdefault("behavior", "UNKNOWN")
    row.setdefault("confidence", 0)
    _ensure_bucket_fields(row, symbol, direction)
    return row


def _normal_result(result: str) -> str:
    r = str(result or "").upper().strip()
    if r in {"TP", "TP1", "TAKE_PROFIT", "TAKEPROFIT", "EARLY_PROFIT", "AI_EXIT_PROFIT", "DYNAMIC_PROFIT", "PROFIT_PROTECT"}:
        return "TP1"
    if r == "TP2":
        return "TP2"
    if r in {"SL", "STOP", "STOP_LOSS", "STOPLOSS"}:
        return "SL"
    return r or "UNKNOWN"


def _normal_source(source: Optional[str] = None, is_ghost: Optional[bool] = None) -> str:
    if is_ghost is True:
        return "GHOST"
    src = str(source or "REAL").upper().strip()
    if src in {"GHOST", "SHADOW", "PAPER_GHOST"}:
        return "GHOST"
    return "REAL"


def _apply_result(row: Dict[str, Any], result: str, source: str, ts: int) -> None:
    if result == "SL":
        row["sl"] = int(row.get("sl", 0)) + 1
        row["ghost_sl" if source == "GHOST" else "real_sl"] = int(row.get("ghost_sl" if source == "GHOST" else "real_sl", 0)) + 1
    elif result in {"TP1", "TP2"}:
        row["tp"] = int(row.get("tp", 0)) + 1
        row["tp1" if result == "TP1" else "tp2"] = int(row.get("tp1" if result == "TP1" else "tp2", 0)) + 1
        row["ghost_tp" if source == "GHOST" else "real_tp"] = int(row.get("ghost_tp" if source == "GHOST" else "real_tp", 0)) + 1
    row["last_result"] = result
    row["last_updated"] = ts


def _compact_snapshot(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    keys = [
        "rsi", "rsi_5m", "rsi_slope_15m", "macd_hist", "macd_hist_slope_15m",
        "macd_hist_accel_15m", "adx", "adx_slope_15m", "vwap_status",
        "vwap_distance_pct", "ema_structure_15m", "market_regime", "market_mode",
        "btc_bias", "move_state", "trap_risk", "prediction_score", "reversal_risk_score",
        "relative_status", "timeframe_core", "entry_timing_tf",
    ]
    out = {k: snapshot.get(k) for k in keys if snapshot.get(k) is not None}
    for nk in ("liquidity_trap", "state_awareness", "prediction_layer", "candle_behavior"):
        if isinstance(snapshot.get(nk), dict):
            out[nk] = snapshot.get(nk)
    return out


def _condition_keys(direction: str, snapshot: Optional[Dict[str, Any]]) -> List[str]:
    snap = _compact_snapshot(snapshot)
    d = str(direction or snap.get("direction") or "UNKNOWN").upper()
    state = snap.get("move_state") or ((snap.get("state_awareness") or {}).get("move_state") if isinstance(snap.get("state_awareness"), dict) else None) or "NA"
    trap = snap.get("trap_risk") or ((snap.get("liquidity_trap") or {}).get("trap_risk") if isinstance(snap.get("liquidity_trap"), dict) else None) or "NA"
    keys = [
        f"{d}:ADX:{_bucket_number(snap.get('adx'), 4, min_value=0, max_value=80)}",
        f"{d}:ADX_SLOPE:{_bucket_number(snap.get('adx_slope_15m'), 1, min_value=-10, max_value=10)}",
        f"{d}:RSI:{_bucket_number(snap.get('rsi'), 5, min_value=0, max_value=100)}",
        f"{d}:RSI_SLOPE:{_bucket_number(snap.get('rsi_slope_15m'), 1, min_value=-10, max_value=10)}",
        f"{d}:MACD_ACCEL:{_bucket_number(snap.get('macd_hist_accel_15m'), 0.0005, min_value=-0.01, max_value=0.01)}",
        f"{d}:VWAP:{str(snap.get('vwap_status') or 'NA').upper()}",
        f"{d}:EMA:{str(snap.get('ema_structure_15m') or 'NA').upper()}",
        f"{d}:STATE:{str(state).upper()}",
        f"{d}:TRAP:{str(trap).upper()}",
        f"{d}:PRED:{_bucket_number(snap.get('prediction_score'), 10, min_value=0, max_value=100)}",
        f"{d}:REVERSAL:{_bucket_number(snap.get('reversal_risk_score'), 10, min_value=0, max_value=100)}",
        f"{d}:MARKET:{str(snap.get('market_regime') or snap.get('market_mode') or 'NA').upper()}",
        f"{d}:BTC:{str(snap.get('btc_bias') or 'NA').upper()}",
    ]
    adx = _bucket_number(snap.get("adx"), 4, min_value=0, max_value=80)
    vwap = str(snap.get("vwap_status") or "NA").upper()
    keys.append(f"{d}:ADX_VWAP:{adx}:{vwap}")
    keys.append(f"{d}:STATE_TRAP:{str(state).upper()}:{str(trap).upper()}")
    return keys


def _update_stat_row(row: Dict[str, Any], result: str, source: str) -> None:
    row.setdefault("tp_w", 0.0)
    row.setdefault("sl_w", 0.0)
    row.setdefault("total_w", 0.0)
    w = 0.5 if source == "GHOST" else 1.0
    row["total_w"] = _safe_float(row.get("total_w")) + w
    if result == "SL":
        row["sl_w"] = _safe_float(row.get("sl_w")) + w
    elif result in {"TP1", "TP2"}:
        row["tp_w"] = _safe_float(row.get("tp_w")) + w


def _update_condition_stats(row: Dict[str, Any], direction: str, result: str, source: str, snapshot: Optional[Dict[str, Any]]) -> None:
    stats = row.setdefault("condition_stats", {})
    for ck in _condition_keys(direction, snapshot):
        _update_stat_row(stats.setdefault(ck, {}), result, source)
    snap = _compact_snapshot(snapshot)
    ts = _safe_float(snap.get("snapshot_at"), 0) or _now()
    hour = time.gmtime(int(ts)).tm_hour
    _update_stat_row(row.setdefault("time_stats", {}).setdefault(f"UTC_HOUR:{hour:02d}", {}), result, source)


def _append_event(s: Dict[str, Any], symbol: str, direction: str, result: str, source: str, ts: int, snapshot: Optional[Dict[str, Any]] = None) -> None:
    event = {
        "ts": ts,
        "day": _day_key(ts),
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "result": result,
        "source": source,
    }
    snap = _compact_snapshot(snapshot)
    if snap:
        event["snapshot"] = snap
    events = s.setdefault("recent_events", [])
    events.append(event)
    if len(events) > MAX_RECENT_EVENTS:
        del events[:-MAX_RECENT_EVENTS]


def _count_recent_events(s: Dict[str, Any], symbol: str, direction: str, days: int) -> Dict[str, int]:
    cutoff = _now() - int(days) * DAY_SECONDS
    sym = str(symbol).upper()
    direct = str(direction).upper()
    out = _empty_counts()
    for ev in s.get("recent_events", []):
        if not isinstance(ev, dict) or int(ev.get("ts", 0) or 0) < cutoff:
            continue
        if str(ev.get("symbol", "")).upper() != sym or str(ev.get("direction", "")).upper() != direct:
            continue
        result = _normal_result(ev.get("result"))
        source = _normal_source(ev.get("source"))
        if result == "SL":
            out["sl"] += 1
            out["ghost_sl" if source == "GHOST" else "real_sl"] += 1
        elif result in {"TP1", "TP2"}:
            out["tp"] += 1
            out["tp1" if result == "TP1" else "tp2"] += 1
            out["ghost_tp" if source == "GHOST" else "real_tp"] += 1
    return out


def _risk_score(daily: Dict[str, Any], archive: Dict[str, Any], recent_7d: Dict[str, int], recent_30d: Dict[str, int] = None) -> int:
    """Time-weighted risk score.

    Recent behavior should dominate old archive data for scalping.
    0-1 day has the highest weight, 7 days medium/high, 30 days medium,
    and all-time archive only a small background influence.
    """
    recent_30d = recent_30d or _empty_counts()
    daily_score = (
        int(daily.get("real_sl", 0)) * 24
        + int(daily.get("ghost_sl", 0)) * 12
        - int(daily.get("real_tp", 0)) * 9
        - int(daily.get("ghost_tp", 0)) * 4
    )
    recent_7_score = (
        int(recent_7d.get("real_sl", 0)) * 10
        + int(recent_7d.get("ghost_sl", 0)) * 5
        - int(recent_7d.get("real_tp", 0)) * 4
        - int(recent_7d.get("ghost_tp", 0)) * 2
    )
    recent_30_score = (
        int(recent_30d.get("real_sl", 0)) * 4
        + int(recent_30d.get("ghost_sl", 0)) * 2
        - int(recent_30d.get("real_tp", 0)) * 2
        - int(recent_30d.get("ghost_tp", 0))
    )
    long_score = (
        int(archive.get("real_sl", 0)) * 1.2
        + int(archive.get("ghost_sl", 0)) * 0.6
        - int(archive.get("real_tp", 0)) * 0.6
        - int(archive.get("ghost_tp", 0)) * 0.3
    )
    return max(0, min(100, int(daily_score + recent_7_score + recent_30_score + long_score)))


def _behavior_from_stats(tp: int, sl: int) -> str:
    total = tp + sl
    if total < 5:
        return "UNKNOWN"
    wr = tp / max(total, 1)
    if wr >= 0.68:
        return "GOOD"
    if wr >= 0.55:
        return "NORMAL"
    if wr >= 0.45:
        return "WEAK"
    return "BAD"


def _strictness_level(daily: Dict[str, Any], archive: Dict[str, Any], recent_7d: Dict[str, int], risk_score: int, condition_penalty: int = 0) -> int:
    # User preference: adaptive strictness must start from the 3rd SL
    # for the same coin+direction, not earlier.
    start = max(3, int(DAILY_SL_STRICTNESS_START))
    max_level = max(1, int(MAX_DAILY_STRICTNESS_LEVEL))
    daily_sl_weighted = int(daily.get("real_sl", 0)) + int(daily.get("ghost_sl", 0)) * 0.5
    recent_sl_weighted = int(recent_7d.get("real_sl", 0)) + int(recent_7d.get("ghost_sl", 0)) * 0.5
    archive_sl_weighted = int(archive.get("real_sl", 0)) + int(archive.get("ghost_sl", 0)) * 0.35
    level = 0
    if daily_sl_weighted >= start:
        level += int(daily_sl_weighted - start + 1)
    if recent_sl_weighted >= start + 1:
        level += 1
    if archive_sl_weighted >= 5:
        level += 1
    if risk_score >= 70:
        level += 1
    if risk_score >= 90:
        level += 1
    level += int(condition_penalty)
    daily_tp = int(daily.get("real_tp", 0)) + int(daily.get("ghost_tp", 0)) * 0.5
    daily_sl = int(daily.get("real_sl", 0)) + int(daily.get("ghost_sl", 0)) * 0.5
    if daily_tp >= 3 and daily_sl <= 1 and daily_tp >= daily_sl * 2 and level > 0:
        level -= 1
    return max(0, min(max_level, int(level)))


def _condition_penalty_from_stats(row: Dict[str, Any], direction: str, snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    stats = row.get("condition_stats", {}) if isinstance(row.get("condition_stats"), dict) else {}
    penalty = 0
    weak_matches = []
    for ck in _condition_keys(direction, snapshot):
        st = stats.get(ck)
        if not isinstance(st, dict):
            continue
        tpw = _safe_float(st.get("tp_w"))
        slw = _safe_float(st.get("sl_w"))
        total = tpw + slw
        if total >= 3 and slw > tpw:
            penalty += 1
            weak_matches.append(ck)
        if total >= 5 and slw >= tpw * 1.7:
            penalty += 1
    return {"condition_penalty": min(2, penalty), "weak_condition_matches": weak_matches[:5]}


def _refresh_archive_computed_fields(s: Dict[str, Any], symbol: str, direction: str, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    daily = _get_day_row(s, symbol, direction)
    archive = _get_archive_row(s, symbol, direction)
    recent_7d = _count_recent_events(s, symbol, direction, 7)
    recent_30d = _count_recent_events(s, symbol, direction, 30)
    score = _risk_score(daily, archive, recent_7d, recent_30d)
    cond = _condition_penalty_from_stats(archive, direction, snapshot)
    strict = _strictness_level(daily, archive, recent_7d, score, cond.get("condition_penalty", 0))
    archive["recent_7d"] = recent_7d
    archive["recent_30d"] = recent_30d
    archive["risk_score"] = score
    archive["strictness_level"] = strict
    archive["behavior"] = _behavior_from_stats(int(archive.get("tp", 0)), int(archive.get("sl", 0)))
    archive["confidence"] = min(100, (int(archive.get("tp", 0)) + int(archive.get("sl", 0))) * 5)
    return archive


def register_result(symbol: str, direction: str, result: str, source: str = "REAL", snapshot: Optional[Dict[str, Any]] = None, is_ghost: Optional[bool] = None) -> Dict[str, Any]:
    ts = _now()
    result_norm = _normal_result(result)
    source_norm = _normal_source(source, is_ghost=is_ghost)
    s = _state()
    daily = _get_day_row(s, symbol, direction, ts)
    archive = _get_archive_row(s, symbol, direction)
    _apply_result(daily, result_norm, source_norm, ts)
    _apply_result(archive, result_norm, source_norm, ts)
    _update_condition_stats(daily, direction, result_norm, source_norm, snapshot)
    _update_condition_stats(archive, direction, result_norm, source_norm, snapshot)
    _append_event(s, symbol, direction, result_norm, source_norm, ts, snapshot=snapshot)
    _refresh_archive_computed_fields(s, symbol, direction, snapshot=snapshot)
    s["version"] = VERSION
    s["updated_at"] = ts
    save_json(RISK_FILE, s)
    return get_direction_risk_state(symbol, direction, snapshot=snapshot)


def register_ghost_result(symbol: str, direction: str, result: str, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return register_result(symbol, direction, result, source="GHOST", snapshot=snapshot, is_ghost=True)


def register_real_result(symbol: str, direction: str, result: str, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return register_result(symbol, direction, result, source="REAL", snapshot=snapshot, is_ghost=False)


def get_direction_risk_state(symbol: str, direction: str, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    s = _state()
    daily = _get_day_row(s, symbol, direction)
    archive = _refresh_archive_computed_fields(s, symbol, direction, snapshot=snapshot)
    recent_7d = archive.get("recent_7d", {}) or {}
    recent_30d = archive.get("recent_30d", {}) or {}
    cond = _condition_penalty_from_stats(archive, direction, snapshot)
    daily_sl = int(daily.get("sl", 0))
    daily_tp = int(daily.get("tp", 0))
    strict = int(archive.get("strictness_level", 0) or 0)
    risk_score = int(archive.get("risk_score", 0) or 0)
    out = {
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "sl_count": daily_sl,
        "tp_count": daily_tp,
        "strictness_level": strict,
        "risk_score": risk_score,
        "bad_day": daily_sl >= max(3, int(DAILY_SL_STRICTNESS_START)),
        "recommend_reduce": strict >= 3 or risk_score >= 70,
        "hard_block": False,
        "soft_layer": True,
        "condition_penalty": cond.get("condition_penalty", 0),
        "weak_condition_matches": cond.get("weak_condition_matches", []),
        "daily": dict(daily),
        "archive": {
            "tp": int(archive.get("tp", 0)),
            "sl": int(archive.get("sl", 0)),
            "real_tp": int(archive.get("real_tp", 0)),
            "real_sl": int(archive.get("real_sl", 0)),
            "ghost_tp": int(archive.get("ghost_tp", 0)),
            "ghost_sl": int(archive.get("ghost_sl", 0)),
            "behavior": archive.get("behavior", "UNKNOWN"),
            "confidence": int(archive.get("confidence", 0) or 0),
        },
        "recent_7d": dict(recent_7d),
        "recent_30d": dict(recent_30d),
        "source_weights": {"real_sl": 1.0, "ghost_sl": 0.5, "real_tp": 1.0, "ghost_tp": 0.5, "time_weighting": "daily > 7d > 30d > archive"},
        "message": "ریسک نرم/تدریجی است؛ این لایه به‌تنهایی سیگنال را بلاک نمی‌کند.",
    }
    return out


def get_condition_risk_state(symbol: str, direction: str, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Explicit helper for analysis.py/scanner.py when they want condition-aware soft risk."""
    return get_direction_risk_state(symbol, direction, snapshot=snapshot)


def format_coin_risk_report(symbol: Optional[str] = None, limit: int = 12) -> str:
    s = _state()
    rows = []
    for k, row in (s.get("archive") or {}).items():
        if symbol and not k.startswith(str(symbol).upper()):
            continue
        try:
            sym, direction = k.split(":", 1)
        except Exception:
            sym, direction = row.get("symbol", "?"), row.get("direction", "?")
        state = get_direction_risk_state(sym, direction)
        rows.append((state.get("risk_score", 0), state.get("strictness_level", 0), sym, direction, state))
    rows.sort(reverse=True, key=lambda x: (x[0], x[1]))
    if not rows:
        return "هنوز داده ریسک کوین ثبت نشده."
    lines = ["🧠 گزارش ریسک AI کوین‌ها"]
    for risk, strict, sym, direction, st in rows[:max(1, int(limit))]:
        ar = st.get("archive", {})
        lines.append(
            f"{sym} {direction} | Risk={risk} | Strict={strict} | Cond+{st.get('condition_penalty',0)} | "
            f"Real SL/TP={ar.get('real_sl',0)}/{ar.get('real_tp',0)} | "
            f"Ghost SL/TP={ar.get('ghost_sl',0)}/{ar.get('ghost_tp',0)} | "
            f"رفتار={ar.get('behavior','UNKNOWN')}"
        )
    return "\n".join(lines)
