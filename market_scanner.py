from __future__ import annotations

"""
Market Scanner.

Responsibilities:
- Fetch real candles from Toobit/Binance-compatible public endpoints.
- Build feature map for all symbols.
- Build market context/cache.
- Provide fast market status for bot commands.
- Never generate final signals by itself.

No synthetic/random candles are used here.
If data is missing, symbol is skipped safely and diagnostics are returned.
"""

import time
from typing import Any, Dict, List, Optional, Sequence

from config import DEFAULT_SYMBOLS, CORE_DATA_FILES, MARKET_CACHE_TTL_SECONDS
from data_store import cache_get, cache_set, load_dict, save_json
from diagnostics import safe, record_error, warning
import tobit_client
import analysis
import market_structure
import market_sentiment


TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h"]
DEFAULT_LIMIT = 160


def _ts() -> int:
    return int(time.time())


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


@safe(default={})
def fetch_symbol_candles(symbol: str, timeframes: Optional[List[str]] = None, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    c = tobit_client.client()
    symbol = str(symbol).upper()
    timeframes = timeframes or TIMEFRAMES
    out: Dict[str, Any] = {"ok": True, "symbol": symbol, "candles": {}, "errors": {}}
    for tf in timeframes:
        candles = c.klines(symbol, tf, limit)
        if not candles:
            out["errors"][tf] = "missing_candles"
            out["ok"] = False
            continue
        out["candles"][tf] = candles
    return out


@safe(default={})
def build_symbol_snapshot(symbol: str, timeframes: Optional[List[str]] = None, limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    raw = fetch_symbol_candles(symbol, timeframes, limit)
    if not raw.get("candles"):
        return {"ok": False, "symbol": str(symbol).upper(), "reason": "no_candles", "errors": raw.get("errors", {})}

    candle_map = raw["candles"]
    features = analysis.multi_timeframe_features(candle_map, symbol=symbol)
    structure = market_structure.multi_timeframe_structure(candle_map, symbol=symbol)
    latest_price = _latest_price(candle_map)
    return {
        "ok": True,
        "symbol": str(symbol).upper(),
        "price": latest_price,
        "features": features,
        "structure": structure,
        "errors": raw.get("errors", {}),
        "created_at": _ts(),
    }


def _latest_price(candle_map: Dict[str, List[Dict[str, Any]]]) -> float:
    for tf in ["5m", "15m", "30m", "1h", "4h"]:
        rows = candle_map.get(tf) or []
        if rows:
            return _safe_float(rows[-1].get("close"))
    return 0.0


@safe(default={})
def scan_market(
    symbols: Optional[List[str]] = None,
    timeframes: Optional[List[str]] = None,
    limit: int = DEFAULT_LIMIT,
    external_context: Optional[Dict[str, Any]] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """
    Returns full market snapshot. This can be heavy; bot commands should use cached status.
    """
    cache_key = "latest_market_scan"
    if use_cache:
        cached = cache_get("market_cache", cache_key, MARKET_CACHE_TTL_SECONDS)
        if cached:
            return cached

    symbols = [s.upper() for s in (symbols or DEFAULT_SYMBOLS)]
    snapshots: Dict[str, Any] = {}
    feature_map: Dict[str, Any] = {}
    errors: Dict[str, Any] = {}

    for symbol in symbols:
        snap = build_symbol_snapshot(symbol, timeframes=timeframes, limit=limit)
        if not snap.get("ok"):
            errors[symbol] = snap
            continue
        snapshots[symbol] = snap
        # Use 5m as symbol's quick market feature for breadth.
        tf5 = snap.get("features", {}).get("timeframes", {}).get("5m") or snap.get("features", {}).get("timeframes", {}).get("5M") or {}
        feature_map[symbol] = tf5

    ctx = market_sentiment.build_market_context(feature_map, external_context=external_context or {})
    result = {
        "ok": True,
        "symbols_requested": len(symbols),
        "symbols_ok": len(snapshots),
        "snapshots": snapshots,
        "feature_map": feature_map,
        "market_context": ctx,
        "errors": errors,
        "created_at": _ts(),
    }
    cache_set("market_cache", cache_key, result)
    cache_set("market_cache", "market_status_fast", compact_market_status(result))
    return result


@safe(default={})
def compact_market_status(scan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if scan is None:
        scan = cache_get("market_cache", "latest_market_scan", MARKET_CACHE_TTL_SECONDS * 10, default={}) or {}
    ctx = scan.get("market_context", {})
    breadth = ctx.get("breadth", {})
    return {
        "ok": bool(scan),
        "market_mode": ctx.get("market_mode", "UNKNOWN"),
        "market_score": ctx.get("market_score", 0),
        "btc_bias": ctx.get("btc_bias", "UNKNOWN"),
        "btc_trend": ctx.get("btc_trend", "UNKNOWN"),
        "bullish_pct": ctx.get("market_breadth_bullish", 0),
        "bearish_pct": ctx.get("market_breadth_bearish", 0),
        "neutral_pct": ctx.get("market_breadth_neutral", 0),
        "symbols_ok": scan.get("symbols_ok", 0),
        "symbols_requested": scan.get("symbols_requested", 0),
        "created_at": scan.get("created_at", 0),
    }


@safe(default={})
def get_cached_market_status() -> Dict[str, Any]:
    return cache_get("market_cache", "market_status_fast", MARKET_CACHE_TTL_SECONDS * 20, default={}) or {}


@safe(default="")
def market_status_fa() -> str:
    s = get_cached_market_status()
    if not s:
        return "وضعیت بازار هنوز آماده نیست."
    mode_fa = {"BULLISH": "صعودی", "BEARISH": "نزولی", "RANGE": "رنج", "UNKNOWN": "نامشخص"}.get(str(s.get("market_mode")), str(s.get("market_mode")))
    return (
        "🌐 وضعیت بازار\n"
        f"حالت: {mode_fa}\n"
        f"BTC: {s.get('btc_bias')}\n"
        f"صعودی: {s.get('bullish_pct')}% | نزولی: {s.get('bearish_pct')}% | خنثی: {s.get('neutral_pct')}%\n"
        f"ارزهای بررسی‌شده: {s.get('symbols_ok')}/{s.get('symbols_requested')}"
    )


@safe(default=[])
def scan_symbols_quick(symbols: Optional[List[str]] = None, limit_symbols: int = 10) -> List[Dict[str, Any]]:
    symbols = (symbols or DEFAULT_SYMBOLS)[:limit_symbols]
    rows = []
    for s in symbols:
        snap = build_symbol_snapshot(s, timeframes=["5m", "15m"], limit=120)
        if snap.get("ok"):
            rows.append(snap)
    return rows
