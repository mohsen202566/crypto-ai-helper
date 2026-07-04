from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSymbol:
    name: str
    okx_inst_id: str
    toobit_symbol: str
    role: str = "main"


# Reduced to 10 liquid futures symbols. BTC/ETH also remain context symbols in config.
MAIN_SYMBOLS: tuple[MarketSymbol, ...] = (
    MarketSymbol("BTC", "BTC-USDT-SWAP", "BTC-SWAP-USDT"),
    MarketSymbol("ETH", "ETH-USDT-SWAP", "ETH-SWAP-USDT"),
    MarketSymbol("SOL", "SOL-USDT-SWAP", "SOL-SWAP-USDT"),
    MarketSymbol("XRP", "XRP-USDT-SWAP", "XRP-SWAP-USDT"),
    MarketSymbol("ADA", "ADA-USDT-SWAP", "ADA-SWAP-USDT"),
    MarketSymbol("AVAX", "AVAX-USDT-SWAP", "AVAX-SWAP-USDT"),
    MarketSymbol("LINK", "LINK-USDT-SWAP", "LINK-SWAP-USDT"),
    MarketSymbol("LTC", "LTC-USDT-SWAP", "LTC-SWAP-USDT"),
    MarketSymbol("DOT", "DOT-USDT-SWAP", "DOT-SWAP-USDT"),
    MarketSymbol("BCH", "BCH-USDT-SWAP", "BCH-SWAP-USDT"),
)

CONTEXT_SYMBOLS: tuple[MarketSymbol, ...] = (
    MarketSymbol("BTC", "BTC-USDT-SWAP", "BTC-SWAP-USDT", "context"),
    MarketSymbol("ETH", "ETH-USDT-SWAP", "ETH-SWAP-USDT", "context"),
)

ACTIVE_SYMBOLS = MAIN_SYMBOLS
SYMBOLS = CONTEXT_SYMBOLS + MAIN_SYMBOLS
SYMBOL_BY_NAME = {symbol.name: symbol for symbol in SYMBOLS}
