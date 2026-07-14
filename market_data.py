"""داده بازار: OKX منبع اصلی و Bybit fallback کامل، بدون مخلوط‌کردن چرخه."""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from typing import Any

import requests

import config
from models import Candle, DataSource, SymbolMapping
from utils import clamp, now_ms, safe_float, safe_int

logger = logging.getLogger("adaptive_bot")


class MarketDataError(RuntimeError):
    pass


_INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1H": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}
_BYBIT_INTERVAL = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1H": "60", "4H": "240", "1D": "D"}


class MarketDataClient:
    def __init__(self, session: requests.Session | None = None):
        # Signal scans run concurrently. requests.Session is not guaranteed to be
        # thread-safe, so production uses one Session per worker thread. Tests may inject
        # a single controlled Session explicitly.
        self._external_session = session
        self._session_local = threading.local()
        self._sessions_lock = threading.RLock()
        self._sessions: list[requests.Session] = []
        self._ticker_lock = threading.RLock()
        self._candle_cache_lock = threading.RLock()
        self._candle_cache: dict[tuple[str, str, str], list[Candle]] = {}
        self._candle_cache_updated: dict[tuple[str, str, str], int] = {}
        self._bundle_locks_lock = threading.RLock()
        self._bundle_locks: dict[tuple[str, str], threading.RLock] = {}
        self._tickers: dict[str, float] = {}
        self._ticker_updated_at = 0
        self._last_source = DataSource.OKX.value

    @property
    def session(self) -> requests.Session:
        if self._external_session is not None:
            return self._external_session
        current = getattr(self._session_local, "session", None)
        if current is None:
            current = requests.Session()
            self._session_local.session = current
            with self._sessions_lock:
                self._sessions.append(current)
        return current

    def close(self) -> None:
        seen: set[int] = set()
        sessions = ([self._external_session] if self._external_session is not None else [])
        with self._sessions_lock:
            sessions += list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            if session is None or id(session) in seen:
                continue
            seen.add(id(session))
            session.close()

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(config.HTTP_RETRIES + 1):
            try:
                res = self.session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
                res.raise_for_status()
                data = res.json()
                if not isinstance(data, dict):
                    raise MarketDataError("invalid response shape")
                return data
            except Exception as exc:
                last = exc
                if attempt < config.HTTP_RETRIES:
                    time.sleep(config.HTTP_BACKOFF_SECONDS * (attempt + 1))
        raise MarketDataError(str(last))

    @staticmethod
    def _dedupe_sort(candles: list[Candle], limit: int) -> list[Candle]:
        by_ts = {c.ts: c for c in candles}
        out = [by_ts[k] for k in sorted(by_ts)]
        return out[-limit:]

    def _bundle_lock(self, source: str, symbol: str) -> threading.RLock:
        key = (source, symbol.upper())
        with self._bundle_locks_lock:
            lock = self._bundle_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._bundle_locks[key] = lock
            return lock

    def _cached_candles(self, source: str, symbol: str, interval: str) -> list[Candle]:
        with self._candle_cache_lock:
            return list(self._candle_cache.get((source, symbol.upper(), interval), ()))

    def _cache_age_ms(self, source: str, symbol: str, interval: str) -> int:
        with self._candle_cache_lock:
            updated = int(self._candle_cache_updated.get((source, symbol.upper(), interval), 0))
        return max(0, now_ms() - updated) if updated else 2**63 - 1

    def _store_candles(
        self, source: str, symbol: str, interval: str, rows: list[Candle], limit: int
    ) -> list[Candle]:
        key = (source, symbol.upper(), interval)
        with self._candle_cache_lock:
            merged = self._dedupe_sort(list(self._candle_cache.get(key, ())) + list(rows), limit)
            self._candle_cache[key] = merged
            self._candle_cache_updated[key] = now_ms()
            return list(merged)

    def _analysis_source_bundle(
        self, source: str, symbol: str, fetcher: Any
    ) -> tuple[list[Candle], list[Candle]]:
        """Build one exchange-consistent bundle with one small request per later scan.

        The first use loads 1m/5m history. Afterwards only a short 1m tail is fetched;
        current 5m buckets are rebuilt from that same exchange's 1m candles and merged
        into the cached 5m history. This avoids roughly three full-history requests per
        symbol on every minute scan while preserving complete source isolation.
        """
        with self._bundle_lock(source, symbol):
            one = self._cached_candles(source, symbol, "1m")
            if len(one) < 240:
                one = fetcher(symbol, "1m", 240)
                one = self._store_candles(source, symbol, "1m", one, 240)
            elif self._cache_age_ms(source, symbol, "1m") >= config.ANALYSIS_CANDLE_CACHE_FRESH_SECONDS * 1000:
                recent_one = fetcher(symbol, "1m", 12)
                one = self._store_candles(source, symbol, "1m", recent_one, 240)

            five = self._cached_candles(source, symbol, "5m")
            if len(five) < 900:
                five = fetcher(symbol, "5m", 900)
                five = self._store_candles(source, symbol, "5m", five, 2160)
            # Always rebuild recent 5m buckets from this exact source's 1m tail.
            derived_five = self.resample(one[-120:], 5)
            five = self._store_candles(source, symbol, "5m", derived_five, 900)
            return one[-240:], five[-900:]

    def _okx_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        if interval not in _INTERVAL_MS:
            raise MarketDataError(f"unsupported interval {interval}")
        path = "/api/v5/market/history-candles" if limit > 300 else "/api/v5/market/candles"
        out: list[Candle] = []
        after: str | None = None
        while len(out) < limit:
            batch_limit = min(300, limit - len(out))
            params: dict[str, Any] = {"instId": symbol, "bar": interval, "limit": batch_limit}
            if after:
                params["after"] = after
            payload = self._get_json(f"{config.OKX_BASE_URL}{path}", params)
            if str(payload.get("code", "0")) != "0":
                raise MarketDataError(f"OKX {payload.get('code')}: {payload.get('msg')}")
            rows = payload.get("data") or []
            if not rows:
                break
            oldest = None
            for row in rows:
                if not isinstance(row, list) or len(row) < 6:
                    continue
                ts = safe_int(row[0])
                candle = Candle(
                    ts=ts,
                    open=safe_float(row[1]),
                    high=safe_float(row[2]),
                    low=safe_float(row[3]),
                    close=safe_float(row[4]),
                    volume=safe_float(row[5]),
                    turnover=safe_float(row[7] if len(row) > 7 else row[6] if len(row) > 6 else 0),
                    confirmed=str(row[8] if len(row) > 8 else "1") == "1",
                )
                if candle.close > 0 and candle.high >= candle.low > 0:
                    out.append(candle)
                    oldest = ts if oldest is None else min(oldest, ts)
            if oldest is None or len(rows) < batch_limit:
                break
            after = str(oldest)
            time.sleep(0.03)
        candles = self._dedupe_sort(out, limit)
        if len(candles) < min(limit, 60):
            raise MarketDataError(f"OKX insufficient candles {symbol} {interval}: {len(candles)}")
        return candles

    def _bybit_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        if interval not in _BYBIT_INTERVAL:
            raise MarketDataError(f"unsupported interval {interval}")
        out: list[Candle] = []
        end: int | None = None
        while len(out) < limit:
            batch_limit = min(1000, limit - len(out))
            params: dict[str, Any] = {
                "category": "linear",
                "symbol": symbol,
                "interval": _BYBIT_INTERVAL[interval],
                "limit": batch_limit,
            }
            if end:
                params["end"] = end
            payload = self._get_json(f"{config.BYBIT_BASE_URL}/v5/market/kline", params)
            if int(payload.get("retCode", -1)) != 0:
                raise MarketDataError(f"Bybit {payload.get('retCode')}: {payload.get('retMsg')}")
            rows = (payload.get("result") or {}).get("list") or []
            if not rows:
                break
            oldest = None
            for row in rows:
                if not isinstance(row, list) or len(row) < 7:
                    continue
                ts = safe_int(row[0])
                candle = Candle(
                    ts=ts,
                    open=safe_float(row[1]), high=safe_float(row[2]), low=safe_float(row[3]), close=safe_float(row[4]),
                    volume=safe_float(row[5]), turnover=safe_float(row[6]), confirmed=ts + _INTERVAL_MS[interval] <= now_ms(),
                )
                if candle.close > 0 and candle.high >= candle.low > 0:
                    out.append(candle)
                    oldest = ts if oldest is None else min(oldest, ts)
            if oldest is None or len(rows) < batch_limit:
                break
            end = oldest - 1
            time.sleep(0.03)
        candles = self._dedupe_sort(out, limit)
        if len(candles) < min(limit, 60):
            raise MarketDataError(f"Bybit insufficient candles {symbol} {interval}: {len(candles)}")
        return candles

    def candles(self, mapping: SymbolMapping, interval: str, limit: int, allow_fallback: bool = True) -> tuple[str, list[Candle]]:
        try:
            rows = self._okx_candles(mapping.okx, interval, limit)
            self._store_candles(DataSource.OKX.value, mapping.okx, interval, rows, max(limit, 2160))
            return DataSource.OKX.value, rows
        except Exception as okx_exc:
            if not allow_fallback:
                raise MarketDataError(str(okx_exc)) from okx_exc
            try:
                rows = self._bybit_candles(mapping.bybit, interval, limit)
                self._store_candles(DataSource.BYBIT_FALLBACK.value, mapping.bybit, interval, rows, max(limit, 2160))
                logger.info("FALLBACK | %s | OKX→BYBIT | %s", mapping.canonical, str(okx_exc)[:120])
                return DataSource.BYBIT_FALLBACK.value, rows
            except Exception as bybit_exc:
                raise MarketDataError(f"OKX={okx_exc}; BYBIT={bybit_exc}") from bybit_exc

    def _analysis_high_tf_bundle(self, source: str, symbol: str, fetcher: Any) -> dict[str, list[Candle]]:
        """Load a source-consistent higher-timeframe bundle with incremental caching."""
        bundle: dict[str, list[Candle]] = {}
        with self._bundle_lock(source, symbol):
            for interval, limit in config.ANALYSIS_CANDLE_LIMITS.items():
                rows = self._cached_candles(source, symbol, interval)
                interval_ms = _INTERVAL_MS[interval]
                refresh_after_ms = max(
                    config.ANALYSIS_CANDLE_CACHE_FRESH_SECONDS * 1000,
                    min(interval_ms // 4, 15 * 60_000),
                )
                if len(rows) < min(limit, 80):
                    rows = fetcher(symbol, interval, limit)
                    rows = self._store_candles(source, symbol, interval, rows, limit)
                elif self._cache_age_ms(source, symbol, interval) >= refresh_after_ms:
                    tail = fetcher(symbol, interval, min(12, limit))
                    rows = self._store_candles(source, symbol, interval, tail, limit)
                bundle[interval] = rows[-limit:]
        return bundle

    def analysis_bundle(self, mapping: SymbolMapping) -> tuple[str, dict[str, list[Candle]]]:
        """A bundle is entirely OKX or entirely Bybit; sources are never mixed."""
        try:
            bundle = self._analysis_high_tf_bundle(
                DataSource.OKX.value, mapping.okx, self._okx_candles
            )
            source = DataSource.OKX.value
        except Exception as okx_exc:
            try:
                bundle = self._analysis_high_tf_bundle(
                    DataSource.BYBIT_FALLBACK.value, mapping.bybit, self._bybit_candles
                )
                source = DataSource.BYBIT_FALLBACK.value
                logger.info("FALLBACK | %s | OKX→BYBIT | %s", mapping.canonical, str(okx_exc)[:120])
            except Exception as bybit_exc:
                raise MarketDataError(f"bundle failed OKX={okx_exc}; BYBIT={bybit_exc}") from bybit_exc
        return source, bundle

    @staticmethod
    def resample(candles: list[Candle], target_minutes: int) -> list[Candle]:
        bucket_ms = target_minutes * 60_000
        grouped: dict[int, list[Candle]] = defaultdict(list)
        for c in candles:
            grouped[c.ts // bucket_ms * bucket_ms].append(c)
        out: list[Candle] = []
        for ts in sorted(grouped):
            rows = sorted(grouped[ts], key=lambda x: x.ts)
            out.append(
                Candle(
                    ts=ts,
                    open=rows[0].open,
                    high=max(x.high for x in rows),
                    low=min(x.low for x in rows),
                    close=rows[-1].close,
                    volume=sum(x.volume for x in rows),
                    turnover=sum(x.turnover for x in rows),
                    confirmed=all(x.confirmed for x in rows),
                )
            )
        return out

    def refresh_tickers(self, mappings: list[SymbolMapping]) -> tuple[str, dict[str, float]]:
        map_okx = {m.okx: m.canonical for m in mappings}
        map_bybit = {m.bybit: m.canonical for m in mappings}
        prices: dict[str, float] = {}
        source = DataSource.OKX.value
        try:
            payload = self._get_json(f"{config.OKX_BASE_URL}/api/v5/market/tickers", {"instType": "SWAP"})
            if str(payload.get("code", "0")) != "0":
                raise MarketDataError(payload.get("msg") or "OKX ticker error")
            for item in payload.get("data", []):
                canonical = map_okx.get(str(item.get("instId", "")).upper())
                price = safe_float(item.get("last"))
                if canonical and price > 0:
                    prices[canonical] = price
            if len(prices) < max(1, len(mappings) // 2):
                raise MarketDataError(f"OKX ticker coverage low: {len(prices)}")
        except Exception as okx_exc:
            source = DataSource.BYBIT_FALLBACK.value
            payload = self._get_json(f"{config.BYBIT_BASE_URL}/v5/market/tickers", {"category": "linear"})
            if int(payload.get("retCode", -1)) != 0:
                raise MarketDataError(f"ticker fallback failed: {payload.get('retMsg')}; OKX={okx_exc}")
            for item in (payload.get("result") or {}).get("list", []):
                canonical = map_bybit.get(str(item.get("symbol", "")).upper())
                price = safe_float(item.get("lastPrice"))
                if canonical and price > 0:
                    prices[canonical] = price
        with self._ticker_lock:
            self._tickers = prices
            self._ticker_updated_at = now_ms()
            self._last_source = source
        return source, prices

    def cached_price(self, canonical: str, max_age_seconds: int = 30) -> float | None:
        with self._ticker_lock:
            if now_ms() - self._ticker_updated_at > max_age_seconds * 1000:
                return None
            return self._tickers.get(canonical)

    def ticker_snapshot(self) -> tuple[str, int, dict[str, float]]:
        with self._ticker_lock:
            return self._last_source, self._ticker_updated_at, dict(self._tickers)

    @staticmethod
    def data_quality(bundle: dict[str, list[Candle]]) -> float:
        quality = 100.0
        for tf, candles in bundle.items():
            expected = {"5m": 300, "15m": 180, "30m": 160, "1H": 160, "4H": 120, "1D": 100}.get(tf, 60)
            if len(candles) < expected:
                quality -= min(20.0, (expected - len(candles)) / expected * 20.0)
            if candles:
                gaps = 0
                interval = _INTERVAL_MS[tf]
                for a, b in zip(candles[-100:], candles[-99:]):
                    if b.ts - a.ts > interval * 1.5:
                        gaps += 1
                quality -= min(20.0, gaps * 2.0)
                if now_ms() - candles[-1].ts > interval * 3:
                    quality -= 30.0
        return clamp(quality, 0.0, 100.0)
