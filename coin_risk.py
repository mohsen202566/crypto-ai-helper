# -*- coding: utf-8 -*-
"""
coin_risk.py

AI Coin Risk / Direction Memory Engine

Purpose:
- Keep DAILY risk memory for fast same-day strictness.
- Keep LONG-TERM archive so the bot does not forget coin behavior after midnight.
- Track REAL and GHOST results separately, but use both for learning.
- Return a compact risk state used directly by analysis.py:
    sl_count, tp_count, strictness_level, risk_score, bad_day, recommend_reduce

Design rule agreed with user:
- Risk memory is per coin AND per direction.
  Example: DOGEUSDT:LONG is separate from DOGEUSDT:SHORT.
- After 2 SLs, the next signal for that coin+direction should become stricter.
- Further SLs increase strictness gradually.
- TP results reduce risk gradually, but do not erase history completely.
- Data must persist across code updates.
"""

import time
from typing import Dict, Any, Optional

from data_store import load_json, save_json
from config import DAILY_SL_STRICTNESS_START, MAX_DAILY_STRICTNESS_LEVEL

RISK_FILE = "coin_risk.json"
DAY_SECONDS = 86400
MAX_RECENT_EVENTS = 300


def _now() -> int:
    return int(time.time())


def _day_key(ts: Optional[int] = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts or _now()))


def _key(symbol: str, direction: str) -> str:
    return f"{str(symbol).upper()}:{str(direction).upper()}"


def _empty_bucket(symbol: str, direction: str) -> Dict[str, Any]:
    return {
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "tp": 0,
        "tp1": 0,
        "tp2": 0,
        "sl": 0,
        "real_tp": 0,
        "real_sl": 0,
        "ghost_tp": 0,
        "ghost_sl": 0,
        "risk_score": 0,
        "last_result": None,
        "last_updated": 0,
    }


def _default_state() -> Dict[str, Any]:
    return {
        "version": 2,
        "days": {},
        "archive": {},
        "recent_events": [],
        "updated_at": 0,
    }


def _state() -> Dict[str, Any]:
    s = load_json(RISK_FILE, _default_state())
    if not isinstance(s, dict):
        s = _default_state()

    # Backward-compatible migration from old file shape: {'days': {...}}
    s.setdefault("version", 2)
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
    row.setdefault("recent_7d", {"tp": 0, "sl": 0, "real_tp": 0, "real_sl": 0, "ghost_tp": 0, "ghost_sl": 0})
    row.setdefault("recent_30d", {"tp": 0, "sl": 0, "real_tp": 0, "real_sl": 0, "ghost_tp": 0, "ghost_sl": 0})
    row.setdefault("behavior", "UNKNOWN")
    row.setdefault("confidence", 0)
    _ensure_bucket_fields(row, symbol, direction)
    return row


def _ensure_bucket_fields(row: Dict[str, Any], symbol: str, direction: str) -> None:
    defaults = _empty_bucket(symbol, direction)
    for k, v in defaults.items():
        row.setdefault(k, v)


def _normal_result(result: str) -> str:
    r = str(result or "").upper().strip()
    if r in {"TP", "TP1", "TP2", "TAKE_PROFIT", "TAKEPROFIT"}:
        return r if r in {"TP1", "TP2"} else "TP1"
    if r in {"SL", "STOP", "STOP_LOSS", "STOPLOSS"}:
        return "SL"
    return r or "UNKNOWN"


def _normal_source(source: Optional[str] = None, is_ghost: Optional[bool] = None) -> str:
    if is_ghost is True:
        return "GHOST"
    src = str(source or "REAL").upper().strip()
    if src in {"GHOST", "SHADOW", "PAPER_GHOST"}:
        return "GHOST"
    if src in {"REAL", "LIVE", "TOOBIT", "PAPER", "SIGNAL"}:
        return "REAL"
    return "REAL"


def _apply_result(row: Dict[str, Any], result: str, source: str, ts: int) -> None:
    if result == "SL":
        row["sl"] = int(row.get("sl", 0)) + 1
        if source == "GHOST":
            row["ghost_sl"] = int(row.get("ghost_sl", 0)) + 1
        else:
            row["real_sl"] = int(row.get("real_sl", 0)) + 1
    elif result in {"TP1", "TP2"}:
        row["tp"] = int(row.get("tp", 0)) + 1
        if result == "TP1":
            row["tp1"] = int(row.get("tp1", 0)) + 1
        elif result == "TP2":
            row["tp2"] = int(row.get("tp2", 0)) + 1
        if source == "GHOST":
            row["ghost_tp"] = int(row.get("ghost_tp", 0)) + 1
        else:
            row["real_tp"] = int(row.get("real_tp", 0)) + 1
    row["last_result"] = result
    row["last_updated"] = ts


def _append_event(s: Dict[str, Any], symbol: str, direction: str, result: str, source: str, ts: int, snapshot: Optional[Dict[str, Any]] = None) -> None:
    event = {
        "ts": ts,
        "day": _day_key(ts),
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "result": result,
        "source": source,
    }
    if isinstance(snapshot, dict) and snapshot:
        # Keep only compact learning fields to avoid bloating the risk file.
        event["snapshot"] = {
            "rsi": snapshot.get("rsi"),
            "rsi_5m": snapshot.get("rsi_5m"),
            "macd_hist": snapshot.get("macd_hist"),
            "adx": snapshot.get("adx"),
            "vwap_status": snapshot.get("vwap_status"),
            "power2_buy": snapshot.get("power2_buy"),
            "power2_sell": snapshot.get("power2_sell"),
            "power3_buy": snapshot.get("power3_buy"),
            "power3_sell": snapshot.get("power3_sell"),
            "market_regime": snapshot.get("market_regime"),
            "btc_bias": snapshot.get("btc_bias"),
        }
    events = s.setdefault("recent_events", [])
    events.append(event)
    if len(events) > MAX_RECENT_EVENTS:
        del events[:-MAX_RECENT_EVENTS]


def _count_recent_events(s: Dict[str, Any], symbol: str, direction: str, days: int) -> Dict[str, int]:
    cutoff = _now() - int(days) * DAY_SECONDS
    sym = str(symbol).upper()
    direct = str(direction).upper()
    out = {"tp": 0, "sl": 0, "real_tp": 0, "real_sl": 0, "ghost_tp": 0, "ghost_sl": 0}
    for ev in s.get("recent_events", []):
        if not isinstance(ev, dict):
            continue
        if int(ev.get("ts", 0) or 0) < cutoff:
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
            out["ghost_tp" if source == "GHOST" else "real_tp"] += 1
    return out


def _risk_score(daily: Dict[str, Any], archive: Dict[str, Any], recent_7d: Dict[str, int], recent_30d: Dict[str, int]) -> int:
    # Heavier weight for real SL, lower but meaningful weight for ghost SL.
    daily_score = int(daily.get("real_sl", 0)) * 24 + int(daily.get("ghost_sl", 0)) * 14 - int(daily.get("real_tp", 0)) * 8 - int(daily.get("ghost_tp", 0)) * 4
    recent_score = int(recent_7d.get("real_sl", 0)) * 10 + int(recent_7d.get("ghost_sl", 0)) * 6 - int(recent_7d.get("real_tp", 0)) * 4 - int(recent_7d.get("ghost_tp", 0)) * 2
    long_score = int(archive.get("real_sl", 0)) * 4 + int(archive.get("ghost_sl", 0)) * 2 - int(archive.get("real_tp", 0)) * 2 - int(archive.get("ghost_tp", 0))
    return max(0, min(100, int(daily_score + recent_score + long_score)))


def _behavior_from_stats(tp: int, sl: int) -> str:
    total = tp + sl
    if total < 5:
        return "UNKNOWN"
    win_rate = tp / max(total, 1)
    if win_rate >= 0.68:
        return "GOOD"
    if win_rate >= 0.55:
        return "NORMAL"
    if win_rate >= 0.45:
        return "WEAK"
    return "BAD"


def _strictness_level(daily: Dict[str, Any], archive: Dict[str, Any], recent_7d: Dict[str, int], risk_score: int) -> int:
    start = max(1, int(DAILY_SL_STRICTNESS_START))
    max_level = max(1, int(MAX_DAILY_STRICTNESS_LEVEL))

    # Real SLs count fully, ghost SLs count half-ish. This preserves the rule:
    # after 2 SLs, signal 3 becomes stricter; ghost results also influence it.
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

    # Winning history softens strictness slightly, but never below daily SL rule.
    daily_tp = int(daily.get("real_tp", 0)) + int(daily.get("ghost_tp", 0)) * 0.5
    if daily_tp >= 3 and level > 0:
        level -= 1

    return max(0, min(max_level, int(level)))


def _refresh_archive_computed_fields(s: Dict[str, Any], symbol: str, direction: str) -> Dict[str, Any]:
    daily = _get_day_row(s, symbol, direction)
    archive = _get_archive_row(s, symbol, direction)
    recent_7d = _count_recent_events(s, symbol, direction, 7)
    recent_30d = _count_recent_events(s, symbol, direction, 30)
    score = _risk_score(daily, archive, recent_7d, recent_30d)
    strict = _strictness_level(daily, archive, recent_7d, score)

    archive["recent_7d"] = recent_7d
    archive["recent_30d"] = recent_30d
    archive["risk_score"] = score
    archive["strictness_level"] = strict
    archive["behavior"] = _behavior_from_stats(int(archive.get("tp", 0)), int(archive.get("sl", 0)))
    archive["confidence"] = min(100, int(int(archive.get("tp", 0)) + int(archive.get("sl", 0))) * 5)
    return archive


def register_result(
    symbol: str,
    direction: str,
    result: str,
    source: str = "REAL",
    snapshot: Optional[Dict[str, Any]] = None,
    is_ghost: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Register TP/SL result for a real or ghost signal.

    Backward compatible old call:
        register_result(symbol, direction, result)

    New preferred calls:
        register_result(symbol, direction, "SL", source="REAL", snapshot=snapshot)
        register_result(symbol, direction, "TP1", source="GHOST", snapshot=snapshot)
    """
    ts = _now()
    result_norm = _normal_result(result)
    source_norm = _normal_source(source, is_ghost=is_ghost)

    s = _state()
    daily = _get_day_row(s, symbol, direction, ts)
    archive = _get_archive_row(s, symbol, direction)

    _apply_result(daily, result_norm, source_norm, ts)
    _apply_result(archive, result_norm, source_norm, ts)
    _append_event(s, symbol, direction, result_norm, source_norm, ts, snapshot=snapshot)
    archive = _refresh_archive_computed_fields(s, symbol, direction)

    s["updated_at"] = ts
    save_json(RISK_FILE, s)
    return get_direction_risk_state(symbol, direction)


def register_ghost_result(symbol: str, direction: str, result: str, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return register_result(symbol, direction, result, source="GHOST", snapshot=snapshot, is_ghost=True)


def register_real_result(symbol: str, direction: str, result: str, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return register_result(symbol, direction, result, source="REAL", snapshot=snapshot, is_ghost=False)


def get_direction_risk_state(symbol: str, direction: str) -> Dict[str, Any]:
    """
    Return risk state used directly by analysis.py.
    This function must stay fast and safe: on any malformed old data, it repairs defaults.
    """
    s = _state()
    daily = _get_day_row(s, symbol, direction)
    archive = _refresh_archive_computed_fields(s, symbol, direction)
    recent_7d = archive.get("recent_7d", {}) or {}
    recent_30d = archive.get("recent_30d", {}) or {}

    daily_sl = int(daily.get("sl", 0))
    daily_tp = int(daily.get("tp", 0))
    total_sl = int(archive.get("sl", 0))
    total_tp = int(archive.get("tp", 0))
    strict = int(archive.get("strictness_level", 0) or 0)
    risk_score = int(archive.get("risk_score", 0) or 0)

    out = {
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),

        # Fields consumed by analysis.py
        "sl_count": daily_sl,
        "tp_count": daily_tp,
        "strictness_level": strict,
        "risk_score": risk_score,
        "bad_day": daily_sl >= int(DAILY_SL_STRICTNESS_START),
        "recommend_reduce": strict >= 3 or risk_score >= 70,

        # Extra AI memory fields
        "daily": dict(daily),
        "archive": {
            "tp": total_tp,
            "sl": total_sl,
            "real_tp": int(archive.get("real_tp", 0)),
            "real_sl": int(archive.get("real_sl", 0)),
            "ghost_tp": int(archive.get("ghost_tp", 0)),
            "ghost_sl": int(archive.get("ghost_sl", 0)),
            "behavior": archive.get("behavior", "UNKNOWN"),
            "confidence": int(archive.get("confidence", 0) or 0),
        },
        "recent_7d": dict(recent_7d),
        "recent_30d": dict(recent_30d),
        "source_weights": {"real_sl": 1.0, "ghost_sl": 0.5, "real_tp": 1.0, "ghost_tp": 0.5},
    }
    return out


def format_coin_risk_report(symbol: Optional[str] = None, limit: int = 12) -> str:
    """Small Persian report for Telegram/debug commands if bot.py uses it later."""
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
            f"{sym} {direction} | Risk={risk} | Strict={strict} | "
            f"Real SL/TP={ar.get('real_sl',0)}/{ar.get('real_tp',0)} | "
            f"Ghost SL/TP={ar.get('ghost_sl',0)}/{ar.get('ghost_tp',0)} | "
            f"رفتار={ar.get('behavior','UNKNOWN')}"
        )
    return "\n".join(lines)
