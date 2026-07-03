from __future__ import annotations

import os
from pathlib import Path

BOT_NAME = os.getenv("BOT_NAME", "Crypto AI Helper Spot")
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DB_PATH = os.getenv("DB_PATH", str(DATA_DIR / "spot_bot.db"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0") or "0")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")

OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
OKX_CANDLE_LIMIT = int(os.getenv("OKX_CANDLE_LIMIT", "300"))
OKX_TIMEOUT_SECONDS = int(os.getenv("OKX_TIMEOUT_SECONDS", "12"))

TOOBIT_BASE_URL = os.getenv("TOOBIT_BASE_URL", "https://api.toobit.com").rstrip("/")
TOOBIT_API_KEY = os.getenv("TOOBIT_API_KEY", "")
TOOBIT_SECRET_KEY = os.getenv("TOOBIT_SECRET_KEY", "")
TOOBIT_API_SECRET = os.getenv("TOOBIT_API_SECRET", "")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
RECV_WINDOW = int(os.getenv("RECV_WINDOW", "5000"))

TIMEFRAME_ENTRY = "5m"
TIMEFRAME_CONFIRM = "15m"
TIMEFRAME_1H = "1H"
TIMEFRAME_4H = "4H"
TIMEFRAME_1D = "1D"
TIMEFRAMES = (TIMEFRAME_ENTRY, TIMEFRAME_CONFIRM, TIMEFRAME_1H, TIMEFRAME_4H, TIMEFRAME_1D)
CONTEXT_SYMBOLS = ("BTC-USDT", "ETH-USDT")

SCANNER_SECONDS = int(os.getenv("SCANNER_SECONDS", "60"))
MONITOR_SECONDS = int(os.getenv("MONITOR_SECONDS", "15"))
REAL_TOOBIT_MONITOR_SECONDS = int(os.getenv("REAL_TOOBIT_MONITOR_SECONDS", "60"))
PANEL_CACHE_SECONDS = int(os.getenv("PANEL_CACHE_SECONDS", "20"))
BUY_FILL_VERIFY_SECONDS = int(os.getenv("BUY_FILL_VERIFY_SECONDS", "80"))
SELL_ORDER_VERIFY_SECONDS = int(os.getenv("SELL_ORDER_VERIFY_SECONDS", "20"))
WARNING_COOLDOWN_SECONDS = int(os.getenv("WARNING_COOLDOWN_SECONDS", "1800"))
MAX_SIGNAL_HOURS_BEFORE_WARNING = float(os.getenv("MAX_SIGNAL_HOURS_BEFORE_WARNING", "6"))

REPLAY_DAYS = int(os.getenv("REPLAY_DAYS", "7"))
REPLAY_MAX_CANDLES = int(os.getenv("REPLAY_MAX_CANDLES", "2200"))
REPLAY_SYMBOL_LIMIT = int(os.getenv("REPLAY_SYMBOL_LIMIT", "30"))
RUN_REPLAY_ON_START = os.getenv("RUN_REPLAY_ON_START", "1") == "1"
REPLAY_REFRESH_HOURS = int(os.getenv("REPLAY_REFRESH_HOURS", "24"))

SPOT_MAKER_FEE_RATE = float(os.getenv("SPOT_MAKER_FEE_RATE", "0.0007"))
SPOT_TAKER_FEE_RATE = float(os.getenv("SPOT_TAKER_FEE_RATE", "0.0009"))
SLIPPAGE_BUFFER_RATE = float(os.getenv("SLIPPAGE_BUFFER_RATE", "0.0002"))
MIN_NET_PROFIT_USDT = float(os.getenv("MIN_NET_PROFIT_USDT", "0.01"))
SAFE_TARGET_FRACTION_MIN = float(os.getenv("SAFE_TARGET_FRACTION_MIN", "0.62"))
SAFE_TARGET_FRACTION_MAX = float(os.getenv("SAFE_TARGET_FRACTION_MAX", "0.82"))
MIN_TARGET_MOVE_PCT = float(os.getenv("MIN_TARGET_MOVE_PCT", "0.0025"))
MAX_TARGET_MOVE_PCT = float(os.getenv("MAX_TARGET_MOVE_PCT", "0.055"))

DEFAULT_TRADE_ENABLED = os.getenv("DEFAULT_TRADE_ENABLED", "0") == "1"
DEFAULT_TRADE_USDT = float(os.getenv("DEFAULT_TRADE_USDT", "10"))
DEFAULT_MAX_POSITIONS = int(os.getenv("DEFAULT_MAX_POSITIONS", "3"))
TRADE_USDT_MIN = 1.0
TRADE_USDT_MAX = 10000.0
MAX_POSITIONS_MIN = 1
MAX_POSITIONS_MAX = 200

INITIAL_SOFT_MODE = os.getenv("INITIAL_SOFT_MODE", "1") == "1"
BOOT_NORMAL_SAMPLE_LIMIT = int(os.getenv("BOOT_NORMAL_SAMPLE_LIMIT", "50"))
REAL_MIN_CONFIDENCE = int(os.getenv("REAL_MIN_CONFIDENCE", "35"))
STRONG_CONFIDENCE_SAMPLES = int(os.getenv("STRONG_CONFIDENCE_SAMPLES", "150"))

MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.0006"))
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "0.030"))
MIN_ADX_SOFT = float(os.getenv("MIN_ADX_SOFT", "12"))
MIN_ADX_HARD_BLOCK = float(os.getenv("MIN_ADX_HARD_BLOCK", "8"))
MIN_VOLUME_RATIO_HARD = float(os.getenv("MIN_VOLUME_RATIO_HARD", "0.35"))
MAX_VOLUME_RATIO_HARD = float(os.getenv("MAX_VOLUME_RATIO_HARD", "6.0"))

PRICE_TICK_DECIMALS = int(os.getenv("PRICE_TICK_DECIMALS", "8"))


def ensure_runtime_config() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN تنظیم نشده است.")
    if TELEGRAM_CHAT_ID == 0:
        raise RuntimeError("TELEGRAM_CHAT_ID تنظیم نشده است.")
