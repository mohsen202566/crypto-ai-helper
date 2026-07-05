from __future__ import annotations

import os
from pathlib import Path

BOT_NAME = os.getenv("BOT_NAME", "AI Range Learning 5m Futures Bot")
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DB_PATH = os.getenv("DB_PATH", str(DATA_DIR / "bot.db"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0") or "0")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")

OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
OKX_CANDLE_LIMIT = int(os.getenv("OKX_CANDLE_LIMIT", "300"))
OKX_TIMEOUT_SECONDS = int(os.getenv("OKX_TIMEOUT_SECONDS", "12"))
MIN_ENTRY_CANDLES = int(os.getenv("MIN_ENTRY_CANDLES", "205"))
MIN_HTF_CANDLES = int(os.getenv("MIN_HTF_CANDLES", "60"))

TIMEFRAME_ENTRY = "5m"
TIMEFRAME_1H = "1H"
TIMEFRAME_4H = "4H"
TIMEFRAME_1D = "1D"
TIMEFRAMES = (TIMEFRAME_ENTRY, TIMEFRAME_1H, TIMEFRAME_4H, TIMEFRAME_1D)
CONTEXT_SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")

SCANNER_SECONDS = int(os.getenv("SCANNER_SECONDS", "45"))
MONITOR_SECONDS = int(os.getenv("MONITOR_SECONDS", "10"))
REPLAY_DAYS = int(os.getenv("REPLAY_DAYS", "7"))
REPLAY_MAX_CANDLES = int(os.getenv("REPLAY_MAX_CANDLES", "2200"))
RUN_REPLAY_ON_START = os.getenv("RUN_REPLAY_ON_START", "0") == "1"

TAKER_FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.0005"))
SLIPPAGE_BUFFER_RATE = float(os.getenv("SLIPPAGE_BUFFER_RATE", "0.0002"))
MIN_NET_PROFIT_USDT = float(os.getenv("MIN_NET_PROFIT_USDT", "0.01"))
MIN_RISK_REWARD = float(os.getenv("MIN_RISK_REWARD", "1.20"))
SAFE_TP_FRACTION_MIN = float(os.getenv("SAFE_TP_FRACTION_MIN", "0.65"))
SAFE_TP_FRACTION_MAX = float(os.getenv("SAFE_TP_FRACTION_MAX", "0.82"))

DEFAULT_TRADE_ENABLED = os.getenv("DEFAULT_TRADE_ENABLED", "0") == "1"
DEFAULT_MARGIN_USDT = float(os.getenv("DEFAULT_MARGIN_USDT", "5"))
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "10"))
DEFAULT_MAX_POSITIONS = int(os.getenv("DEFAULT_MAX_POSITIONS", "3"))

MARGIN_MIN_USDT = 1.0
MARGIN_MAX_USDT = 10000.0
LEVERAGE_MIN = 1
LEVERAGE_MAX = 100
MAX_POSITIONS_MIN = 1
MAX_POSITIONS_MAX = 200

REAL_OPEN_VERIFY_SECONDS = int(os.getenv("REAL_OPEN_VERIFY_SECONDS", "70"))
PANEL_CACHE_SECONDS = int(os.getenv("PANEL_CACHE_SECONDS", "20"))

INITIAL_SOFT_MODE = os.getenv("INITIAL_SOFT_MODE", "1") == "1"
BOOT_NORMAL_SAMPLE_LIMIT = int(os.getenv("BOOT_NORMAL_SAMPLE_LIMIT", "50"))
REAL_MIN_SAMPLES = int(os.getenv("REAL_MIN_SAMPLES", "30"))
STRONG_CONFIDENCE_SAMPLES = int(os.getenv("STRONG_CONFIDENCE_SAMPLES", "150"))

MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.0008"))
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "0.018"))
MIN_ADX_SOFT = float(os.getenv("MIN_ADX_SOFT", "14"))
MIN_ADX_HARD_BLOCK = float(os.getenv("MIN_ADX_HARD_BLOCK", "10"))
MAX_VOLUME_RATIO_SOFT = float(os.getenv("MAX_VOLUME_RATIO_SOFT", "3.8"))
MAX_VOLUME_RATIO_HARD = float(os.getenv("MAX_VOLUME_RATIO_HARD", "5.5"))
MIN_VOLUME_RATIO_SOFT = float(os.getenv("MIN_VOLUME_RATIO_SOFT", "0.70"))
MIN_VOLUME_RATIO_HARD = float(os.getenv("MIN_VOLUME_RATIO_HARD", "0.45"))

PRICE_TICK_DECIMALS = int(os.getenv("PRICE_TICK_DECIMALS", "8"))


def ensure_runtime_config() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN تنظیم نشده است.")
    if TELEGRAM_CHAT_ID == 0:
        raise RuntimeError("TELEGRAM_CHAT_ID تنظیم نشده است.")
