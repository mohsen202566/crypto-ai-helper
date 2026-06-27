from __future__ import annotations

import os
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
)

TIMEFRAME = "5m"
OKX_CANDLE_LIMIT = 120
SCAN_INTERVAL_SECONDS = 20
MONITOR_INTERVAL_SECONDS = 5
ACCEPT_SCORE = 80
MIN_ADX = 20.0
TP_PCT = 0.006
SL_PCT = 0.004
DEFAULT_TRADE_ENABLED = False
DEFAULT_MARGIN_USDT = 10.0
DEFAULT_LEVERAGE = 5
DEFAULT_MAX_POSITIONS = 3
DATA_DIR = os.getenv("BOT_DATA_DIR", "data")
DB_PATH = os.getenv("BOT_DB_PATH", os.path.join(DATA_DIR, "bot.sqlite3"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")


def ensure_runtime_config() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN داخل .env تنظیم نشده است.")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID داخل .env تنظیم نشده است.")
