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
    register_tp_sl_v2_result
"""

import math
import time
from typing import Dict, Any, Optional, List, Tuple

from data_store import load_json, save_json

LEARNING_FILE = "coin_learning.json"
MAX_SIGNALS_STORED = 800
MAX_EVENTS_PER_BUCKET = 240
MAX_PATTERNS_PER_BUCKET = 80
REAL_WEIGHT = 1.0
GHOST_WEIGHT = 0.45
MIN_TP_MEMORY_WINS = 3
TP_SL_V2_VERSION = 1


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
    if r in {"TP", "TP1", "TAKE_PROFIT", "TAKEPROFIT"}:
        return "TP1"
    if r == "TP2":
        return "TP2"
    if r in {"SL", "STOP", "STOP_LOSS", "STOPLOSS"}:
        return "SL"
    return r or "UNKNOWN"


def _norm_source(source: Any = None, signal_type: Any = None) -> str:
    src = str(source or signal_type or "REAL").upper().strip()
    if src in {"GHOST", "SHADOW", "PAPER_GHOST"}:
        return "GHOST"
    return "REAL"


def _source_weight(source: str) -> float:
    return GHOST_WEIGHT if str(source).upper() == "GHOST" else REAL_WEIGHT


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
        "rsi", "rsi_5m", "macd", "macd_signal", "macd_hist", "adx", "atr",
        "ema20", "ema50", "ema200", "vwap", "vwap_status",
        "power2_buy", "power2_sell", "power3_buy", "power3_sell", "buy_power", "sell_power",
        "market_regime", "market_mode", "btc_bias", "support", "resistance",
        "risk_level", "risk_reward", "confirmations", "freshness", "entry_mode",
        "result_source", "move_percent", "exit_price",
    ]
    out = {k: snapshot.get(k) for k in keys if snapshot.get(k) is not None}
    if isinstance(snapshot.get("market_context"), dict):
        out["market_context"] = snapshot.get("market_context")
    if isinstance(snapshot.get("trends"), dict):
        out["trends"] = snapshot.get("trends")
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


def _weighted_win_rate(b: Dict[str, Any]) -> float:
    tp = _safe_float(b.get("weighted_tp"))
    sl = _safe_float(b.get("weighted_sl"))
    total = tp + sl
    return tp / total if total > 0 else 0.0


def _closed_weight(b: Dict[str, Any]) -> float:
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

    if behavior == "GOOD" and avg_fav >= 1.4:
        personality = "CLEAN_RUNNER"
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
    weight = _source_weight(source_norm)

    sig.update({
        "result": r,
        "exit_price": exit_price,
        "move_percent": move_percent,
        "closed_at": ts,
        "signal_type": source_norm,
        "result_snapshot": snap,
    })

    b = _bucket(s, sig.get("symbol"), sig.get("direction"))
    event = {
        "ts": ts,
        "result": r,
        "source": source_norm,
        "exit_price": exit_price,
        "move_percent": move_percent,
        "snapshot": snap,
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

    for name in ["rsi", "rsi_5m", "macd_hist", "adx", "power2_buy", "power2_sell", "power3_buy", "power3_sell", "buy_power", "sell_power"]:
        _update_avg_stat(b["indicator_stats"], name, snap.get(name), weight, r)
    for name in ["market_regime", "market_mode", "btc_bias", "vwap_status"]:
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


def should_require_extra_strength(symbol: str, direction: str, snapshot: Optional[Dict] = None) -> Dict:
    s = _state()
    b = s.get("by_coin_direction", {}).get(_key(symbol, direction), {})
    if not b:
        return {"required": False, "extra_score": 0, "extra_confirmations": 0}

    tp = _safe_float(b.get("weighted_tp"))
    sl = _safe_float(b.get("weighted_sl"))
    total = tp + sl
    wr = tp / max(total, 1e-9) if total else 0.0
    risk_bias = _safe_int(b.get("risk_bias"), 0)
    similar_sl = _similar_sl_pressure(b, snapshot)

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


def format_learning_summary() -> str:
    s = _state()
    rows = list(s.get("by_coin_direction", {}).values())
    real = sum(int(r.get("real_total", 0)) for r in rows)
    ghost = sum(int(r.get("ghost_total", 0)) for r in rows)
    sl = sum(int(r.get("sl", 0)) for r in rows)
    tp = sum(int(r.get("tp1", 0)) + int(r.get("tp2", 0)) for r in rows)
    wr = round(tp / max(tp + sl, 1) * 100, 1) if (tp + sl) else 0
    good = len([r for r in rows if r.get("behavior") == "GOOD"])
    bad = len([r for r in rows if r.get("behavior") == "BAD"])
    return f"🧠 خلاصه یادگیری\nReal: {real}\nGhost: {ghost}\nTP: {tp} | SL: {sl}\nWinRate: {wr}%\nرفتار خوب: {good} | رفتار بد: {bad}"


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
            f"رفتار:{r.get('behavior','UNKNOWN')} | شخصیت:{r.get('personality','UNKNOWN')} | اعتماد:{r.get('confidence',0)}"
        )
    return "\n".join(lines)


def format_smart_stats() -> str:
    return format_learning_summary()
