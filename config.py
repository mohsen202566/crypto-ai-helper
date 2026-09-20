"""تنظیمات ربات V3 — Extreme Pump Fade (Short-Only) روی Toobit.

قوانین استراتژی FROZEN هستند: از ابتدای Paper Test تا پایان آن هیچ‌یک از
پارامترهای بخش «استراتژی» تغییر نمی‌کنند. تغییر آن‌ها بعد از دیدن نتایج،
همان دام Overfitting است که پنج فرضیهٔ قبلی را از بین برد.

تنظیمات اجرایی (دلار هر پوزیشن، تعداد پوزیشن، لوریج، استراحت) از پنل
تلگرام کنترل می‌شوند و جزو قوانین استراتژی نیستند.
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

BUILD_VERSION = "2026.09.11-v3-pumpfade-paper"
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
#  اسکن چندارزی — جهان ارزها (منطق استراتژی پایین‌تر است)
# ============================================================
# هر سیگنال = یک پوزیشن مستقل، بدون پله و مارتینگل.

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

# --- تایم‌فریم ---
# نتیجهٔ بک‌تست: ۱h و ۴h بهترین بودند؛ ۱h برای واکنش سریع‌تر به پامپ انتخاب شد.
ENTRY_TIMEFRAME = os.getenv("ENTRY_TIMEFRAME", "1h").strip()
ENTRY_CANDLE_LIMIT = int(os.getenv("ENTRY_CANDLE_LIMIT", "150"))

# --- سرمایه ---
FALLBACK_CAPITAL_USDT = float(os.getenv("FALLBACK_CAPITAL_USDT", "0"))
BALANCE_REFRESH_SECONDS = int(os.getenv("BALANCE_REFRESH_SECONDS", "60"))
VIRTUAL_START_CAPITAL_USDT = float(os.getenv("VIRTUAL_START_CAPITAL_USDT", "50"))
MIN_CAPITAL_TO_TRADE_USDT = float(os.getenv("MIN_CAPITAL_TO_TRADE_USDT", "10"))
# سقف درصدی از کل سرمایه که همهٔ پوزیشن‌های باز روی هم مجازند درگیر کنند.
MAX_CAPITAL_ENGAGED_RATE = float(os.getenv("MAX_CAPITAL_ENGAGED_RATE", "0.6"))

# --- اندازهٔ هر پوزیشن ---
# دلار مارجین هر پوزیشن. صفر = خودکار (سرمایه بین اسلات‌ها پخش می‌شود).
# از تلگرام با «دلار ۱۰» تنظیم می‌شود.
POSITION_SIZE_USDT = float(os.getenv("POSITION_SIZE_USDT", "0"))
POSITION_SIZE_MIN = 1.0
POSITION_SIZE_MAX = 1_000.0

# --- تعداد پوزیشن هم‌زمان ---
# از تلگرام با «پوزیشن ۵» تغییر می‌کند. سرمایه بین این تعداد اسلات پخش می‌شود.
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "3"))
MAX_CONCURRENT_LIMIT = 30
# هر ارز حداکثر یک پوزیشن باز دارد (نه چند پوزیشن روی یک نماد).
ONE_POSITION_PER_SYMBOL = True

# --- لوریج و مارجین ---
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "5"))
LEVERAGE_MIN = 1
# سقف مجاز تنظیم کاربر. لوریج بالا فاصلهٔ لیکوئید را کوچک می‌کند
# (فاصله ≈ ۱÷لوریج)، ولی حد ضرر همیشه خیلی زودتر از لیکوئید فعال می‌شود.
LEVERAGE_MAX = int(os.getenv("LEVERAGE_MAX", "100"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "ISOLATED").strip().upper()
MAINTENANCE_MARGIN_RATE = float(os.getenv("MAINTENANCE_MARGIN_RATE", "0.005"))
# حد ضرر باید همیشه خیلی زودتر از لیکوئید فعال شود.
LIQUIDATION_TO_STOP_BUFFER = float(os.getenv("LIQUIDATION_TO_STOP_BUFFER", "2.0"))

# ============================================================
#  استراتژی V3: Extreme Pump Fade  (FROZEN — تغییر ممنوع)
# ============================================================
# جریان:
#   24h Change >= WATCHLIST_MIN_GAIN_PCT  ->  Watchlist (اسکن هر 15 دقیقه)
#   Monitoring هر 5 دقیقه روی اعضای Watchlist
#   مسیر A (FAST_CASCADE):  شتاب نزولی + انبساط دامنه + انبساط حجم
#   مسیر B (NORMAL_REVERSAL): خستگی -> کاهش شتاب -> شکست کف تأییدشده
#   خروج: حد ضرر سخت (فوری) یا برگشت ساختاری تأییدشده یا تریلینگ سود
#   (TRAIL_PROFIT_PCT از بهترین قیمت، فقط در سود) -- بدون TP ثابت
#
# هیچ‌یک از اعداد این بخش در طول Paper Test تغییر نمی‌کند.

# --- دروازهٔ Watchlist ---
# تنها شرط کاندید شدن. ورود به Watchlist به معنی ورود به معامله نیست.
# WATCHLIST_MIN_GAIN_PCT فقط مقدار پیش‌فرض/اولیه است -- با دستور «واچ N»
# تو تلگرام در زمان اجرا قابل تغییره (۱ تا ۱۰۰۰٪)؛ خود الگوریتم ورود
# (Wick + Deceleration + Structure Break) هم‌چنان کاملاً فریز و دست‌نخورده است.
WATCHLIST_MIN_GAIN_PCT = float(os.getenv("WATCHLIST_MIN_GAIN_PCT", "15.0"))
WATCHLIST_THRESHOLD_MIN = 1.0
WATCHLIST_THRESHOLD_MAX = 1000.0
# اگر نماد زیر آستانه برگشت، فوراً حذف نمی‌شود؛ ممکن است دقیقاً وارد فاز
# برگشت شده باشد. این مهلت نگه‌داری بر حسب ساعت است.
WATCHLIST_RETENTION_HOURS = float(os.getenv("WATCHLIST_RETENTION_HOURS", "12.0"))

# --- تریلینگ سود (لایهٔ خروج دوم، در کنار برگشت ساختاری) ---
# اگر قیمت از بهترین نقطهٔ رسیده‌شده (کف، برای شورت) به اندازهٔ این درصد
# برگردد بالا -- فقط در حالی که هنوز واقعاً زیر قیمت ورود (در سود) هستیم --
# پوزیشن بسته می‌شود. مستقل از Hard Stop و برگشت ساختاری؛ هرکدام زودتر
# برسد همان اعمال می‌شود.
TRAIL_PROFIT_PCT = float(os.getenv("TRAIL_PROFIT_PCT", "0.0"))  # پیش‌فرض خاموش
TRAIL_PROFIT_PCT_MIN = 0.0    # ۰ = کاملاً خاموش
TRAIL_PROFIT_PCT_MAX = 20.0

# --- رزرو اسلات برای شکار پامپ‌های قوی ---
# نمادی که پامپش به این آستانه برسه «رزرو» می‌شه: اگه بعداً سیگنال بده و
# همهٔ اسلات‌ها پر باشه، جای ضعیف‌ترین پوزیشن باز (کمترین پامپ ورودی، اگه
# به‌قدر کافی قدیمی باشه) براش آزاد می‌شه. از پنل: «رزرو N».
RESERVE_THRESHOLD_PCT = float(os.getenv("RESERVE_THRESHOLD_PCT", "50.0"))
RESERVE_THRESHOLD_MIN = 1.0
RESERVE_THRESHOLD_MAX = 1000.0
# پوزیشنی که کمتر از این مدت (دقیقه) باز شده، هیچ‌وقت preempt نمی‌شود --
# حتی اگه ضعیف‌ترینه -- تا فرصت واقعی برای جواب دادن داشته باشد.
RESERVE_MIN_HOLD_MINUTES = float(os.getenv("RESERVE_MIN_HOLD_MINUTES", "15.0"))

# --- تی‌پی دلاری پیش‌فرض ---
# برخلاف قبل که پیش‌فرض خاموش بود، حالا خروج فقط دلاریه و از همون اول
# روشنه (کاربر می‌تونه با «تیپی N» عوضش کنه یا «تیپی خاموش» کاملاً خاموش کنه).
DEFAULT_FIXED_TP_USD = float(os.getenv("DEFAULT_FIXED_TP_USD", "5.0"))

# --- تاپ N: فقط N تای برتر واچ‌لیست معامله بشن ---
# ۰ = خاموش (هیچ محدودیتی، هر نماد واجد شرایط واچ‌لیست قابل معامله‌ست).
TOP_N_COUNT = int(os.getenv("TOP_N_COUNT", "3"))
TOP_N_MIN = 0
TOP_N_MAX = 18

# --- ورود Peak-Pullback (جایگزین کامل منطق قبلی) ---
# به محض این‌که قیمت لحظه‌ای از سقف ردیابی‌شده (از لحظه‌ی ورود به واچ‌لیست)
# به این‌اندازه برگرده، بلافاصله وارد میشیم -- بدون نیاز به بسته‌شدن کندل،
# بدون تأیید چندکندلی. از پنل: «برگشت N».
PULLBACK_ENTRY_PCT = float(os.getenv("PULLBACK_ENTRY_PCT", "3.0"))
PULLBACK_ENTRY_MIN = 0.5
PULLBACK_ENTRY_MAX = 30.0

# چرخه‌ی چک ورود Peak-Pullback (ثانیه) -- سریع و مستقل از اسکن ۱۵دقیقه‌ای
# واچ‌لیست، چون کل هدف همینه که منتظر چیزی نمونیم.
PEAK_PULLBACK_CHECK_SECONDS = float(os.getenv("PEAK_PULLBACK_CHECK_SECONDS", "5.0"))

# --- تایم‌فریم‌ها ---
# 15m = کشف کاندید / زمینه | 5m = مانیتور و اجرا
CONTEXT_TIMEFRAME = os.getenv("CONTEXT_TIMEFRAME", "15m").strip()
ENTRY_TIMEFRAME = os.getenv("ENTRY_TIMEFRAME", "5m").strip()
ENTRY_CANDLE_LIMIT = int(os.getenv("ENTRY_CANDLE_LIMIT", "120"))
# حداقل کندل لازم برای همهٔ محاسبات (ATR + pivot + baseline + deceleration)
MIN_CANDLES_REQUIRED = 40

# --- ۱) خستگی (Exhaustion) ---
# نسبت سایهٔ بالایی به کل دامنهٔ کندل. >= 0.5 یعنی بیش از نصف حرکت صعودی
# پس زده شده: خریدارها نتوانستند سطح را نگه دارند.
WICK_RATIO_MIN = float(os.getenv("WICK_RATIO_MIN", "0.5"))

# --- ۲) کاهش شتاب (Deceleration) ---
# بازدهی N کندل اخیر با N کندل قبل از آن مقایسه می‌شود.
DECEL_WINDOW_BARS = int(os.getenv("DECEL_WINDOW_BARS", "3"))

# --- ۳) ساختار (Swing Pivot) ---
# تعداد کندل تأیید در هر طرف. تأیید راست باعث تأخیر عمدی می‌شود
# (2 کندل 5 دقیقه‌ای = 10 دقیقه) — این Look-Ahead نیست، فقط دیرتر فهمیدن.
PIVOT_CONFIRM_BARS = int(os.getenv("PIVOT_CONFIRM_BARS", "2"))

# --- ۴) آبشار فروش (Cascade) ---
# ضریب انبساط برای دامنه و حجم نسبت به میانهٔ پنجرهٔ پایه.
CASCADE_EXPANSION_MULT = float(os.getenv("CASCADE_EXPANSION_MULT", "2.0"))
# پنجرهٔ پایه: 12 کندل 5 دقیقه‌ای = یک ساعت گذشته (همه بسته‌شده).
CASCADE_BASELINE_BARS = int(os.getenv("CASCADE_BASELINE_BARS", "12"))
# حداقل زمان سپری‌شده از شروع کندل جاری برای اینکه نرمال‌سازی نرخ معنی
# داشته باشد. زیر یک دقیقه، نمونه خیلی کوچک است و نویز تشدید می‌شود.
CASCADE_MIN_ELAPSED_SECONDS = float(os.getenv("CASCADE_MIN_ELAPSED_SECONDS", "60"))

# --- ۵) حد ضرر تطبیقی ---
# stop = آخرین سقف تأییدشده + k × ATR
# ATR در لحظهٔ ورود فریز می‌شود و بعد از آن هرگز بازمحاسبه نمی‌شود؛
# وگرنه ریسک پوزیشن بعد از ورود تغییر می‌کند.
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
STOP_ATR_MULT = float(os.getenv("STOP_ATR_MULT", "1.5"))

# --- جهت ---
# V3 فقط شورت است. لانگ وجود ندارد.
ALLOW_LONG = False
ALLOW_SHORT = True

# --- استراحت (Cooldown) ---
# بعد از هر خروج، همان نماد این تعداد ساعت قابل معامله نیست — حتی اگر
# دوباره پامپ کند یا سیگنال جدید بدهد. جلوگیری از Overtrading روی یک
# Pump Episode. از تلگرام با «استراحت ۲» تنظیم می‌شود.
COOLDOWN_HOURS = float(os.getenv("COOLDOWN_HOURS", "2"))
COOLDOWN_HOURS_MIN = 1
COOLDOWN_HOURS_MAX = 12

# --- مهلت نگه‌داشتن ---
# صفر = بدون مهلت. خروج فقط با حد ضرر یا برگشت ساختاری. این عدد صرفاً
# یک شبکهٔ ایمنی برای پوزیشن‌های فراموش‌شده است، نه بخشی از استراتژی.
MAX_HOLD_HOURS = float(os.getenv("MAX_HOLD_HOURS", "0"))

# --- ایمنی اجرا ---
MAX_ENTRY_SPREAD_RATE = float(os.getenv("MAX_ENTRY_SPREAD_RATE", "0.0015"))

# --- حالت ترید ---
DEFAULT_REAL_TRADING_ENABLED = False
# ترید مجازی مستقل روشن/خاموش می‌شود؛ اگر هر دو خاموش باشند ربات فقط اسکن می‌کند.
DEFAULT_VIRTUAL_TRADING_ENABLED = True

# --- اقتصاد معامله ---
TAKER_FEE_RATE = float(os.getenv("TOOBIT_TAKER_FEE_RATE", "0.0005"))
ROUND_TRIP_SLIPPAGE_RATE = float(os.getenv("ROUND_TRIP_SLIPPAGE_RATE", "0.0006"))
FUNDING_RESERVE_RATE = float(os.getenv("FUNDING_RESERVE_RATE", "0.0002"))

# --- گزارش‌دهی تلگرام ---
# گزارش لحظه‌ای پوزیشن‌های باز؛ صفر = خاموش. از تلگرام: «گزارش ۱۰»
LIVE_REPORT_MINUTES = int(os.getenv("LIVE_REPORT_MINUTES", "15"))
LIVE_REPORT_MIN = 0
LIVE_REPORT_MAX = 240
# خلاصهٔ روزانه در پایان هر روز
DAILY_SUMMARY_ENABLED = os.getenv("DAILY_SUMMARY_ENABLED", "1").strip() not in {"0", "false", "no"}

# --- زمان‌بندی حلقه‌ها ---
CONTRACT_REFRESH_SECONDS = int(os.getenv("CONTRACT_REFRESH_SECONDS", "300"))
# اسکن Watchlist: هر ۱۵ دقیقه. کشف کاندید کافی است همین‌قدر کند باشد.
WATCHLIST_SCAN_SECONDS = float(os.getenv("WATCHLIST_SCAN_SECONDS", "900"))
# مانیتور ورود: هر ۵ دقیقه. یک Cascade می‌تواند در همین بازه بخش بزرگی از
# حرکت را طی کند؛ انتظار برای کندل ۱۵ دقیقه‌ای یعنی از دست دادن آن.
MONITOR_INTERVAL_SECONDS = float(os.getenv("MONITOR_INTERVAL_SECONDS", "300"))
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
