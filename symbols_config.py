from __future__ import annotations

WATCHLIST = [
    {"name": "BTC", "okx_symbol": "BTC-USDT-SWAP", "toobit_symbol": "BTCUSDT", "enabled": True, "group": "core"},
    {"name": "ETH", "okx_symbol": "ETH-USDT-SWAP", "toobit_symbol": "ETHUSDT", "enabled": True, "group": "core"},
    {"name": "SOL", "okx_symbol": "SOL-USDT-SWAP", "toobit_symbol": "SOLUSDT", "enabled": True, "group": "core"},
    {"name": "XRP", "okx_symbol": "XRP-USDT-SWAP", "toobit_symbol": "XRPUSDT", "enabled": True, "group": "core"},
    {"name": "BNB", "okx_symbol": "BNB-USDT-SWAP", "toobit_symbol": "BNBUSDT", "enabled": True, "group": "core"},
    {"name": "DOGE", "okx_symbol": "DOGE-USDT-SWAP", "toobit_symbol": "DOGEUSDT", "enabled": True, "group": "large"},
    {"name": "ADA", "okx_symbol": "ADA-USDT-SWAP", "toobit_symbol": "ADAUSDT", "enabled": True, "group": "large"},
    {"name": "AVAX", "okx_symbol": "AVAX-USDT-SWAP", "toobit_symbol": "AVAXUSDT", "enabled": True, "group": "large"},
    {"name": "LINK", "okx_symbol": "LINK-USDT-SWAP", "toobit_symbol": "LINKUSDT", "enabled": True, "group": "large"},
    {"name": "TRX", "okx_symbol": "TRX-USDT-SWAP", "toobit_symbol": "TRXUSDT", "enabled": True, "group": "large"},
    {"name": "DOT", "okx_symbol": "DOT-USDT-SWAP", "toobit_symbol": "DOTUSDT", "enabled": True, "group": "large"},
    {"name": "LTC", "okx_symbol": "LTC-USDT-SWAP", "toobit_symbol": "LTCUSDT", "enabled": True, "group": "large"},
    {"name": "BCH", "okx_symbol": "BCH-USDT-SWAP", "toobit_symbol": "BCHUSDT", "enabled": True, "group": "large"},
    {"name": "TON", "okx_symbol": "TON-USDT-SWAP", "toobit_symbol": "TONUSDT", "enabled": True, "group": "trend"},
    {"name": "SUI", "okx_symbol": "SUI-USDT-SWAP", "toobit_symbol": "SUIUSDT", "enabled": True, "group": "trend"},
    {"name": "APT", "okx_symbol": "APT-USDT-SWAP", "toobit_symbol": "APTUSDT", "enabled": True, "group": "trend"},
    {"name": "OP", "okx_symbol": "OP-USDT-SWAP", "toobit_symbol": "OPUSDT", "enabled": True, "group": "trend"},
    {"name": "ARB", "okx_symbol": "ARB-USDT-SWAP", "toobit_symbol": "ARBUSDT", "enabled": True, "group": "trend"},
    {"name": "NEAR", "okx_symbol": "NEAR-USDT-SWAP", "toobit_symbol": "NEARUSDT", "enabled": True, "group": "trend"},
    {"name": "INJ", "okx_symbol": "INJ-USDT-SWAP", "toobit_symbol": "INJUSDT", "enabled": True, "group": "trend"},
    {"name": "ATOM", "okx_symbol": "ATOM-USDT-SWAP", "toobit_symbol": "ATOMUSDT", "enabled": True, "group": "stable_alt"},
    {"name": "FIL", "okx_symbol": "FIL-USDT-SWAP", "toobit_symbol": "FILUSDT", "enabled": True, "group": "stable_alt"},
    {"name": "ETC", "okx_symbol": "ETC-USDT-SWAP", "toobit_symbol": "ETCUSDT", "enabled": True, "group": "stable_alt"},
    {"name": "AAVE", "okx_symbol": "AAVE-USDT-SWAP", "toobit_symbol": "AAVEUSDT", "enabled": True, "group": "stable_alt"},
    {"name": "UNI", "okx_symbol": "UNI-USDT-SWAP", "toobit_symbol": "UNIUSDT", "enabled": True, "group": "stable_alt"},
    {"name": "SEI", "okx_symbol": "SEI-USDT-SWAP", "toobit_symbol": "SEIUSDT", "enabled": True, "group": "high_volatility"},
    {"name": "JUP", "okx_symbol": "JUP-USDT-SWAP", "toobit_symbol": "JUPUSDT", "enabled": True, "group": "high_volatility"},
    {"name": "WIF", "okx_symbol": "WIF-USDT-SWAP", "toobit_symbol": "WIFUSDT", "enabled": True, "group": "high_volatility"},
    {"name": "PEPE", "okx_symbol": "PEPE-USDT-SWAP", "toobit_symbol": "PEPEUSDT", "enabled": True, "group": "high_volatility"},
    {"name": "SHIB", "okx_symbol": "SHIB-USDT-SWAP", "toobit_symbol": "SHIBUSDT", "enabled": True, "group": "high_volatility"},
]


def enabled_symbols() -> list[dict[str, object]]:
    return [item for item in WATCHLIST if item.get("enabled") is True]
