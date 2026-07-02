"""تنظیمات مرکزی ربات Crypto AI Helper Spot Hunter.

این ربات فقط برای Spot، فقط LONG و بدون لوریج/شورت/استاپ ساخته شده است.
اطلاعات تحلیل از OKX گرفته می‌شود و اجرای واقعی فقط با Toobit انجام می‌شود.
"""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# -----------------------------
# تلگرام
# -----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# -----------------------------
# مسیرهای دیتا
# -----------------------------
BOT_DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "./data")).expanduser()
BOT_DATA_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_STATE_FILE = BOT_DATA_DIR / "runtime_state.json"
BOT_LOG_FILE = BOT_DATA_DIR / "bot.log"
LOCK_FILE = os.getenv("BOT_LOCK_FILE", "/tmp/crypto-ai-helper-spot-hunter.lock")

# -----------------------------
# OKX عمومی برای تحلیل و سیگنال عادی
# -----------------------------
OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))

# -----------------------------
# Toobit Spot برای اجرای واقعی
# مسیرها قابل تنظیم هستند چون در بعضی نسخه‌های API نام endpointها فرق دارد.
# -----------------------------
TOOBIT_BASE_URL = os.getenv("TOOBIT_BASE_URL", "https://api.toobit.com").rstrip("/")
TOOBIT_API_KEY = os.getenv("TOOBIT_API_KEY", "").strip()
TOOBIT_API_SECRET = os.getenv("TOOBIT_API_SECRET", "").strip()
RECV_WINDOW = int(os.getenv("RECV_WINDOW", "5000"))

TOOBIT_SPOT_PATH_EXCHANGE_INFO = os.getenv("TOOBIT_SPOT_PATH_EXCHANGE_INFO", "/api/v1/spot/exchangeInfo")
TOOBIT_SPOT_PATH_BALANCE = os.getenv("TOOBIT_SPOT_PATH_BALANCE", "/api/v1/spot/account")
TOOBIT_SPOT_PATH_ORDER = os.getenv("TOOBIT_SPOT_PATH_ORDER", "/api/v1/spot/order")
TOOBIT_SPOT_PATH_OPEN_ORDERS = os.getenv("TOOBIT_SPOT_PATH_OPEN_ORDERS", "/api/v1/spot/openOrders")
TOOBIT_SPOT_PATH_ORDER_HISTORY = os.getenv("TOOBIT_SPOT_PATH_ORDER_HISTORY", "/api/v1/spot/historyOrders")
TOOBIT_SPOT_PATH_ORDER_HISTORY_ALT = os.getenv("TOOBIT_SPOT_PATH_ORDER_HISTORY_ALT", "/api/v1/spot/order/history")

# -----------------------------
# ارزهای امن شروع
# -----------------------------
SAFE_SYMBOLS = [
    "BTC", "ETH", "SOL", "BNB", "XRP",
    "DOGE", "LINK", "AVAX", "ADA", "SUI",
    "NEAR", "APT", "ARB", "OP", "LTC",
]
QUOTE_ASSET = "USDT"

# -----------------------------
# تنظیمات پیش‌فرض قابل تغییر با دستور تلگرام
# -----------------------------
DEFAULT_TRADING_ENABLED = False
DEFAULT_TRADE_AMOUNT_USDT = 10.0
DEFAULT_MAX_REAL_POSITIONS = 3
DEFAULT_TARGET_PERCENT = 3.0
DEFAULT_ACTIVE_SYMBOL_COUNT = 15
DEFAULT_HISTORY_CHECK_MINUTES = 5
DEFAULT_MAKER_FEE_PCT = 0.07
DEFAULT_TAKER_FEE_PCT = 0.09

# -----------------------------
# حدود مجاز دستورها
# -----------------------------
MIN_TRADE_AMOUNT_USDT = 1.0
MAX_TRADE_AMOUNT_USDT = 10_000.0
MIN_MAX_POSITIONS = 1
MAX_MAX_POSITIONS = 200
MIN_TARGET_PERCENT = 1.0
MAX_TARGET_PERCENT = 100.0
MIN_ACTIVE_SYMBOL_COUNT = 1
MAX_ACTIVE_SYMBOL_COUNT = len(SAFE_SYMBOLS)
MIN_HISTORY_CHECK_MINUTES = 5
MAX_HISTORY_CHECK_MINUTES = 60
MIN_FEE_PCT = 0.0
MAX_FEE_PCT = 5.0

# -----------------------------
# زمان‌بندی‌ها
# -----------------------------
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
NORMAL_MONITOR_INTERVAL_SECONDS = int(os.getenv("NORMAL_MONITOR_INTERVAL_SECONDS", "60"))
BUY_CONFIRM_DELAY_SECONDS = int(os.getenv("BUY_CONFIRM_DELAY_SECONDS", "5"))
BUY_FILL_TIMEOUT_SECONDS = int(os.getenv("BUY_FILL_TIMEOUT_SECONDS", "70"))
BUY_FILL_POLL_SECONDS = int(os.getenv("BUY_FILL_POLL_SECONDS", "5"))

# -----------------------------
# استراتژی
# -----------------------------
MIN_SIGNAL_SCORE = int(os.getenv("MIN_SIGNAL_SCORE", "85"))
WARN_SIGNAL_SCORE = int(os.getenv("WARN_SIGNAL_SCORE", "75"))
MAX_LAST_HOUR_PUMP_PCT = float(os.getenv("MAX_LAST_HOUR_PUMP_PCT", "4.0"))
MIN_PULLBACK_PCT = float(os.getenv("MIN_PULLBACK_PCT", "0.4"))
MAX_PULLBACK_PCT = float(os.getenv("MAX_PULLBACK_PCT", "8.0"))
MIN_VOLUME_RATIO = float(os.getenv("MIN_VOLUME_RATIO", "0.75"))

# -----------------------------
# وضعیت سیگنال‌ها
# -----------------------------
STATUS_OPEN = "open"
STATUS_PENDING_BUY = "pending_buy"
STATUS_REAL_OPEN = "real_open"
STATUS_NORMAL_OPEN = "normal_open"
STATUS_CLOSED = "closed"
STATUS_FAILED = "failed"

MODE_REAL = "real"
MODE_NORMAL = "normal"
