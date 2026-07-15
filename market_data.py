"""داده بازار مقاوم: OKX اصلی، Bybit و Binance Futures جایگزین مستقل.

قانون ایمنی مهم: یک Bundle تحلیلی هرگز از چند صرافی مخلوط نمی‌شود. اگر منبع
اصلی در میانه بارگیری خراب شود، کل Bundle از منبع بعدی دوباره ساخته می‌شود.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import requests

import config
from models import Candle, DataSource, SymbolMapping
from utils import clamp, now_ms, safe_float, safe_int

logger = logging.getLogger("adaptive_bot")


class MarketDataError(RuntimeError):
    pass


_INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1H": 3_600_000,
    "4H": 14_400_000,
    "1D": 86_400_000,
}
_INTERVAL_ALIASES = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1D",
    "d": "1D",
}
_BYBIT_INTERVAL = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1H": "60",
    "4H": "240",
    "1D": "D",
}
_BINANCE_INTERVAL = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1H": "1h",
    "4H": "4h",
    "1D": "1d",
}
_SOURCE_LABELS = {
    "OKX": DataSource.OKX.value,
    "BYBIT": DataSource.BYBIT_FALLBACK.value,
    "BINANCE": DataSource.BINANCE_FALLBACK.value,
}


class MarketDataClient:
    def __init__(self, session: requests.Session | None = None):
        # اسکن‌ها هم‌زمان‌اند. برای حالت واقعی هر Thread یک Session مستقل دارد.
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

        # محافظ سراسری هر صرافی: هم محدودیت هم‌زمانی، هم pacing و هم Circuit Breaker.
        self._source_state_lock = threading.RLock()
        self._source_failures = {name: 0 for name in _SOURCE_LABELS}
        self._source_blocked_until = {name: 0.0 for name in _SOURCE_LABELS}
        self._source_last_request = {name: 0.0 for name in _SOURCE_LABELS}
        self._source_semaphores = {
            "OKX": threading.BoundedSemaphore(config.MARKET_DATA_OKX_CONCURRENCY),
            "BYBIT": threading.BoundedSemaphore(config.MARKET_DATA_BYBIT_CONCURRENCY),
            "BINANCE": threading.BoundedSemaphore(config.MARKET_DATA_BINANCE_CONCURRENCY),
        }
        self._source_pace_locks = {name: threading.RLock() for name in _SOURCE_LABELS}
        self._fallback_log_lock = threading.RLock()
        self._fallback_log_at: dict[tuple[str, str], float] = {}
        self._empty_interval_warned = False

    @property
    def session(self) -> requests.Session:
        if self._external_session is not None:
            return self._external_session
        current = getattr(self._session_local, "session", None)
        if current is None:
            current = requests.Session()
            current.headers.update({"User-Agent": "crypto-ai-helper/market-data"})
            self._session_local.session = current
            with self._sessions_lock:
                self._sessions.append(current)
        return current

    def close(self) -> None:
        seen: set[int] = set()
        sessions = [self._external_session] if self._external_session is not None else []
        with self._sessions_lock:
            sessions += list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            if session is None or id(session) in seen:
                continue
            seen.add(id(session))
            session.close()

    @staticmethod
    def _normalize_symbol(symbol: str, source: str) -> str:
        value = str(symbol or "").strip().upper()
        if not value:
            raise MarketDataError(f"{source} symbol is empty")
        return value

    def _normalize_interval(self, interval: str | None) -> str:
        raw = str(interval or "").strip()
        if not raw:
            raw = str(config.PROFILE_BAR or "5m").strip() or "5m"
            if not self._empty_interval_warned:
                self._empty_interval_warned = True
                logger.warning("EMPTY_INTERVAL_FIXED | default=%s", raw)
        normalized = _INTERVAL_ALIASES.get(raw.lower())
        if normalized is None or normalized not in _INTERVAL_MS:
            raise MarketDataError(f"unsupported interval {interval!r}")
        return normalized

    def _source_is_blocked(self, source: str) -> tuple[bool, float]:
        now = time.monotonic()
        with self._source_state_lock:
            until = float(self._source_blocked_until.get(source, 0.0))
        return until > now, max(0.0, until - now)

    def _block_source(self, source: str, seconds: float, reason: str) -> None:
        until = time.monotonic() + max(1.0, seconds)
        should_log = False
        with self._source_state_lock:
            previous = float(self._source_blocked_until.get(source, 0.0))
            if until > previous:
                self._source_blocked_until[source] = until
                should_log = True
        if should_log:
            logger.warning("SOURCE_COOLDOWN | %s | %.0fs | %s", source, seconds, reason[:180])

    def _record_success(self, source: str) -> None:
        with self._source_state_lock:
            self._source_failures[source] = 0

    def _record_failure(self, source: str, exc: Exception, status_code: int | None = None) -> None:
        with self._source_state_lock:
            failures = int(self._source_failures.get(source, 0)) + 1
            self._source_failures[source] = failures
        if status_code in {418, 429}:
            self._block_source(source, config.MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS, str(exc))
        elif status_code in {403, 451}:
            self._block_source(source, config.MARKET_DATA_FORBIDDEN_COOLDOWN_SECONDS, str(exc))
        elif failures >= config.MARKET_DATA_FAILURES_BEFORE_COOLDOWN:
            self._block_source(source, config.MARKET_DATA_NETWORK_COOLDOWN_SECONDS, str(exc))

    @contextmanager
    def _request_slot(self, source: str) -> Iterator[None]:
        blocked, remaining = self._source_is_blocked(source)
        if blocked:
            raise MarketDataError(f"{source} circuit open for {remaining:.1f}s")
        semaphore = self._source_semaphores[source]
        acquired = semaphore.acquire(timeout=max(1.0, config.REQUEST_TIMEOUT))
        if not acquired:
            raise MarketDataError(f"{source} request queue timeout")
        try:
            # Serialize only the start time of requests. The HTTP calls themselves may
            # still run concurrently up to the source semaphore limit.
            with self._source_pace_locks[source]:
                with self._source_state_lock:
                    last = float(self._source_last_request.get(source, 0.0))
                wait_for = config.MARKET_DATA_MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - last)
                if wait_for > 0:
                    time.sleep(wait_for)
                with self._source_state_lock:
                    self._source_last_request[source] = time.monotonic()
            yield
        finally:
            semaphore.release()

    @staticmethod
    def _retry_after_seconds(response: requests.Response) -> float:
        raw = response.headers.get("Retry-After", "")
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0

    def _get_payload(self, source: str, url: str, params: dict[str, Any]) -> Any:
        last: Exception | None = None
        for attempt in range(config.HTTP_RETRIES + 1):
            response: requests.Response | None = None
            try:
                with self._request_slot(source):
                    response = self.session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
                if response.status_code in {418, 429}:
                    retry_after = max(
                        self._retry_after_seconds(response),
                        float(config.MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS),
                    )
                    exc = MarketDataError(f"HTTP {response.status_code}: rate limited")
                    self._block_source(source, retry_after, str(exc))
                    raise exc
                if response.status_code in {403, 451}:
                    exc = MarketDataError(f"HTTP {response.status_code}: access forbidden")
                    self._block_source(source, config.MARKET_DATA_FORBIDDEN_COOLDOWN_SECONDS, str(exc))
                    raise exc
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, (dict, list)):
                    raise MarketDataError("invalid response shape")
                self._record_success(source)
                return payload
            except Exception as exc:
                last = exc
                status = response.status_code if response is not None else None
                # Rate-limit/forbidden errors immediately move to the next source.
                if status in {403, 418, 429, 451} or "circuit open" in str(exc):
                    raise MarketDataError(str(exc)) from exc
                if attempt < config.HTTP_RETRIES:
                    delay = config.HTTP_BACKOFF_SECONDS * (2**attempt) + random.uniform(0.0, 0.20)
                    time.sleep(delay)
                    continue
                self._record_failure(source, exc, status)
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

    def _log_fallback(self, canonical: str, target: str, detail: str) -> None:
        key = (canonical, target)
        now = time.monotonic()
        with self._fallback_log_lock:
            last = float(self._fallback_log_at.get(key, 0.0))
            if now - last < config.MARKET_DATA_FALLBACK_LOG_SECONDS:
                return
            self._fallback_log_at[key] = now
        logger.info("FALLBACK | %s | ->%s | %s", canonical, target, detail[:160])

    def _okx_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        symbol = self._normalize_symbol(symbol, "OKX")
        interval = self._normalize_interval(interval)
        path = "/api/v5/market/history-candles" if limit > 300 else "/api/v5/market/candles"
        out: list[Candle] = []
        after: str | None = None
        while len(out) < limit:
            batch_limit = min(300, limit - len(out))
            params: dict[str, Any] = {"instId": symbol, "bar": interval, "limit": batch_limit}
            if after:
                params["after"] = after
            payload = self._get_payload("OKX", f"{config.OKX_BASE_URL}{path}", params)
            if not isinstance(payload, dict):
                raise MarketDataError("OKX invalid payload")
            if str(payload.get("code", "0")) != "0":
                code = str(payload.get("code") or "")
                msg = str(payload.get("msg") or "OKX error")
                if code in {"50011", "50040"}:
                    self._block_source("OKX", config.MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS, msg)
                raise MarketDataError(f"OKX {code}: {msg}")
            rows = payload.get("data") or []
            if not rows:
                break
            oldest = None
            parsed = 0
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
                    parsed += 1
            if oldest is None or parsed == 0 or len(rows) < batch_limit:
                break
            if after == str(oldest):
                break
            after = str(oldest)
        candles = self._dedupe_sort(out, limit)
        if len(candles) < min(limit, 60):
            raise MarketDataError(f"OKX insufficient candles {symbol} {interval}: {len(candles)}")
        return candles

    def _bybit_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        symbol = self._normalize_symbol(symbol, "BYBIT")
        interval = self._normalize_interval(interval)
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
            if end is not None:
                params["end"] = end
            payload = self._get_payload("BYBIT", f"{config.BYBIT_BASE_URL}/v5/market/kline", params)
            if not isinstance(payload, dict):
                raise MarketDataError("Bybit invalid payload")
            if int(payload.get("retCode", -1)) != 0:
                code = int(payload.get("retCode", -1))
                msg = str(payload.get("retMsg") or "Bybit error")
                if code in {10006, 10429}:
                    self._block_source("BYBIT", config.MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS, msg)
                raise MarketDataError(f"Bybit {code}: {msg}")
            rows = (payload.get("result") or {}).get("list") or []
            if not rows:
                break
            oldest = None
            parsed = 0
            for row in rows:
                if not isinstance(row, list) or len(row) < 7:
                    continue
                ts = safe_int(row[0])
                candle = Candle(
                    ts=ts,
                    open=safe_float(row[1]),
                    high=safe_float(row[2]),
                    low=safe_float(row[3]),
                    close=safe_float(row[4]),
                    volume=safe_float(row[5]),
                    turnover=safe_float(row[6]),
                    confirmed=ts + _INTERVAL_MS[interval] <= now_ms(),
                )
                if candle.close > 0 and candle.high >= candle.low > 0:
                    out.append(candle)
                    oldest = ts if oldest is None else min(oldest, ts)
                    parsed += 1
            if oldest is None or parsed == 0 or len(rows) < batch_limit:
                break
            if end == oldest - 1:
                break
            end = oldest - 1
        candles = self._dedupe_sort(out, limit)
        if len(candles) < min(limit, 60):
            raise MarketDataError(f"Bybit insufficient candles {symbol} {interval}: {len(candles)}")
        return candles

    def _binance_candles(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        symbol = self._normalize_symbol(symbol, "BINANCE")
        interval = self._normalize_interval(interval)
        out: list[Candle] = []
        end_time: int | None = None
        while len(out) < limit:
            batch_limit = min(config.BINANCE_KLINE_PAGE_LIMIT, limit - len(out))
            params: dict[str, Any] = {
                "symbol": symbol,
                "interval": _BINANCE_INTERVAL[interval],
                "limit": batch_limit,
            }
            if end_time is not None:
                params["endTime"] = end_time
            payload = self._get_payload(
                "BINANCE",
                f"{config.BINANCE_FUTURES_BASE_URL}/fapi/v1/klines",
                params,
            )
            if isinstance(payload, dict):
                raise MarketDataError(f"Binance {payload.get('code')}: {payload.get('msg')}")
            rows = payload or []
            if not rows:
                break
            oldest = None
            parsed = 0
            for row in rows:
                if not isinstance(row, list) or len(row) < 8:
                    continue
                ts = safe_int(row[0])
                close_time = safe_int(row[6])
                candle = Candle(
                    ts=ts,
                    open=safe_float(row[1]),
                    high=safe_float(row[2]),
                    low=safe_float(row[3]),
                    close=safe_float(row[4]),
                    volume=safe_float(row[5]),
                    turnover=safe_float(row[7]),
                    confirmed=close_time <= now_ms(),
                )
                if candle.close > 0 and candle.high >= candle.low > 0:
                    out.append(candle)
                    oldest = ts if oldest is None else min(oldest, ts)
                    parsed += 1
            if oldest is None or parsed == 0 or len(rows) < batch_limit:
                break
            if end_time == oldest - 1:
                break
            end_time = oldest - 1
        candles = self._dedupe_sort(out, limit)
        if len(candles) < min(limit, 60):
            raise MarketDataError(f"Binance insufficient candles {symbol} {interval}: {len(candles)}")
        return candles

    def _source_candidates(
        self, mapping: SymbolMapping, allow_fallback: bool = True
    ) -> list[tuple[str, str, Callable[[str, str, int], list[Candle]]]]:
        available = {
            "OKX": (str(mapping.okx or ""), self._okx_candles),
            "BYBIT": (str(mapping.bybit or ""), self._bybit_candles),
            "BINANCE": (str(mapping.binance or mapping.canonical or ""), self._binance_candles),
        }
        order = ["OKX"] if not allow_fallback else list(config.MARKET_DATA_SOURCE_ORDER)
        # Always keep a safe deterministic tail even if env order is malformed.
        for name in ("OKX", "BYBIT", "BINANCE"):
            if name not in order:
                order.append(name)
        out = []
        for name in order:
            if name not in available:
                continue
            symbol, fetcher = available[name]
            if symbol:
                out.append((_SOURCE_LABELS[name], symbol, fetcher))
            if not allow_fallback:
                break
        return out

    def candles(
        self,
        mapping: SymbolMapping,
        interval: str,
        limit: int,
        allow_fallback: bool = True,
    ) -> tuple[str, list[Candle]]:
        interval = self._normalize_interval(interval)
        errors: list[str] = []
        for index, (source, symbol, fetcher) in enumerate(self._source_candidates(mapping, allow_fallback)):
            try:
                rows = fetcher(symbol, interval, limit)
                self._store_candles(source, symbol, interval, rows, max(limit, 2160))
                if index > 0:
                    self._log_fallback(mapping.canonical, source, "; ".join(errors))
                return source, rows
            except Exception as exc:
                errors.append(f"{source}={exc}")
        raise MarketDataError("; ".join(errors) or "no market-data source available")

    def _analysis_high_tf_bundle(
        self, source: str, symbol: str, fetcher: Callable[[str, str, int], list[Candle]]
    ) -> dict[str, list[Candle]]:
        """بارگیری Bundle یکپارچه با کش افزایشی؛ هیچ منبعی مخلوط نمی‌شود."""
        bundle: dict[str, list[Candle]] = {}
        with self._bundle_lock(source, symbol):
            for raw_interval, limit in config.ANALYSIS_CANDLE_LIMITS.items():
                interval = self._normalize_interval(raw_interval)
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
        """کل Bundle یا OKX است یا Bybit یا Binance؛ منبع‌ها مخلوط نمی‌شوند."""
        errors: list[str] = []
        for index, (source, symbol, fetcher) in enumerate(self._source_candidates(mapping, True)):
            try:
                bundle = self._analysis_high_tf_bundle(source, symbol, fetcher)
                if index > 0:
                    self._log_fallback(mapping.canonical, source, "; ".join(errors))
                return source, bundle
            except Exception as exc:
                errors.append(f"{source}={exc}")
        raise MarketDataError("bundle failed " + "; ".join(errors))

    @staticmethod
    def resample(candles: list[Candle], target_minutes: int) -> list[Candle]:
        bucket_ms = target_minutes * 60_000
        grouped: dict[int, list[Candle]] = defaultdict(list)
        for candle in candles:
            grouped[candle.ts // bucket_ms * bucket_ms].append(candle)
        out: list[Candle] = []
        for ts in sorted(grouped):
            rows = sorted(grouped[ts], key=lambda item: item.ts)
            out.append(
                Candle(
                    ts=ts,
                    open=rows[0].open,
                    high=max(item.high for item in rows),
                    low=min(item.low for item in rows),
                    close=rows[-1].close,
                    volume=sum(item.volume for item in rows),
                    turnover=sum(item.turnover for item in rows),
                    confirmed=all(item.confirmed for item in rows),
                )
            )
        return out

    def _okx_tickers(self, mappings: list[SymbolMapping]) -> dict[str, float]:
        lookup = {m.okx.upper(): m.canonical for m in mappings if m.okx}
        payload = self._get_payload(
            "OKX", f"{config.OKX_BASE_URL}/api/v5/market/tickers", {"instType": "SWAP"}
        )
        if not isinstance(payload, dict) or str(payload.get("code", "0")) != "0":
            raise MarketDataError(f"OKX ticker error: {payload}")
        prices: dict[str, float] = {}
        for item in payload.get("data", []):
            canonical = lookup.get(str(item.get("instId", "")).upper())
            price = safe_float(item.get("last"))
            if canonical and price > 0:
                prices[canonical] = price
        return prices

    def _bybit_tickers(self, mappings: list[SymbolMapping]) -> dict[str, float]:
        lookup = {m.bybit.upper(): m.canonical for m in mappings if m.bybit}
        payload = self._get_payload(
            "BYBIT", f"{config.BYBIT_BASE_URL}/v5/market/tickers", {"category": "linear"}
        )
        if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
            raise MarketDataError(f"Bybit ticker error: {payload}")
        prices: dict[str, float] = {}
        for item in (payload.get("result") or {}).get("list", []):
            canonical = lookup.get(str(item.get("symbol", "")).upper())
            price = safe_float(item.get("lastPrice"))
            if canonical and price > 0:
                prices[canonical] = price
        return prices

    def _binance_tickers(self, mappings: list[SymbolMapping]) -> dict[str, float]:
        lookup = {
            str(m.binance or m.canonical).upper(): m.canonical
            for m in mappings
            if (m.binance or m.canonical)
        }
        payload = self._get_payload(
            "BINANCE", f"{config.BINANCE_FUTURES_BASE_URL}/fapi/v1/ticker/price", {}
        )
        if not isinstance(payload, list):
            raise MarketDataError(f"Binance ticker error: {payload}")
        prices: dict[str, float] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            canonical = lookup.get(str(item.get("symbol", "")).upper())
            price = safe_float(item.get("price"))
            if canonical and price > 0:
                prices[canonical] = price
        return prices

    def refresh_tickers(self, mappings: list[SymbolMapping]) -> tuple[str, dict[str, float]]:
        fetchers = {
            "OKX": (DataSource.OKX.value, self._okx_tickers),
            "BYBIT": (DataSource.BYBIT_FALLBACK.value, self._bybit_tickers),
            "BINANCE": (DataSource.BINANCE_FALLBACK.value, self._binance_tickers),
        }
        errors: list[str] = []
        order = list(config.MARKET_DATA_SOURCE_ORDER)
        for default_name in ("OKX", "BYBIT", "BINANCE"):
            if default_name not in order:
                order.append(default_name)
        for index, name in enumerate(order):
            item = fetchers.get(name)
            if item is None:
                continue
            source, fetcher = item
            try:
                prices = fetcher(mappings)
                if len(prices) < max(1, len(mappings) // 2):
                    raise MarketDataError(f"ticker coverage low: {len(prices)}/{len(mappings)}")
                with self._ticker_lock:
                    self._tickers = prices
                    self._ticker_updated_at = now_ms()
                    self._last_source = source
                if index > 0:
                    self._log_fallback("TICKERS", source, "; ".join(errors))
                return source, prices
            except Exception as exc:
                errors.append(f"{source}={exc}")

        # یک قطعی کوتاه نباید مانیتور را کور کند؛ Snapshot نزدیک قبلی حفظ می‌شود.
        with self._ticker_lock:
            age = now_ms() - self._ticker_updated_at
            if self._tickers and age <= config.TICKER_STALE_GRACE_SECONDS * 1000:
                logger.warning("TICKER_STALE_CACHE | age=%.1fs | %s", age / 1000.0, "; ".join(errors)[:180])
                return self._last_source, dict(self._tickers)
        raise MarketDataError("ticker sources failed: " + "; ".join(errors))

    def cached_price(self, canonical: str, max_age_seconds: int = 30) -> float | None:
        with self._ticker_lock:
            if now_ms() - self._ticker_updated_at > max_age_seconds * 1000:
                return None
            return self._tickers.get(canonical)

    def ticker_snapshot(self) -> tuple[str, int, dict[str, float]]:
        with self._ticker_lock:
            return self._last_source, self._ticker_updated_at, dict(self._tickers)

    def source_health(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        with self._source_state_lock:
            return {
                source: {
                    "failures": int(self._source_failures.get(source, 0)),
                    "cooldown_seconds": max(0.0, float(self._source_blocked_until.get(source, 0.0)) - now),
                }
                for source in _SOURCE_LABELS
            }

    @staticmethod
    def data_quality(bundle: dict[str, list[Candle]]) -> float:
        quality = 100.0
        for tf, candles in bundle.items():
            expected = {
                "5m": 300,
                "15m": 180,
                "30m": 160,
                "1H": 160,
                "4H": 120,
                "1D": 100,
            }.get(tf, 60)
            if len(candles) < expected:
                quality -= min(20.0, (expected - len(candles)) / expected * 20.0)
            if candles:
                gaps = 0
                interval = _INTERVAL_MS[tf]
                recent = candles[-100:]
                for previous, current in zip(recent, recent[1:]):
                    if current.ts - previous.ts > interval * 1.5:
                        gaps += 1
                quality -= min(20.0, gaps * 2.0)
                if now_ms() - candles[-1].ts > interval * 3:
                    quality -= 30.0
        return clamp(quality, 0.0, 100.0)
