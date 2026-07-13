"""بیست نماد ثابت و نگاشت OKX/Toobit."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class SymbolMap:
    id: str
    okx: str
    toobit: str
    base: str
    quote: str = "USDT"

SYMBOLS: list[SymbolMap] = [
    SymbolMap("BTC", "BTC-USDT-SWAP", "BTCUSDT", "BTC"),
    SymbolMap("ETH", "ETH-USDT-SWAP", "ETHUSDT", "ETH"),
    SymbolMap("SOL", "SOL-USDT-SWAP", "SOLUSDT", "SOL"),
    SymbolMap("BNB", "BNB-USDT-SWAP", "BNBUSDT", "BNB"),
    SymbolMap("XRP", "XRP-USDT-SWAP", "XRPUSDT", "XRP"),
    SymbolMap("DOGE", "DOGE-USDT-SWAP", "DOGEUSDT", "DOGE"),
    SymbolMap("LINK", "LINK-USDT-SWAP", "LINKUSDT", "LINK"),
    SymbolMap("AVAX", "AVAX-USDT-SWAP", "AVAXUSDT", "AVAX"),
    SymbolMap("SUI", "SUI-USDT-SWAP", "SUIUSDT", "SUI"),
    SymbolMap("ADA", "ADA-USDT-SWAP", "ADAUSDT", "ADA"),
    SymbolMap("DOT", "DOT-USDT-SWAP", "DOTUSDT", "DOT"),
    SymbolMap("LTC", "LTC-USDT-SWAP", "LTCUSDT", "LTC"),
    SymbolMap("BCH", "BCH-USDT-SWAP", "BCHUSDT", "BCH"),
    SymbolMap("TRX", "TRX-USDT-SWAP", "TRXUSDT", "TRX"),
    SymbolMap("TON", "TON-USDT-SWAP", "TONUSDT", "TON"),
    SymbolMap("NEAR", "NEAR-USDT-SWAP", "NEARUSDT", "NEAR"),
    SymbolMap("APT", "APT-USDT-SWAP", "APTUSDT", "APT"),
    SymbolMap("ARB", "ARB-USDT-SWAP", "ARBUSDT", "ARB"),
    SymbolMap("OP", "OP-USDT-SWAP", "OPUSDT", "OP"),
    SymbolMap("PEPE", "PEPE-USDT-SWAP", "PEPEUSDT", "PEPE"),
]
BY_ID = {s.id: s for s in SYMBOLS}
def get_symbol(symbol_id: str) -> SymbolMap | None:
    return BY_ID.get(symbol_id.upper())
