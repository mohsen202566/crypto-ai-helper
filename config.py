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

BUILD_VERSION = "2026.07.20-v8"
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


# ============================================================
#  استراتژی: اسکن چندارزی با امتیازدهی وزن‌دار
# ============================================================
# تغییر نسبت به نسخهٔ قبل: ورود پله‌ای/مارتینگل کنار گذاشته شد.
# حالا هر سیگنال = یک پوزیشن مستقل با حد ضرر و حد سود مشخص از همان لحظه.

# --- جهان ارزها ---
# اگر SYMBOL_LIST تنظیم شود، دقیقاً همان‌ها اسکن می‌شوند؛ وگرنه ربات خودش
# پرحجم‌ترین قراردادها را از صرافی می‌گیرد (نقدینگی بالا = اسپرد کمتر).
SYMBOL_LIST = tuple(
    x.strip().upper() for x in os.getenv("SYMBOL_LIST", "").split(",") if x.strip()
)
SCAN_SYMBOL_COUNT = max(30, int(os.getenv("SCAN_SYMBOL_COUNT", "30")))
SYMBOL_REFRESH_SECONDS = int(os.getenv("SYMBOL_REFRESH_SECONDS", "3600"))
# ارزهایی که هرگز اسکن نمی‌شوند (مثلاً استیبل‌ها یا نمادهای کم‌عمق).
SYMBOL_BLACKLIST = tuple(
    x.strip().upper() for x in os.getenv("SYMBOL_BLACKLIST", "USDC,FDUSD,TUSD").split(",") if x.strip()
)
# حداقل حجم ۲۴ ساعته برای اینکه یک ارز اصلاً وارد فهرست اسکن شود.
MIN_24H_QUOTE_VOLUME = float(os.getenv("MIN_24H_QUOTE_VOLUME", "2000000"))

# --- تایم‌فریم‌ها ---
ENTRY_TIMEFRAME = os.getenv("ENTRY_TIMEFRAME", "15m").strip()
TREND_TIMEFRAME = os.getenv("TREND_TIMEFRAME", "1h").strip()
ENTRY_CANDLE_LIMIT = int(os.getenv("ENTRY_CANDLE_LIMIT", "250"))
TREND_CANDLE_LIMIT = int(os.getenv("TREND_CANDLE_LIMIT", "250"))

# --- سرمایه ---
FALLBACK_CAPITAL_USDT = float(os.getenv("FALLBACK_CAPITAL_USDT", "0"))
BALANCE_REFRESH_SECONDS = int(os.getenv("BALANCE_REFRESH_SECONDS", "60"))
VIRTUAL_START_CAPITAL_USDT = float(os.getenv("VIRTUAL_START_CAPITAL_USDT", "50"))
MIN_CAPITAL_TO_TRADE_USDT = float(os.getenv("MIN_CAPITAL_TO_TRADE_USDT", "10"))
# سقف درصدی از کل سرمایه که همهٔ پوزیشن‌های باز روی هم مجازند درگیر کنند.
MAX_CAPITAL_ENGAGED_RATE = float(os.getenv("MAX_CAPITAL_ENGAGED_RATE", "0.6"))

# --- تعداد پوزیشن هم‌زمان ---
# از تلگرام با «پوزیشن ۵» تغییر می‌کند. سرمایه بین این تعداد اسلات پخش می‌شود.
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "3"))
MAX_CONCURRENT_LIMIT = 30
# هر ارز حداکثر یک پوزیشن باز دارد (نه چند پوزیشن روی یک نماد).
ONE_POSITION_PER_SYMBOL = True

# --- لوریج و مارجین ---
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "5"))
LEVERAGE_MIN = 1
LEVERAGE_MAX = int(os.getenv("LEVERAGE_MAX", "10"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "ISOLATED").strip().upper()
MAINTENANCE_MARGIN_RATE = float(os.getenv("MAINTENANCE_MARGIN_RATE", "0.005"))
# حد ضرر باید همیشه خیلی زودتر از لیکوئید فعال شود.
LIQUIDATION_TO_STOP_BUFFER = float(os.getenv("LIQUIDATION_TO_STOP_BUFFER", "2.0"))

# --- امتیازدهی سه‌بخشی ---
# هر بخش عددی بین ۰ تا ۱۰۰ به سمت لانگ و ۰ تا ۱۰۰ به سمت شورت می‌دهد؛
# امتیاز نهایی میانگین وزن‌دار همان‌هاست. ورود فقط بالای آستانه.
WEIGHT_TREND = float(os.getenv("WEIGHT_TREND", "0.35"))
WEIGHT_MOMENTUM = float(os.getenv("WEIGHT_MOMENTUM", "0.40"))
WEIGHT_VOLUME = float(os.getenv("WEIGHT_VOLUME", "0.25"))
# آستانهٔ ورود؛ از تلگرام با «امتیاز ۸۰» تغییر می‌کند.
SCORE_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "80"))
SCORE_THRESHOLD_MIN = 55.0
SCORE_THRESHOLD_MAX = 95.0
# اگر امتیاز جهت مخالف هم بالا باشد، بازار مبهم است و ورود لغو می‌شود.
MAX_OPPOSITE_SCORE = float(os.getenv("MAX_OPPOSITE_SCORE", "55"))

# --- اندیکاتورها ---
EMA_FAST = int(os.getenv("EMA_FAST", "50"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "200"))
EMA_SLOPE_LOOKBACK = int(os.getenv("EMA_SLOPE_LOOKBACK", "10"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "32"))
RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "68"))
MACD_FAST = int(os.getenv("MACD_FAST", "12"))
MACD_SLOW = int(os.getenv("MACD_SLOW", "26"))
MACD_SIGNAL = int(os.getenv("MACD_SIGNAL", "9"))
VOLUME_SMA_PERIOD = int(os.getenv("VOLUME_SMA_PERIOD", "20"))
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))

# --- فیلتر بازار رنج (نسبت کارایی کافمن) ---
# در رنج، قیمت زیاد نوسان می‌کند ولی جایی نمی‌رود؛ بدون این فیلتر هر نوسان
# محلی به‌اشتباه روند خوانده می‌شود و ربات در سقف و کف رنج پوزیشن باز می‌کند.
EFFICIENCY_PERIOD = int(os.getenv("EFFICIENCY_PERIOD", "20"))
MIN_EFFICIENCY_RATIO = float(os.getenv("MIN_EFFICIENCY_RATIO", "0.25"))

ALLOW_LONG = os.getenv("ALLOW_LONG", "1").strip() not in {"0", "false", "no"}
ALLOW_SHORT = os.getenv("ALLOW_SHORT", "1").strip() not in {"0", "false", "no"}

# --- خروج: حد ضرر و حد سود ---
# حد ضرر بر پایهٔ ATR واقعی هر ارز (نه درصد ثابت) — نوسان هر ارز فرق دارد.
STOP_ATR_MULTIPLIER = float(os.getenv("STOP_ATR_MULTIPLIER", "1.5"))
# حد سود = ریسک × این نسبت. با نسبت ۲، حتی نرخ برد ۴۰٪ هم سودده می‌ماند.
RISK_REWARD_RATIO = float(os.getenv("RISK_REWARD_RATIO", "2.0"))
# کف و سقف فاصلهٔ حد ضرر تا قیمت (جلوگیری از استاپ بیش از حد نزدیک یا دور).
MIN_STOP_DISTANCE_RATE = float(os.getenv("MIN_STOP_DISTANCE_RATE", "0.004"))
MAX_STOP_DISTANCE_RATE = float(os.getenv("MAX_STOP_DISTANCE_RATE", "0.05"))
# خروج زودهنگام اگر مومنتوم کاملاً برگردد (قبل از رسیدن به حد سود یا ضرر).
EARLY_EXIT_ON_REVERSAL = os.getenv("EARLY_EXIT_ON_REVERSAL", "1").strip() not in {"0", "false", "no"}
# امتیاز جهت مخالف که برای خروج زودهنگام لازم است.
REVERSAL_EXIT_SCORE = float(os.getenv("REVERSAL_EXIT_SCORE", "78"))
# حداکثر عمر یک پوزیشن؛ بعد از آن اگر نه سود نه ضرر، بسته می‌شود تا اسلات آزاد شود.
MAX_POSITION_AGE_MINUTES = int(os.getenv("MAX_POSITION_AGE_MINUTES", "480"))
# حداقل سود خالص (بعد از کارمزد و اسلیپیج) که بستن ارزش داشته باشد.
MIN_NET_PROFIT_USDT = float(os.getenv("MIN_NET_PROFIT_USDT", "0.05"))

# --- ایمنی اجرا ---
MAX_ENTRY_SPREAD_RATE = float(os.getenv("MAX_ENTRY_SPREAD_RATE", "0.0015"))
# حداقل نسبت «سود مورد انتظار به هزینهٔ رفت‌وبرگشت»؛ زیر این، ورود بی‌معناست
# چون کارمزد سود را می‌خورد. این همان شرط «بعد از کارمزد صرف کند» است.
MIN_PROFIT_TO_COST_RATIO = float(os.getenv("MIN_PROFIT_TO_COST_RATIO", "2.5"))

# --- حالت ترید ---
DEFAULT_REAL_TRADING_ENABLED = False

# --- اقتصاد معامله ---
TAKER_FEE_RATE = float(os.getenv("TOOBIT_TAKER_FEE_RATE", "0.0005"))
ROUND_TRIP_SLIPPAGE_RATE = float(os.getenv("ROUND_TRIP_SLIPPAGE_RATE", "0.0006"))
FUNDING_RESERVE_RATE = float(os.getenv("FUNDING_RESERVE_RATE", "0.0002"))

# --- زمان‌بندی حلقه‌ها ---
CONTRACT_REFRESH_SECONDS = int(os.getenv("CONTRACT_REFRESH_SECONDS", "300"))
SCAN_INTERVAL_SECONDS = float(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
POSITION_MONITOR_SECONDS = float(os.getenv("POSITION_MONITOR_SECONDS", "5"))
REAL_MONITOR_SECONDS = int(os.getenv("REAL_MONITOR_SECONDS", "60"))
ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS = int(os.getenv("ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS", "180"))

# --- دیتابیس ---
SQLITE_BUSY_TIMEOUT_MS = 5000

# --- سقف اختیاری سرمایهٔ درگیر ---
CAPITAL_CAP_USDT = float(os.getenv("CAPITAL_CAP_USDT", "0"))
CAPITAL_CAP_MIN = 5.0
CAPITAL_CAP_MAX = 100_000.0

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
