from __future__ import annotations

"""
03 - symbol_mapper.py

Production-ready symbol normalization layer for the locked Movement Hunter bot.

Responsibilities:
- Be the ONLY place that converts user/AI/internal symbols to exchange symbols.
- Normalize symbols for OKX public market data.
- Normalize symbols for Toobit v2 USDT-M futures real trading.
- Handle special leveraged-token-like contract names such as 1000SHIB, 1000PEPE, 1000FLOKI.
- Prevent repeated "symbol not found" and wrong-symbol trade bugs.

Strictly forbidden in this file:
- No API calls.
- No market analysis.
- No AI decisions.
- No trading logic.
- No Telegram handlers.
- No persistence logic.
- No Paper mode.
- No Setup flow.

Architecture lock:
- Every module must call this mapper before using symbols.
- No other file is allowed to invent Toobit/OKX symbol formats.
"""

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from config import SETTINGS


QUOTE_ASSETS: Tuple[str, ...] = ("USDT", "USDC", "USD")
DEFAULT_QUOTE = "USDT"


class SymbolMapperError(ValueError):
    """Raised when a symbol cannot be normalized safely."""


@dataclass(frozen=True)
class SymbolInfo:
    raw: str
    base: str
    quote: str
    internal: str
    toobit: str
    okx: str
    display: str
    aliases: Tuple[str, ...]
    is_supported: bool = True
    note: str = ""


# Explicit symbols that often break when blindly normalized.
# Key is canonical internal symbol.
SPECIAL_SYMBOLS: Dict[str, Dict[str, str]] = {
    "1000SHIBUSDT": {
        "base": "1000SHIB",
        "quote": "USDT",
        "toobit": "1000SHIB-SWAP-USDT",
        "okx": "SHIB-USDT-SWAP",
        "display": "1000SHIBUSDT",
        "note": "Toobit may list this contract as 1000SHIB; OKX public data may use SHIB-USDT-SWAP.",
    },
    "1000PEPEUSDT": {
        "base": "1000PEPE",
        "quote": "USDT",
        "toobit": "1000PEPE-SWAP-USDT",
        "okx": "PEPE-USDT-SWAP",
        "display": "1000PEPEUSDT",
        "note": "Toobit may list this contract as 1000PEPE; OKX public data may use PEPE-USDT-SWAP.",
    },
    "1000FLOKIUSDT": {
        "base": "1000FLOKI",
        "quote": "USDT",
        "toobit": "1000FLOKI-SWAP-USDT",
        "okx": "FLOKI-USDT-SWAP",
        "display": "1000FLOKIUSDT",
        "note": "Toobit may list this contract as 1000FLOKI; OKX public data may use FLOKI-USDT-SWAP.",
    },
}


# Human/Persian/common aliases.
ALIASES: Dict[str, str] = {
    "BTC": "BTCUSDT",
    "BITCOIN": "BTCUSDT",
    "بیتکوین": "BTCUSDT",
    "بیت کوین": "BTCUSDT",

    "ETH": "ETHUSDT",
    "ETHEREUM": "ETHUSDT",
    "اتریوم": "ETHUSDT",

    "BNB": "BNBUSDT",
    "SOL": "SOLUSDT",
    "سولانا": "SOLUSDT",

    "XRP": "XRPUSDT",
    "ریپل": "XRPUSDT",

    "DOGE": "DOGEUSDT",
    "DOGECOIN": "DOGEUSDT",
    "داج": "DOGEUSDT",
    "داج کوین": "DOGEUSDT",

    "ADA": "ADAUSDT",
    "کاردانو": "ADAUSDT",

    "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT",
    "MATIC": "MATICUSDT",
    "POL": "POLUSDT",
    "TRX": "TRXUSDT",
    "DOT": "DOTUSDT",
    "LTC": "LTCUSDT",
    "ATOM": "ATOMUSDT",
    "NEAR": "NEARUSDT",
    "ARB": "ARBUSDT",
    "OP": "OPUSDT",
    "FIL": "FILUSDT",
    "APT": "APTUSDT",
    "SUI": "SUIUSDT",

    "SHIB": "1000SHIBUSDT",
    "1000SHIB": "1000SHIBUSDT",
    "SHIB1000": "1000SHIBUSDT",
    "شیبا": "1000SHIBUSDT",

    "PEPE": "1000PEPEUSDT",
    "1000PEPE": "1000PEPEUSDT",
    "PEPE1000": "1000PEPEUSDT",
    "پپه": "1000PEPEUSDT",

    "FLOKI": "1000FLOKIUSDT",
    "1000FLOKI": "1000FLOKIUSDT",
    "FLOKI1000": "1000FLOKIUSDT",
    "فلوکی": "1000FLOKIUSDT",
}


def _clean_symbol(raw: str) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    s = s.replace("‌", " ")  # Persian zero-width non-joiner
    s = re.sub(r"\s+", " ", s)
    if s in ALIASES:
        return ALIASES[s]
    upper = s.upper()
    upper = upper.replace("/", "-").replace("_", "-").replace(" ", "")
    return ALIASES.get(upper, upper)


def _strip_swap_variants(symbol: str) -> str:
    s = symbol.upper().replace("/", "-").replace("_", "-")
    if "-SWAP-" in s:
        base, quote = s.split("-SWAP-", 1)
        return f"{base}{quote}"
    if s.endswith("-SWAP"):
        parts = [p for p in s.split("-") if p and p != "SWAP"]
        if len(parts) >= 2:
            return f"{parts[0]}{parts[1]}"
    return s.replace("-", "")


def _split_base_quote(symbol: str) -> Tuple[str, str]:
    s = _strip_swap_variants(symbol)
    for quote in QUOTE_ASSETS:
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)], quote
    return s, DEFAULT_QUOTE


def _to_internal(raw: str) -> str:
    s = _clean_symbol(raw)
    if not s:
        raise SymbolMapperError("empty_symbol")

    s = _strip_swap_variants(s)
    s = ALIASES.get(s, s)

    if s in SPECIAL_SYMBOLS:
        return s

    base, quote = _split_base_quote(s)
    if not base:
        raise SymbolMapperError(f"invalid_symbol:{raw}")
    return f"{base}{quote}"


def _to_toobit(base: str, quote: str) -> str:
    return f"{base}-SWAP-{quote}"


def _to_okx(base: str, quote: str) -> str:
    okx_base = base
    if base.startswith("1000"):
        # OKX usually lists the underlying spot/swap asset without Toobit's 1000 multiplier.
        okx_base = base[4:]
    return f"{okx_base}-{quote}-SWAP"


def _aliases_for_internal(internal: str) -> Tuple[str, ...]:
    values: Set[str] = {internal}
    base, quote = _split_base_quote(internal)
    values.add(base)
    values.add(f"{base}{quote}")
    values.add(f"{base}-{quote}")
    values.add(f"{base}-SWAP-{quote}")
    values.add(f"{base}-{quote}-SWAP")

    for alias, target in ALIASES.items():
        if target == internal:
            values.add(alias)

    return tuple(sorted(values))


class SymbolMapper:
    """
    Canonical symbol mapper.

    Internal symbol format:
        BTCUSDT
        DOGEUSDT
        1000SHIBUSDT

    Toobit v2 futures symbol format:
        BTC-SWAP-USDT
        DOGE-SWAP-USDT
        1000SHIB-SWAP-USDT

    OKX public swap symbol format:
        BTC-USDT-SWAP
        DOGE-USDT-SWAP
        SHIB-USDT-SWAP
    """

    def __init__(self, supported_symbols: Optional[Sequence[str]] = None):
        self.supported_internal: Set[str] = set()
        for symbol in supported_symbols or SETTINGS.market_data.scan_symbols:
            try:
                self.supported_internal.add(_to_internal(symbol))
            except SymbolMapperError:
                continue

    def normalize(self, raw: str, require_supported: bool = False) -> SymbolInfo:
        internal = _to_internal(raw)

        if internal in SPECIAL_SYMBOLS:
            special = SPECIAL_SYMBOLS[internal]
            info = SymbolInfo(
                raw=str(raw),
                base=special["base"],
                quote=special["quote"],
                internal=internal,
                toobit=special["toobit"],
                okx=special["okx"],
                display=special["display"],
                aliases=_aliases_for_internal(internal),
                is_supported=self.is_supported(internal),
                note=special.get("note", ""),
            )
        else:
            base, quote = _split_base_quote(internal)
            info = SymbolInfo(
                raw=str(raw),
                base=base,
                quote=quote,
                internal=internal,
                toobit=_to_toobit(base, quote),
                okx=_to_okx(base, quote),
                display=internal,
                aliases=_aliases_for_internal(internal),
                is_supported=self.is_supported(internal),
                note="",
            )

        if require_supported and not info.is_supported:
            raise SymbolMapperError(f"unsupported_symbol:{raw}->{info.internal}")
        return info

    def internal(self, raw: str, require_supported: bool = False) -> str:
        return self.normalize(raw, require_supported=require_supported).internal

    def toobit(self, raw: str, require_supported: bool = False) -> str:
        return self.normalize(raw, require_supported=require_supported).toobit

    def okx(self, raw: str, require_supported: bool = False) -> str:
        return self.normalize(raw, require_supported=require_supported).okx

    def display(self, raw: str, require_supported: bool = False) -> str:
        return self.normalize(raw, require_supported=require_supported).display

    def is_supported(self, raw: str) -> bool:
        try:
            internal = _to_internal(raw)
        except SymbolMapperError:
            return False
        if not self.supported_internal:
            return True
        return internal in self.supported_internal

    def add_supported(self, raw: str) -> str:
        internal = _to_internal(raw)
        self.supported_internal.add(internal)
        return internal

    def remove_supported(self, raw: str) -> bool:
        internal = _to_internal(raw)
        if internal in self.supported_internal:
            self.supported_internal.remove(internal)
            return True
        return False

    def all_supported(self) -> List[str]:
        return sorted(self.supported_internal)

    def all_infos(self) -> List[SymbolInfo]:
        return [self.normalize(symbol) for symbol in self.all_supported()]

    def parse_many(self, symbols: Iterable[str], require_supported: bool = False) -> List[SymbolInfo]:
        infos: List[SymbolInfo] = []
        seen: Set[str] = set()
        for raw in symbols:
            info = self.normalize(raw, require_supported=require_supported)
            if info.internal in seen:
                continue
            seen.add(info.internal)
            infos.append(info)
        return infos

    def match_user_text(self, text: str) -> Optional[SymbolInfo]:
        """
        Find a symbol inside free-form Persian/English user text.

        This is used by bot.py command router. It does not decide direction or trade.
        """
        if not text:
            return None

        normalized_text = str(text).strip()
        compact_upper = normalized_text.upper().replace("/", "-").replace("_", "-")
        compact_no_space = compact_upper.replace(" ", "")

        # First exact alias search, including Persian aliases.
        for alias, internal in ALIASES.items():
            if alias in normalized_text or alias.upper() in compact_upper or alias.upper() in compact_no_space:
                try:
                    return self.normalize(internal)
                except SymbolMapperError:
                    continue

        # Then scan supported symbols/base names.
        candidates = sorted(self.supported_internal, key=len, reverse=True)
        for internal in candidates:
            info = self.normalize(internal)
            for alias in info.aliases:
                alias_u = alias.upper()
                if alias_u and (alias_u in compact_upper or alias_u in compact_no_space):
                    return info

        # Final fallback: detect common Binance-like symbol pattern.
        match = re.search(r"\b([0-9A-Z]{2,20}(?:USDT|USDC|USD))\b", compact_no_space)
        if match:
            try:
                return self.normalize(match.group(1))
            except SymbolMapperError:
                return None
        return None

    def validate_no_duplicates(self) -> List[str]:
        """
        Return duplicate exchange symbols if any internal symbols map to same Toobit symbol.
        This protects real trading from symbol collisions.
        """
        seen: Dict[str, str] = {}
        duplicates: List[str] = []
        for internal in self.supported_internal:
            info = self.normalize(internal)
            if info.toobit in seen and seen[info.toobit] != internal:
                duplicates.append(f"{seen[info.toobit]} and {internal} -> {info.toobit}")
            seen[info.toobit] = internal
        return duplicates

    def safe_summary(self) -> Dict[str, object]:
        return {
            "supported_count": len(self.supported_internal),
            "supported": self.all_supported(),
            "toobit_symbols": {s: self.toobit(s) for s in self.all_supported()},
            "okx_symbols": {s: self.okx(s) for s in self.all_supported()},
            "duplicates": self.validate_no_duplicates(),
        }


_default_mapper: Optional[SymbolMapper] = None


def mapper() -> SymbolMapper:
    global _default_mapper
    if _default_mapper is None:
        _default_mapper = SymbolMapper()
    return _default_mapper


def normalize_symbol(raw: str, require_supported: bool = False) -> str:
    return mapper().internal(raw, require_supported=require_supported)


def toobit_symbol(raw: str, require_supported: bool = False) -> str:
    return mapper().toobit(raw, require_supported=require_supported)


def okx_symbol(raw: str, require_supported: bool = False) -> str:
    return mapper().okx(raw, require_supported=require_supported)


def display_symbol(raw: str, require_supported: bool = False) -> str:
    return mapper().display(raw, require_supported=require_supported)


def symbol_info(raw: str, require_supported: bool = False) -> SymbolInfo:
    return mapper().normalize(raw, require_supported=require_supported)


def is_supported_symbol(raw: str) -> bool:
    return mapper().is_supported(raw)


def match_symbol_from_text(text: str) -> Optional[SymbolInfo]:
    return mapper().match_user_text(text)


def validate_symbol_mapping() -> List[str]:
    return mapper().validate_no_duplicates()


# Backward-compatible aliases for older code during migration.
normalize_pair = normalize_symbol
format_toobit_symbol = toobit_symbol
to_okx_inst_id = okx_symbol
