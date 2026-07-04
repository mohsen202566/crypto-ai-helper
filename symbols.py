from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSymbol:
    name: str
    okx_inst_id: str
    toobit_symbol: str
    role: str = "main"


MAIN_SYMBOLS: tuple[MarketSymbol, ...] = (
    MarketSymbol("SOL", "SOL-USDT-SWAP", "SOL-SWAP-USDT"),
    MarketSymbol("XRP", "XRP-USDT-SWAP", "XRP-SWAP-USDT"),
    MarketSymbol("DOGE", "DOGE-USDT-SWAP", "DOGE-SWAP-USDT"),
    MarketSymbol("ADA", "ADA-USDT-SWAP", "ADA-SWAP-USDT"),
    MarketSymbol("AVAX", "AVAX-USDT-SWAP", "AVAX-SWAP-USDT"),
    MarketSymbol("LINK", "LINK-USDT-SWAP", "LINK-SWAP-USDT"),
    MarketSymbol("LTC", "LTC-USDT-SWAP", "LTC-SWAP-USDT"),
    MarketSymbol("SUI", "SUI-USDT-SWAP", "SUI-SWAP-USDT"),
    MarketSymbol("NEAR", "NEAR-USDT-SWAP", "NEAR-SWAP-USDT"),
    MarketSymbol("APT", "APT-USDT-SWAP", "APT-SWAP-USDT"),
    MarketSymbol("ARB", "ARB-USDT-SWAP", "ARB-SWAP-USDT"),
    MarketSymbol("OP", "OP-USDT-SWAP", "OP-SWAP-USDT"),
    MarketSymbol("DOT", "DOT-USDT-SWAP", "DOT-SWAP-USDT"),
    MarketSymbol("ATOM", "ATOM-USDT-SWAP", "ATOM-SWAP-USDT"),
    MarketSymbol("FIL", "FIL-USDT-SWAP", "FIL-SWAP-USDT"),
    MarketSymbol("INJ", "INJ-USDT-SWAP", "INJ-SWAP-USDT"),
    MarketSymbol("BCH", "BCH-USDT-SWAP", "BCH-SWAP-USDT"),
    MarketSymbol("ETC", "ETC-USDT-SWAP", "ETC-SWAP-USDT"),
    MarketSymbol("UNI", "UNI-USDT-SWAP", "UNI-SWAP-USDT"),
    MarketSymbol("AAVE", "AAVE-USDT-SWAP", "AAVE-SWAP-USDT"),
    MarketSymbol("TRX", "TRX-USDT-SWAP", "TRX-SWAP-USDT"),
    MarketSymbol("XLM", "XLM-USDT-SWAP", "XLM-SWAP-USDT"),
    MarketSymbol("HBAR", "HBAR-USDT-SWAP", "HBAR-SWAP-USDT"),
    MarketSymbol("ICP", "ICP-USDT-SWAP", "ICP-SWAP-USDT"),
    MarketSymbol("ALGO", "ALGO-USDT-SWAP", "ALGO-SWAP-USDT"),
    MarketSymbol("SAND", "SAND-USDT-SWAP", "SAND-SWAP-USDT"),
    MarketSymbol("MANA", "MANA-USDT-SWAP", "MANA-SWAP-USDT"),
    MarketSymbol("WLD", "WLD-USDT-SWAP", "WLD-SWAP-USDT"),
    MarketSymbol("ENS", "ENS-USDT-SWAP", "ENS-SWAP-USDT"),
    MarketSymbol("LDO", "LDO-USDT-SWAP", "LDO-SWAP-USDT"),
)

CONTEXT_SYMBOLS: tuple[MarketSymbol, ...] = (
    MarketSymbol("BTC", "BTC-USDT-SWAP", "BTC-SWAP-USDT", "context"),
    MarketSymbol("ETH", "ETH-USDT-SWAP", "ETH-SWAP-USDT", "context"),
)

ACTIVE_SYMBOLS = MAIN_SYMBOLS
SYMBOLS = MAIN_SYMBOLS + CONTEXT_SYMBOLS
SYMBOL_BY_NAME = {symbol.name: symbol for symbol in SYMBOLS}
