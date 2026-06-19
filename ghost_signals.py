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
    MAX_GHOST_SIGNALS = 20000
    GHOST_LEARNING_ENABLED = True

MIN_GHOST_MEMORY_STORED = 20000

def _ghost_memory_limit() -> int:
    """Keep enough Ghost history for the agreed 20k learning memory.

    config.MAX_GHOST_SIGNALS may exist from older deployments and can be too
    small (for example 500/1000).  Use it only when it is larger than the
    agreed learning floor so updates do not silently erase Ghost evidence.
    """
    try:
        return max(MIN_GHOST_MEMORY_STORED, int(MAX_GHOST_SIGNALS or 0))
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
_GHOST_PRICE_CACHE = {"ts": 0, "prices": {}}
_GHOST_PRICE_TTL_SECONDS = 20


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
    return s


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
    ]:
        if key not in out and g.get(key) is not None:
            out[key] = g.get(key)
    if result is not None:
        out["result"] = result
    if exit_price is not None:
        out["exit_price"] = exit_price
    if move_percent is not None:
        out["move_percent"] = move_percent
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
        "snapshot": snapshot or {},
        "source": source,
        "reason": reason,
        "created_at": _now(),
        "status": "OPEN",
    }
    s["open"][gid] = g
    limit = _ghost_memory_limit()
    while len(s["open"]) > limit:
        first = sorted(s["open"].keys())[0]
        del s["open"][first]
    save_json(GHOST_FILE, s)

    if record_signal:
        try:
            record_signal(g, signal_type="GHOST")
        except Exception:
            pass
    if update_ai_summary:
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
    })
    s["closed"].append(g)
    s["closed"] = s["closed"][-_ghost_memory_limit():]
    save_json(GHOST_FILE, s)

    _record_ghost_outcome_to_ai(g, result, exit_price, move_percent)

    if update_ai_summary:
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

    for gid, g in open_items:
        try:
            symbol = str(g.get("symbol", "")).upper()
            price = prices.get(symbol)
            if price is None:
                errors += 1
                continue
            result, exit_price = _ghost_hit_result(g, price)
            if not result:
                continue
            pct = _move_percent(g.get("direction"), g.get("entry"), exit_price)
            if close_ghost_signal(gid, result, exit_price, pct):
                closed_count += 1
                if str(result).upper() == "SL":
                    sl_count += 1
                else:
                    tp_count += 1
        except Exception:
            errors += 1
            continue
    return {"checked": len(open_items), "closed": closed_count, "tp": tp_count, "sl": sl_count, "errors": errors}


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
    out = {"open": len(s.get("open", {})), "closed": len(closed), "tp": tp, "sl": sl}
    if checked is not None:
        out["checked"] = checked
    return out


def format_ghost_report() -> str:
    st = get_ghost_stats(auto_check=True)
    checked = st.get("checked") or {}
    extra = ""
    if checked:
        extra = f"\nبررسی اخیر: {checked.get('checked', 0)} | بسته‌شده جدید: {checked.get('closed', 0)}"
    return f"👻 Ghost Signals\nباز: {st['open']}\nبسته: {st['closed']}\nTP: {st['tp']} | SL: {st['sl']}{extra}"
