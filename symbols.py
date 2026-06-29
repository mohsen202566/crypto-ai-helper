from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSymbol:
    name: str
    okx_inst_id: str
    toobit_symbol: str
    role: str = "main"


# 12 volatile/liquid enough, but still reasonably predictable for 1H crypto futures.
MAIN_SYMBOLS: tuple[MarketSymbol, ...] = (
    MarketSymbol("SOL", "SOL-USDT-SWAP", "SOL-SWAP-USDT", "main"),
    MarketSymbol("XRP", "XRP-USDT-SWAP", "XRP-SWAP-USDT", "main"),
    MarketSymbol("DOGE", "DOGE-USDT-SWAP", "DOGE-SWAP-USDT", "main"),
    MarketSymbol("AVAX", "AVAX-USDT-SWAP", "AVAX-SWAP-USDT", "main"),
    MarketSymbol("LINK", "LINK-USDT-SWAP", "LINK-SWAP-USDT", "main"),
    MarketSymbol("ADA", "ADA-USDT-SWAP", "ADA-SWAP-USDT", "main"),
    MarketSymbol("SUI", "SUI-USDT-SWAP", "SUI-SWAP-USDT", "main"),
    MarketSymbol("LTC", "LTC-USDT-SWAP", "LTC-SWAP-USDT", "main"),
    MarketSymbol("NEAR", "NEAR-USDT-SWAP", "NEAR-SWAP-USDT", "main"),
    MarketSymbol("APT", "APT-USDT-SWAP", "APT-SWAP-USDT", "main"),
    MarketSymbol("ARB", "ARB-USDT-SWAP", "ARB-SWAP-USDT", "main"),
    MarketSymbol("OP", "OP-USDT-SWAP", "OP-SWAP-USDT", "main"),
)

# Not active by default. AI can compare/replace after enough learning if you later enable replacement logic.
ALTERNATIVE_SYMBOLS: tuple[MarketSymbol, ...] = (
    MarketSymbol("INJ", "INJ-USDT-SWAP", "INJ-SWAP-USDT", "alternative"),
    MarketSymbol("ATOM", "ATOM-USDT-SWAP", "ATOM-SWAP-USDT", "alternative"),
    MarketSymbol("DOT", "DOT-USDT-SWAP", "DOT-SWAP-USDT", "alternative"),
    MarketSymbol("FIL", "FIL-USDT-SWAP", "FIL-SWAP-USDT", "alternative"),
)

SYMBOLS: tuple[MarketSymbol, ...] = MAIN_SYMBOLS + ALTERNATIVE_SYMBOLS
ACTIVE_SYMBOLS: tuple[MarketSymbol, ...] = MAIN_SYMBOLS
SYMBOL_BY_NAME = {symbol.name: symbol for symbol in SYMBOLS}
