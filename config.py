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
#  استراتژی جدید: ورود پله‌ای هوشمند روی یک ارز (DOGE)
# ============================================================

# --- ارز هدف ---
# فقط روی یک نماد کار می‌کنیم. برای تغییر ارز فقط این مقدار عوض می‌شود.
TARGET_SYMBOL = os.getenv("TARGET_SYMBOL", "DOGE-SWAP-USDT").strip().upper()

# --- سرمایه و سقف ریسک ---
# سرمایه هرگز عدد ثابت نیست: قبل از هر چرخه، موجودی واقعی حساب از صرافی
# خوانده می‌شود و همهٔ محاسبات پله/لوریج/لیکوئید بر پایهٔ همان انجام می‌شود.
# این مقدار فقط پشتیبان است؛ اگر به هر دلیل خواندن موجودی شکست بخورد
# و مقدار پشتیبان هم صفر باشد، ربات وارد ترید واقعی نمی‌شود.
FALLBACK_CAPITAL_USDT = float(os.getenv("FALLBACK_CAPITAL_USDT", "0"))
# حداکثر عمر مجاز آخرین موجودی خوانده‌شده؛ بعد از آن دوباره از صرافی می‌گیرد.
BALANCE_REFRESH_SECONDS = int(os.getenv("BALANCE_REFRESH_SECONDS", "60"))
# در حالت مجازی (paper) موجودی فرضی شروع، اگر تاریخچه‌ای موجود نباشد.
VIRTUAL_START_CAPITAL_USDT = float(os.getenv("VIRTUAL_START_CAPITAL_USDT", "40"))
# حداقل موجودی لازم برای باز کردن چرخه؛ زیر این مقدار، ورود انجام نمی‌شود.
MIN_CAPITAL_TO_TRADE_USDT = float(os.getenv("MIN_CAPITAL_TO_TRADE_USDT", "10"))

# سقف درصدی از کل سرمایه که همهٔ پله‌ها روی هم مجازند درگیر کنند.
# ۰.۵ یعنی حتی با همهٔ پله‌ها، بیش از نصف سرمایه در ریسک نیست.
# این نسبت است، نه عدد دلاری — پس با کم و زیاد شدن سرمایه، خودش مقیاس می‌خورد.
MAX_CAPITAL_ENGAGED_RATE = float(os.getenv("MAX_CAPITAL_ENGAGED_RATE", "0.5"))
# حداکثر ضرر مجاز کل چرخه (درصدی از سرمایهٔ درگیر). با رسیدن به آن، حد ضرر سخت.
MAX_CYCLE_LOSS_RATE = float(os.getenv("MAX_CYCLE_LOSS_RATE", "0.35"))

# --- پله‌بندی ---
# حداکثر تعداد پله‌های مجاز در یک چرخه (شامل پلهٔ اول).
MAX_ENTRY_STEPS = int(os.getenv("MAX_ENTRY_STEPS", "3"))
MAX_ENTRY_STEPS_LIMIT = 6  # سقف سخت؛ حتی با تنظیم دستی از این بالاتر نمی‌رود.
# ضریب رشد اندازهٔ هر پله نسبت به پلهٔ قبل (۱.۵ = رشد ملایم، نه مارتینگل ۲x).
STEP_SIZE_MULTIPLIER = float(os.getenv("STEP_SIZE_MULTIPLIER", "1.5"))
# فاصلهٔ لازم برای فعال شدن پلهٔ بعدی، بر حسب ضریب ATR (نه درصد ثابت).
STEP_TRIGGER_ATR_MULTIPLIER = float(os.getenv("STEP_TRIGGER_ATR_MULTIPLIER", "1.0"))
# کف و سقف فاصلهٔ پله‌ها تا نویز عادی بازار پله را بی‌جهت فعال نکند.
MIN_STEP_GAP_PERCENT = float(os.getenv("MIN_STEP_GAP_PERCENT", "0.025"))
MAX_STEP_GAP_PERCENT = float(os.getenv("MAX_STEP_GAP_PERCENT", "0.06"))

# --- لوریج و ایمنی لیکوئید ---
DEFAULT_STAGED_LEVERAGE = int(os.getenv("DEFAULT_STAGED_LEVERAGE", "5"))
STAGED_LEVERAGE_MIN = 1
STAGED_LEVERAGE_MAX = 10  # سقف سخت برای این استراتژی؛ بالاتر ریسک لیکوئید را واقعی می‌کند.
MARGIN_MODE = os.getenv("MARGIN_MODE", "ISOLATED").strip().upper()
# --- ایمنی لیکوئید ---
# نکتهٔ کلیدی: فاصلهٔ لیکوئید ریاضاً تقریباً برابر ۱÷لوریج است و با اضافه شدن
# پله‌ها دورتر نمی‌شود. پس «لیکوئید غیرممکن» با اصرار روی فاصلهٔ بزرگ به دست
# نمی‌آید (آن فقط لوریج ۱ را مجاز می‌کند که سودش از کارمزد کمتر است).
# محافظت واقعی این است: حد ضرر سخت همیشه خیلی زودتر از لیکوئید فعال شود.
# نسبت زیر یعنی فاصلهٔ لیکوئید باید حداقل این چند برابر فاصلهٔ حد ضرر باشد.
LIQUIDATION_TO_STOP_BUFFER = float(os.getenv("LIQUIDATION_TO_STOP_BUFFER", "2.0"))
# کف مطلق فاصلهٔ لیکوئید از میانگین ورود پس از آخرین پله (پشتیبان دوم).
MIN_LIQUIDATION_DISTANCE_FINAL_RATE = float(os.getenv("MIN_LIQUIDATION_DISTANCE_FINAL_RATE", "0.08"))
# حاشیهٔ نگهداری صرافی (Maintenance Margin) برای محاسبهٔ محافظه‌کارانهٔ لیکوئید.
MAINTENANCE_MARGIN_RATE = float(os.getenv("MAINTENANCE_MARGIN_RATE", "0.005"))

# --- تشخیص جهت روند (تایم‌فریم بالادست) ---
TREND_TIMEFRAMES = tuple(
    x.strip() for x in os.getenv("TREND_TIMEFRAMES", "4h,1d").split(",") if x.strip()
)
ENTRY_TIMEFRAME = os.getenv("ENTRY_TIMEFRAME", "15m").strip()
TREND_EMA_FAST = int(os.getenv("TREND_EMA_FAST", "20"))
TREND_EMA_SLOW = int(os.getenv("TREND_EMA_SLOW", "50"))
# حداقل تعداد تأییدهای هم‌جهت لازم برای باز کردن چرخه.
MIN_TREND_CONFIRMATIONS = int(os.getenv("MIN_TREND_CONFIRMATIONS", "2"))
# اگر روند خنثی/نامشخص بود، هیچ چرخه‌ای باز نمی‌شود.
ALLOW_LONG = os.getenv("ALLOW_LONG", "1").strip() not in {"0", "false", "no"}
ALLOW_SHORT = os.getenv("ALLOW_SHORT", "1").strip() not in {"0", "false", "no"}

# --- خروج ---
# حد سود بر حسب ضریب ATR؛ هرگز کمتر از کف سود خالص بستن نمی‌شود.
TAKE_PROFIT_ATR_MULTIPLIER = float(os.getenv("TAKE_PROFIT_ATR_MULTIPLIER", "1.2"))
MIN_TAKE_PROFIT_PERCENT = float(os.getenv("MIN_TAKE_PROFIT_PERCENT", "0.008"))
MAX_TAKE_PROFIT_PERCENT = float(os.getenv("MAX_TAKE_PROFIT_PERCENT", "0.05"))
# حد ضرر سخت پس از آخرین پله (بر حسب ضریب ATR از میانگین ورود).
HARD_STOP_ATR_MULTIPLIER = float(os.getenv("HARD_STOP_ATR_MULTIPLIER", "2.5"))
# حداقل سود خالص (بعد از کارمزد و اسپرد) که ارزش بستن داشته باشد.
MIN_NET_PROFIT_USDT = float(os.getenv("MIN_NET_PROFIT_USDT", "0.30"))

# --- ایمنی اجرا ---
# اگر اسپرد لحظه‌ای از این بیشتر بود، ورود انجام نمی‌شود.
MAX_ENTRY_SPREAD_RATE = float(os.getenv("MAX_ENTRY_SPREAD_RATE", "0.0015"))
# هشدار تلگرام هنگام مصرف آخرین پله.
WARN_ON_FINAL_STEP = os.getenv("WARN_ON_FINAL_STEP", "1").strip() not in {"0", "false", "no"}

# --- حالت ترید ---
# پیش‌فرض همیشه مجازی است؛ ترید واقعی فقط با دستور تلگرام فعال می‌شود.
DEFAULT_REAL_TRADING_ENABLED = False

# --- ثابت‌های تحلیل ---
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))

# --- اقتصاد معامله (از نسخهٔ قبلی، بدون تغییر) ---
TAKER_FEE_RATE = float(os.getenv("TOOBIT_TAKER_FEE_RATE", "0.0005"))
ROUND_TRIP_SLIPPAGE_RATE = float(os.getenv("ROUND_TRIP_SLIPPAGE_RATE", "0.0006"))
FUNDING_RESERVE_RATE = float(os.getenv("FUNDING_RESERVE_RATE", "0.0002"))

# --- زمان‌بندی حلقه‌ها ---
CONTRACT_REFRESH_SECONDS = int(os.getenv("CONTRACT_REFRESH_SECONDS", "300"))
TREND_REFRESH_SECONDS = int(os.getenv("TREND_REFRESH_SECONDS", "300"))
PRICE_CHECK_SECONDS = float(os.getenv("PRICE_CHECK_SECONDS", "5"))
POSITION_MONITOR_SECONDS = float(os.getenv("POSITION_MONITOR_SECONDS", "5"))
REAL_MONITOR_SECONDS = int(os.getenv("REAL_MONITOR_SECONDS", "60"))
ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS = int(os.getenv("ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS", "180"))

# --- دیتابیس ---
SQLITE_BUSY_TIMEOUT_MS = 5000

# --- محدودهٔ تنظیمات قابل تغییر از تلگرام ---
# سقف اختیاری روی سرمایهٔ درگیر: اگر کاربر بخواهد حتی وقتی موجودی حساب زیاد
# است، ربات فقط تا سقف مشخصی وارد شود. صفر یعنی بدون سقف (کل موجودی مبناست).
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
