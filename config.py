"""تنظیمات اصلی ربات.
همه فایل‌ها در ریشه پروژه هستند؛ فایل .env یا example لازم نیست.
برای سرور واقعی می‌توان مقادیر را همینجا گذاشت یا از environment خواند.
"""
from __future__ import annotations

import os

# -----------------------------
# Telegram
# -----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_POLL_SECONDS = 1.0

# -----------------------------
# OKX public data - تمام دیتاهای تحلیل از OKX
# -----------------------------
OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com")
OKX_REQUEST_TIMEOUT = 8
OKX_BAR = "30m"
OKX_PRIMARY_BAR = "30m"
OKX_CONTEXT_BAR = "1H"
OKX_SENSOR_BAR = "5m"
OKX_CANDLE_LIMIT = 260

# -----------------------------
# Toobit futures trading - ترید واقعی
# -----------------------------
TOOBIT_BASE_URL = os.getenv("TOOBIT_BASE_URL", "https://api.toobit.com")
TOOBIT_API_KEY = os.getenv("TOOBIT_API_KEY", "")
TOOBIT_API_SECRET = os.getenv("TOOBIT_API_SECRET", os.getenv("TOOBIT_SECRET_KEY", ""))
REQUEST_TIMEOUT = 8
RECV_WINDOW = 5000

# مسیرهای فیوچرز توبیت قابل تنظیم هستند چون نسخه‌های API ممکن است متفاوت باشند.
TOOBIT_FUTURES_PATH_EXCHANGE_INFO = os.getenv("TOOBIT_FUTURES_PATH_EXCHANGE_INFO", "/api/v1/futures/exchangeInfo")
TOOBIT_FUTURES_PATH_BALANCE = os.getenv("TOOBIT_FUTURES_PATH_BALANCE", "/api/v1/futures/balance")
TOOBIT_FUTURES_PATH_ORDER = os.getenv("TOOBIT_FUTURES_PATH_ORDER", "/api/v1/futures/order")
TOOBIT_FUTURES_PATH_POSITIONS = os.getenv("TOOBIT_FUTURES_PATH_POSITIONS", "/api/v1/futures/position")
TOOBIT_FUTURES_PATH_LEVERAGE = os.getenv("TOOBIT_FUTURES_PATH_LEVERAGE", "/api/v1/futures/leverage")
TOOBIT_FUTURES_PATH_MARGIN_TYPE = os.getenv("TOOBIT_FUTURES_PATH_MARGIN_TYPE", "/api/v1/futures/marginType")
TOOBIT_FUTURES_PATH_ORDER_HISTORY = os.getenv("TOOBIT_FUTURES_PATH_ORDER_HISTORY", "/api/v1/futures/historyOrders")

# -----------------------------
# Core trading settings
# -----------------------------
TRADING_ENABLED_DEFAULT = False
AUTO_SIGNAL_ENABLED_DEFAULT = True
TRADE_USDT_DEFAULT = 10.0
LEVERAGE_DEFAULT = 10
MAX_POSITIONS_DEFAULT = 3
MAX_POSITIONS_MIN = 1
MAX_POSITIONS_MAX = 200
TRADE_USDT_MIN = 1.0
TRADE_USDT_MAX = 10000.0
LEVERAGE_MIN = 1
LEVERAGE_MAX = 100

RISK_REWARD = 1.35
MIN_NET_PROFIT_USDT = 0.05
FALLBACK_FEE_PCT_PER_SIDE = 0.06
SLIPPAGE_PCT_PER_SIDE = 0.02
ORDER_OPEN_CHECK_SECONDS = 70
ISOLATED_MARGIN_REQUIRED = True

# -----------------------------
# Strategy speed rules
# -----------------------------
ANALYSIS_INTERVAL_SECONDS = 20
SYMBOL_ERROR_BLACKLIST_SECONDS = 30 * 60
COMMAND_TARGET_RESPONSE_SECONDS = 1.0

# -----------------------------
# Pre-move and direction settings
# -----------------------------
COMPRESSION_LOOKBACK = 24
COMPRESSION_RECENT = 6
COMPRESSION_RATIO_MAX = 0.88
MIN_COMPRESSION_BARS = 30
PREMOVE_PRICE_MOVE_MAX_PCT = 0.55
FLOW_BIAS_LOOKBACK = 6
FLOW_BIAS_MIN_ABS = 0.10
ABSORPTION_MIN_SCORE = 0.58
# معماری 30-60M: سیگنال نباید خفه شود، اما ورود ضعیف هم ممنوع است.
MIN_SIGNAL_STRENGTH_SCORE = 70.0
ALLOW_WEAK_SIGNALS = False
SIGNAL_COOLDOWN_SECONDS_PER_SYMBOL = 25 * 60

# -----------------------------
# Smart SL/TP profiles
# -----------------------------
PROFILE_LOOKBACK_DAYS = 14
PROFILE_MIN_SIGNALS = 6
NOISE_PERCENTILE = 70
NOISE_SL_MULTIPLIER = 1.15
TP_PROFILE_PERCENTILE = 70
PROFILE_UPDATE_HOUR_UTC = 0
PROFILE_UPDATE_MINUTE_UTC = 5
VIRTUAL_MONITOR_MAX_MINUTES = 240
REQUIRE_PROFILE_READY = True
PROFILE_STALE_MAX_HOURS = 36
RISK_FALLBACK_MIN_SL_PCT = 0.55

# -----------------------------
# Storage
# -----------------------------
DB_PATH = os.getenv("BOT_DB_PATH", "bot_state.sqlite3")
LOG_LEVEL = os.getenv("BOT_LOG_LEVEL", "INFO")

# کش وضعیت اتصال و مارجین توبیت برای پنل سریع
TOOBIT_STATUS_INTERVAL_SECONDS = int(os.getenv("TOOBIT_STATUS_INTERVAL_SECONDS", "15"))

# -----------------------------
# Lightweight reject diagnostics
# -----------------------------
DEBUG_REJECTS = True
REJECT_SUMMARY_EVERY_CYCLES = 1
REJECT_DETAIL_LIMIT_PER_CYCLE = 8

# -----------------------------
# Watchlist / Start Hunter / Direction Lock (نسخه زنده جدید)
# -----------------------------
LIGHT_SCAN_INTERVAL_SECONDS = 25.0
WATCH_POLL_INTERVAL_SECONDS = 3.0
WATCH_TTL_SECONDS = 30 * 60
WATCH_MAX_SIDE_CHANGES = 0
WATCH_BAD_OBSERVATIONS_TO_REMOVE = 3
WATCH_CONFIRMATIONS_REQUIRED = 3

# PIOM / MDW: واچ بالغ، نه خام و نه دیرهنگام
PIOM_MIN_PREMOVE_SCORE = 58.0
MDW_MIN_WATCH_CONFIDENCE = 66.0
DWE_MIN_DIRECTION_FOR_WATCH = 66.0
DWE_INITIAL_GAP = 14.0
WATCH_COMPRESSION_SOFT_RATIO = 0.94
WATCH_VOLUME_RATIO_MIN = 1.10
WATCH_RANGE_RATIO_MIN = 1.06
WATCH_EARLY_FLOW_MIN = 0.035
WATCH_TENTATIVE_SIDE_MIN = 0.05

# DWE: قفل جهت با امتیاز + اختلاف + پایداری
DWE_LOCK_SCORE = 78.0
DWE_LOCK_GAP = 22.0
DWE_MAX_CONFLICT = 26.0
DWE_PERSISTENCE_REQUIRED = 3
DWE_FAST_LOCK_SCORE = 90.0
DWE_FAST_LOCK_GAP = 30.0

# IWG: شروع حرکت در جهت قفل‌شده، نه بریک‌اوت دیرهنگام
IWG_MIN_IGNITION_SCORE = 86.0
IWG_TRADE_SUPPORT_MIN = 0.12
IWG_BOOK_SUPPORT_MIN = 0.08
IWG_OPPOSITE_MAX = 0.10
WATCH_TRADE_IMBALANCE_MIN = 0.08
WATCH_BOOK_IMBALANCE_MIN = 0.05
WATCH_PRICE_RESPONSE_MIN_PCT = 0.015
WATCH_STRONG_CONFLICT = 0.16
WATCH_STRONG_TRADE_IMBALANCE = 0.24
WATCH_STRONG_BOOK_IMBALANCE = 0.20
WATCH_INTENSITY_ACCEL_MIN = 0.12
WATCH_MIN_START_DISPLACEMENT_PCT = 0.08
WATCH_LATE_EXPECTED_FRACTION = 0.38
WATCH_LATE_MIN_PCT = 0.16
WATCH_LATE_MAX_PCT = 0.72

# LTSF: فقط ضعف واضح را بلاک می‌کند تا حرکت‌های خوب خفه نشوند
LTSF_OBSERVATION_MAX = 12
LTSF_MIN_EFFICIENCY = 0.22
LTSF_MIN_HOLD_RATIO = 0.44
LTSF_PULLBACK_DAMAGE_MAX = 0.66
LTSF_EXPANSION_GRACE_SECONDS = 90
LTSF_BLOCK_WEAKNESS_COUNT = 3

# Proof-of-move: جلوگیری از سیگنال‌هایی که بعد از ورود MFE تقریباً صفر می‌دهند
PROOF_MIN_FAVORABLE_PCT = 0.10
PROOF_MIN_FAVORABLE_FRACTION_OF_LATE = 0.24
PROOF_RETAIN_RATIO = 0.55
PROOF_MAX_PULLBACK_DAMAGE = 0.48
PROOF_MIN_OBSERVATIONS_AFTER_LOCK = 4
PROOF_MIN_SECONDS_AFTER_LOCK = 40
PROOF_REQUIRED_COUNT = 2

# محافظ بازار چاپی: بعد از چند SL کم‌کیفیت پشت‌سرهم، ورودی‌ها موقتاً سخت/متوقف می‌شوند
CHOP_GUARD_ENABLED = True
CHOP_GUARD_LOOKBACK_MINUTES = 90
CHOP_GUARD_MIN_CLOSED = 3
CHOP_GUARD_SL_COUNT = 3
CHOP_GUARD_MAX_AVG_MFE = 0.16

WATCH_LOG_PROGRESS_SECONDS = 45
WATCH_SUMMARY_SECONDS = 60
OKX_MICRO_TRADES_LIMIT = 120
OKX_BOOK_DEPTH = 5
