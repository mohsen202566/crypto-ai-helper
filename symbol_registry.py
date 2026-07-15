"""اعتبارسنجی ۱۰۰ نماد و انتخاب ۳۵ ارز فعال با سه منبع داده عمومی."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import requests

import config
from models import SymbolMapping
from storage import Storage
from utils import (
    alias_candidates,
    canonical_base_from_symbol,
    clamp,
    extract_filter,
    normalize_symbol,
    now_ms,
    safe_float,
)

logger = logging.getLogger("adaptive_bot")


class SymbolRegistryError(RuntimeError):
    pass


class SymbolRegistry:
    def __init__(self, storage: Storage, toobit_client: Any, session: requests.Session | None = None):
        self.storage = storage
        self.toobit = toobit_client
        self.session = session or requests.Session()
        self._mappings: dict[str, SymbolMapping] = {}

    @staticmethod
    def _request_payload(session: requests.Session, url: str, params: dict[str, Any]) -> Any:
        last: Exception | None = None
        for attempt in range(config.HTTP_RETRIES + 1):
            try:
                res = session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
                if res.status_code in {418, 429}:
                    raise SymbolRegistryError(f"HTTP {res.status_code}: rate limited")
                res.raise_for_status()
                payload = res.json()
                if not isinstance(payload, (dict, list)):
                    raise SymbolRegistryError("invalid JSON shape")
                return payload
            except Exception as exc:
                last = exc
                if attempt < config.HTTP_RETRIES:
                    time.sleep(config.HTTP_BACKOFF_SECONDS * (2**attempt))
        raise SymbolRegistryError(str(last))

    def _okx_instruments(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        payload = self._request_payload(
            self.session,
            f"{config.OKX_BASE_URL}/api/v5/public/instruments",
            {"instType": "SWAP"},
        )
        if not isinstance(payload, dict) or str(payload.get("code", "0")) != "0":
            raise SymbolRegistryError(f"OKX instruments: {payload}")
        by_symbol: dict[str, dict[str, Any]] = {}
        by_base: dict[str, dict[str, Any]] = {}
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("settleCcy", "")).upper() != "USDT":
                continue
            if str(item.get("state", "live")).lower() not in {"live", "trading"}:
                continue
            symbol = str(item.get("instId", "")).upper()
            base = str(item.get("baseCcy") or canonical_base_from_symbol(symbol)).upper()
            by_symbol[symbol] = item
            by_base.setdefault(base, item)
        return by_symbol, by_base

    def _bybit_instruments(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        by_symbol: dict[str, dict[str, Any]] = {}
        by_base: dict[str, dict[str, Any]] = {}
        cursor = ""
        for _ in range(10):
            params: dict[str, Any] = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            payload = self._request_payload(
                self.session,
                f"{config.BYBIT_BASE_URL}/v5/market/instruments-info",
                params,
            )
            if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
                raise SymbolRegistryError(f"Bybit instruments: {payload}")
            result = payload.get("result") or {}
            for item in result.get("list", []):
                if not isinstance(item, dict):
                    continue
                if str(item.get("quoteCoin", "")).upper() != "USDT":
                    continue
                if str(item.get("status", "Trading")).lower() not in {"trading", "live"}:
                    continue
                symbol = str(item.get("symbol", "")).upper()
                base = str(item.get("baseCoin") or canonical_base_from_symbol(symbol)).upper()
                by_symbol[symbol] = item
                by_base.setdefault(base, item)
            cursor = str(result.get("nextPageCursor") or "")
            if not cursor:
                break
        return by_symbol, by_base

    def _binance_instruments(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        payload = self._request_payload(
            self.session,
            f"{config.BINANCE_FUTURES_BASE_URL}/fapi/v1/exchangeInfo",
            {},
        )
        if not isinstance(payload, dict):
            raise SymbolRegistryError("Binance exchangeInfo invalid")
        by_symbol: dict[str, dict[str, Any]] = {}
        by_base: dict[str, dict[str, Any]] = {}
        for item in payload.get("symbols", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("quoteAsset", "")).upper() != "USDT":
                continue
            if str(item.get("contractType", "PERPETUAL")).upper() != "PERPETUAL":
                continue
            if str(item.get("status", "TRADING")).upper() != "TRADING":
                continue
            symbol = str(item.get("symbol", "")).upper()
            base = str(item.get("baseAsset") or canonical_base_from_symbol(symbol)).upper()
            by_symbol[symbol] = item
            by_base.setdefault(base, item)
        return by_symbol, by_base

    @staticmethod
    def _score_liquidity(raw: list[tuple[str, float, float, float]]) -> dict[str, float]:
        if not raw:
            return {}
        max_turnover = max(item[1] for item in raw) or 1.0
        out: dict[str, float] = {}
        for base, turnover, spread, opportunity in raw:
            volume_score = clamp((turnover / max_turnover) ** 0.35, 0.0, 1.0)
            spread_score = clamp(1.0 - spread / 0.004, 0.0, 1.0)
            opportunity_score = clamp(opportunity / 0.12, 0.0, 1.0)
            out[base] = 100.0 * (
                0.55 * volume_score + 0.30 * spread_score + 0.15 * opportunity_score
            )
        return out

    def _okx_liquidity(self) -> dict[str, float]:
        payload = self._request_payload(
            self.session,
            f"{config.OKX_BASE_URL}/api/v5/market/tickers",
            {"instType": "SWAP"},
        )
        if not isinstance(payload, dict):
            return {}
        raw: list[tuple[str, float, float, float]] = []
        for item in payload.get("data", []):
            symbol = str(item.get("instId", "")).upper()
            if not symbol.endswith("-USDT-SWAP"):
                continue
            base = canonical_base_from_symbol(symbol)
            last = safe_float(item.get("last"))
            bid = safe_float(item.get("bidPx"))
            ask = safe_float(item.get("askPx"))
            turnover = safe_float(item.get("volCcy24h") or item.get("vol24h"))
            high = safe_float(item.get("high24h"))
            low = safe_float(item.get("low24h"))
            spread = (ask - bid) / last if last > 0 and ask >= bid > 0 else 0.02
            opportunity = (high - low) / last if last > 0 and high >= low > 0 else 0.0
            raw.append((base, turnover, spread, opportunity))
        return self._score_liquidity(raw)

    def _bybit_liquidity(self) -> dict[str, float]:
        payload = self._request_payload(
            self.session,
            f"{config.BYBIT_BASE_URL}/v5/market/tickers",
            {"category": "linear"},
        )
        if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
            return {}
        raw: list[tuple[str, float, float, float]] = []
        for item in (payload.get("result") or {}).get("list", []):
            symbol = str(item.get("symbol", "")).upper()
            if not symbol.endswith("USDT"):
                continue
            base = canonical_base_from_symbol(symbol)
            last = safe_float(item.get("lastPrice"))
            bid = safe_float(item.get("bid1Price"))
            ask = safe_float(item.get("ask1Price"))
            turnover = safe_float(item.get("turnover24h"))
            high = safe_float(item.get("highPrice24h"))
            low = safe_float(item.get("lowPrice24h"))
            spread = (ask - bid) / last if last > 0 and ask >= bid > 0 else 0.02
            opportunity = (high - low) / last if last > 0 and high >= low > 0 else 0.0
            raw.append((base, turnover, spread, opportunity))
        return self._score_liquidity(raw)

    def _binance_liquidity(self) -> dict[str, float]:
        payload = self._request_payload(
            self.session,
            f"{config.BINANCE_FUTURES_BASE_URL}/fapi/v1/ticker/24hr",
            {},
        )
        if not isinstance(payload, list):
            return {}
        raw: list[tuple[str, float, float, float]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).upper()
            if not symbol.endswith("USDT"):
                continue
            base = canonical_base_from_symbol(symbol)
            last = safe_float(item.get("lastPrice"))
            bid = safe_float(item.get("bidPrice"))
            ask = safe_float(item.get("askPrice"))
            turnover = safe_float(item.get("quoteVolume"))
            high = safe_float(item.get("highPrice"))
            low = safe_float(item.get("lowPrice"))
            spread = (ask - bid) / last if last > 0 and ask >= bid > 0 else 0.02
            opportunity = (high - low) / last if last > 0 and high >= low > 0 else 0.0
            raw.append((base, turnover, spread, opportunity))
        return self._score_liquidity(raw)

    @staticmethod
    def _find_alias(
        base: str,
        exchange: str,
        by_symbol: dict[str, dict[str, Any]],
        by_base: dict[str, dict[str, Any]],
    ) -> tuple[str, dict[str, Any]] | None:
        if not by_symbol and not by_base:
            return None
        overrides = config.SYMBOL_ALIAS_OVERRIDES.get(base, {}).get(exchange, ())
        candidates = tuple(overrides) + alias_candidates(base, exchange)
        normalized_map = {normalize_symbol(key): (key, value) for key, value in by_symbol.items()}
        for alias in candidates:
            direct = by_symbol.get(alias.upper())
            if direct is not None:
                return alias.upper(), direct
            found = normalized_map.get(normalize_symbol(alias))
            if found:
                return found
        item = by_base.get(base)
        if item:
            key = str(item.get("instId", "")) if exchange == "okx" else str(
                item.get("symbol") or item.get("symbolId") or ""
            )
            if key:
                return key.upper(), item
        return None

    def _load_persisted_complete(self) -> list[SymbolMapping]:
        rows = self.storage.learning.symbols(valid=True)
        if len(rows) < config.UNIVERSE_SIZE:
            return []
        mappings: list[SymbolMapping] = []
        for row in rows[: config.UNIVERSE_SIZE]:
            clean = {key: row.get(key) for key in SymbolMapping.__dataclass_fields__}
            for key in ("okx_aliases", "bybit_aliases", "binance_aliases", "toobit_aliases"):
                clean[key] = tuple(clean.get(key) or ())
            clean["binance"] = clean.get("binance") or row.get("canonical") or ""
            mappings.append(SymbolMapping(**clean))
        return mappings

    def validate_universe(self, progress: Callable[[str], None] | None = None) -> list[SymbolMapping]:
        progress = progress or (lambda _msg: None)
        persisted = self._load_persisted_complete()
        market_maps: dict[str, tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]] = {}
        source_errors: list[str] = []

        for label, message, loader in (
            ("okx", "دریافت نمادهای OKX", self._okx_instruments),
            ("bybit", "دریافت نمادهای Bybit", self._bybit_instruments),
            ("binance", "دریافت نمادهای Binance Futures", self._binance_instruments),
        ):
            progress(message)
            try:
                market_maps[label] = loader()
            except Exception as exc:
                market_maps[label] = ({}, {})
                source_errors.append(f"{label}={exc}")
                logger.warning("REGISTRY_SOURCE_SKIP | %s | %s", label.upper(), str(exc)[:180])

        try:
            progress("دریافت نمادهای Toobit")
            toobit_symbols = self.toobit.get_exchange_symbols()
        except Exception as exc:
            if persisted:
                logger.warning("Toobit registry unavailable; persisted registry used: %s", exc)
                self._mappings = {mapping.canonical: mapping for mapping in persisted}
                return persisted
            raise SymbolRegistryError(f"Toobit symbols unavailable and no cache exists: {exc}") from exc

        toobit_base: dict[str, dict[str, Any]] = {}
        for symbol, item in toobit_symbols.items():
            base = canonical_base_from_symbol(symbol)
            if not base.startswith("1000"):
                toobit_base.setdefault(base, item)

        liquidity: dict[str, float] = {}
        for name, loader in (
            ("OKX", self._okx_liquidity),
            ("BYBIT", self._bybit_liquidity),
            ("BINANCE", self._binance_liquidity),
        ):
            try:
                liquidity = loader()
                if liquidity:
                    break
            except Exception as exc:
                logger.warning("LIQUIDITY_SOURCE_SKIP | %s | %s", name, str(exc)[:160])

        valid: list[SymbolMapping] = []
        for base in config.CANDIDATE_BASE_ASSETS:
            okx = self._find_alias(base, "okx", *market_maps.get("okx", ({}, {})))
            bybit = self._find_alias(base, "bybit", *market_maps.get("bybit", ({}, {})))
            binance = self._find_alias(base, "binance", *market_maps.get("binance", ({}, {})))
            toobit = self._find_alias(base, "toobit", toobit_symbols, toobit_base)
            market_found = [item for item in (okx, bybit, binance) if item]
            if not toobit or len(market_found) < config.MARKET_DATA_MIN_SOURCES:
                continue

            toobit_symbol, toobit_info = toobit
            resolved_symbols = [item[0] for item in market_found] + [toobit_symbol]
            bases = {canonical_base_from_symbol(symbol) for symbol in resolved_symbols}
            if any(resolved.startswith("1000") for resolved in bases):
                continue
            allowed_bases = config.SYMBOL_EQUIVALENT_BASES.get(base, frozenset({base}))
            if not bases or not bases.issubset(allowed_bases):
                continue

            okx_symbol, okx_info = okx if okx else ("", {})
            bybit_symbol, _ = bybit if bybit else ("", {})
            binance_symbol, _ = binance if binance else ("", {})
            price_filter = extract_filter(toobit_info, "PRICE_FILTER")
            lot_filter = extract_filter(toobit_info, "LOT_SIZE")
            notional_filter = extract_filter(toobit_info, "MIN_NOTIONAL")
            tick = safe_float(
                toobit_info.get("tickSize")
                or price_filter.get("tickSize")
                or (
                    (toobit_info.get("priceFilter") or {}).get("tickSize")
                    if isinstance(toobit_info.get("priceFilter"), dict)
                    else None
                )
                or okx_info.get("tickSz")
            )
            qty_step = safe_float(
                toobit_info.get("stepSize")
                or lot_filter.get("stepSize")
                or lot_filter.get("qtyStep")
                or (
                    (toobit_info.get("lotSizeFilter") or {}).get("qtyStep")
                    if isinstance(toobit_info.get("lotSizeFilter"), dict)
                    else None
                )
            )
            min_qty = safe_float(
                toobit_info.get("minQty") or lot_filter.get("minQty") or toobit_info.get("minTradeQty")
            )
            min_notional = safe_float(
                toobit_info.get("minNotional")
                or notional_filter.get("minNotional")
                or toobit_info.get("minTradeAmount")
            )
            contract_multiplier = safe_float(
                toobit_info.get("contractMultiplier")
                or toobit_info.get("contractSize")
                or toobit_info.get("multiplier"),
                1.0,
            ) or 1.0
            overrides = config.SYMBOL_ALIAS_OVERRIDES.get(base, {})
            valid.append(
                SymbolMapping(
                    canonical=f"{base}USDT",
                    base=base,
                    okx=okx_symbol,
                    bybit=bybit_symbol,
                    toobit=toobit_symbol,
                    binance=binance_symbol,
                    okx_aliases=tuple(overrides.get("okx", ())) + alias_candidates(base, "okx"),
                    bybit_aliases=tuple(overrides.get("bybit", ())) + alias_candidates(base, "bybit"),
                    binance_aliases=tuple(overrides.get("binance", ())) + alias_candidates(base, "binance"),
                    toobit_aliases=tuple(overrides.get("toobit", ())) + alias_candidates(base, "toobit"),
                    tick_size=tick,
                    quantity_step=qty_step,
                    min_qty=min_qty,
                    min_notional=min_notional,
                    contract_multiplier=contract_multiplier,
                    liquidity_score=liquidity.get(base, 0.0),
                    active=False,
                    valid=True,
                )
            )

        valid.sort(key=lambda item: (item.liquidity_score, item.base in {"BTC", "ETH"}), reverse=True)
        if len(valid) < config.UNIVERSE_SIZE:
            if persisted:
                logger.warning(
                    "Only %s live mappings found; persisted complete registry used. Sources: %s",
                    len(valid),
                    "; ".join(source_errors),
                )
                self._mappings = {mapping.canonical: mapping for mapping in persisted}
                return persisted
            raise SymbolRegistryError(
                f"فقط {len(valid)} نماد با Toobit و حداقل {config.MARKET_DATA_MIN_SOURCES} منبع داده پیدا شد"
            )

        universe = valid[: config.UNIVERSE_SIZE]
        previous_active = {
            item["canonical"] for item in self.storage.learning.symbols(active=True, valid=True)
        }
        active_selected: list[SymbolMapping] = []
        for mapping in universe:
            if mapping.canonical in previous_active and len(active_selected) < config.ACTIVE_SYMBOLS:
                mapping.active = True
                active_selected.append(mapping)
        for mapping in universe:
            if len(active_selected) >= config.ACTIVE_SYMBOLS:
                break
            if not mapping.active:
                mapping.active = True
                active_selected.append(mapping)

        for mapping in universe:
            self.storage.learning.upsert_symbol(mapping.to_dict())
        new_keys = {mapping.canonical for mapping in universe}
        for old in self.storage.learning.symbols():
            if old.get("canonical") not in new_keys:
                self.storage.learning.set_symbol_activity(old["canonical"], False)

        self._mappings = {mapping.canonical: mapping for mapping in universe}
        active_count = sum(1 for mapping in universe if mapping.active)
        self.storage.runtime.set_setting("active_symbols_count", active_count)
        self.storage.runtime.set_setting("reserve_symbols_count", len(universe) - active_count)
        progress(f"نمادها آماده: {len(universe)} کل، {active_count} فعال")
        return universe

    def load(self) -> list[SymbolMapping]:
        rows = self.storage.learning.symbols(valid=True)
        self._mappings = {}
        for row in rows[: config.UNIVERSE_SIZE]:
            clean = {key: row.get(key) for key in SymbolMapping.__dataclass_fields__}
            for key in ("okx_aliases", "bybit_aliases", "binance_aliases", "toobit_aliases"):
                clean[key] = tuple(clean.get(key) or ())
            clean["binance"] = clean.get("binance") or row.get("canonical") or ""
            self._mappings[row["canonical"]] = SymbolMapping(**clean)
        return list(self._mappings.values())

    def get(self, canonical: str) -> SymbolMapping | None:
        return self._mappings.get(canonical)

    def active(self) -> list[SymbolMapping]:
        return sorted(
            (mapping for mapping in self._mappings.values() if mapping.active and mapping.valid),
            key=lambda mapping: mapping.liquidity_score,
            reverse=True,
        )

    def reserve(self) -> list[SymbolMapping]:
        return sorted(
            (mapping for mapping in self._mappings.values() if not mapping.active and mapping.valid),
            key=lambda mapping: mapping.liquidity_score,
            reverse=True,
        )

    def record_data_result(self, canonical: str, success: bool) -> int:
        cooldown = 0 if success else now_ms() + config.SYMBOL_COOLDOWN_SECONDS * 1000
        return self.storage.learning.record_symbol_error(canonical, success, cooldown)

    def in_cooldown(self, canonical: str) -> bool:
        for item in self.storage.learning.symbols():
            if item.get("canonical") == canonical:
                return int(item.get("cooldown_until") or 0) > now_ms()
        return False

    def replace_failed_active(self, canonical: str, is_locked: bool) -> str | None:
        mapping = self.get(canonical)
        if not mapping or not mapping.active or is_locked:
            return None
        rows = {item["canonical"]: item for item in self.storage.learning.symbols()}
        if int(rows.get(canonical, {}).get("error_count", 0)) < config.SYMBOL_ERROR_REPLACE_AFTER:
            return None
        reserves = [
            candidate
            for candidate in self.reserve()
            if all(
                (self.storage.learning.get_profile(candidate.canonical, side, timeframe) or {}).get("ready")
                for timeframe in config.TRADE_TIMEFRAMES
                for side in ("LONG", "SHORT")
            )
        ]
        if not reserves:
            logger.warning("SYMBOL_REPLACE_WAIT | %s | no ready reserve profile", canonical)
            return None
        replacement = reserves[0]
        mapping.active = False
        replacement.active = True
        self.storage.learning.set_symbol_activity(mapping.canonical, False)
        self.storage.learning.set_symbol_activity(replacement.canonical, True)
        logger.warning("SYMBOL_REPLACED | %s -> %s", mapping.canonical, replacement.canonical)
        return replacement.canonical
