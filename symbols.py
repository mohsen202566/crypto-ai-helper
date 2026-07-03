from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSymbol:
    name: str
    okx_inst_id: str
    toobit_symbol: str
    role: str = "main"


MAIN_SYMBOLS: tuple[MarketSymbol, ...] = (
    MarketSymbol("SOL", "SOL-USDT", "SOLUSDT"),
    MarketSymbol("XRP", "XRP-USDT", "XRPUSDT"),
    MarketSymbol("DOGE", "DOGE-USDT", "DOGEUSDT"),
    MarketSymbol("ADA", "ADA-USDT", "ADAUSDT"),
    MarketSymbol("AVAX", "AVAX-USDT", "AVAXUSDT"),
    MarketSymbol("LINK", "LINK-USDT", "LINKUSDT"),
    MarketSymbol("LTC", "LTC-USDT", "LTCUSDT"),
    MarketSymbol("SUI", "SUI-USDT", "SUIUSDT"),
    MarketSymbol("NEAR", "NEAR-USDT", "NEARUSDT"),
    MarketSymbol("APT", "APT-USDT", "APTUSDT"),
    MarketSymbol("ARB", "ARB-USDT", "ARBUSDT"),
    MarketSymbol("OP", "OP-USDT", "OPUSDT"),
    MarketSymbol("DOT", "DOT-USDT", "DOTUSDT"),
    MarketSymbol("ATOM", "ATOM-USDT", "ATOMUSDT"),
    MarketSymbol("FIL", "FIL-USDT", "FILUSDT"),
    MarketSymbol("INJ", "INJ-USDT", "INJUSDT"),
    MarketSymbol("BCH", "BCH-USDT", "BCHUSDT"),
    MarketSymbol("ETC", "ETC-USDT", "ETCUSDT"),
    MarketSymbol("UNI", "UNI-USDT", "UNIUSDT"),
    MarketSymbol("AAVE", "AAVE-USDT", "AAVEUSDT"),
    MarketSymbol("TRX", "TRX-USDT", "TRXUSDT"),
    MarketSymbol("XLM", "XLM-USDT", "XLMUSDT"),
    MarketSymbol("HBAR", "HBAR-USDT", "HBARUSDT"),
    MarketSymbol("ICP", "ICP-USDT", "ICPUSDT"),
    MarketSymbol("ALGO", "ALGO-USDT", "ALGOUSDT"),
    MarketSymbol("SAND", "SAND-USDT", "SANDUSDT"),
    MarketSymbol("MANA", "MANA-USDT", "MANAUSDT"),
    MarketSymbol("WLD", "WLD-USDT", "WLDUSDT"),
    MarketSymbol("ENS", "ENS-USDT", "ENSUSDT"),
    MarketSymbol("LDO", "LDO-USDT", "LDOUSDT"),
)

CONTEXT_SYMBOLS: tuple[MarketSymbol, ...] = (
    MarketSymbol("BTC", "BTC-USDT", "BTCUSDT", "context"),
    MarketSymbol("ETH", "ETH-USDT", "ETHUSDT", "context"),
)

ACTIVE_SYMBOLS = MAIN_SYMBOLS
SYMBOLS = MAIN_SYMBOLS + CONTEXT_SYMBOLS
SYMBOL_BY_NAME = {symbol.name: symbol for symbol in SYMBOLS}
