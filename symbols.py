from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSymbol:
    name: str
    okx_inst_id: str
    toobit_symbol: str


SYMBOLS: tuple[MarketSymbol, ...] = (
    MarketSymbol("SOL", "SOL-USDT-SWAP", "SOL-SWAP-USDT"),
    MarketSymbol("XRP", "XRP-USDT-SWAP", "XRP-SWAP-USDT"),
    MarketSymbol("DOGE", "DOGE-USDT-SWAP", "DOGE-SWAP-USDT"),
    MarketSymbol("ADA", "ADA-USDT-SWAP", "ADA-SWAP-USDT"),
    MarketSymbol("LTC", "LTC-USDT-SWAP", "LTC-SWAP-USDT"),
    MarketSymbol("BCH", "BCH-USDT-SWAP", "BCH-SWAP-USDT"),
    MarketSymbol("LINK", "LINK-USDT-SWAP", "LINK-SWAP-USDT"),
    MarketSymbol("AVAX", "AVAX-USDT-SWAP", "AVAX-SWAP-USDT"),
    MarketSymbol("DOT", "DOT-USDT-SWAP", "DOT-SWAP-USDT"),
    MarketSymbol("TRX", "TRX-USDT-SWAP", "TRX-SWAP-USDT"),
    MarketSymbol("SUI", "SUI-USDT-SWAP", "SUI-SWAP-USDT"),
    MarketSymbol("NEAR", "NEAR-USDT-SWAP", "NEAR-SWAP-USDT"),
    MarketSymbol("APT", "APT-USDT-SWAP", "APT-SWAP-USDT"),
    MarketSymbol("INJ", "INJ-USDT-SWAP", "INJ-SWAP-USDT"),
)
