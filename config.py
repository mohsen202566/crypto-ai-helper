"""تنظیمات اصلی ربات اسکالپ کلاسیک ۵ دقیقه‌ای."""
from __future__ import annotations

import os
import re
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
ENV_FILE = BASE_DIR / ".env"

_ENV_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "OWNER_ID",
    "TOOBIT_API_KEY",
    "TOOBIT_API_SECRET",
    "TOOBIT_SECRET_KEY",
    "TOOBIT_BASE_URL",
    "OKX_BASE_URL",
    "BOT_NAME",
    "TIMEFRAME",
    "TRADE_ENABLED",
    "DEFAULT_TRADE_ENABLED",
    "DEFAULT_TRADE_AMOUNT_USDT",
    "DEFAULT_LEVERAGE",
    "DEFAULT_MAX_POSITIONS",
    "DEFAULT_MARGIN_TYPE",
    "POLL_INTERVAL_SECONDS",
    "SYMBOL_ERROR_COOLDOWN_SECONDS",
    "RECV_WINDOW",
    "REQUEST_TIMEOUT",
    "REAL_ORDER_MISSING_TO_NORMAL_SECONDS",
    "REAL_HISTORY_FALLBACK_SECONDS",
    "TOOBIT_PATH_ORDER_HISTORY",
    "TOOBIT_PATH_ORDER_HISTORY_ALT",
    "MARKET_TREND_REFRESH_SECONDS",
    "MARKET_TREND_MIN_AGREEMENT",
    "MARKET_TREND_MIN_SYMBOLS",
]


def _raw_env_text() -> str:
    try:
        return ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    except Exception:
        return ""


_RAW_ENV = _raw_env_text()
_LOOKAHEAD = r"(?=(?:#\s*)?(?:" + "|".join(map(re.escape, _ENV_KEYS)) + r")\s*=|\n\s*#|$)"


def _clean_env_value(value: str) -> str:
    value = str(value or "").strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1].strip()
    return value


def _get_env(name: str, default: str = "") -> str:
    """خواندن env هم در حالت استاندارد و هم وقتی کاربر اشتباهاً همه خطوط را چسبانده باشد."""
    value = os.getenv(name)
    if value not in (None, ""):
        return _clean_env_value(value)
    if _RAW_ENV:
        pattern = rf"(?:^|[#\s]){re.escape(name)}\s*=\s*(.*?)" + _LOOKAHEAD
        match = re.search(pattern, _RAW_ENV, flags=re.S)
        if match:
            return _clean_env_value(match.group(1))
    return default


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get_env(name, "")
    if raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "فعال", "روشن")


def _get_int(name: str, default: int) -> int:
    try:
        return int(float(_get_env(name, str(default))))
    except Exception:
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get_env(name, str(default)))
    except Exception:
        return default


# -----------------------------
# اتصال‌ها
# -----------------------------
TELEGRAM_BOT_TOKEN = _get_env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _get_env("TELEGRAM_CHAT_ID", "")

OKX_BASE_URL = _get_env("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
TOOBIT_BASE_URL = _get_env("TOOBIT_BASE_URL", "https://api.toobit.com").rstrip("/")

TOOBIT_API_KEY = _get_env("TOOBIT_API_KEY", "")
TOOBIT_API_SECRET = _get_env("TOOBIT_API_SECRET", "") or _get_env("TOOBIT_SECRET_KEY", "")
RECV_WINDOW = _get_int("RECV_WINDOW", 5000)
REQUEST_TIMEOUT = _get_int("REQUEST_TIMEOUT", 12)

# -----------------------------
# بازار و واچ‌لیست
# -----------------------------
TIMEFRAME = "5m"
TIMEFRAME_SECONDS = 5 * 60
CANDLE_LIMIT = 160
POLL_INTERVAL_SECONDS = _get_float("POLL_INTERVAL_SECONDS", 4.0)
SYMBOL_ERROR_COOLDOWN_SECONDS = _get_int("SYMBOL_ERROR_COOLDOWN_SECONDS", 60)

WATCHLIST = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "TRXUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "DOTUSDT",
    "NEARUSDT",
    "UNIUSDT",
    "AAVEUSDT",
    "APTUSDT",
    "ARBUSDT",
    "OPUSDT",
    "FILUSDT",
    "ATOMUSDT",
    "INJUSDT",
    "SUIUSDT",
    "SEIUSDT",
    "ETCUSDT",
    "XLMUSDT",
    "HBARUSDT",
    "ICPUSDT",
    "TIAUSDT",
    "ORDIUSDT",
]

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
EMA_TREND = 50
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
BOLLINGER_PERIOD = 20
BOLLINGER_STD = 2.0
VOLUME_MA_PERIOD = 20

# -----------------------------
# ورود و خروج
# -----------------------------
FIXED_TP_PERCENT = 0.58
FIXED_SL_PERCENT = 0.45
MIN_SIGNAL_SCORE = 80
ALLOW_FAST_ENTRY_SCORE = 75
FAST_VOLUME_MULTIPLIER = 1.50
MIN_PROJECTED_VOLUME_MULTIPLIER = 1.10
STRONG_PROJECTED_VOLUME_MULTIPLIER = 1.30
MIN_CANDLE_AGE_SECONDS = 15
MAX_CANDLE_AGE_SECONDS = 270
SIGNAL_COOLDOWN_SECONDS = 8 * 60
ATR_MIN_PERCENT = 0.05
ATR_MAX_PERCENT = 3.00

# -----------------------------
# فیلتر بازه‌ای بدون امتیاز
# -----------------------------
# بازار فقط سه حالت دارد: BUY، SELL، RANGE. در RANGE هیچ سیگنالی صادر نمی‌شود.
MARKET_TREND_TIMEFRAMES = ("1H", "4H")
MARKET_TREND_ANCHORS = ("BTCUSDT", "ETHUSDT")
MARKET_TREND_REFRESH_SECONDS = _get_int("MARKET_TREND_REFRESH_SECONDS", 180)
MARKET_TREND_MIN_AGREEMENT = _get_float("MARKET_TREND_MIN_AGREEMENT", 0.50)
MARKET_TREND_MIN_SYMBOLS = _get_int("MARKET_TREND_MIN_SYMBOLS", 8)
MARKET_TREND_CANDLE_LIMIT = _get_int("MARKET_TREND_CANDLE_LIMIT", 120)

TREND_RSI_BUY_MIN = 50.0
TREND_RSI_SELL_MAX = 50.0
TREND_ADX_MIN = 0.0

ZONE_LONG_RSI_MIN = 50.0
ZONE_LONG_RSI_MAX = 72.0
ZONE_SHORT_RSI_MIN = 28.0
ZONE_SHORT_RSI_MAX = 50.0
ZONE_ADX_MIN = 12.0
ZONE_ADX_MAX = 45.0
ZONE_VWAP_DISTANCE_MIN_PERCENT = 0.02
ZONE_VWAP_DISTANCE_MAX_PERCENT = 1.20
ZONE_VOLUME_MULTIPLIER_MIN = 1.05
ZONE_VOLUME_MULTIPLIER_MAX = 5.00
ZONE_BB_LONG_MAX_POSITION = 0.98
ZONE_BB_SHORT_MIN_POSITION = 0.02

# -----------------------------
# تنظیمات قابل تغییر از تلگرام
# -----------------------------
DEFAULT_TRADE_AMOUNT_USDT = _get_float("DEFAULT_TRADE_AMOUNT_USDT", 10.0)
DEFAULT_LEVERAGE = _get_int("DEFAULT_LEVERAGE", 10)
DEFAULT_MAX_POSITIONS = _get_int("DEFAULT_MAX_POSITIONS", 1)
DEFAULT_TRADE_ENABLED = _get_bool("DEFAULT_TRADE_ENABLED", _get_bool("TRADE_ENABLED", False))
DEFAULT_MARGIN_TYPE = _get_env("DEFAULT_MARGIN_TYPE", "ISOLATED").upper()

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
LOCK_FILE = DATA_DIR / "bot.lock"

# -----------------------------
# تنظیمات تأیید اجرای واقعی Toobit
# -----------------------------
TOOBIT_VERIFY_AFTER_ERROR_SECONDS = _get_int("TOOBIT_VERIFY_AFTER_ERROR_SECONDS", 70)
TOOBIT_CLOSE_VERIFY_SECONDS = _get_float("TOOBIT_CLOSE_VERIFY_SECONDS", 2.0)
TOOBIT_PLACE_REAL_TP = _get_bool("TOOBIT_PLACE_REAL_TP", True)

# مانیتورینگ نتیجه رئال
# اگر پیام رئال ثبت شد ولی real_order داخل state نیامد، بعد از این زمان به عادی تبدیل می‌شود تا گیر نکند.
REAL_ORDER_MISSING_TO_NORMAL_SECONDS = _get_int("REAL_ORDER_MISSING_TO_NORMAL_SECONDS", 25)
# اگر Toobit پوزیشن را بسته نشان داد ولی history/order history هنوز PnL نداد، بعد از این زمان fallback ثبت می‌شود.
REAL_HISTORY_FALLBACK_SECONDS = _get_int("REAL_HISTORY_FALLBACK_SECONDS", 180)

# مسیرهای قابل override برای تاریخچه توبیت
TOOBIT_PATH_ORDER_HISTORY = _get_env("TOOBIT_PATH_ORDER_HISTORY", "/api/v1/futures/historyOrders")
TOOBIT_PATH_ORDER_HISTORY_ALT = _get_env("TOOBIT_PATH_ORDER_HISTORY_ALT", "/api/v1/futures/order/history")
