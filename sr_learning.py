# -*- coding: utf-8 -*-
"""
sr_learning.py

AI Support/Resistance + Liquidity Learning Engine

Purpose:
- Persist S/R behavior memory across code updates.
- Learn support/resistance behavior per symbol + direction + level type.
- Track bounce, clean break, fake breakout, trap, liquidity grab, stop-hunt, TP/SL around levels.
- Provide compact S/R profile for analysis.py / coin_learning.py / managers.
- Keep old public functions:
    record_sr_event
    get_sr_profile
    classify_sr_event_from_prices
    format_sr_report

Design:
- Non-breaking: old calls with (symbol, direction, level_type, price, result) still work.
- Compact: stores latest events and aggregate stats only.
- Soft AI layer: it returns risk/profile data; it does not hard-block signals by itself.
"""

import time
import math
from typing import Dict, Any, Optional, List

from data_store import load_json, save_json

SR_FILE = "sr_learning.json"
MAX_EVENTS = 20000
MAX_EVENTS_PER_LEVEL = 300
LEVEL_BUCKET_PCT = 0.0015  # 0.15% buckets so near levels merge safely
VERSION = 3
DAY_SECONDS = 86400



def _now() -> int:
    return int(time.time())


def _time_weight(ts: Any) -> float:
    """Recency weight used by SR/Liquidity memory.

    Matches the bot-wide Mode B learning preference:
    0-7 days = 1.00, 8-30 days = 0.70, 31-90 days = 0.40,
    older = 0.20. This keeps old experience useful but prevents stale market
    behavior from dominating current scalping decisions.
    """
    t = _safe_float(ts, 0.0)
    if t <= 0:
        return 0.70
    age_days = max(0.0, (_now() - t) / DAY_SECONDS)
    if age_days <= 7:
        return 1.00
    if age_days <= 30:
        return 0.70
    if age_days <= 90:
        return 0.40
    return 0.20


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
    if lt in {"SUPPORT", "S", "LOW", "DEMAND", "DEMAND_ZONE", "BUY_ZONE"}:
        return "SUPPORT"
    if lt in {"RESISTANCE", "R", "HIGH", "SUPPLY", "SUPPLY_ZONE", "SELL_ZONE"}:
        return "RESISTANCE"
    if lt in {"LIQUIDITY", "LIQUIDITY_ZONE", "STOP_ZONE", "STOP_CLUSTER", "LIQUIDITY_POOL"}:
        return "LIQUIDITY"
    return lt or "UNKNOWN"


def _norm_result(result: Any = None) -> str:
    r = str(result or "").upper().strip()
    if r in {"FAKE", "FAKE_BREAK", "FAKE_BREAKOUT", "FALSE_BREAK"}:
        return "FAKE_BREAKOUT"
    if r in {"BREAK", "BREAKOUT", "CLEAN_BREAK", "CLEAN_BREAKOUT"}:
        return "CLEAN_BREAK"
    if r in {"BOUNCE", "REJECT", "REJECTION", "HOLD"}:
        return "BOUNCE"
    if r in {"TP", "TP1", "TP2", "TAKE_PROFIT", "TAKEPROFIT", "EARLY_PROFIT", "AI_EXIT_PROFIT"}:
        return "TP"
    if r in {"SL", "STOP", "STOP_LOSS", "STOPLOSS"}:
        return "SL"
    if r in {"TRAP", "LONG_TRAP", "SHORT_TRAP"}:
        return "TRAP"
    if r in {"LIQUIDITY_GRAB", "LIQ_GRAB", "STOP_HUNT", "STOPHUNT"}:
        return "LIQUIDITY_GRAB"
    return r or "TOUCH"


def _default_state() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "events": [],
        "levels": {},
        "by_symbol": {},
        "updated_at": 0,
    }


def _state() -> Dict[str, Any]:
    s = load_json(SR_FILE, _default_state())
    if not isinstance(s, dict):
        s = _default_state()

    s.setdefault("version", VERSION)
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


def _empty_common_stats() -> Dict[str, Any]:
    return {
        "touches": 0,
        "bounces": 0,
        "clean_breaks": 0,
        "fake_breakouts": 0,
        "tp_after_touch": 0,
        "sl_after_touch": 0,
        "trap_events": 0,
        "liquidity_grabs": 0,
        "stop_hunts": 0,
        "long_traps": 0,
        "short_traps": 0,
        "market_memory_tests": 0,
        "failed_moves": 0,
        "strength_score": 50,
        "fake_break_rate": 0.0,
        "bounce_rate": 0.0,
        "break_rate": 0.0,
        "trap_rate": 0.0,
        "liquidity_grab_rate": 0.0,
        "stop_hunt_rate": 0.0,
        "last_result": None,
        "last_updated": 0,
    }


def _empty_level(symbol: str, direction: str, level_type: str, price: float) -> Dict[str, Any]:
    row = {
        "symbol": _norm_symbol(symbol),
        "direction": _norm_direction(direction),
        "level_type": _norm_level_type(level_type),
        "price": _safe_float(price),
        "events": [],
        "timeframe_stats": {},
        "condition_stats": {},
    }
    row.update(_empty_common_stats())
    return row


def _ensure_common_fields(row: Dict[str, Any]) -> None:
    for k, v in _empty_common_stats().items():
        row.setdefault(k, v)
    row.setdefault("events", [])
    row.setdefault("timeframe_stats", {})
    row.setdefault("condition_stats", {})
    if not isinstance(row.get("events"), list):
        row["events"] = []
    if not isinstance(row.get("timeframe_stats"), dict):
        row["timeframe_stats"] = {}
    if not isinstance(row.get("condition_stats"), dict):
        row["condition_stats"] = {}


def _ensure_symbol_row(s: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    sym = _norm_symbol(symbol)
    row = s.setdefault("by_symbol", {}).setdefault(sym, {"symbol": sym})
    _ensure_common_fields(row)
    return row


def _recompute_stats(row: Dict[str, Any]) -> None:
    touches = max(1, int(row.get("touches", 0) or 0))
    bounces = int(row.get("bounces", 0) or 0)
    clean = int(row.get("clean_breaks", 0) or 0)
    fake = int(row.get("fake_breakouts", 0) or 0)
    tp = int(row.get("tp_after_touch", 0) or 0)
    sl = int(row.get("sl_after_touch", 0) or 0)
    trap = int(row.get("trap_events", 0) or 0)
    liq = int(row.get("liquidity_grabs", 0) or 0)
    stop = int(row.get("stop_hunts", 0) or 0)

    row["bounce_rate"] = round(bounces / touches, 4)
    row["break_rate"] = round(clean / touches, 4)
    row["fake_break_rate"] = round(fake / touches, 4)
    row["trap_rate"] = round(trap / touches, 4)
    row["liquidity_grab_rate"] = round(liq / touches, 4)
    row["stop_hunt_rate"] = round(stop / touches, 4)

    # Higher = cleaner/useful level. Fake break/trap/stop-hunt/SL reduce quality.
    score = 50
    score += min(24, bounces * 4)
    score += min(18, clean * 3)
    score += min(15, tp * 3)
    score += min(8, int(row.get("market_memory_tests", 0) or 0))
    score -= min(30, fake * 6)
    score -= min(24, sl * 5)
    score -= min(28, trap * 7)
    score -= min(22, stop * 6)
    score -= min(14, liq * 3)
    row["strength_score"] = max(0, min(100, int(score)))


def _apply_result(row: Dict[str, Any], result: str, direction: str = None, kwargs: Optional[Dict[str, Any]] = None) -> None:
    kwargs = kwargs or {}
    _ensure_common_fields(row)
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
    elif result == "TRAP":
        row["trap_events"] = int(row.get("trap_events", 0) or 0) + 1
        d = _norm_direction(direction or row.get("direction"))
        if d == "LONG":
            row["long_traps"] = int(row.get("long_traps", 0) or 0) + 1
        elif d == "SHORT":
            row["short_traps"] = int(row.get("short_traps", 0) or 0) + 1
    elif result == "LIQUIDITY_GRAB":
        row["liquidity_grabs"] = int(row.get("liquidity_grabs", 0) or 0) + 1
        if str(kwargs.get("subtype") or "").upper() in {"STOP_HUNT", "STOPHUNT"}:
            row["stop_hunts"] = int(row.get("stop_hunts", 0) or 0) + 1

    if kwargs.get("market_memory_test"):
        row["market_memory_tests"] = int(row.get("market_memory_tests", 0) or 0) + 1
    if kwargs.get("failed_move"):
        row["failed_moves"] = int(row.get("failed_moves", 0) or 0) + 1

    row["last_result"] = result
    row["last_updated"] = _now()
    _recompute_stats(row)


def _snapshot_compact(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    keys = [
        "snapshot_at", "atr", "adx", "adx_slope_15m", "rsi", "rsi_slope_15m", "macd_hist", "macd_hist_accel_15m",
        "vwap_status", "vwap_distance_pct", "market_regime", "market_mode", "btc_bias", "move_state", "trap_risk",
        "prediction_score", "reversal_risk_score", "timeframe_core", "entry_timing_tf",
    ]
    out = {k: snapshot.get(k) for k in keys if snapshot.get(k) is not None}
    for nk in ("candle_behavior", "liquidity_trap", "state_awareness", "prediction_layer"):
        if isinstance(snapshot.get(nk), dict):
            out[nk] = snapshot.get(nk)
    return out


def _condition_key(event: Dict[str, Any]) -> str:
    snap = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
    tf = str(event.get("timeframe") or snap.get("timeframe_core") or "NA").upper()
    regime = str(snap.get("market_regime") or "NA").upper()
    trap = str(snap.get("trap_risk") or ((snap.get("liquidity_trap") or {}).get("trap_risk") if isinstance(snap.get("liquidity_trap"), dict) else "NA")).upper()
    vwap = str(snap.get("vwap_status") or "NA").upper()
    return f"TF:{tf}|REG:{regime}|TRAP:{trap}|VWAP:{vwap}"


def _update_condition_stats(row: Dict[str, Any], event: Dict[str, Any], result: str) -> None:
    stats = row.setdefault("condition_stats", {})
    key = _condition_key(event)
    item = stats.setdefault(key, {"touches": 0, "tp": 0, "sl": 0, "fake": 0, "trap": 0, "clean": 0, "bounce": 0})
    item["touches"] = int(item.get("touches", 0) or 0) + 1
    if result == "TP": item["tp"] = int(item.get("tp", 0) or 0) + 1
    elif result == "SL": item["sl"] = int(item.get("sl", 0) or 0) + 1
    elif result == "FAKE_BREAKOUT": item["fake"] = int(item.get("fake", 0) or 0) + 1
    elif result == "TRAP": item["trap"] = int(item.get("trap", 0) or 0) + 1
    elif result == "CLEAN_BREAK": item["clean"] = int(item.get("clean", 0) or 0) + 1
    elif result == "BOUNCE": item["bounce"] = int(item.get("bounce", 0) or 0) + 1


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
    """Record S/R, liquidity, trap, stop-hunt, and market-memory event."""
    s = _state()
    sym = _norm_symbol(symbol)
    direct = _norm_direction(direction)
    lt = _norm_level_type(level_type)
    p = _safe_float(price)
    res = _norm_result(result)

    # Allow kwargs flags to override generic TOUCH result.
    if kwargs.get("fake_breakout") is True:
        res = "FAKE_BREAKOUT"
    elif kwargs.get("clean_breakout") is True:
        res = "CLEAN_BREAK"
    elif kwargs.get("trap") is True or str(kwargs.get("trap_type") or "").upper() in {"LONG_TRAP", "SHORT_TRAP", "TRAP"}:
        res = "TRAP"
    elif kwargs.get("liquidity_grab") is True or kwargs.get("stop_hunt") is True:
        res = "LIQUIDITY_GRAB"

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
    for k in (
        "move_percent", "source", "trap_type", "liquidity_zone", "stop_hunt",
        "market_memory_test", "failed_move", "zone_strength", "freshness",
        "reaction_percent", "clean_breakout", "fake_breakout", "liquidity_grab",
    ):
        if kwargs.get(k) is not None:
            event[k] = kwargs.get(k)
    snap = _snapshot_compact(snapshot)
    if snap:
        event["snapshot"] = snap

    s.setdefault("events", []).append(event)
    s["events"] = s["events"][-MAX_EVENTS:]

    key = _level_key(sym, direct, lt, p)
    level = s.setdefault("levels", {}).setdefault(key, _empty_level(sym, direct, lt, p))
    _ensure_common_fields(level)
    level.setdefault("events", []).append(event)
    level["events"] = level["events"][-MAX_EVENTS_PER_LEVEL:]

    old_price = _safe_float(level.get("price"), p)
    level["price"] = round(old_price * 0.85 + p * 0.15, 8) if old_price > 0 and p > 0 else p
    _apply_result(level, res, direct, kwargs)
    _update_condition_stats(level, event, res)

    # Timeframe stats help 15M S/R become more important without removing 5M/30M memory.
    tf = str(timeframe or (snap.get("timeframe_core") if snap else "UNKNOWN") or "UNKNOWN").upper()
    tf_row = level.setdefault("timeframe_stats", {}).setdefault(tf, {"touches": 0, "tp": 0, "sl": 0, "fake": 0, "trap": 0})
    tf_row["touches"] = int(tf_row.get("touches", 0) or 0) + 1
    if res == "TP": tf_row["tp"] = int(tf_row.get("tp", 0) or 0) + 1
    elif res == "SL": tf_row["sl"] = int(tf_row.get("sl", 0) or 0) + 1
    elif res == "FAKE_BREAKOUT": tf_row["fake"] = int(tf_row.get("fake", 0) or 0) + 1
    elif res == "TRAP": tf_row["trap"] = int(tf_row.get("trap", 0) or 0) + 1

    sym_row = _ensure_symbol_row(s, sym)
    _apply_result(sym_row, res, direct, kwargs)
    _update_condition_stats(sym_row, event, res)

    s["version"] = VERSION
    s["updated_at"] = _now()
    save_json(SR_FILE, s)
    return True


def _aggregate_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_touches = sum(int(r.get("touches", 0) or 0) for r in rows)
    if total_touches <= 0:
        total_touches = 1
    avg_strength = sum(_safe_float(r.get("strength_score"), 50) * max(1, int(r.get("touches", 0) or 0)) for r in rows) / max(1, total_touches)
    fake = sum(int(r.get("fake_breakouts", 0) or 0) for r in rows)
    bounce = sum(int(r.get("bounces", 0) or 0) for r in rows)
    clean = sum(int(r.get("clean_breaks", 0) or 0) for r in rows)
    trap = sum(int(r.get("trap_events", 0) or 0) for r in rows)
    liq = sum(int(r.get("liquidity_grabs", 0) or 0) for r in rows)
    stop = sum(int(r.get("stop_hunts", 0) or 0) for r in rows)
    sl = sum(int(r.get("sl_after_touch", 0) or 0) for r in rows)
    tp = sum(int(r.get("tp_after_touch", 0) or 0) for r in rows)

    # Time-weighted view from raw events. This is additive and does not break
    # old fields that other modules may already consume.
    wt = wfake = wbounce = wclean = wtrap = wliq = wstop = wsl = wtp = 0.0
    for r in rows:
        events = r.get("events", []) if isinstance(r.get("events"), list) else []
        if not events:
            # Fallback when only aggregate old rows exist.
            w = _time_weight(r.get("last_updated")) * max(1, int(r.get("touches", 0) or 0))
            wt += w
            wfake += _safe_float(r.get("fake_breakouts")) * _time_weight(r.get("last_updated"))
            wbounce += _safe_float(r.get("bounces")) * _time_weight(r.get("last_updated"))
            wclean += _safe_float(r.get("clean_breaks")) * _time_weight(r.get("last_updated"))
            wtrap += _safe_float(r.get("trap_events")) * _time_weight(r.get("last_updated"))
            wliq += _safe_float(r.get("liquidity_grabs")) * _time_weight(r.get("last_updated"))
            wstop += _safe_float(r.get("stop_hunts")) * _time_weight(r.get("last_updated"))
            wsl += _safe_float(r.get("sl_after_touch")) * _time_weight(r.get("last_updated"))
            wtp += _safe_float(r.get("tp_after_touch")) * _time_weight(r.get("last_updated"))
            continue
        for ev in events:
            if not isinstance(ev, dict):
                continue
            w = _time_weight(ev.get("ts"))
            wt += w
            res = _norm_result(ev.get("result"))
            if res == "FAKE_BREAKOUT": wfake += w
            elif res == "BOUNCE": wbounce += w
            elif res == "CLEAN_BREAK": wclean += w
            elif res == "TRAP": wtrap += w
            elif res == "LIQUIDITY_GRAB":
                wliq += w
                if ev.get("stop_hunt") or str(ev.get("trap_type") or "").upper() in {"STOP_HUNT", "STOPHUNT"}:
                    wstop += w
            elif res == "SL": wsl += w
            elif res == "TP": wtp += w

    wt_base = max(wt, 1e-9)
    weighted_trap_pressure = (wfake / wt_base) * 35 + (wtrap / wt_base) * 35 + (wstop / wt_base) * 20 + (wliq / wt_base) * 10
    weighted_clean_pressure = (wbounce / wt_base) * 30 + (wclean / wt_base) * 30 + (wtp / wt_base) * 25 - (wsl / wt_base) * 20

    preferred_action = "NEUTRAL"
    if wt >= 3:
        if weighted_trap_pressure >= 35:
            preferred_action = "CAUTION_TRAP_OR_STOP_HUNT"
        elif weighted_clean_pressure >= 35:
            preferred_action = "CLEAN_REACTION_ZONE"
        elif (wsl / wt_base) >= 0.45:
            preferred_action = "LOW_QUALITY_ZONE"

    return {
        "samples": total_touches,
        "weighted_samples": round(wt, 3),
        "strength_score": round(avg_strength, 2),
        "fake_break_rate": round(fake / max(1, total_touches), 4),
        "bounce_rate": round(bounce / max(1, total_touches), 4),
        "break_rate": round(clean / max(1, total_touches), 4),
        "trap_rate": round(trap / max(1, total_touches), 4),
        "liquidity_grab_rate": round(liq / max(1, total_touches), 4),
        "stop_hunt_rate": round(stop / max(1, total_touches), 4),
        "weighted_fake_break_rate": round(wfake / wt_base, 4) if wt else 0.0,
        "weighted_bounce_rate": round(wbounce / wt_base, 4) if wt else 0.0,
        "weighted_break_rate": round(wclean / wt_base, 4) if wt else 0.0,
        "weighted_trap_rate": round(wtrap / wt_base, 4) if wt else 0.0,
        "weighted_liquidity_grab_rate": round(wliq / wt_base, 4) if wt else 0.0,
        "weighted_stop_hunt_rate": round(wstop / wt_base, 4) if wt else 0.0,
        "tp_after_touch": tp,
        "sl_after_touch": sl,
        "weighted_tp_after_touch": round(wtp, 3),
        "weighted_sl_after_touch": round(wsl, 3),
        "fake_breakouts": fake,
        "trap_events": trap,
        "liquidity_grabs": liq,
        "stop_hunts": stop,
        "weighted_trap_pressure": round(max(0, min(100, weighted_trap_pressure)), 2),
        "weighted_clean_pressure": round(max(0, min(100, weighted_clean_pressure)), 2),
        "preferred_action": preferred_action,
    }


def get_sr_profile(symbol: str, direction: str = None, level_type: str = None, price: float = None) -> Dict[str, Any]:
    """Return compact SR/liquidity memory profile for analysis / TP-SL engines."""
    s = _state()
    sym = _norm_symbol(symbol)
    direct = _norm_direction(direction) if direction else None
    lt = _norm_level_type(level_type) if level_type else None
    p = _safe_float(price, 0.0)

    rows: List[Dict[str, Any]] = []
    for row in (s.get("levels") or {}).values():
        if not isinstance(row, dict):
            continue
        _ensure_common_fields(row)
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
        if isinstance(sym_row, dict):
            _ensure_common_fields(sym_row)
            agg = _aggregate_rows([sym_row])
        else:
            agg = {"samples": 0, "strength_score": 50, "fake_break_rate": 0.0, "bounce_rate": 0.0, "break_rate": 0.0, "trap_rate": 0.0, "liquidity_grab_rate": 0.0, "stop_hunt_rate": 0.0}
        return {
            "available": False,
            "symbol": sym,
            "direction": direct,
            "level_type": lt,
            **agg,
            "soft_layer": True,
            "source": "sr_learning_v3",
        }

    rows.sort(key=lambda r: (int(r.get("strength_score", 0) or 0), int(r.get("touches", 0) or 0)), reverse=True)
    agg = _aggregate_rows(rows)
    return {
        "available": True,
        "symbol": sym,
        "direction": direct,
        "level_type": lt,
        **agg,
        "levels": rows[:20],
        "soft_layer": True,
        "source": "sr_learning_v3",
    }


def get_liquidity_trap_profile(symbol: str, direction: str = None, price: float = None) -> Dict[str, Any]:
    """Convenience profile focused on trap/liquidity/stop-hunt risk."""
    profile = get_sr_profile(symbol, direction=direction, price=price)
    samples = int(profile.get("samples", 0) or 0)
    trap_risk_score = 0
    trap_risk_score += int(profile.get("weighted_fake_break_rate", profile.get("fake_break_rate", 0.0)) * 35)
    trap_risk_score += int(profile.get("weighted_trap_rate", profile.get("trap_rate", 0.0)) * 35)
    trap_risk_score += int(profile.get("weighted_stop_hunt_rate", profile.get("stop_hunt_rate", 0.0)) * 20)
    trap_risk_score += int(profile.get("weighted_liquidity_grab_rate", profile.get("liquidity_grab_rate", 0.0)) * 10)
    if samples < 3:
        risk = "UNKNOWN"
    elif trap_risk_score >= 45:
        risk = "HIGH"
    elif trap_risk_score >= 22:
        risk = "MEDIUM"
    else:
        risk = "LOW"
    profile.update({"trap_risk_score": max(0, min(100, trap_risk_score)), "trap_risk": risk})
    return profile


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

    if direct == "LONG" and lt == "RESISTANCE":
        return "CLEAN_BREAK" if ex > lp else "FAKE_BREAKOUT"
    if direct == "SHORT" and lt == "SUPPORT":
        return "CLEAN_BREAK" if ex < lp else "FAKE_BREAKOUT"
    if (direct == "LONG" and lt == "SUPPORT") or (direct == "SHORT" and lt == "RESISTANCE"):
        return "BOUNCE" if res != "SL" else "SL"
    return res


def suggest_liquidity_aware_buffer(symbol: str, direction: str, level_type: str, price: float, atr: float = None) -> Dict[str, Any]:
    """Return a soft TP/SL buffer suggestion so TP/SL can avoid obvious stop-hunt zones."""
    p = _safe_float(price, 0.0)
    a = _safe_float(atr, 0.0)
    if a <= 0 and p > 0:
        a = p * 0.0015
    profile = get_liquidity_trap_profile(symbol, direction=direction, price=price)
    risk = profile.get("trap_risk")
    mult = 0.18
    if risk == "HIGH":
        mult = 0.32
    elif risk == "MEDIUM":
        mult = 0.24
    return {
        "buffer_atr": round(mult, 4),
        "buffer_price": round(a * mult, 12),
        "trap_risk": risk,
        "trap_risk_score": profile.get("trap_risk_score", 0),
        "source": "sr_learning_v3",
        "soft_layer": True,
    }



def get_dynamic_zone_bias(symbol: str, direction: str = None, price: float = None) -> Dict[str, Any]:
    """Return a compact dynamic supply/demand bias for AI ranking/TP logic.

    LONG near demand/support with clean reaction is favorable; LONG near strong
    resistance/supply or trap-heavy area is caution. SHORT is symmetric. This
    stays soft and never blocks by itself.
    """
    direct = _norm_direction(direction) if direction else None
    support = get_sr_profile(symbol, direction=direction, level_type="SUPPORT", price=price)
    resistance = get_sr_profile(symbol, direction=direction, level_type="RESISTANCE", price=price)
    liquidity = get_liquidity_trap_profile(symbol, direction=direction, price=price)

    score = 0
    reasons: List[str] = []
    if direct == "LONG":
        if support.get("available") and support.get("weighted_clean_pressure", 0) >= 30:
            score += 4; reasons.append("demand/support reaction")
        if resistance.get("available") and resistance.get("weighted_trap_pressure", 0) >= 30:
            score -= 4; reasons.append("supply/trap pressure")
    elif direct == "SHORT":
        if resistance.get("available") and resistance.get("weighted_clean_pressure", 0) >= 30:
            score += 4; reasons.append("supply/resistance reaction")
        if support.get("available") and support.get("weighted_trap_pressure", 0) >= 30:
            score -= 4; reasons.append("demand/trap pressure")

    trap_score = int(liquidity.get("trap_risk_score", 0) or 0)
    if trap_score >= 45:
        score -= 3; reasons.append("liquidity trap high")
    elif trap_score <= 15 and liquidity.get("available"):
        score += 1; reasons.append("liquidity risk low")

    return {
        "symbol": _norm_symbol(symbol),
        "direction": direct,
        "zone_bias_score": max(-8, min(8, int(score))),
        "support": support,
        "resistance": resistance,
        "liquidity": liquidity,
        "reasons": reasons[:4],
        "soft_layer": True,
        "source": "sr_learning_v3",
    }

def format_sr_report(symbol: str = None, limit: int = 12) -> str:
    s = _state()
    events = s.get("events", []) if isinstance(s.get("events"), list) else []
    rows = list((s.get("levels") or {}).values())
    if symbol:
        sym = _norm_symbol(symbol)
        rows = [r for r in rows if isinstance(r, dict) and r.get("symbol") == sym]
    for r in rows:
        if isinstance(r, dict):
            _ensure_common_fields(r)
    rows.sort(key=lambda r: (int(r.get("strength_score", 0) or 0), int(r.get("touches", 0) or 0)), reverse=True)

    lines = [f"📐 SR / Liquidity Learning\nرویدادها: {len(events)}"]
    if not rows:
        return "\n".join(lines)

    lines.append("سطوح مهم:")
    for r in rows[:max(1, int(limit))]:
        lines.append(
            f"{r.get('symbol')} {r.get('direction')} {r.get('level_type')} | "
            f"Price={r.get('price')} | Strength={r.get('strength_score',50)} | "
            f"Bounce={round(_safe_float(r.get('bounce_rate'))*100,1)}% | "
            f"Break={round(_safe_float(r.get('break_rate'))*100,1)}% | "
            f"Fake={round(_safe_float(r.get('fake_break_rate'))*100,1)}% | "
            f"Trap={round(_safe_float(r.get('trap_rate'))*100,1)}% | "
            f"StopHunt={round(_safe_float(r.get('stop_hunt_rate'))*100,1)}%"
        )
    return "\n".join(lines)
