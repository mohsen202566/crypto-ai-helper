"""اعتبارسنجی ۱۰۰ نماد مشترک و انتخاب ۳۵ ارز فعال."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import requests

import config
from models import SymbolMapping
from storage import Storage
from utils import alias_candidates, canonical_base_from_symbol, clamp, extract_filter, normalize_symbol, now_ms, safe_float

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
    def _request_json(session: requests.Session, url: str, params: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(config.HTTP_RETRIES + 1):
            try:
                res = session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
                res.raise_for_status()
                payload = res.json()
                if not isinstance(payload, dict):
                    raise SymbolRegistryError("invalid JSON shape")
                return payload
            except Exception as exc:
                last = exc
                if attempt < config.HTTP_RETRIES:
                    time.sleep(config.HTTP_BACKOFF_SECONDS * (attempt + 1))
        raise SymbolRegistryError(str(last))

    def _okx_instruments(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        payload = self._request_json(
            self.session,
            f"{config.OKX_BASE_URL}/api/v5/public/instruments",
            {"instType": "SWAP"},
        )
        if str(payload.get("code", "0")) != "0":
            raise SymbolRegistryError(f"OKX instruments: {payload.get('msg')}")
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
            payload = self._request_json(self.session, f"{config.BYBIT_BASE_URL}/v5/market/instruments-info", params)
            if int(payload.get("retCode", -1)) != 0:
                raise SymbolRegistryError(f"Bybit instruments: {payload.get('retMsg')}")
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

    def _okx_liquidity(self) -> dict[str, float]:
        payload = self._request_json(
            self.session,
            f"{config.OKX_BASE_URL}/api/v5/market/tickers",
            {"instType": "SWAP"},
        )
        scores: dict[str, float] = {}
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
        if not raw:
            return scores
        max_turnover = max(x[1] for x in raw) or 1.0
        for base, turnover, spread, opportunity in raw:
            volume_score = clamp((turnover / max_turnover) ** 0.35, 0.0, 1.0)
            spread_score = clamp(1.0 - spread / 0.004, 0.0, 1.0)
            opportunity_score = clamp(opportunity / 0.12, 0.0, 1.0)
            scores[base] = 100.0 * (0.55 * volume_score + 0.30 * spread_score + 0.15 * opportunity_score)
        return scores

    @staticmethod
    def _find_alias(base: str, exchange: str, by_symbol: dict[str, dict[str, Any]], by_base: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
        overrides = config.SYMBOL_ALIAS_OVERRIDES.get(base, {}).get(exchange, ())
        candidates = tuple(overrides) + alias_candidates(base, exchange)
        normalized_map = {normalize_symbol(k): (k, v) for k, v in by_symbol.items()}
        for alias in candidates:
            direct = by_symbol.get(alias.upper())
            if direct is not None:
                return alias.upper(), direct
            found = normalized_map.get(normalize_symbol(alias))
            if found:
                return found
        item = by_base.get(base)
        if item:
            if exchange == "okx":
                key = str(item.get("instId", ""))
            else:
                key = str(item.get("symbol") or item.get("symbolId") or "")
            if key:
                return key.upper(), item
        return None

    def validate_universe(self, progress: Callable[[str], None] | None = None) -> list[SymbolMapping]:
        progress = progress or (lambda _msg: None)
        try:
            progress("دریافت نمادهای OKX")
            okx_symbols, okx_base = self._okx_instruments()
            progress("دریافت نمادهای Bybit")
            bybit_symbols, bybit_base = self._bybit_instruments()
            progress("دریافت نمادهای Toobit")
            toobit_symbols = self.toobit.get_exchange_symbols()
            toobit_base: dict[str, dict[str, Any]] = {}
            for sym, item in toobit_symbols.items():
                base = canonical_base_from_symbol(sym)
                # Never equate 1000TOKEN with TOKEN.
                if base.startswith("1000"):
                    continue
                toobit_base.setdefault(base, item)
            liquidity = self._okx_liquidity()
        except Exception as exc:
            persisted = self.storage.learning.symbols(valid=True)
            if len(persisted) >= config.UNIVERSE_SIZE:
                logger.warning("اعتبارسنجی آنلاین نمادها ناموفق بود؛ Registry ذخیره‌شده استفاده شد: %s", exc)
                self._mappings = {m["canonical"]: SymbolMapping(**{k: v for k, v in m.items() if k in SymbolMapping.__dataclass_fields__}) for m in persisted[: config.UNIVERSE_SIZE]}
                return list(self._mappings.values())
            raise SymbolRegistryError(f"symbol validation failed and no complete cache exists: {exc}") from exc

        valid: list[SymbolMapping] = []
        for base in config.CANDIDATE_BASE_ASSETS:
            okx = self._find_alias(base, "okx", okx_symbols, okx_base)
            bybit = self._find_alias(base, "bybit", bybit_symbols, bybit_base)
            toobit = self._find_alias(base, "toobit", toobit_symbols, toobit_base)
            if not (okx and bybit and toobit):
                continue
            okx_symbol, okx_info = okx
            bybit_symbol, _bybit_info = bybit
            toobit_symbol, toobit_info = toobit
            # Reject multiplier or semantically different contracts. Explicit token
            # rebrands (POL/MATIC, RENDER/RNDR, SONIC/S) are accepted only when every
            # resolved base belongs to the declared equivalence family.
            bases = {
                canonical_base_from_symbol(okx_symbol),
                canonical_base_from_symbol(bybit_symbol),
                canonical_base_from_symbol(toobit_symbol),
            }
            if any(resolved.startswith("1000") for resolved in bases):
                continue
            allowed_bases = config.SYMBOL_EQUIVALENT_BASES.get(base, frozenset({base}))
            if not bases or not bases.issubset(allowed_bases):
                continue
            price_filter = extract_filter(toobit_info, "PRICE_FILTER")
            lot_filter = extract_filter(toobit_info, "LOT_SIZE")
            notional_filter = extract_filter(toobit_info, "MIN_NOTIONAL")
            tick = safe_float(
                toobit_info.get("tickSize")
                or price_filter.get("tickSize")
                or ((toobit_info.get("priceFilter") or {}).get("tickSize") if isinstance(toobit_info.get("priceFilter"), dict) else None)
                or okx_info.get("tickSz")
            )
            qty_step = safe_float(
                toobit_info.get("stepSize")
                or lot_filter.get("stepSize")
                or lot_filter.get("qtyStep")
                or ((toobit_info.get("lotSizeFilter") or {}).get("qtyStep") if isinstance(toobit_info.get("lotSizeFilter"), dict) else None)
            )
            min_qty = safe_float(toobit_info.get("minQty") or lot_filter.get("minQty") or toobit_info.get("minTradeQty"))
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
            mapping = SymbolMapping(
                canonical=f"{base}USDT",
                base=base,
                okx=okx_symbol,
                bybit=bybit_symbol,
                toobit=toobit_symbol,
                okx_aliases=tuple(config.SYMBOL_ALIAS_OVERRIDES.get(base, {}).get("okx", ())) + alias_candidates(base, "okx"),
                bybit_aliases=tuple(config.SYMBOL_ALIAS_OVERRIDES.get(base, {}).get("bybit", ())) + alias_candidates(base, "bybit"),
                toobit_aliases=tuple(config.SYMBOL_ALIAS_OVERRIDES.get(base, {}).get("toobit", ())) + alias_candidates(base, "toobit"),
                tick_size=tick,
                quantity_step=qty_step,
                min_qty=min_qty,
                min_notional=min_notional,
                contract_multiplier=contract_multiplier,
                liquidity_score=liquidity.get(base, 0.0),
                active=False,
                valid=True,
            )
            valid.append(mapping)

        valid.sort(key=lambda x: (x.liquidity_score, x.base in {"BTC", "ETH"}), reverse=True)
        if len(valid) < config.UNIVERSE_SIZE:
            raise SymbolRegistryError(f"فقط {len(valid)} نماد مشترک سالم پیدا شد؛ حداقل {config.UNIVERSE_SIZE} لازم است")
        universe = valid[: config.UNIVERSE_SIZE]
        # Preserve active set when possible; otherwise select top 35.
        previous_active = {m["canonical"] for m in self.storage.learning.symbols(active=True, valid=True)}
        active_selected: list[SymbolMapping] = []
        for m in universe:
            if m.canonical in previous_active and len(active_selected) < config.ACTIVE_SYMBOLS:
                m.active = True
                active_selected.append(m)
        for m in universe:
            if len(active_selected) >= config.ACTIVE_SYMBOLS:
                break
            if not m.active:
                m.active = True
                active_selected.append(m)

        for mapping in universe:
            self.storage.learning.upsert_symbol(mapping.to_dict())
        # Any old symbol outside the newly validated universe is inactive.
        new_keys = {m.canonical for m in universe}
        for old in self.storage.learning.symbols():
            if old.get("canonical") not in new_keys:
                self.storage.learning.set_symbol_activity(old["canonical"], False)

        self._mappings = {m.canonical: m for m in universe}
        self.storage.runtime.set_setting("active_symbols_count", sum(1 for m in universe if m.active))
        self.storage.runtime.set_setting("reserve_symbols_count", sum(1 for m in universe if not m.active))
        progress(f"نمادها آماده: {len(universe)} کل، {sum(1 for m in universe if m.active)} فعال")
        return universe

    def load(self) -> list[SymbolMapping]:
        rows = self.storage.learning.symbols(valid=True)
        self._mappings = {}
        for row in rows[: config.UNIVERSE_SIZE]:
            clean = {k: row.get(k) for k in SymbolMapping.__dataclass_fields__}
            for key in ("okx_aliases", "bybit_aliases", "toobit_aliases"):
                clean[key] = tuple(clean.get(key) or ())
            self._mappings[row["canonical"]] = SymbolMapping(**clean)
        return list(self._mappings.values())

    def get(self, canonical: str) -> SymbolMapping | None:
        return self._mappings.get(canonical)

    def active(self) -> list[SymbolMapping]:
        return sorted((m for m in self._mappings.values() if m.active and m.valid), key=lambda m: m.liquidity_score, reverse=True)

    def reserve(self) -> list[SymbolMapping]:
        return sorted((m for m in self._mappings.values() if not m.active and m.valid), key=lambda m: m.liquidity_score, reverse=True)

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
        rows = {x["canonical"]: x for x in self.storage.learning.symbols()}
        if int(rows.get(canonical, {}).get("error_count", 0)) < config.SYMBOL_ERROR_REPLACE_AFTER:
            return None
        reserves = [
            candidate for candidate in self.reserve()
            if all(
                (self.storage.learning.get_profile(candidate.canonical, side, tf) or {}).get("ready")
                for tf in config.TRADE_TIMEFRAMES for side in ("LONG", "SHORT")
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
