"""
market_data.py
Level 4 / 1H Smart Scalp Bot

OKX market data adapter.

Architecture lock:
- Owns lightweight market data fetching and normalization only.
- No AI decision logic, no technical indicator calculations, no JSON state writes,
  no exchange trading, no Telegram message creation.
- Allowed project imports: constants.py, utils.py, models.py only.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from constants import (
    CONTEXT_SYMBOLS,
    LEVEL_4_SYMBOLS,
    MARKET_CONTEXT_REFRESH_SECONDS,
    OKX_BASE_URL,
    OKX_CANDLE_LIMIT_DEFAULT,
    OKX_TIMEOUT_SECONDS,
    PRIMARY_TIMEFRAME,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    SYSTEM_VERSION,
)
from models import Candle, MarketDataResult, MarketSnapshot
from utils import normalize_symbol, safe_float, safe_int, safe_str, to_okx_inst_id, utc_now_iso


MARKET_DATA_VERSION: str = SYSTEM_VERSION


# =============================================================================
# OKX timeframe mapping
# =============================================================================

OKX_TIMEFRAME_MAP: dict[str, str] = {
    "1M": "1m",
    "3M": "3m",
    "5M": "5m",
    "15M": "15m",
    "30M": "30m",
    "1H": "1H",
    "2H": "2H",
    "4H": "4H",
    "6H": "6H",
    "12H": "12H",
    "1D": "1D",
    "1W": "1W",
}


# =============================================================================
# Small in-memory cache
# =============================================================================

_MARKET_CACHE: dict[str, tuple[float, Any]] = {}


def _cache_key(*parts: Any) -> str:
    return ":".join(safe_str(part) for part in parts)


def get_cached(key: str, ttl_seconds: int) -> Any:
    """Return cached value if still fresh."""
    item = _MARKET_CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts <= ttl_seconds:
        return value
    _MARKET_CACHE.pop(key, None)
    return None


def set_cached(key: str, value: Any) -> Any:
    """Set in-memory cache and return value."""
    _MARKET_CACHE[key] = (time.time(), value)
    return value


def clear_market_cache() -> None:
    """Clear in-memory market cache."""
    _MARKET_CACHE.clear()


# =============================================================================
# HTTP / OKX helpers
# =============================================================================

def normalize_timeframe(timeframe: Any) -> str:
    """Normalize bot timeframe labels to contract labels."""
    tf = safe_str(timeframe, PRIMARY_TIMEFRAME)
    if not tf:
        return PRIMARY_TIMEFRAME
    # Keep 15m lowercase style if passed exactly.
    upper = tf.upper()
    return OKX_TIMEFRAME_MAP.get(upper, tf)


def okx_timeframe(timeframe: Any) -> str:
    """Return OKX bar name."""
    tf = normalize_timeframe(timeframe)
    return OKX_TIMEFRAME_MAP.get(tf.upper(), tf)


def build_okx_url(path: str, params: Optional[dict[str, Any]] = None) -> str:
    """Build OKX REST URL."""
    base = safe_str(OKX_BASE_URL).rstrip("/")
    clean_path = "/" + safe_str(path).lstrip("/")
    query = ""
    if params:
        clean_params = {k: v for k, v in params.items() if v is not None and safe_str(v) != ""}
        if clean_params:
            query = "?" + urlencode(clean_params)
    return f"{base}{clean_path}{query}"


def http_get_json(url: str, *, timeout: int = OKX_TIMEOUT_SECONDS) -> dict[str, Any]:
    """
    Lightweight stdlib JSON GET.

    Raises URLError/HTTPError/ValueError on network or JSON failure.
    """
    request = Request(
        url,
        headers={
            "User-Agent": "crypto-ai-helper-level4/1.0",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("response_json_is_not_object")
    return data


def okx_get(path: str, params: Optional[dict[str, Any]] = None, *, timeout: int = OKX_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Call OKX public API and return parsed JSON."""
    return http_get_json(build_okx_url(path, params), timeout=timeout)


def okx_success(data: dict[str, Any]) -> bool:
    """Return True if OKX response code is success."""
    return safe_str(data.get("code")) in {"0", ""} and isinstance(data.get("data"), list)


# =============================================================================
# Candle parsing
# =============================================================================

def parse_okx_candle(raw: list[Any], timeframe: str = "") -> Candle:
    """
    Parse OKX candle row.

    OKX candles format:
    [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    """
    if not isinstance(raw, list):
        raw = []

    return Candle(
        timestamp=safe_int(raw[0] if len(raw) > 0 else 0, 0) or 0,
        open=safe_float(raw[1] if len(raw) > 1 else 0.0, 0.0) or 0.0,
        high=safe_float(raw[2] if len(raw) > 2 else 0.0, 0.0) or 0.0,
        low=safe_float(raw[3] if len(raw) > 3 else 0.0, 0.0) or 0.0,
        close=safe_float(raw[4] if len(raw) > 4 else 0.0, 0.0) or 0.0,
        volume=safe_float(raw[5] if len(raw) > 5 else 0.0, 0.0) or 0.0,
        timeframe=timeframe,
    )


def parse_okx_candles(rows: Any, timeframe: str = "") -> list[Candle]:
    """Parse OKX candle rows, returned oldest -> newest."""
    if not isinstance(rows, list):
        return []

    candles = [parse_okx_candle(row, timeframe=timeframe) for row in rows if isinstance(row, list)]
    # OKX usually returns newest first. Sort by timestamp for indicator engines.
    candles.sort(key=lambda c: c.timestamp)
    return candles


def candles_current_price(candles: list[Candle]) -> float:
    """Return latest close price from candle list."""
    if not candles:
        return 0.0
    return safe_float(candles[-1].close, 0.0) or 0.0


def validate_candles(candles: list[Candle], *, min_count: int = 50) -> tuple[bool, str]:
    """Validate enough candles and sane OHLC values."""
    if len(candles) < min_count:
        return False, "not_enough_candles"

    for candle in candles[-min(10, len(candles)):]:
        if candle.close <= 0 or candle.high <= 0 or candle.low <= 0:
            return False, "invalid_ohlc"
        if candle.high < candle.low:
            return False, "high_below_low"

    return True, ""


# =============================================================================
# Public fetch functions
# =============================================================================

def fetch_candles(
    symbol: str,
    timeframe: str = PRIMARY_TIMEFRAME,
    *,
    limit: int = OKX_CANDLE_LIMIT_DEFAULT,
    use_cache: bool = True,
    min_count: int = 50,
) -> MarketDataResult:
    """
    Fetch candles from OKX and return MarketDataResult.

    This is network-capable but still lightweight. It does not calculate indicators.
    """
    normalized_symbol = normalize_symbol(symbol)
    tf = normalize_timeframe(timeframe)
    inst_id = to_okx_inst_id(normalized_symbol)
    candle_limit = max(1, min(300, safe_int(limit, OKX_CANDLE_LIMIT_DEFAULT) or OKX_CANDLE_LIMIT_DEFAULT))

    key = _cache_key("candles", normalized_symbol, tf, candle_limit)
    if use_cache:
        cached = get_cached(key, ttl_seconds=30)
        if isinstance(cached, MarketDataResult):
            return cached

    try:
        response = okx_get(
            "/api/v5/market/candles",
            {
                "instId": inst_id,
                "bar": okx_timeframe(tf),
                "limit": candle_limit,
            },
        )

        if not okx_success(response):
            error = safe_str(response.get("msg"), "okx_response_failed")
            return MarketDataResult(
                status=STATUS_FAILED,
                symbol=normalized_symbol,
                timeframe=tf,
                message="okx_candle_fetch_failed",
                error=error,
                raw=response,
            )

        candles = parse_okx_candles(response.get("data"), timeframe=tf)
        valid, reason = validate_candles(candles, min_count=min_count)

        snapshot = MarketSnapshot(
            symbol=normalized_symbol,
            timeframe=tf,
            candles=candles,
            current_price=candles_current_price(candles),
            ok=valid,
            source="OKX",
            error=reason,
        )

        result = MarketDataResult(
            status=STATUS_OK if valid else STATUS_FAILED,
            symbol=normalized_symbol,
            timeframe=tf,
            snapshot=snapshot,
            message="candles_fetched" if valid else reason,
            error="" if valid else reason,
            raw={"inst_id": inst_id, "count": len(candles)},
        )

        return set_cached(key, result) if use_cache else result

    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        return MarketDataResult(
            status=STATUS_UNAVAILABLE,
            symbol=normalized_symbol,
            timeframe=tf,
            message="market_data_unavailable",
            error=str(exc),
            raw={"inst_id": inst_id},
        )


def fetch_ticker(symbol: str, *, use_cache: bool = True) -> MarketDataResult:
    """Fetch OKX ticker for one symbol."""
    normalized_symbol = normalize_symbol(symbol)
    inst_id = to_okx_inst_id(normalized_symbol)
    key = _cache_key("ticker", normalized_symbol)

    if use_cache:
        cached = get_cached(key, ttl_seconds=10)
        if isinstance(cached, MarketDataResult):
            return cached

    try:
        response = okx_get("/api/v5/market/ticker", {"instId": inst_id})

        if not okx_success(response) or not response.get("data"):
            error = safe_str(response.get("msg"), "okx_ticker_fetch_failed")
            return MarketDataResult(
                status=STATUS_FAILED,
                symbol=normalized_symbol,
                timeframe="TICKER",
                message="ticker_fetch_failed",
                error=error,
                raw=response,
            )

        row = response["data"][0]
        price = safe_float(row.get("last"), 0.0) or 0.0

        snapshot = MarketSnapshot(
            symbol=normalized_symbol,
            timeframe="TICKER",
            candles=[],
            current_price=price,
            ok=price > 0,
            source="OKX",
            error="" if price > 0 else "invalid_ticker_price",
        )

        result = MarketDataResult(
            status=STATUS_OK if price > 0 else STATUS_FAILED,
            symbol=normalized_symbol,
            timeframe="TICKER",
            snapshot=snapshot,
            message="ticker_fetched" if price > 0 else "invalid_ticker_price",
            error="" if price > 0 else "invalid_ticker_price",
            raw=row,
        )

        return set_cached(key, result) if use_cache else result

    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        return MarketDataResult(
            status=STATUS_UNAVAILABLE,
            symbol=normalized_symbol,
            timeframe="TICKER",
            message="market_data_unavailable",
            error=str(exc),
            raw={"inst_id": inst_id},
        )


def fetch_market_snapshot(symbol: str, timeframe: str = PRIMARY_TIMEFRAME, *, limit: int = OKX_CANDLE_LIMIT_DEFAULT) -> MarketDataResult:
    """Alias for fetch_candles used by later modules."""
    return fetch_candles(symbol, timeframe=timeframe, limit=limit)


def fetch_many_snapshots(
    symbols: list[str] | tuple[str, ...],
    timeframe: str = PRIMARY_TIMEFRAME,
    *,
    limit: int = OKX_CANDLE_LIMIT_DEFAULT,
) -> dict[str, MarketDataResult]:
    """Fetch snapshots for multiple symbols sequentially."""
    result: dict[str, MarketDataResult] = {}
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        result[normalized] = fetch_market_snapshot(normalized, timeframe=timeframe, limit=limit)
    return result


def fetch_level4_universe(timeframe: str = PRIMARY_TIMEFRAME, *, limit: int = OKX_CANDLE_LIMIT_DEFAULT) -> dict[str, MarketDataResult]:
    """Fetch Level 4 configured universe."""
    return fetch_many_snapshots(LEVEL_4_SYMBOLS, timeframe=timeframe, limit=limit)


def fetch_context_snapshots(timeframe: str = PRIMARY_TIMEFRAME, *, use_cache: bool = True) -> dict[str, MarketDataResult]:
    """Fetch BTC/ETH context snapshots with slightly longer cache."""
    key = _cache_key("context", timeframe)
    if use_cache:
        cached = get_cached(key, ttl_seconds=MARKET_CONTEXT_REFRESH_SECONDS)
        if isinstance(cached, dict):
            return cached

    result = fetch_many_snapshots(CONTEXT_SYMBOLS, timeframe=timeframe, limit=OKX_CANDLE_LIMIT_DEFAULT)
    return set_cached(key, result) if use_cache else result


# =============================================================================
# Preflight helpers
# =============================================================================

def market_data_light_ping(symbol: str = "BTCUSDT") -> dict[str, Any]:
    """
    Lightweight market-data ping for startup preflight.

    This only requests a ticker. No broad scan, no indicators, no AI.
    """
    result = fetch_ticker(symbol, use_cache=False)
    return {
        "status": result.status,
        "ok": result.status == STATUS_OK,
        "symbol": normalize_symbol(symbol),
        "message": result.message,
        "error": result.error,
        "checked_at": utc_now_iso(),
    }


def make_offline_snapshot(symbol: str, timeframe: str, candles: list[Candle]) -> MarketSnapshot:
    """
    Build a MarketSnapshot from already available candles.

    Useful for tests, backfills, and later learning without network.
    """
    normalized = normalize_symbol(symbol)
    tf = normalize_timeframe(timeframe)
    valid, reason = validate_candles(candles, min_count=1)
    return MarketSnapshot(
        symbol=normalized,
        timeframe=tf,
        candles=sorted(candles, key=lambda c: c.timestamp),
        current_price=candles_current_price(sorted(candles, key=lambda c: c.timestamp)),
        ok=valid,
        source="OFFLINE",
        error=reason,
    )


__all__ = [
    "MARKET_DATA_VERSION",
    "OKX_TIMEFRAME_MAP",
    "normalize_timeframe",
    "okx_timeframe",
    "build_okx_url",
    "http_get_json",
    "okx_get",
    "okx_success",
    "parse_okx_candle",
    "parse_okx_candles",
    "candles_current_price",
    "validate_candles",
    "fetch_candles",
    "fetch_ticker",
    "fetch_market_snapshot",
    "fetch_many_snapshots",
    "fetch_level4_universe",
    "fetch_context_snapshots",
    "market_data_light_ping",
    "make_offline_snapshot",
    "get_cached",
    "set_cached",
    "clear_market_cache",
]
