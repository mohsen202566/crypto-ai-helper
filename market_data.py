from __future__ import annotations

"""
04 - market_data.py

Production-ready raw market data layer for the locked Movement Hunter bot.

Responsibilities:
- Fetch raw public market data only.
- Primary candle source: OKX public API.
- Provide ticker/candles/volume/funding/open-interest snapshots where available.
- Normalize symbols only through symbol_mapper.py.
- Return clean, sorted, analysis-ready raw structures.

Strictly forbidden in this file:
- No AI decision logic.
- No REAL/GHOST/REJECT.
- No Toobit private trading.
- No Telegram handlers.
- No persistence.
- No Paper mode.
- No Setup flow.

Architecture lock:
- analysis_layers.py consumes this file.
- market_data.py must not calculate signals.
- all symbol conversion must go through symbol_mapper.py.
"""

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests

from config import SETTINGS
from symbol_mapper import okx_symbol, normalize_symbol, symbol_info


JsonDict = Dict[str, Any]

# Lightweight runtime TTL cache to reduce API pressure during fast scans.
_RUNTIME_CACHE: Dict[str, Tuple[float, Any]] = {}
DEFAULT_CACHE_TTL_SECONDS = 15.0


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
    if len(_RUNTIME_CACHE) > 500:
        # Cheap pruning by insertion age.
        old_keys = sorted(_RUNTIME_CACHE, key=lambda k: _RUNTIME_CACHE[k][0])[:100]
        for k in old_keys:
            _RUNTIME_CACHE.pop(k, None)
    return value




class MarketDataError(RuntimeError):
    """Raised for market data layer errors."""


def now_ms() -> int:
    return int(time.time() * 1000)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _okx_bar(interval: str) -> str:
    tf = str(interval or "5m").strip()
    mapping = {
        "1m": "1m",
        "3m": "3m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "1H",
        "2h": "2H",
        "4h": "4H",
        "6h": "6H",
        "12h": "12H",
        "1d": "1D",
        "1H": "1H",
        "2H": "2H",
        "4H": "4H",
        "6H": "6H",
        "12H": "12H",
        "1D": "1D",
    }
    return mapping.get(tf, tf)


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

    def to_dict(self) -> JsonDict:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "quote_volume": self.quote_volume,
            "confirm": self.confirm,
        }


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
        return {
            "symbol": self.symbol,
            "exchange_symbol": self.exchange_symbol,
            "price": self.price,
            "bid": self.bid,
            "ask": self.ask,
            "high_24h": self.high_24h,
            "low_24h": self.low_24h,
            "volume_24h": self.volume_24h,
            "timestamp": self.timestamp,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    exchange_symbol: str
    interval: str
    candles: List[Candle]
    ticker: Optional[Ticker] = None
    funding_rate: float = 0.0
    open_interest: float = 0.0
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
        if self.candles:
            return self.candles[-1].volume
        return 0.0

    def to_dict(self) -> JsonDict:
        return {
            "symbol": self.symbol,
            "exchange_symbol": self.exchange_symbol,
            "interval": self.interval,
            "candles": [c.to_dict() for c in self.candles],
            "ticker": self.ticker.to_dict() if self.ticker else None,
            "funding_rate": self.funding_rate,
            "open_interest": self.open_interest,
            "timestamp": self.timestamp,
            "source": self.source,
        }


@dataclass(frozen=True)
class MultiTimeframeSnapshot:
    symbol: str
    exchange_symbol: str
    snapshots: Dict[str, MarketSnapshot]
    timestamp: int
    source: str = "OKX"

    def to_dict(self) -> JsonDict:
        return {
            "symbol": self.symbol,
            "exchange_symbol": self.exchange_symbol,
            "snapshots": {tf: snap.to_dict() for tf, snap in self.snapshots.items()},
            "timestamp": self.timestamp,
            "source": self.source,
        }


class OKXPublicClient:
    """
    Lightweight OKX public market data client.

    This client only touches public endpoints and never signs/private-trades.
    """

    def __init__(self, base_url: Optional[str] = None, timeout: Optional[int] = None):
        self.base_url = (base_url or SETTINGS.market_data.okx_base_url).rstrip("/")
        self.timeout = int(timeout or SETTINGS.market_data.request_timeout_seconds)
        self.session = requests.Session()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> JsonDict:
        url = self.base_url + path
        try:
            response = self.session.get(url, params=params or {}, timeout=self.timeout)
            try:
                data = response.json()
            except Exception:
                data = {"raw": response.text}

            if response.status_code >= 400:
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "error": data,
                    "path": path,
                    "params": params or {},
                }

            if isinstance(data, dict) and str(data.get("code", "0")) not in {"0", ""}:
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "error": data,
                    "path": path,
                    "params": params or {},
                }

            return {"ok": True, "data": data, "status_code": response.status_code}
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "path": path,
                "params": params or {},
            }

    def candles(self, symbol: str, interval: str = "5m", limit: int = 200) -> List[Candle]:
        inst_id = okx_symbol(symbol)
        limit = max(1, min(int(limit or SETTINGS.market_data.candle_limit), 300))
        params = {"instId": inst_id, "bar": _okx_bar(interval), "limit": str(limit)}
        res = self._get("/api/v5/market/candles", params=params)
        if not res.get("ok"):
            raise MarketDataError(f"okx_candles_failed:{symbol}:{res}")

        rows = []
        data = res.get("data", {})
        if isinstance(data, dict):
            rows = data.get("data", [])
        elif isinstance(data, list):
            rows = data

        candles: List[Candle] = []
        for row in rows or []:
            if not isinstance(row, list) or len(row) < 6:
                continue
            # OKX candle row:
            # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
            candles.append(
                Candle(
                    timestamp=_safe_int(row[0]),
                    open=_safe_float(row[1]),
                    high=_safe_float(row[2]),
                    low=_safe_float(row[3]),
                    close=_safe_float(row[4]),
                    volume=_safe_float(row[5]),
                    quote_volume=_safe_float(row[7] if len(row) > 7 else 0.0),
                    confirm=str(row[8]) == "1" if len(row) > 8 else True,
                )
            )

        candles = [c for c in candles if c.timestamp > 0 and c.close > 0]
        candles.sort(key=lambda c: c.timestamp)
        return candles[-limit:]

    def ticker(self, symbol: str) -> Optional[Ticker]:
        internal = normalize_symbol(symbol)
        inst_id = okx_symbol(symbol)
        res = self._get("/api/v5/market/ticker", params={"instId": inst_id})
        if not res.get("ok"):
            return None

        payload = res.get("data", {})
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not rows:
            return None

        row = rows[0]
        if not isinstance(row, dict):
            return None

        return Ticker(
            symbol=internal,
            exchange_symbol=inst_id,
            price=_safe_float(row.get("last")),
            bid=_safe_float(row.get("bidPx")),
            ask=_safe_float(row.get("askPx")),
            high_24h=_safe_float(row.get("high24h")),
            low_24h=_safe_float(row.get("low24h")),
            volume_24h=_safe_float(row.get("vol24h")),
            timestamp=_safe_int(row.get("ts"), now_ms()),
            raw=row,
        )

    def funding_rate(self, symbol: str) -> float:
        inst_id = okx_symbol(symbol)
        res = self._get("/api/v5/public/funding-rate", params={"instId": inst_id})
        if not res.get("ok"):
            return 0.0
        payload = res.get("data", {})
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not rows or not isinstance(rows[0], dict):
            return 0.0
        return _safe_float(rows[0].get("fundingRate"))

    def open_interest(self, symbol: str) -> float:
        inst_id = okx_symbol(symbol)
        res = self._get("/api/v5/public/open-interest", params={"instType": "SWAP", "instId": inst_id})
        if not res.get("ok"):
            return 0.0
        payload = res.get("data", {})
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not rows or not isinstance(rows[0], dict):
            return 0.0
        return _safe_float(rows[0].get("oi"))


class MarketDataProvider:
    """
    Public raw market data provider.

    analysis_layers.py should consume this provider and compute indicators there.
    """

    def __init__(self, client: Optional[OKXPublicClient] = None):
        self.client = client or OKXPublicClient()

    def get_candles(self, symbol: str, interval: str = "5m", limit: Optional[int] = None) -> List[Candle]:
        internal = normalize_symbol(symbol)
        limit = limit or SETTINGS.market_data.candle_limit
        return self.client.candles(internal, interval=interval, limit=limit)

    def get_ticker(self, symbol: str) -> Optional[Ticker]:
        return self.client.ticker(symbol)

    def get_snapshot(self, symbol: str, interval: str = "5m", limit: Optional[int] = None, include_optional: bool = True) -> MarketSnapshot:
        info = symbol_info(symbol)
        candles = self.get_candles(info.internal, interval=interval, limit=limit)
        ticker = self.get_ticker(info.internal)

        funding = 0.0
        oi = 0.0
        if include_optional:
            funding = self.client.funding_rate(info.internal)
            oi = self.client.open_interest(info.internal)

        return MarketSnapshot(
            symbol=info.internal,
            exchange_symbol=info.okx,
            interval=interval,
            candles=candles,
            ticker=ticker,
            funding_rate=funding,
            open_interest=oi,
            timestamp=now_ms(),
            source="OKX",
        )

    def get_multi_timeframe_snapshot(self, symbol: str, timeframes: Optional[Sequence[str]] = None, limit: Optional[int] = None, include_optional: bool = True) -> MultiTimeframeSnapshot:
        info = symbol_info(symbol)
        tfs = list(timeframes or SETTINGS.market_data.default_timeframes)
        snapshots: Dict[str, MarketSnapshot] = {}

        for tf in tfs:
            snapshots[tf] = self.get_snapshot(
                info.internal,
                interval=tf,
                limit=limit,
                include_optional=include_optional if tf == tfs[0] else False,
            )

        return MultiTimeframeSnapshot(
            symbol=info.internal,
            exchange_symbol=info.okx,
            snapshots=snapshots,
            timestamp=now_ms(),
            source="OKX",
        )

    def scan_raw_snapshots(self, symbols: Optional[Iterable[str]] = None, interval: str = "5m", limit: Optional[int] = None) -> Dict[str, MarketSnapshot]:
        result: Dict[str, MarketSnapshot] = {}
        for raw in symbols or SETTINGS.market_data.scan_symbols:
            try:
                snap = self.get_snapshot(raw, interval=interval, limit=limit, include_optional=False)
                result[snap.symbol] = snap
            except Exception:
                # Raw data layer should not crash the scanner because one symbol failed.
                continue
        return result

    def health_check(self) -> Dict[str, Any]:
        test_symbol = SETTINGS.market_data.scan_symbols[0] if SETTINGS.market_data.scan_symbols else "BTCUSDT"
        try:
            candles = self.get_candles(test_symbol, interval="5m", limit=5)
            return {
                "ok": bool(candles),
                "source": "OKX",
                "symbol": normalize_symbol(test_symbol),
                "candles": len(candles),
                "last_price": candles[-1].close if candles else 0.0,
            }
        except Exception as exc:
            return {
                "ok": False,
                "source": "OKX",
                "symbol": test_symbol,
                "error": str(exc),
            }


_default_provider: Optional[MarketDataProvider] = None


def provider() -> MarketDataProvider:
    global _default_provider
    if _default_provider is None:
        _default_provider = MarketDataProvider()
    return _default_provider


def get_candles(symbol: str, interval: str = "5m", limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Backward-compatible dict candle output.

    New code may prefer provider().get_candles() for Candle objects.
    """
    return [c.to_dict() for c in provider().get_candles(symbol, interval=interval, limit=limit)]


def get_latest_price(symbol: str) -> float:
    cache_key = f'latest_price:{locals()}'
    cached = _cache_get(cache_key, ttl_seconds=10.0)
    if cached is not None:
        return cached
    ticker = provider().get_ticker(symbol)
    if ticker and ticker.price > 0:
        return ticker.price
    candles = provider().get_candles(symbol, interval="5m", limit=1)
    return candles[-1].close if candles else 0.0


def get_market_snapshot(symbol: str, interval: str = "5m", limit: Optional[int] = None) -> MarketSnapshot:
    return provider().get_snapshot(symbol, interval=interval, limit=limit)


def get_multi_timeframe_snapshot(symbol: str, timeframes: Optional[Sequence[str]] = None, limit: Optional[int] = None) -> MultiTimeframeSnapshot:
    return provider().get_multi_timeframe_snapshot(symbol, timeframes=timeframes, limit=limit)


def scan_raw_market(symbols: Optional[Iterable[str]] = None, interval: str = "5m", limit: Optional[int] = None) -> Dict[str, MarketSnapshot]:
    return provider().scan_raw_snapshots(symbols=symbols, interval=interval, limit=limit)


def market_data_health_check() -> Dict[str, Any]:
    return provider().health_check()
