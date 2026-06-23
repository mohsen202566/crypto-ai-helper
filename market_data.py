from __future__ import annotations

"""
04 - market_data.py

Simplified raw market data layer for the Level 1 / 5M crypto futures bot.

Locked goals:
- Only public OKX data.
- Only the configured 10 Level-1 symbols for scanning.
- 5m candles are the default.
- Technical indicators are NOT calculated here.
- No AI decision, no REAL/GHOST/REJECT, no Telegram, no Toobit private calls.
- Fast lightweight cache for near-real-time monitoring.
- Lightweight OKX market mode for bullish / bearish / neutral / choppy context.
"""

import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from config import (
    SETTINGS,
    LEVEL1_WATCHLIST,
    MARKET_MODE_SYMBOLS,
    normalize_symbol,
)


JsonDict = Dict[str, Any]


_RUNTIME_CACHE: Dict[str, Tuple[float, Any]] = {}
DEFAULT_CACHE_TTL_SECONDS = 3.0


def _cache_get(key: str, ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS) -> Any:
    item = _RUNTIME_CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > ttl_seconds:
        _RUNTIME_CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> Any:
    _RUNTIME_CACHE[key] = (time.time(), value)
    if len(_RUNTIME_CACHE) > 300:
        old_keys = sorted(_RUNTIME_CACHE, key=lambda k: _RUNTIME_CACHE[k][0])[:80]
        for k in old_keys:
            _RUNTIME_CACHE.pop(k, None)
    return value


class MarketDataError(RuntimeError):
    """Raised for market data layer errors."""


def now_ms() -> int:
    return int(time.time() * 1000)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def okx_swap_symbol(symbol: str) -> str:
    internal = normalize_symbol(symbol)
    if not internal:
        raise MarketDataError(f"unsupported_symbol:{symbol}")
    base = internal[:-4] if internal.endswith("USDT") else internal
    return f"{base}-USDT-SWAP"


def _okx_bar(interval: str) -> str:
    tf = str(interval or "5m").strip()
    mapping = {
        "1m": "1m",
        "3m": "3m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "1H",
        "4h": "4H",
        "1d": "1D",
        "1H": "1H",
        "4H": "4H",
        "1D": "1D",
    }
    return mapping.get(tf, "5m")


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0
    confirm: bool = True

    @property
    def body(self) -> float:
        return self.close - self.open

    @property
    def range(self) -> float:
        return max(0.0, self.high - self.low)

    @property
    def change_percent(self) -> float:
        if self.open <= 0:
            return 0.0
        return (self.close - self.open) / self.open * 100.0

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class Ticker:
    symbol: str
    exchange_symbol: str
    price: float
    bid: float = 0.0
    ask: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    volume_24h: float = 0.0
    timestamp: int = 0
    raw: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    exchange_symbol: str
    interval: str
    candles: List[Candle]
    ticker: Optional[Ticker] = None
    timestamp: int = 0
    source: str = "OKX"

    @property
    def price(self) -> float:
        if self.ticker and self.ticker.price > 0:
            return self.ticker.price
        if self.candles:
            return self.candles[-1].close
        return 0.0

    @property
    def volume(self) -> float:
        return self.candles[-1].volume if self.candles else 0.0

    def to_dict(self) -> JsonDict:
        return {
            "symbol": self.symbol,
            "exchange_symbol": self.exchange_symbol,
            "interval": self.interval,
            "candles": [c.to_dict() for c in self.candles],
            "ticker": self.ticker.to_dict() if self.ticker else None,
            "timestamp": self.timestamp,
            "source": self.source,
        }


@dataclass(frozen=True)
class MarketMode:
    mode: str
    strength: float
    bullish_count: int
    bearish_count: int
    neutral_count: int
    choppy_count: int
    leader_details: Dict[str, JsonDict]
    timestamp: int
    source: str = "OKX"

    def to_dict(self) -> JsonDict:
        return asdict(self)


class OKXPublicClient:
    """Small OKX public client. No private trading endpoints."""

    def __init__(self, base_url: Optional[str] = None, timeout: Optional[int] = None):
        self.base_url = (base_url or SETTINGS.market_data.okx_base_url).rstrip("/")
        self.timeout = int(timeout or SETTINGS.market_data.request_timeout_seconds)
        self.session = requests.Session()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> JsonDict:
        url = self.base_url + path
        try:
            response = self.session.get(url, params=params or {}, timeout=self.timeout)
            try:
                payload = response.json()
            except Exception:
                payload = {"raw": response.text}

            if response.status_code >= 400:
                return {"ok": False, "status_code": response.status_code, "error": payload}

            if isinstance(payload, dict) and str(payload.get("code", "0")) not in {"0", ""}:
                return {"ok": False, "status_code": response.status_code, "error": payload}

            return {"ok": True, "data": payload, "status_code": response.status_code}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "path": path, "params": params or {}}

    def candles(self, symbol: str, interval: str = "5m", limit: Optional[int] = None) -> List[Candle]:
        internal = normalize_symbol(symbol)
        if not internal:
            raise MarketDataError(f"unsupported_symbol:{symbol}")

        limit_value = int(limit or SETTINGS.market_data.candle_limit)
        limit_value = max(1, min(limit_value, 300))
        inst_id = okx_swap_symbol(internal)

        cache_key = f"candles:{inst_id}:{interval}:{limit_value}"
        cached = _cache_get(cache_key, ttl_seconds=DEFAULT_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached

        res = self._get(
            "/api/v5/market/candles",
            params={"instId": inst_id, "bar": _okx_bar(interval), "limit": str(limit_value)},
        )
        if not res.get("ok"):
            raise MarketDataError(f"okx_candles_failed:{internal}:{res}")

        payload = res.get("data", {})
        rows = payload.get("data", []) if isinstance(payload, dict) else []

        candles: List[Candle] = []
        for row in rows or []:
            if not isinstance(row, list) or len(row) < 6:
                continue
            candles.append(
                Candle(
                    timestamp=safe_int(row[0]),
                    open=safe_float(row[1]),
                    high=safe_float(row[2]),
                    low=safe_float(row[3]),
                    close=safe_float(row[4]),
                    volume=safe_float(row[5]),
                    quote_volume=safe_float(row[7] if len(row) > 7 else 0.0),
                    confirm=str(row[8]) == "1" if len(row) > 8 else True,
                )
            )

        candles = [c for c in candles if c.timestamp > 0 and c.close > 0]
        candles.sort(key=lambda c: c.timestamp)
        return _cache_set(cache_key, candles[-limit_value:])

    def ticker(self, symbol: str) -> Optional[Ticker]:
        internal = normalize_symbol(symbol)
        if not internal:
            return None

        inst_id = okx_swap_symbol(internal)
        cache_key = f"ticker:{inst_id}"
        cached = _cache_get(cache_key, ttl_seconds=2.0)
        if cached is not None:
            return cached

        res = self._get("/api/v5/market/ticker", params={"instId": inst_id})
        if not res.get("ok"):
            return None

        payload = res.get("data", {})
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not rows or not isinstance(rows[0], dict):
            return None

        row = rows[0]
        ticker = Ticker(
            symbol=internal,
            exchange_symbol=inst_id,
            price=safe_float(row.get("last")),
            bid=safe_float(row.get("bidPx")),
            ask=safe_float(row.get("askPx")),
            high_24h=safe_float(row.get("high24h")),
            low_24h=safe_float(row.get("low24h")),
            volume_24h=safe_float(row.get("vol24h")),
            timestamp=safe_int(row.get("ts"), now_ms()),
            raw=row,
        )
        return _cache_set(cache_key, ticker)


class MarketDataProvider:
    """Raw data provider for 10-coin Level 1 monitoring."""

    def __init__(self, client: Optional[OKXPublicClient] = None):
        self.client = client or OKXPublicClient()

    def get_candles(self, symbol: str, interval: str = "5m", limit: Optional[int] = None) -> List[Candle]:
        internal = normalize_symbol(symbol)
        if not internal:
            raise MarketDataError(f"unsupported_symbol:{symbol}")
        return self.client.candles(internal, interval=interval, limit=limit)

    def get_ticker(self, symbol: str) -> Optional[Ticker]:
        internal = normalize_symbol(symbol)
        if not internal:
            return None
        return self.client.ticker(internal)

    def get_snapshot(self, symbol: str, interval: str = "5m", limit: Optional[int] = None) -> MarketSnapshot:
        internal = normalize_symbol(symbol)
        if not internal:
            raise MarketDataError(f"unsupported_symbol:{symbol}")

        candles = self.get_candles(internal, interval=interval, limit=limit)
        ticker = self.get_ticker(internal)

        return MarketSnapshot(
            symbol=internal,
            exchange_symbol=okx_swap_symbol(internal),
            interval=interval,
            candles=candles,
            ticker=ticker,
            timestamp=now_ms(),
            source="OKX",
        )

    def scan_raw_snapshots(
        self,
        symbols: Optional[Iterable[str]] = None,
        interval: str = "5m",
        limit: Optional[int] = None,
    ) -> Dict[str, MarketSnapshot]:
        result: Dict[str, MarketSnapshot] = {}
        selected = list(symbols or SETTINGS.market_data.scan_symbols or LEVEL1_WATCHLIST)

        for raw in selected:
            internal = normalize_symbol(raw)
            if not internal or internal not in LEVEL1_WATCHLIST:
                continue
            try:
                result[internal] = self.get_snapshot(internal, interval=interval, limit=limit)
            except Exception:
                continue

        return result

    def get_market_mode(self) -> MarketMode:
        """Fast lightweight market mode from OKX leaders."""
        cache_key = "market_mode"
        cached = _cache_get(cache_key, ttl_seconds=float(SETTINGS.market_context.cache_ttl_seconds))
        if cached is not None:
            return cached

        bullish = 0
        bearish = 0
        neutral = 0
        choppy = 0
        details: Dict[str, JsonDict] = {}

        for symbol in MARKET_MODE_SYMBOLS:
            try:
                candles = self.client.candles(symbol, interval="5m", limit=24)
                if len(candles) < 8:
                    neutral += 1
                    details[symbol] = {"state": "UNKNOWN"}
                    continue

                last = candles[-1].close
                c3 = candles[-4].close
                c12 = candles[-13].close if len(candles) >= 13 else candles[0].close
                change_15m = (last - c3) / c3 * 100.0 if c3 > 0 else 0.0
                change_60m = (last - c12) / c12 * 100.0 if c12 > 0 else 0.0

                recent_ranges = [c.range / c.close * 100.0 for c in candles[-8:] if c.close > 0]
                avg_range = sum(recent_ranges) / len(recent_ranges) if recent_ranges else 0.0

                if abs(change_60m) < 0.18 and avg_range < 0.28:
                    state = "CHOPPY"
                    choppy += 1
                elif change_15m > 0.10 and change_60m > 0.20:
                    state = "BULLISH"
                    bullish += 1
                elif change_15m < -0.10 and change_60m < -0.20:
                    state = "BEARISH"
                    bearish += 1
                else:
                    state = "NEUTRAL"
                    neutral += 1

                details[symbol] = {
                    "state": state,
                    "change_15m": round(change_15m, 4),
                    "change_60m": round(change_60m, 4),
                    "avg_range": round(avg_range, 4),
                }
            except Exception:
                neutral += 1
                details[symbol] = {"state": "UNKNOWN"}

        total = max(1, bullish + bearish + neutral + choppy)
        if bullish >= 2 and bullish > bearish:
            mode = "BULLISH"
            strength = bullish / total * 100.0
        elif bearish >= 2 and bearish > bullish:
            mode = "BEARISH"
            strength = bearish / total * 100.0
        elif choppy >= 2:
            mode = "CHOPPY"
            strength = choppy / total * 100.0
        else:
            mode = "NEUTRAL"
            strength = neutral / total * 100.0

        market_mode = MarketMode(
            mode=mode,
            strength=round(strength, 2),
            bullish_count=bullish,
            bearish_count=bearish,
            neutral_count=neutral,
            choppy_count=choppy,
            leader_details=details,
            timestamp=now_ms(),
            source="OKX",
        )
        return _cache_set(cache_key, market_mode)

    def health_check(self) -> Dict[str, Any]:
        test_symbol = LEVEL1_WATCHLIST[0]
        try:
            candles = self.get_candles(test_symbol, interval="5m", limit=5)
            return {
                "ok": bool(candles),
                "source": "OKX",
                "symbol": test_symbol,
                "candles": len(candles),
                "last_price": candles[-1].close if candles else 0.0,
                "watchlist_size": len(LEVEL1_WATCHLIST),
            }
        except Exception as exc:
            return {"ok": False, "source": "OKX", "symbol": test_symbol, "error": str(exc)}


_default_provider: Optional[MarketDataProvider] = None


def provider() -> MarketDataProvider:
    global _default_provider
    if _default_provider is None:
        _default_provider = MarketDataProvider()
    return _default_provider


def get_candles(symbol: str, interval: str = "5m", limit: Optional[int] = None) -> List[Dict[str, Any]]:
    return [c.to_dict() for c in provider().get_candles(symbol, interval=interval, limit=limit)]


def get_latest_price(symbol: str) -> float:
    ticker = provider().get_ticker(symbol)
    if ticker and ticker.price > 0:
        return ticker.price
    candles = provider().get_candles(symbol, interval="5m", limit=1)
    return candles[-1].close if candles else 0.0


def get_market_snapshot(symbol: str, interval: str = "5m", limit: Optional[int] = None) -> MarketSnapshot:
    return provider().get_snapshot(symbol, interval=interval, limit=limit)


def scan_raw_market(
    symbols: Optional[Iterable[str]] = None,
    interval: str = "5m",
    limit: Optional[int] = None,
) -> Dict[str, MarketSnapshot]:
    return provider().scan_raw_snapshots(symbols=symbols, interval=interval, limit=limit)


def get_market_mode() -> Dict[str, Any]:
    return provider().get_market_mode().to_dict()


def market_data_health_check() -> Dict[str, Any]:
    return provider().health_check()
