"""تنظیمات اصلی ربات اسکالپ کلاسیک ۵ دقیقه‌ای."""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# -----------------------------
# اتصال‌ها
# -----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
TOOBIT_BASE_URL = os.getenv("TOOBIT_BASE_URL", "https://api.toobit.com").rstrip("/")

TOOBIT_API_KEY = os.getenv("TOOBIT_API_KEY", "").strip()
TOOBIT_API_SECRET = os.getenv("TOOBIT_API_SECRET", "").strip()
RECV_WINDOW = int(os.getenv("RECV_WINDOW", "5000"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "12"))

# -----------------------------
# بازار و واچ‌لیست
# -----------------------------
TIMEFRAME = "5m"
TIMEFRAME_SECONDS = 5 * 60
CANDLE_LIMIT = 160
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "4"))
SYMBOL_ERROR_COOLDOWN_SECONDS = int(os.getenv("SYMBOL_ERROR_COOLDOWN_SECONDS", "60"))

WATCHLIST = [
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "TRXUSDT",
    "LINKUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LTCUSDT",
    "NEARUSDT",
    "XLMUSDT",
]

# نگاشت داخلی به نمادهای هر صرافی.
# OKX برای تحلیل فیوچرز سواپ: SOL-USDT-SWAP
# Toobit برای اجرای فیوچرز: SOL-SWAP-USDT
SYMBOL_MAP = {
    s: {
        "base": s.replace("USDT", ""),
        "quote": "USDT",
        "okx": f"{s.replace('USDT', '')}-USDT-SWAP",
        "toobit": f"{s.replace('USDT', '')}-SWAP-USDT",
    }
    for s in WATCHLIST
}

# -----------------------------
# اندیکاتورها
# -----------------------------
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
BOLLINGER_PERIOD = 20
BOLLINGER_STD = 2.0
VOLUME_MA_PERIOD = 20

# -----------------------------
# ورود و خروج
# -----------------------------
FIXED_TP_PERCENT = 0.70
FIXED_SL_PERCENT = 0.45
MIN_SIGNAL_SCORE = 80
ALLOW_FAST_ENTRY_SCORE = 75
FAST_VOLUME_MULTIPLIER = 1.50
MIN_PROJECTED_VOLUME_MULTIPLIER = 1.10
STRONG_PROJECTED_VOLUME_MULTIPLIER = 1.30
MIN_CANDLE_AGE_SECONDS = 20
MAX_CANDLE_AGE_SECONDS = 210
SIGNAL_COOLDOWN_SECONDS = 8 * 60

# ATR باید با TP/SL ثابت هماهنگ باشد؛ خیلی کم یعنی حرکت کافی ندارد، خیلی زیاد یعنی ریسک اسلیپیج/نویز زیاد است.
ATR_MIN_PERCENT = 0.18
ATR_MAX_PERCENT = 2.20

# -----------------------------
# تنظیمات قابل تغییر از تلگرام
# -----------------------------
DEFAULT_TRADE_AMOUNT_USDT = float(os.getenv("DEFAULT_TRADE_AMOUNT_USDT", "10"))
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "10"))
DEFAULT_MAX_POSITIONS = int(os.getenv("DEFAULT_MAX_POSITIONS", "1"))
DEFAULT_TRADE_ENABLED = os.getenv("DEFAULT_TRADE_ENABLED", "false").lower() == "true"
DEFAULT_MARGIN_TYPE = os.getenv("DEFAULT_MARGIN_TYPE", "ISOLATED").upper()

TRADE_AMOUNT_MIN = 1
TRADE_AMOUNT_MAX = 10000
LEVERAGE_MIN = 1
LEVERAGE_MAX = 100
MAX_POSITIONS_MIN = 1
MAX_POSITIONS_MAX = 100

# -----------------------------
# ذخیره‌سازی
# -----------------------------
STATE_FILE = DATA_DIR / "state.json"
LOG_FILE = DATA_DIR / "bot.log"
