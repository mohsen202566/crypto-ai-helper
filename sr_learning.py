# -*- coding: utf-8 -*-
"""
sr_learning.py

AI Support/Resistance Learning Engine

Purpose:
- Persist S/R behavior memory across code updates.
- Learn support/resistance behavior per symbol + direction + level type.
- Track bounce, clean break, fake breakout, TP/SL around levels.
- Provide compact S/R profile for analysis.py / coin_learning.py / managers.
- Keep old public functions:
    record_sr_event
    format_sr_report

Design:
- Non-breaking: old calls with (symbol, direction, level_type, price, result) still work.
- Compact: stores latest events and aggregate stats only.
- Safe: malformed old JSON is migrated automatically.
"""

import time
import math
from typing import Dict, Any, Optional, List

from data_store import load_json, save_json

SR_FILE = "sr_learning.json"
MAX_EVENTS = 1500
MAX_EVENTS_PER_LEVEL = 80
LEVEL_BUCKET_PCT = 0.0015  # 0.15% buckets so near levels merge safely


def _now() -> int:
    return int(time.time())


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


def _norm_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    return s if s else "UNKNOWN"


def _norm_direction(direction: str) -> str:
    d = str(direction or "").upper().strip()
    return d if d in {"LONG", "SHORT"} else (d or "UNKNOWN")


def _norm_level_type(level_type: str) -> str:
    lt = str(level_type or "").upper().strip()
    if lt in {"SUPPORT", "S", "LOW"}:
        return "SUPPORT"
    if lt in {"RESISTANCE", "R", "HIGH"}:
        return "RESISTANCE"
    return lt or "UNKNOWN"


def _norm_result(result: Any = None) -> str:
    r = str(result or "").upper().strip()
    if r in {"FAKE", "FAKE_BREAK", "FAKE_BREAKOUT", "FALSE_BREAK"}:
        return "FAKE_BREAKOUT"
    if r in {"BREAK", "BREAKOUT", "CLEAN_BREAK", "CLEAN_BREAKOUT"}:
        return "CLEAN_BREAK"
    if r in {"BOUNCE", "REJECT", "REJECTION", "HOLD"}:
        return "BOUNCE"
    if r in {"TP", "TP1", "TP2", "TAKE_PROFIT", "TAKEPROFIT"}:
        return "TP"
    if r in {"SL", "STOP", "STOP_LOSS", "STOPLOSS"}:
        return "SL"
    return r or "TOUCH"


def _default_state() -> Dict[str, Any]:
    return {
        "version": 2,
        "events": [],
        "levels": {},
        "by_symbol": {},
        "updated_at": 0,
    }


def _state() -> Dict[str, Any]:
    s = load_json(SR_FILE, _default_state())
    if not isinstance(s, dict):
        s = _default_state()

    # Backward-compatible migration from old {'events': [...]} shape.
    s.setdefault("version", 2)
    s.setdefault("events", [])
    s.setdefault("levels", {})
    s.setdefault("by_symbol", {})
    s.setdefault("updated_at", 0)

    if not isinstance(s.get("events"), list):
        s["events"] = []
    if not isinstance(s.get("levels"), dict):
        s["levels"] = {}
    if not isinstance(s.get("by_symbol"), dict):
        s["by_symbol"] = {}

    return s


def _level_bucket(price: float) -> str:
    p = _safe_float(price, 0.0)
    if p <= 0:
        return "0"
    step = max(p * LEVEL_BUCKET_PCT, 1e-12)
    return str(round(round(p / step) * step, 8))


def _level_key(symbol: str, direction: str, level_type: str, price: float) -> str:
    return f"{_norm_symbol(symbol)}:{_norm_direction(direction)}:{_norm_level_type(level_type)}:{_level_bucket(price)}"


def _empty_level(symbol: str, direction: str, level_type: str, price: float) -> Dict[str, Any]:
    return {
        "symbol": _norm_symbol(symbol),
        "direction": _norm_direction(direction),
        "level_type": _norm_level_type(level_type),
        "price": _safe_float(price),
        "touches": 0,
        "bounces": 0,
        "clean_breaks": 0,
        "fake_breakouts": 0,
        "tp_after_touch": 0,
        "sl_after_touch": 0,
        "strength_score": 50,
        "fake_break_rate": 0.0,
        "bounce_rate": 0.0,
        "break_rate": 0.0,
        "last_result": None,
        "last_updated": 0,
        "events": [],
    }


def _ensure_symbol_row(s: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    sym = _norm_symbol(symbol)
    row = s.setdefault("by_symbol", {}).setdefault(sym, {
        "symbol": sym,
        "touches": 0,
        "bounces": 0,
        "clean_breaks": 0,
        "fake_breakouts": 0,
        "tp_after_touch": 0,
        "sl_after_touch": 0,
        "strength_score": 50,
        "fake_break_rate": 0.0,
        "bounce_rate": 0.0,
        "break_rate": 0.0,
        "last_updated": 0,
    })
    for k, v in {
        "touches": 0, "bounces": 0, "clean_breaks": 0, "fake_breakouts": 0,
        "tp_after_touch": 0, "sl_after_touch": 0, "strength_score": 50,
        "fake_break_rate": 0.0, "bounce_rate": 0.0, "break_rate": 0.0,
        "last_updated": 0,
    }.items():
        row.setdefault(k, v)
    return row


def _recompute_stats(row: Dict[str, Any]) -> None:
    touches = max(1, int(row.get("touches", 0) or 0))
    bounces = int(row.get("bounces", 0) or 0)
    clean = int(row.get("clean_breaks", 0) or 0)
    fake = int(row.get("fake_breakouts", 0) or 0)
    tp = int(row.get("tp_after_touch", 0) or 0)
    sl = int(row.get("sl_after_touch", 0) or 0)

    row["bounce_rate"] = round(bounces / touches, 4)
    row["break_rate"] = round(clean / touches, 4)
    row["fake_break_rate"] = round(fake / touches, 4)

    # Higher = cleaner/useful level. Fake break and SL after touch reduce quality.
    score = 50
    score += min(25, bounces * 4)
    score += min(18, clean * 3)
    score += min(15, tp * 3)
    score -= min(30, fake * 6)
    score -= min(24, sl * 5)
    row["strength_score"] = max(0, min(100, int(score)))


def _apply_result(row: Dict[str, Any], result: str) -> None:
    row["touches"] = int(row.get("touches", 0) or 0) + 1
    if result == "BOUNCE":
        row["bounces"] = int(row.get("bounces", 0) or 0) + 1
    elif result == "CLEAN_BREAK":
        row["clean_breaks"] = int(row.get("clean_breaks", 0) or 0) + 1
    elif result == "FAKE_BREAKOUT":
        row["fake_breakouts"] = int(row.get("fake_breakouts", 0) or 0) + 1
    elif result == "TP":
        row["tp_after_touch"] = int(row.get("tp_after_touch", 0) or 0) + 1
    elif result == "SL":
        row["sl_after_touch"] = int(row.get("sl_after_touch", 0) or 0) + 1
    row["last_result"] = result
    row["last_updated"] = _now()
    _recompute_stats(row)


def record_sr_event(
    symbol: str,
    direction: str,
    level_type: str,
    price: float,
    result: str = None,
    timeframe: str = None,
    strength: float = None,
    snapshot: Optional[Dict[str, Any]] = None,
    **kwargs,
) -> bool:
    """
    Record S/R behavior event.

    Backward-compatible old call:
        record_sr_event(symbol, direction, level_type, price, result)

    New optional fields:
        timeframe, strength, snapshot, move_percent, source
    """
    s = _state()
    sym = _norm_symbol(symbol)
    direct = _norm_direction(direction)
    lt = _norm_level_type(level_type)
    p = _safe_float(price)
    res = _norm_result(result)

    event = {
        "ts": _now(),
        "symbol": sym,
        "direction": direct,
        "level_type": lt,
        "price": p,
        "result": res,
    }
    if timeframe is not None:
        event["timeframe"] = str(timeframe).upper()
    if strength is not None:
        event["strength"] = _safe_float(strength)
    if kwargs.get("move_percent") is not None:
        event["move_percent"] = _safe_float(kwargs.get("move_percent"))
    if kwargs.get("source") is not None:
        event["source"] = str(kwargs.get("source")).upper()
    if isinstance(snapshot, dict) and snapshot:
        event["snapshot"] = {
            "atr": snapshot.get("atr"),
            "adx": snapshot.get("adx"),
            "rsi": snapshot.get("rsi"),
            "vwap_status": snapshot.get("vwap_status"),
            "market_regime": snapshot.get("market_regime"),
            "btc_bias": snapshot.get("btc_bias"),
        }

    s.setdefault("events", []).append(event)
    s["events"] = s["events"][-MAX_EVENTS:]

    key = _level_key(sym, direct, lt, p)
    level = s.setdefault("levels", {}).setdefault(key, _empty_level(sym, direct, lt, p))
    level.setdefault("events", []).append(event)
    level["events"] = level["events"][-MAX_EVENTS_PER_LEVEL:]
    # Keep average-ish level price stable but adaptable.
    old_price = _safe_float(level.get("price"), p)
    level["price"] = round(old_price * 0.85 + p * 0.15, 8) if old_price > 0 and p > 0 else p
    _apply_result(level, res)

    sym_row = _ensure_symbol_row(s, sym)
    _apply_result(sym_row, res)

    s["updated_at"] = _now()
    save_json(SR_FILE, s)
    return True


def get_sr_profile(symbol: str, direction: str = None, level_type: str = None, price: float = None) -> Dict[str, Any]:
    """Return compact SR memory profile for analysis / TP-SL engines."""
    s = _state()
    sym = _norm_symbol(symbol)
    direct = _norm_direction(direction) if direction else None
    lt = _norm_level_type(level_type) if level_type else None
    p = _safe_float(price, 0.0)

    rows: List[Dict[str, Any]] = []
    for row in (s.get("levels") or {}).values():
        if not isinstance(row, dict):
            continue
        if row.get("symbol") != sym:
            continue
        if direct and row.get("direction") != direct:
            continue
        if lt and row.get("level_type") != lt:
            continue
        if p > 0:
            dist = abs(_safe_float(row.get("price")) - p) / max(p, 1e-12)
            if dist > LEVEL_BUCKET_PCT * 3:
                continue
        rows.append(row)

    if not rows:
        sym_row = (s.get("by_symbol") or {}).get(sym, {})
        return {
            "available": False,
            "symbol": sym,
            "direction": direct,
            "level_type": lt,
            "strength_score": int(sym_row.get("strength_score", 50) or 50) if isinstance(sym_row, dict) else 50,
            "fake_break_rate": _safe_float(sym_row.get("fake_break_rate"), 0.0) if isinstance(sym_row, dict) else 0.0,
            "bounce_rate": _safe_float(sym_row.get("bounce_rate"), 0.0) if isinstance(sym_row, dict) else 0.0,
            "break_rate": _safe_float(sym_row.get("break_rate"), 0.0) if isinstance(sym_row, dict) else 0.0,
            "source": "sr_learning",
        }

    total_touches = sum(int(r.get("touches", 0) or 0) for r in rows)
    avg_strength = sum(_safe_float(r.get("strength_score"), 50) * max(1, int(r.get("touches", 0) or 0)) for r in rows) / max(1, total_touches)
    fake = sum(int(r.get("fake_breakouts", 0) or 0) for r in rows)
    bounce = sum(int(r.get("bounces", 0) or 0) for r in rows)
    clean = sum(int(r.get("clean_breaks", 0) or 0) for r in rows)

    return {
        "available": True,
        "symbol": sym,
        "direction": direct,
        "level_type": lt,
        "samples": total_touches,
        "strength_score": round(avg_strength, 2),
        "fake_break_rate": round(fake / max(1, total_touches), 4),
        "bounce_rate": round(bounce / max(1, total_touches), 4),
        "break_rate": round(clean / max(1, total_touches), 4),
        "levels": rows[:20],
        "source": "sr_learning",
    }


def classify_sr_event_from_prices(direction: str, level_type: str, level_price: float, entry: float, exit_price: float, result: str = None) -> str:
    """Best-effort classifier for managers that only know entry/exit/result."""
    res = _norm_result(result)
    if res in {"TP", "SL"}:
        return res

    direct = _norm_direction(direction)
    lt = _norm_level_type(level_type)
    lp = _safe_float(level_price)
    en = _safe_float(entry)
    ex = _safe_float(exit_price)
    if lp <= 0 or en <= 0 or ex <= 0:
        return res

    # For LONG near resistance: close above level = clean break; back below = fake.
    if direct == "LONG" and lt == "RESISTANCE":
        return "CLEAN_BREAK" if ex > lp else "FAKE_BREAKOUT"
    # For SHORT near support: close below level = clean break; back above = fake.
    if direct == "SHORT" and lt == "SUPPORT":
        return "CLEAN_BREAK" if ex < lp else "FAKE_BREAKOUT"
    # For LONG support / SHORT resistance, successful defense is bounce.
    if (direct == "LONG" and lt == "SUPPORT") or (direct == "SHORT" and lt == "RESISTANCE"):
        return "BOUNCE" if res != "SL" else "SL"
    return res


def format_sr_report(symbol: str = None, limit: int = 12) -> str:
    s = _state()
    events = s.get("events", []) if isinstance(s.get("events"), list) else []
    rows = list((s.get("levels") or {}).values())
    if symbol:
        sym = _norm_symbol(symbol)
        rows = [r for r in rows if isinstance(r, dict) and r.get("symbol") == sym]
    rows.sort(key=lambda r: (int(r.get("strength_score", 0) or 0), int(r.get("touches", 0) or 0)), reverse=True)

    lines = [f"📐 SR Learning\nرویدادها: {len(events)}"]
    if not rows:
        return "\n".join(lines)

    lines.append("سطوح مهم:")
    for r in rows[:max(1, int(limit))]:
        lines.append(
            f"{r.get('symbol')} {r.get('direction')} {r.get('level_type')} | "
            f"Price={r.get('price')} | Strength={r.get('strength_score',50)} | "
            f"Bounce={round(_safe_float(r.get('bounce_rate'))*100,1)}% | "
            f"Break={round(_safe_float(r.get('break_rate'))*100,1)}% | "
            f"Fake={round(_safe_float(r.get('fake_break_rate'))*100,1)}%"
        )
    return "\n".join(lines)
