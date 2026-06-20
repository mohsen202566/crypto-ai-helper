# -*- coding: utf-8 -*-
"""
ghost_signals.py

Shadow/Ghost signal learning engine for the crypto AI bot.

Purpose:
- Store eligible signals that were not sent/opened because slots were full or
  scanner gates blocked them.
- Monitor Ghost signals until TP1/SL (TP2 is tolerated for backward
  compatibility but TP1/SL remains the main learning signal).
- Send Ghost outcomes to BOTH coin_learning and coin_risk so Ghost results
  affect future AI strictness with lower weight than real results.
"""

import time
import uuid
from typing import Dict, Any, List, Optional, Tuple

from data_store import load_json, save_json

try:
    from config import MAX_GHOST_SIGNALS, GHOST_LEARNING_ENABLED
except Exception:
    MAX_GHOST_SIGNALS = 1200
    GHOST_LEARNING_ENABLED = True

MIN_GHOST_MEMORY_STORED = 300

def _ghost_memory_limit() -> int:
    """Keep enough Ghost history for the agreed 20k learning memory.

    config.MAX_GHOST_SIGNALS may exist from older deployments and can be too
    small (for example 500/1000).  Use it only when it is larger than the
    agreed learning floor so updates do not silently erase Ghost evidence.
    """
    try:
        return max(100, min(int(MAX_GHOST_SIGNALS or 1200), 1200))
    except Exception:
        return MIN_GHOST_MEMORY_STORED

try:
    import ccxt
except Exception:
    ccxt = None

try:
    from coin_learning import record_signal, update_signal_result
    from ai_memory import update_ai_summary
except Exception:
    record_signal = None
    update_signal_result = None
    update_ai_summary = None

try:
    from coin_risk import register_ghost_result, register_result
except Exception:
    register_ghost_result = None
    register_result = None

GHOST_FILE = "ghost_signals.json"
MAX_OPEN_GHOSTS = 300
MAX_CLOSED_GHOSTS = 700
MAX_GHOST_AGE_SECONDS = 6 * 60 * 60

def _sort_key(row):
    if isinstance(row, tuple):
        row = row[1]
    if not isinstance(row, dict):
        return 0
    return int(row.get("created_at") or row.get("closed_at") or row.get("timestamp") or 0)

def _compact_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        return {}
    keep = [
        "symbol", "direction", "price", "entry", "score", "ai_score", "movement_score",
        "move_phase", "move_state", "move_freshness", "freshness", "freshness_score",
        "trap_risk", "reversal_risk", "prediction_score", "expected_move_atr",
        "rsi", "adx", "macd", "macd_signal", "macd_hist", "atr",
        "power2_buy", "power2_sell", "power3_buy", "power3_sell",
        "buy_power", "sell_power", "market_mode", "btc_bias",
        "support", "resistance", "vwap_status", "entry_mode",
        "entry_confirmed", "setup_detected", "candidate_direction",
    ]
    out = {k: snapshot.get(k) for k in keep if snapshot.get(k) is not None}
    for nested in ("ai_movement_hunter", "ai_decision", "prediction_layer"):
        v = snapshot.get(nested)
        if isinstance(v, dict):
            out[nested] = {k: v.get(k) for k in keep if v.get(k) is not None}
    return out

def _compact_ghost(g):
    if not isinstance(g, dict):
        return g
    x = dict(g)
    x["snapshot"] = _compact_snapshot(x.get("snapshot"))
    return x

def _trim_state(s, save=False):
    if not isinstance(s, dict):
        s = {"open": {}, "closed": []}
    open_map = s.get("open") if isinstance(s.get("open"), dict) else {}
    closed = s.get("closed") if isinstance(s.get("closed"), list) else []
    now = _now()

    # Close very old open Ghosts as EXPIRED so they do not stay open forever.
    still_open = {}
    expired = []
    for gid, g in open_map.items():
        if not isinstance(g, dict):
            continue
        created = int(g.get("created_at") or now)
        if now - created > MAX_GHOST_AGE_SECONDS:
            gg = _compact_ghost(g)
            gg.update({"status": "CLOSED", "result": "EXPIRED", "closed_at": now, "movement_outcome": "GHOST_EXPIRED"})
            expired.append(gg)
        else:
            still_open[gid] = _compact_ghost(g)

    if len(still_open) > MAX_OPEN_GHOSTS:
        items = sorted(still_open.items(), key=_sort_key)
        for gid, g in items[:-MAX_OPEN_GHOSTS]:
            gg = _compact_ghost(g)
            gg.update({"status": "CLOSED", "result": "EXPIRED", "closed_at": now, "movement_outcome": "GHOST_TRIMMED"})
            expired.append(gg)
        still_open = dict(items[-MAX_OPEN_GHOSTS:])

    closed = [_compact_ghost(x) for x in closed if isinstance(x, dict)] + expired
    closed = sorted(closed, key=_sort_key)[-MAX_CLOSED_GHOSTS:]
    s["open"] = still_open
    s["closed"] = closed
    if save:
        save_json(GHOST_FILE, s)
    return s

_GHOST_PRICE_CACHE = {"ts": 0, "prices": {}}
_GHOST_PRICE_TTL_SECONDS = 20

MOVEMENT_KEYS = [
    "movement_architecture", "ai_decision", "movement_decision", "movement_type",
    "move_phase", "move_state", "freshness", "move_freshness", "freshness_score",
    "movement_freshness_score", "trap_risk", "liquidity_risk", "liquidity_risk_score",
    "reversal_risk", "reversal_risk_score", "prediction_score", "expected_move_atr",
    "expected_move_pct", "pump_dump_probability", "setup_quality", "entry_quality",
    "setup_detected", "entry_confirmed", "entry_activation", "candidate_source",
    "ai_final_score", "ai_final_rank", "ai_confidence", "ai_score", "decision",
]


def _now() -> int:
    return int(time.time())


def _state() -> Dict[str, Any]:
    s = load_json(GHOST_FILE, {"open": {}, "closed": []})
    if not isinstance(s, dict):
        s = {"open": {}, "closed": []}
    if not isinstance(s.get("open"), dict):
        s["open"] = {}
    if not isinstance(s.get("closed"), list):
        s["closed"] = []
    return _trim_state(s, save=False)


def _to_okx_symbol(symbol: str) -> str:
    coin = str(symbol).upper().replace("USDT", "").strip()
    return f"{coin}/USDT:USDT"


def _get_exchange():
    if ccxt is None:
        return None
    try:
        return ccxt.okx({"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": "swap"}})
    except Exception:
        return None


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _move_percent(direction: str, entry: float, exit_price: float) -> float:
    entry = _safe_float(entry, 0.0) or 0.0
    exit_price = _safe_float(exit_price, 0.0) or 0.0
    if entry <= 0 or exit_price <= 0:
        return 0.0
    direction = str(direction).upper()
    if direction == "LONG":
        return round((exit_price - entry) / entry * 100, 4)
    if direction == "SHORT":
        return round((entry - exit_price) / entry * 100, 4)
    return 0.0


def _extract_movement_context(snapshot: Optional[Dict[str, Any]] = None, fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Extract AI Movement Hunter metadata without changing old callers.

    Ghost signals are the main shadow-learning path.  They must preserve why AI
    created/ghosted/rejected a movement candidate: setup vs entry, fresh vs late,
    trap/liquidity/reversal risk, and final REAL/GHOST/REJECT style decision.
    """
    snap = snapshot if isinstance(snapshot, dict) else {}
    fb = fallback if isinstance(fallback, dict) else {}
    out: Dict[str, Any] = {
        "movement_architecture": "AI_MOVEMENT_HUNTER",
        "candidate_source": fb.get("source") or snap.get("candidate_source") or snap.get("source") or "scanner",
        "ai_decision": fb.get("decision") or snap.get("ai_decision") or snap.get("movement_decision") or snap.get("decision") or "GHOST",
        "movement_type": snap.get("movement_type") or snap.get("move_type") or snap.get("signal_type") or "UNKNOWN",
        "move_phase": snap.get("move_phase") or snap.get("move_state") or snap.get("state") or "UNKNOWN",
        "move_freshness": snap.get("move_freshness") or snap.get("freshness") or snap.get("freshness_label") or "UNKNOWN",
        "freshness_score": snap.get("freshness_score") or snap.get("movement_freshness_score") or snap.get("freshness"),
        "trap_risk": snap.get("trap_risk") or (snap.get("liquidity_trap") or {}).get("trap_risk") if isinstance(snap.get("liquidity_trap"), dict) else snap.get("trap_risk"),
        "reversal_risk_score": snap.get("reversal_risk_score") or (snap.get("prediction_layer") or {}).get("reversal_risk_score") if isinstance(snap.get("prediction_layer"), dict) else snap.get("reversal_risk_score"),
        "prediction_score": snap.get("prediction_score") or (snap.get("prediction_layer") or {}).get("prediction_score") if isinstance(snap.get("prediction_layer"), dict) else snap.get("prediction_score"),
        "expected_move_atr": snap.get("expected_move_atr") or (snap.get("prediction_layer") or {}).get("expected_move_atr") if isinstance(snap.get("prediction_layer"), dict) else snap.get("expected_move_atr"),
        "setup_detected": bool(snap.get("setup_detected", True)),
        "entry_confirmed": bool(snap.get("entry_confirmed", False)),
    }
    for key in MOVEMENT_KEYS:
        if key not in out and snap.get(key) is not None:
            out[key] = snap.get(key)
    # Normalize ambiguous phase/status strings for reporting/learning.
    phase = str(out.get("move_phase") or "UNKNOWN").upper()
    if phase in {"LATE_OR_EXHAUSTION", "EXHAUSTION", "EXHAUSTED"}:
        out["move_phase"] = "EXHAUSTION"
    elif phase in {"RANGE_AFTER_MOVE", "AFTER_MOVE_RANGE"}:
        out["move_phase"] = "RANGE_AFTER_MOVE"
    elif phase in {"START", "EARLY", "EARLY_MOMENTUM"}:
        out["move_phase"] = "START" if phase == "START" else "EARLY"
    return {k: v for k, v in out.items() if v is not None}


def _update_open_mfe_mae(g: Dict[str, Any], current_price: float) -> Dict[str, Any]:
    """Track maximum favorable/adverse movement for Ghost learning."""
    entry = _safe_float(g.get("entry"), 0.0) or 0.0
    price = _safe_float(current_price, 0.0) or 0.0
    if entry <= 0 or price <= 0:
        return g
    direction = str(g.get("direction") or "").upper()
    if direction == "LONG":
        favorable = max(0.0, (price - entry) / entry * 100.0)
        adverse = max(0.0, (entry - price) / entry * 100.0)
    elif direction == "SHORT":
        favorable = max(0.0, (entry - price) / entry * 100.0)
        adverse = max(0.0, (price - entry) / entry * 100.0)
    else:
        return g
    g["max_favorable_move_pct"] = round(max(_safe_float(g.get("max_favorable_move_pct"), 0.0) or 0.0, favorable), 4)
    g["max_adverse_move_pct"] = round(max(_safe_float(g.get("max_adverse_move_pct"), 0.0) or 0.0, adverse), 4)
    g["last_checked_price"] = price
    g["last_checked_at"] = _now()
    return g


def _fetch_prices(symbols: List[str]) -> Dict[str, float]:
    """Fetch live prices with a short cache, without starving new symbols.

    Older logic returned immediately from cache when the cache was fresh, even
    if the current check requested symbols that were not already cached.  That
    could make newly opened Ghost signals look like price-fetch errors for up
    to the cache TTL.  This version serves cached symbols and fetches only the
    missing ones.
    """
    now = _now()
    requested = [str(s).upper() for s in symbols if s]
    cached_ts = int(_GHOST_PRICE_CACHE.get("ts") or 0)
    cached_prices = _GHOST_PRICE_CACHE.setdefault("prices", {})
    cache_fresh = bool(cached_prices) and now - cached_ts <= _GHOST_PRICE_TTL_SECONDS

    prices: Dict[str, float] = {}
    missing: List[str] = []
    for symbol in requested:
        cached_price = cached_prices.get(symbol) if cache_fresh else None
        if cached_price is not None:
            prices[symbol] = cached_price
        else:
            missing.append(symbol)

    if not missing:
        return prices

    ex = _get_exchange()
    if ex is None:
        return prices

    for symbol in missing:
        try:
            ticker = ex.fetch_ticker(_to_okx_symbol(symbol))
            price = _safe_float(ticker.get("last") or ticker.get("close"))
            if price and price > 0:
                prices[symbol] = price
                cached_prices[symbol] = price
        except Exception:
            continue

    _GHOST_PRICE_CACHE["ts"] = now
    _GHOST_PRICE_CACHE["prices"] = dict(cached_prices)
    return prices


def _ghost_hit_result(g: Dict[str, Any], current_price: float) -> Tuple[Optional[str], Optional[float]]:
    direction = str(g.get("direction", "")).upper()
    sl = _safe_float(g.get("stop_loss"))
    tp1 = _safe_float(g.get("tp1"))
    tp2 = _safe_float(g.get("tp2"))
    price = _safe_float(current_price)
    if price is None or sl is None or tp1 is None:
        return None, None

    # SL is checked first to stay conservative and consistent with real tracker.
    if direction == "LONG":
        if price <= sl:
            return "SL", sl
        if price >= tp1:
            return "TP1", tp1
        if tp2 is not None and price >= tp2:
            return "TP2", tp2
    elif direction == "SHORT":
        if price >= sl:
            return "SL", sl
        if price <= tp1:
            return "TP1", tp1
        if tp2 is not None and price <= tp2:
            return "TP2", tp2
    return None, None


def _learning_snapshot(g: Dict[str, Any], result: Optional[str] = None, exit_price: Optional[float] = None, move_percent: Optional[float] = None) -> Dict[str, Any]:
    """Compact snapshot sent to coin_learning and coin_risk.

    Preserve the analysis snapshot but add Ghost/result metadata so long-term
    risk memory can learn from the exact conditions of the shadow signal.
    """
    snap = g.get("snapshot") if isinstance(g.get("snapshot"), dict) else {}
    out = dict(snap)
    for key in [
        "symbol", "direction", "entry", "price", "score", "risk_level",
        "risk_reward", "confirmations", "freshness", "rsi", "adx", "macd",
        "macd_signal", "macd_hist", "power2_buy", "power2_sell",
        "power3_buy", "power3_sell", "buy_power", "sell_power", "atr",
        "market_mode", "market_regime", "coin_behavior", "btc_bias",
        "support", "resistance", "vwap_status", "entry_mode", "reason",
        "movement_architecture", "ai_decision", "movement_decision", "movement_type",
        "move_phase", "move_state", "move_freshness", "freshness_score",
        "prediction_score", "reversal_risk_score", "expected_move_atr",
        "trap_risk", "liquidity_risk_score", "setup_detected", "entry_confirmed",
        "max_favorable_move_pct", "max_adverse_move_pct",
    ]:
        if key not in out and g.get(key) is not None:
            out[key] = g.get(key)
    movement = _extract_movement_context(snap, g)
    out.update({k: v for k, v in movement.items() if k not in out or out.get(k) in (None, "", "UNKNOWN")})
    out["ghost_reason"] = g.get("reason")
    out["ghost_source"] = g.get("source")
    if result is not None:
        out["result"] = result
    if exit_price is not None:
        out["exit_price"] = exit_price
    if move_percent is not None:
        out["move_percent"] = move_percent
    if g.get("max_favorable_move_pct") is not None:
        out["max_favorable_move_pct"] = g.get("max_favorable_move_pct")
    if g.get("max_adverse_move_pct") is not None:
        out["max_adverse_move_pct"] = g.get("max_adverse_move_pct")
    ts = _now()
    out.setdefault("snapshot_at", g.get("created_at") or ts)
    out["result_source"] = "GHOST"
    out["result_recorded_at"] = ts
    return out


def _record_ghost_outcome_to_ai(g: Dict[str, Any], result: str, exit_price: float, move_percent: float) -> None:
    signal_id = g.get("signal_id") or g.get("id")
    snapshot = _learning_snapshot(g, result=result, exit_price=exit_price, move_percent=move_percent)

    if update_signal_result:
        try:
            update_signal_result(signal_id, result, exit_price=exit_price, move_percent=move_percent, snapshot=snapshot, source="GHOST")
        except TypeError:
            try:
                update_signal_result(signal_id, result, exit_price=exit_price, move_percent=move_percent)
            except TypeError:
                try:
                    update_signal_result(signal_id, result)
                except Exception:
                    pass
            except Exception:
                pass
        except Exception:
            pass

    # New coin_risk.py: Ghost results must affect strictness with lower weight.
    try:
        if register_ghost_result:
            register_ghost_result(g.get("symbol"), g.get("direction"), result, snapshot=snapshot)
            return
    except Exception:
        pass

    # Backward-compatible fallback for older coin_risk.py deployments.
    if register_result:
        try:
            register_result(g.get("symbol"), g.get("direction"), result, source="GHOST", snapshot=snapshot, is_ghost=True)
        except TypeError:
            try:
                register_result(g.get("symbol"), g.get("direction"), result)
            except Exception:
                pass
        except Exception:
            pass


def create_ghost_signal(
    symbol: str,
    direction: str,
    entry: float,
    stop_loss: float,
    tp1: float,
    tp2=None,
    score=None,
    snapshot=None,
    source="scanner",
    reason="SLOT_FULL",
) -> Dict[str, Any]:
    if not GHOST_LEARNING_ENABLED:
        return {}
    s = _state()
    gid = f"ghost_{symbol}_{direction}_{_now()}_{uuid.uuid4().hex[:6]}"
    snap = _compact_snapshot(snapshot if isinstance(snapshot, dict) else {})
    movement_context = _extract_movement_context(snap, {"source": source, "reason": reason, "decision": "GHOST"})
    g = {
        "signal_id": gid,
        "id": gid,
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "entry": float(entry),
        "price": float(entry),
        "stop_loss": float(stop_loss),
        "tp1": float(tp1),
        "tp2": tp2,
        "score": score,
        "snapshot": snap,
        "source": source,
        "reason": reason,
        "movement_architecture": movement_context.get("movement_architecture", "AI_MOVEMENT_HUNTER"),
        "ai_decision": movement_context.get("ai_decision", "GHOST"),
        "movement_type": movement_context.get("movement_type", "UNKNOWN"),
        "move_phase": movement_context.get("move_phase", "UNKNOWN"),
        "move_freshness": movement_context.get("move_freshness", "UNKNOWN"),
        "freshness_score": movement_context.get("freshness_score"),
        "prediction_score": movement_context.get("prediction_score"),
        "reversal_risk_score": movement_context.get("reversal_risk_score"),
        "expected_move_atr": movement_context.get("expected_move_atr"),
        "trap_risk": movement_context.get("trap_risk"),
        "setup_detected": movement_context.get("setup_detected", True),
        "entry_confirmed": movement_context.get("entry_confirmed", False),
        "max_favorable_move_pct": 0.0,
        "max_adverse_move_pct": 0.0,
        "created_at": _now(),
        "status": "OPEN",
    }
    s["open"][gid] = _compact_ghost(g)
    s = _trim_state(s, save=False)
    save_json(GHOST_FILE, s)

    if record_signal:
        try:
            record_signal(g, signal_type="GHOST")
        except Exception:
            pass
    if update_ai_summary:
        try:
            update_ai_summary(
                total_ghost_signals=1,
                source="GHOST",
                movement_event="GHOST_CREATED",
                movement_type=g.get("movement_type"),
                move_phase=g.get("move_phase"),
                move_freshness=g.get("move_freshness"),
                ai_decision=g.get("ai_decision"),
                snapshot=g.get("snapshot"),
            )
        except Exception:
            try:
                update_ai_summary(total_ghost_signals=1)
            except Exception:
                pass
    return g


def close_ghost_signal(signal_id: str, result: str, exit_price: float, move_percent: float = 0.0) -> bool:
    s = _state()
    g = s["open"].pop(signal_id, None)
    if not g:
        return False

    result = str(result or "").upper()
    g.update({
        "status": "CLOSED",
        "result": result,
        "exit_price": exit_price,
        "move_percent": move_percent,
        "closed_at": _now(),
        "movement_outcome": "GHOST_TP" if result in {"TP", "TP1", "TP2"} else "GHOST_SL" if result == "SL" else "GHOST_CLOSED",
    })
    s["closed"].append(_compact_ghost(g))
    s = _trim_state(s, save=False)
    save_json(GHOST_FILE, s)

    _record_ghost_outcome_to_ai(g, result, exit_price, move_percent)

    if update_ai_summary:
        try:
            common = {
                "source": "GHOST",
                "movement_event": g.get("movement_outcome"),
                "movement_type": g.get("movement_type"),
                "move_phase": g.get("move_phase"),
                "move_freshness": g.get("move_freshness"),
                "ai_decision": g.get("ai_decision"),
                "snapshot": _learning_snapshot(g, result=result, exit_price=exit_price, move_percent=move_percent),
            }
            if result == "SL":
                update_ai_summary(total_ghost_sl=1, **common)
            elif result in {"TP", "TP1", "TP2"}:
                update_ai_summary(total_ghost_tp=1, **common)
        except Exception:
            try:
                if result == "SL":
                    update_ai_summary(total_ghost_sl=1)
                elif result in {"TP", "TP1", "TP2"}:
                    update_ai_summary(total_ghost_tp=1)
            except Exception:
                pass
    return True


def check_open_ghost_signals(max_checks: int = 120) -> Dict[str, Any]:
    """Check open Ghost signals against live price and close TP/SL hits.

    This does not change scanner/analysis behavior. It only turns already-open
    Ghost records into CLOSED records when their TP1/SL has been reached, so
    Ghost learning can feed coin_learning and coin_risk.
    """
    s = _state()
    open_items = list(s.get("open", {}).items())[:max_checks]
    if not open_items:
        return {"checked": 0, "closed": 0, "tp": 0, "sl": 0, "errors": 0}

    symbols = sorted({str(g.get("symbol", "")).upper() for _, g in open_items if g.get("symbol")})
    prices = _fetch_prices(symbols)
    closed_count = 0
    tp_count = 0
    sl_count = 0
    errors = 0

    changed_open = False
    closed_ids = set()
    for gid, g in open_items:
        try:
            symbol = str(g.get("symbol", "")).upper()
            price = prices.get(symbol)
            if price is None:
                errors += 1
                continue
            before_mfe = g.get("max_favorable_move_pct")
            before_mae = g.get("max_adverse_move_pct")
            g = _update_open_mfe_mae(g, price)
            if before_mfe != g.get("max_favorable_move_pct") or before_mae != g.get("max_adverse_move_pct"):
                s["open"][gid] = g
                changed_open = True
            result, exit_price = _ghost_hit_result(g, price)
            if not result:
                continue
            pct = _move_percent(g.get("direction"), g.get("entry"), exit_price)
            if close_ghost_signal(gid, result, exit_price, pct):
                closed_ids.add(gid)
                if gid in s.get("open", {}):
                    s["open"].pop(gid, None)
                closed_count += 1
                if str(result).upper() == "SL":
                    sl_count += 1
                else:
                    tp_count += 1
        except Exception:
            errors += 1
            continue
    if changed_open and not closed_ids:
        try:
            s = _trim_state(s, save=False)
            save_json(GHOST_FILE, s)
        except Exception:
            pass
    return {"checked": len(open_items), "closed": closed_count, "tp": tp_count, "sl": sl_count, "errors": errors}


def _movement_ghost_stats(closed: List[Dict[str, Any]], open_map: Dict[str, Any]) -> Dict[str, Any]:
    rows = list(open_map.values()) + list(closed)
    phase_counts: Dict[str, int] = {}
    decision_counts: Dict[str, int] = {}
    fresh_counts: Dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        phase = str(row.get("move_phase") or row.get("move_state") or "UNKNOWN").upper()
        decision = str(row.get("ai_decision") or row.get("decision") or "GHOST").upper()
        fresh = str(row.get("move_freshness") or row.get("freshness") or "UNKNOWN").upper()
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        fresh_counts[fresh] = fresh_counts.get(fresh, 0) + 1
    return {"phase_counts": phase_counts, "decision_counts": decision_counts, "freshness_counts": fresh_counts}


def get_ghost_stats(auto_check: bool = True) -> Dict[str, Any]:
    checked = None
    if auto_check:
        try:
            checked = check_open_ghost_signals()
        except Exception:
            checked = None
    s = _state()
    closed = s.get("closed", [])
    tp = len([x for x in closed if str(x.get("result")).upper() in ["TP1", "TP2", "TP"]])
    sl = len([x for x in closed if str(x.get("result")).upper() == "SL"])
    open_map = s.get("open", {}) if isinstance(s.get("open", {}), dict) else {}
    out = {"open": len(open_map), "closed": len(closed), "tp": tp, "sl": sl}
    out["movement"] = _movement_ghost_stats(closed, open_map)
    if checked is not None:
        out["checked"] = checked
    return out


def format_ghost_report() -> str:
    st = get_ghost_stats(auto_check=True)
    checked = st.get("checked") or {}
    extra = ""
    if checked:
        extra = f"\nبررسی اخیر: {checked.get('checked', 0)} | بسته‌شده جدید: {checked.get('closed', 0)}"
    movement = st.get("movement", {}) if isinstance(st.get("movement"), dict) else {}
    phases = movement.get("phase_counts", {}) if isinstance(movement.get("phase_counts"), dict) else {}
    phase_line = ""
    if phases:
        shown = ", ".join([f"{k}:{v}" for k, v in list(phases.items())[:4]])
        phase_line = f"\nفاز حرکت: {shown}"
    return f"👻 Ghost Signals\nباز: {st['open']}\nبسته: {st['closed']}\nTP: {st['tp']} | SL: {st['sl']}{phase_line}{extra}"
