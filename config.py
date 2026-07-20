"""تنظیمات ثابت ربات شکار پایان پامپ Toobit.

همه فایل‌ها در ریشه پروژه قرار می‌گیرند. ربات هیچ موتور یادگیری ندارد؛
قوانین سیگنال، ترید، اسلات و محدودیت API ثابت هستند.
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Load shell-like KEY=VALUE files without overriding systemd values.

    Supports plain ``KEY=value``, ``export KEY=value`` and lines copied from
    systemd such as ``Environment=KEY=value``. Quoted values and inline comments
    are handled through :mod:`shlex`.
    """
    try:
        if not path.is_file():
            return
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if line.startswith("Environment="):
                line = line[len("Environment="):].strip()
            try:
                parts = shlex.split(line, comments=True, posix=True)
            except ValueError:
                parts = [line]
            if not parts:
                continue
            assignment = parts[0]
            if "=" not in assignment:
                continue
            key, value = assignment.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and key.replace("_", "").isalnum():
                os.environ.setdefault(key, value)
    except OSError:
        # systemd Environment/EnvironmentFile remains the primary source.
        pass


def _load_project_environment() -> None:
    candidates: list[Path] = []
    explicit = os.getenv("BOT_ENV_FILE", "").strip() or os.getenv("ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend((
        ROOT / ".env",
        ROOT / "bot.env",
        Path("/root/.env"),
        Path("/etc/crypto-bot.env"),
        Path("/etc/crypto-ai-helper.env"),
        Path("/etc/default/crypto-bot"),
        Path("/etc/sysconfig/crypto-bot"),
        Path("/etc/forex-signal-bot.env"),
    ))
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        _load_env_file(candidate)


_load_project_environment()

BUILD_VERSION = "2026.07.20-v7"
RUNTIME_DB = Path(os.getenv("RUNTIME_DB", str(ROOT / "runtime.db")))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# اتصال؛ نام‌های قدیمی پروژه نیز برای سازگاری پذیرفته می‌شوند.
TOOBIT_BASE_URL = os.getenv("TOOBIT_BASE_URL", "https://api.toobit.com").rstrip("/")
TOOBIT_API_KEY = (os.getenv("TOOBIT_API_KEY") or os.getenv("TOOBIT_KEY") or "").strip()
TOOBIT_API_SECRET = (os.getenv("TOOBIT_API_SECRET") or os.getenv("TOOBIT_SECRET_KEY") or "").strip()
TOOBIT_RECV_WINDOW = int(os.getenv("TOOBIT_RECV_WINDOW", "5000"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "2"))
HTTP_BACKOFF_SECONDS = float(os.getenv("HTTP_BACKOFF_SECONDS", "0.8"))

TELEGRAM_BOT_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or os.getenv("TG_BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or os.getenv("BOT_API_TOKEN")
    or ""
).strip()
TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID")
    or os.getenv("OWNER_ID")
    or os.getenv("CHAT_ID")
    or os.getenv("TELEGRAM_OWNER_ID")
    or os.getenv("TELEGRAM_ADMIN_ID")
    or os.getenv("ADMIN_CHAT_ID")
    or ""
).strip()
TELEGRAM_POLL_TIMEOUT = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "25"))

# Endpointها؛ بدون تغییر کد قابل جایگزینی هستند.
PATH_EXCHANGE_INFO = os.getenv("TOOBIT_PATH_EXCHANGE_INFO", "/api/v1/exchangeInfo")
PATH_TICKER_24H = os.getenv("TOOBIT_PATH_TICKER_24H", "/quote/v1/contract/ticker/24hr")
PATH_PRICE_TICKER = os.getenv("TOOBIT_PATH_PRICE_TICKER", "/quote/v1/contract/ticker/price")
PATH_BOOK_TICKER = os.getenv("TOOBIT_PATH_BOOK_TICKER", "/quote/v1/contract/ticker/bookTicker")
PATH_KLINES = os.getenv("TOOBIT_PATH_KLINES", "/quote/v1/klines")
PATH_TRADES = os.getenv("TOOBIT_PATH_TRADES", "/quote/v1/trades")
PATH_DEPTH = os.getenv("TOOBIT_PATH_DEPTH", "/quote/v1/depth")
PATH_MARK_PRICE = os.getenv("TOOBIT_PATH_MARK_PRICE", "/quote/v1/markPrice")
PATH_FUNDING = os.getenv("TOOBIT_PATH_FUNDING", "/api/v1/futures/fundingRate")
PATH_OPEN_INTEREST = os.getenv("TOOBIT_PATH_OPEN_INTEREST", "/quote/v1/openInterest")
PATH_LONG_SHORT = os.getenv("TOOBIT_PATH_LONG_SHORT", "/quote/v1/globalLongShortAccountRatio")
PATH_BALANCE = os.getenv("TOOBIT_PATH_BALANCE", "/api/v1/futures/balance")
PATH_POSITIONS = os.getenv("TOOBIT_PATH_POSITIONS", "/api/v1/futures/positions")
PATH_OPEN_ORDERS = os.getenv("TOOBIT_PATH_OPEN_ORDERS", "/api/v1/futures/openOrders")
PATH_MARGIN_MODE = os.getenv("TOOBIT_PATH_MARGIN_MODE", "/api/v1/futures/marginType")
PATH_LEVERAGE = os.getenv("TOOBIT_PATH_LEVERAGE", "/api/v1/futures/leverage")
PATH_POSITION_SETTINGS = os.getenv("TOOBIT_PATH_POSITION_SETTINGS", "/api/v1/futures/accountLeverage")
PATH_ORDER = os.getenv("TOOBIT_PATH_ORDER", "/api/v1/futures/order")
PATH_HISTORY_POSITIONS = os.getenv("TOOBIT_PATH_HISTORY_POSITIONS", "/api/v1/futures/historyPositions")
PATH_ORDER_HISTORY = os.getenv("TOOBIT_PATH_ORDER_HISTORY", "/api/v1/futures/historyOrders")
PATH_ORDER_HISTORY_ALT = os.getenv("TOOBIT_PATH_ORDER_HISTORY_ALT", "/api/v1/futures/order/history")
PATH_TRADING_STOP = os.getenv("TOOBIT_PATH_TRADING_STOP", "/api/v1/futures/position/trading-stop")
PATH_FLASH_CLOSE = os.getenv("TOOBIT_PATH_FLASH_CLOSE", "/api/v1/futures/flashClose")

# سقف رسمی 3000 وزن در دقیقه است؛ ربات عمداً پایین‌تر می‌ماند.
OFFICIAL_REQUEST_WEIGHT_PER_MINUTE = 3000
INTERNAL_TOTAL_WEIGHT_PER_MINUTE = int(os.getenv("INTERNAL_TOTAL_WEIGHT_PER_MINUTE", "1800"))
INTERNAL_MARKET_WEIGHT_PER_MINUTE = int(os.getenv("INTERNAL_MARKET_WEIGHT_PER_MINUTE", "900"))
RATE_LIMIT_SAFETY_SECONDS = float(os.getenv("RATE_LIMIT_SAFETY_SECONDS", "1.0"))

# زمان‌بندی
CONTRACT_REFRESH_SECONDS = int(os.getenv("CONTRACT_REFRESH_SECONDS", "60"))
MARKET_SCAN_SECONDS = float(os.getenv("MARKET_SCAN_SECONDS", "10"))
POSITION_PRICE_SECONDS = float(os.getenv("POSITION_PRICE_SECONDS", "5"))
REAL_MONITOR_SECONDS = int(os.getenv("REAL_MONITOR_SECONDS", "60"))
PENDING_CONFIRM_SECONDS = int(os.getenv("PENDING_CONFIRM_SECONDS", "70"))
PENDING_CHECK_SECONDS = int(os.getenv("PENDING_CHECK_SECONDS", "5"))
ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS = int(os.getenv("ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS", "180"))
DEPTH_REFRESH_SECONDS = float(os.getenv("DEPTH_REFRESH_SECONDS", "10"))
TRAILING_UPDATE_SECONDS = int(os.getenv("TRAILING_UPDATE_SECONDS", "30"))

# تنظیمات قابل تغییر با دستورات تلگرام
TRADE_MARGIN_MIN = 1.0
TRADE_MARGIN_MAX = 10_000.0
LEVERAGE_MIN = 1
LEVERAGE_MAX = 100
MAX_POSITIONS_MIN = 1
MAX_POSITIONS_MAX = 200
DEFAULT_TRADE_MARGIN_USDT = float(os.getenv("DEFAULT_TRADE_MARGIN_USDT", "5"))
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "10"))
DEFAULT_MAX_OPEN_POSITIONS = int(os.getenv("DEFAULT_MAX_OPEN_POSITIONS", "3"))

# اقتصاد معامله
TAKER_FEE_RATE = float(os.getenv("TOOBIT_TAKER_FEE_RATE", "0.0005"))
ROUND_TRIP_SLIPPAGE_RATE = float(os.getenv("ROUND_TRIP_SLIPPAGE_RATE", "0.0006"))
FUNDING_RESERVE_RATE = float(os.getenv("FUNDING_RESERVE_RATE", "0.0002"))
MIN_EXPECTED_NET_PROFIT_USDT = float(os.getenv("MIN_EXPECTED_NET_PROFIT_USDT", "0.05"))

# قیف بازار
WATCHLIST_SIZE = int(os.getenv("WATCHLIST_SIZE", "15"))
DEEP_CANDIDATE_SIZE = int(os.getenv("DEEP_CANDIDATE_SIZE", "5"))
EXCLUDED_BASES = frozenset(
    x.strip().upper() for x in os.getenv(
        "EXCLUDED_BASES", "BTC,ETH,USDC,USDT,FDUSD,TUSD,DAI"
    ).split(",") if x.strip()
)
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "200000"))
MAX_SPREAD_RATE = float(os.getenv("MAX_SPREAD_RATE", "0.008"))
# آستانه‌های پامپ باید همیشه مثبت باشند. حتی اگر مقدار قدیمی/اشتباه منفی
# در Environment مانده باشد، هرگز ارز نزولی وارد قیف پامپ نمی‌شود.
def _positive_float_env(name: str, default: float, minimum: float = 0.0001) -> float:
    try:
        value = abs(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        value = abs(float(default))
    return max(float(minimum), value)


MIN_PUMP_24H_PERCENT = _positive_float_env("MIN_PUMP_24H_PERCENT", 18.0)
MIN_PUMP_15M_PERCENT = _positive_float_env("MIN_PUMP_15M_PERCENT", 8.0)
MIN_PUMP_5M_PERCENT = _positive_float_env("MIN_PUMP_5M_PERCENT", 4.0)
MIN_SIGNAL_SCORE = float(os.getenv("MIN_SIGNAL_SCORE", "72"))
MIN_CONFIRMATIONS = int(os.getenv("MIN_CONFIRMATIONS", "4"))
NEW_CONTRACT_WARMUP_MINUTES = int(os.getenv("NEW_CONTRACT_WARMUP_MINUTES", "3"))

# ورود و خروج ثابت
ATR_PERIOD = 14
RSI_PERIOD = 14
STOP_ATR_MULTIPLIER = float(os.getenv("STOP_ATR_MULTIPLIER", "1.25"))
MIN_STOP_PERCENT = float(os.getenv("MIN_STOP_PERCENT", "0.012"))
MAX_STOP_PERCENT = float(os.getenv("MAX_STOP_PERCENT", "0.06"))
SAFETY_TP_PERCENT = float(os.getenv("SAFETY_TP_PERCENT", "0.22"))
TRAILING_ACTIVATION_PERCENT = float(os.getenv("TRAILING_ACTIVATION_PERCENT", "0.025"))
TRAILING_DISTANCE_PERCENT = float(os.getenv("TRAILING_DISTANCE_PERCENT", "0.018"))
TRAILING_ATR_MULTIPLIER = float(os.getenv("TRAILING_ATR_MULTIPLIER", "1.4"))
REVERSAL_CONFIRMATIONS_TO_EXIT = int(os.getenv("REVERSAL_CONFIRMATIONS_TO_EXIT", "2"))

# دیتابیس
SQLITE_BUSY_TIMEOUT_MS = 5000

# وزن endpointها. در صورت تغییر مستندات فقط این جدول اصلاح می‌شود.
ENDPOINT_WEIGHTS = {
    PATH_EXCHANGE_INFO: 1,
    PATH_TICKER_24H: 40,  # بدون symbol
    PATH_PRICE_TICKER: 1,
    PATH_BOOK_TICKER: 1,
    PATH_KLINES: 1,
    PATH_TRADES: 1,
    PATH_DEPTH: 1,  # limit <= 100
    PATH_MARK_PRICE: 1,
    PATH_FUNDING: 1,
    PATH_OPEN_INTEREST: 1,
    PATH_LONG_SHORT: 1,
    PATH_BALANCE: 5,
    PATH_POSITIONS: 5,
    PATH_OPEN_ORDERS: 5,
    PATH_MARGIN_MODE: 1,
    PATH_LEVERAGE: 1,
    PATH_POSITION_SETTINGS: 1,
    PATH_ORDER: 1,
    PATH_HISTORY_POSITIONS: 5,
    PATH_ORDER_HISTORY: 5,
    PATH_ORDER_HISTORY_ALT: 5,
    PATH_TRADING_STOP: 1,
    PATH_FLASH_CLOSE: 1,
}
