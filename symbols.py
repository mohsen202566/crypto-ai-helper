"""لیست نمادها و مپ OKX/Toobit.
ربات با id داخلی کار می‌کند تا خطای نام نماد باعث کرش نشود.
"""
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
    SymbolMap("ADA", "ADA-USDT-SWAP", "ADAUSDT", "ADA"),
    SymbolMap("LINK", "LINK-USDT-SWAP", "LINKUSDT", "LINK"),
    SymbolMap("AVAX", "AVAX-USDT-SWAP", "AVAXUSDT", "AVAX"),
    SymbolMap("SUI", "SUI-USDT-SWAP", "SUIUSDT", "SUI"),
    SymbolMap("TON", "TON-USDT-SWAP", "TONUSDT", "TON"),
    SymbolMap("TRX", "TRX-USDT-SWAP", "TRXUSDT", "TRX"),
    SymbolMap("LTC", "LTC-USDT-SWAP", "LTCUSDT", "LTC"),
    SymbolMap("BCH", "BCH-USDT-SWAP", "BCHUSDT", "BCH"),
    SymbolMap("DOT", "DOT-USDT-SWAP", "DOTUSDT", "DOT"),
    SymbolMap("APT", "APT-USDT-SWAP", "APTUSDT", "APT"),
    SymbolMap("ARB", "ARB-USDT-SWAP", "ARBUSDT", "ARB"),
    SymbolMap("OP", "OP-USDT-SWAP", "OPUSDT", "OP"),
    SymbolMap("SEI", "SEI-USDT-SWAP", "SEIUSDT", "SEI"),
    SymbolMap("NEAR", "NEAR-USDT-SWAP", "NEARUSDT", "NEAR"),
    SymbolMap("ATOM", "ATOM-USDT-SWAP", "ATOMUSDT", "ATOM"),
    SymbolMap("FIL", "FIL-USDT-SWAP", "FILUSDT", "FIL"),
    SymbolMap("ETC", "ETC-USDT-SWAP", "ETCUSDT", "ETC"),
    SymbolMap("INJ", "INJ-USDT-SWAP", "INJUSDT", "INJ"),
    SymbolMap("UNI", "UNI-USDT-SWAP", "UNIUSDT", "UNI"),
    SymbolMap("HBAR", "HBAR-USDT-SWAP", "HBARUSDT", "HBAR"),
    SymbolMap("ICP", "ICP-USDT-SWAP", "ICPUSDT", "ICP"),
    SymbolMap("PEPE", "PEPE-USDT-SWAP", "PEPEUSDT", "PEPE"),
    SymbolMap("WIF", "WIF-USDT-SWAP", "WIFUSDT", "WIF"),
    SymbolMap("FET", "FET-USDT-SWAP", "FETUSDT", "FET"),
]

BY_ID = {s.id: s for s in SYMBOLS}
BY_OKX = {s.okx: s for s in SYMBOLS}
BY_TOOBIT = {s.toobit: s for s in SYMBOLS}

def get_symbol(symbol_id: str) -> SymbolMap | None:
    return BY_ID.get(symbol_id.upper())
