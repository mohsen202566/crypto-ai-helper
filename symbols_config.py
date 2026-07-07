from __future__ import annotations

# Optional human-readable symbol registry. The actual runtime list is config.WATCHLIST.
_BASE_SYMBOLS = [
    ("BTC", "core"), ("ETH", "core"), ("SOL", "core"), ("XRP", "core"), ("BNB", "core"),
    ("DOGE", "large"), ("ADA", "large"), ("AVAX", "large"), ("LINK", "large"), ("TRX", "large"),
    ("DOT", "large"), ("LTC", "large"), ("BCH", "large"), ("TON", "trend"), ("SUI", "trend"),
    ("APT", "trend"), ("OP", "trend"), ("ARB", "trend"), ("NEAR", "trend"), ("INJ", "trend"),
    ("ATOM", "stable_alt"), ("FIL", "stable_alt"), ("ETC", "stable_alt"), ("AAVE", "stable_alt"), ("UNI", "stable_alt"),
    ("SEI", "high_volatility"), ("WIF", "high_volatility"), ("ORDI", "high_volatility"), ("PEPE", "high_volatility"), ("JUP", "high_volatility"),
    ("TIA", "trend"), ("WLD", "trend"), ("FET", "trend"), ("IMX", "trend"), ("STX", "trend"),
    ("ICP", "large"), ("MKR", "large"), ("LDO", "large"), ("ENS", "stable_alt"), ("AR", "stable_alt"),
    ("GRT", "stable_alt"), ("ALGO", "stable_alt"), ("XLM", "stable_alt"), ("EOS", "stable_alt"), ("PEOPLE", "high_volatility"),
    ("FLOKI", "high_volatility"), ("BONK", "high_volatility"), ("NOT", "high_volatility"), ("ONDO", "trend"), ("PENDLE", "trend"),
]

WATCHLIST = [
    {
        "name": base,
        "okx_symbol": f"{base}-USDT-SWAP",
        "toobit_symbol": f"{base}USDT",
        "enabled": True,
        "group": group,
    }
    for base, group in _BASE_SYMBOLS
]


def enabled_symbols() -> list[dict[str, object]]:
    return [item for item in WATCHLIST if item.get("enabled") is True]
