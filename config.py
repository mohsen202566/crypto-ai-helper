"""تنظیمات ثابت و پیش‌فرض ربات تطبیقی کریپتو.

هیچ کلید محرمانه‌ای در این فایل قرار نمی‌گیرد. همه کلیدها از Environment خوانده می‌شوند.
تنظیمات قابل تغییر کاربر (مارجین، لوریج، حداکثر پوزیشن و...) در runtime.db ذخیره می‌شوند.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_DB = Path(os.getenv("RUNTIME_DB_PATH", PROJECT_ROOT / "runtime.db"))
LEARNING_DB = Path(os.getenv("LEARNING_DB_PATH", PROJECT_ROOT / "learning.db"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", PROJECT_ROOT / "backups"))

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_POLL_TIMEOUT = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "20"))

# Toobit credentials
TOOBIT_API_KEY = os.getenv("TOOBIT_API_KEY", "").strip()
TOOBIT_API_SECRET = os.getenv("TOOBIT_API_SECRET", os.getenv("TOOBIT_SECRET_KEY", "")).strip()
TOOBIT_BASE_URL = os.getenv("TOOBIT_BASE_URL", "https://api.toobit.com").rstrip("/")
TOOBIT_RECV_WINDOW = int(os.getenv("TOOBIT_RECV_WINDOW", "5000"))

# Public market-data endpoints
OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
BYBIT_BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com").rstrip("/")
BINANCE_FUTURES_BASE_URL = os.getenv("BINANCE_FUTURES_BASE_URL", "https://fapi.binance.com").rstrip("/")

REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "1"))
HTTP_BACKOFF_SECONDS = float(os.getenv("HTTP_BACKOFF_SECONDS", "0.8"))

# Public market-data protection. One source is never hammered after a 429/network
# failure; the whole bot temporarily moves to the next exchange instead.
MARKET_DATA_SOURCE_ORDER = tuple(
    item.strip().upper()
    for item in os.getenv("MARKET_DATA_SOURCE_ORDER", "OKX,BYBIT,BINANCE").split(",")
    if item.strip()
)
MARKET_DATA_MIN_SOURCES = max(1, min(3, int(os.getenv("MARKET_DATA_MIN_SOURCES", "2"))))
MARKET_DATA_OKX_CONCURRENCY = max(1, int(os.getenv("MARKET_DATA_OKX_CONCURRENCY", "2")))
MARKET_DATA_BYBIT_CONCURRENCY = max(1, int(os.getenv("MARKET_DATA_BYBIT_CONCURRENCY", "4")))
MARKET_DATA_BINANCE_CONCURRENCY = max(1, int(os.getenv("MARKET_DATA_BINANCE_CONCURRENCY", "4")))
MARKET_DATA_MIN_REQUEST_INTERVAL_SECONDS = max(
    0.0, float(os.getenv("MARKET_DATA_MIN_REQUEST_INTERVAL_SECONDS", "0.15"))
)
MARKET_DATA_FAILURES_BEFORE_COOLDOWN = max(
    1, int(os.getenv("MARKET_DATA_FAILURES_BEFORE_COOLDOWN", "2"))
)
MARKET_DATA_NETWORK_COOLDOWN_SECONDS = max(
    5, int(os.getenv("MARKET_DATA_NETWORK_COOLDOWN_SECONDS", "30"))
)
MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS = max(
    15, int(os.getenv("MARKET_DATA_RATE_LIMIT_COOLDOWN_SECONDS", "90"))
)
MARKET_DATA_FORBIDDEN_COOLDOWN_SECONDS = max(
    60, int(os.getenv("MARKET_DATA_FORBIDDEN_COOLDOWN_SECONDS", "900"))
)
MARKET_DATA_FALLBACK_LOG_SECONDS = max(
    10, int(os.getenv("MARKET_DATA_FALLBACK_LOG_SECONDS", "60"))
)
TICKER_STALE_GRACE_SECONDS = max(30, int(os.getenv("TICKER_STALE_GRACE_SECONDS", "120")))
BINANCE_KLINE_PAGE_LIMIT = max(100, min(1500, int(os.getenv("BINANCE_KLINE_PAGE_LIMIT", "500"))))

# Universe
UNIVERSE_SIZE = 100
ACTIVE_SYMBOLS = 35
PROFILE_DAYS = 7
PROFILE_BAR = "5m"
PROFILE_5M_CANDLES = PROFILE_DAYS * 24 * 12  # 2016
MIN_PROFILE_CANDLES = 1200

# Runtime timing
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
TICKER_REFRESH_SECONDS = int(os.getenv("TICKER_REFRESH_SECONDS", "10"))
ANALYSIS_CANDLE_CACHE_FRESH_SECONDS = int(os.getenv("ANALYSIS_CANDLE_CACHE_FRESH_SECONDS", "20"))
VIRTUAL_MONITOR_SECONDS = int(os.getenv("VIRTUAL_MONITOR_SECONDS", "10"))
REAL_MONITOR_SECONDS = 60
PENDING_CONFIRM_AFTER_SECONDS = 70
TOOBIT_SNAPSHOT_MAX_AGE_SECONDS = int(os.getenv("TOOBIT_SNAPSHOT_MAX_AGE_SECONDS", "180"))
VALIDATOR_INTERVAL_SECONDS = int(os.getenv("VALIDATOR_INTERVAL_SECONDS", "300"))
BACKUP_INTERVAL_SECONDS = int(os.getenv("BACKUP_INTERVAL_SECONDS", "21600"))
PROFILE_REFRESH_SECONDS = int(os.getenv("PROFILE_REFRESH_SECONDS", "21600"))
PROFILE_REFRESH_STEP_SECONDS = int(
    os.getenv("PROFILE_REFRESH_STEP_SECONDS", str(max(300, PROFILE_REFRESH_SECONDS // ACTIVE_SYMBOLS)))
)

# User-controlled ranges
TRADE_MARGIN_MIN = 1.0
TRADE_MARGIN_MAX = 10_000.0
LEVERAGE_MIN = 1
LEVERAGE_MAX = 100
MAX_POSITIONS_MIN = 1
MAX_POSITIONS_MAX = 200

# Defaults. Trading is forcibly OFF on every process start.
DEFAULT_TRADE_MARGIN_USDT = float(os.getenv("DEFAULT_TRADE_MARGIN_USDT", "5"))
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "10"))
DEFAULT_MAX_OPEN_POSITIONS = int(os.getenv("DEFAULT_MAX_OPEN_POSITIONS", "3"))
DEFAULT_MIN_NET_PROFIT_USDT = 0.05
DEFAULT_RR = 2.0
DEFAULT_ENTRY_TIMEFRAME = "15m"

# Higher-timeframe trading architecture. The trade timeframe owns direction,
# strength and TP/SL geometry; a lower timeframe only times the entry.
TRADE_TIMEFRAMES = ("30m", "1H", "4H", "1D")
ENTRY_TIMEFRAME_OPTIONS = {
    "30m": ("15m", "5m"),
    "1H": ("15m", "5m", "30m"),
    "4H": ("1H", "30m"),
    "1D": ("4H", "1H"),
}
DEFAULT_ENTRY_BY_TRADE_TIMEFRAME = {
    "30m": "15m",
    "1H": "15m",
    "4H": "1H",
    "1D": "4H",
}
RR_BOUNDS_BY_TIMEFRAME = {
    "30m": (1.50, 2.50),
    "1H": (1.70, 3.00),
    "4H": (2.00, 4.00),
    "1D": (2.20, 5.00),
}
RR_DEFAULT_BY_TIMEFRAME = {
    "30m": 2.00,
    "1H": 2.20,
    "4H": 2.80,
    "1D": 3.50,
}
HOLD_MINUTES_BY_TIMEFRAME = {
    "30m": (30, 240),
    "1H": (60, 480),
    "4H": (240, 2880),
    "1D": (1440, 20160),
}
ANALYSIS_CANDLE_LIMITS = {
    "5m": 500,
    "15m": 420,
    "30m": 420,
    "1H": 420,
    "4H": 320,
    "1D": 260,
}

# Cost model. Conservative Taker/Taker until real executions recalibrate it.
TOOBIT_TAKER_FEE_RATE = float(os.getenv("TOOBIT_TAKER_FEE_RATE", "0.0005"))
DEFAULT_SLIPPAGE_RATE_ROUND_TRIP = float(os.getenv("DEFAULT_SLIPPAGE_RATE_ROUND_TRIP", "0.0004"))
DEFAULT_FUNDING_RESERVE_RATE = float(os.getenv("DEFAULT_FUNDING_RESERVE_RATE", "0.0001"))

# Signal thresholds are deliberately soft at startup.
INITIAL_MIN_SCORE = 47.0
INITIAL_MIN_DIRECTION = 50.0
INITIAL_MIN_ENTRY = 44.0
MEDIUM_MIN_SCORE = 52.0
MEDIUM_MIN_DIRECTION = 53.0
MEDIUM_MIN_ENTRY = 48.0
REAL_MIN_SCORE = 54.0  # Only a tiny sanity step above Medium; never a second wall.
REAL_MIN_DIRECTION = 54.0
REAL_MIN_ENTRY = 49.0
MIN_DATA_QUALITY = 68.0
MIN_DIRECTION_EDGE = 3.0

# Scenario budget
SCENARIOS_DEFAULT_PER_SIGNAL = 6
SCENARIOS_MIN_PER_SIGNAL = 2
SCENARIOS_MAX_PER_SIGNAL = 10
MAX_LIVE_SCENARIOS = 180
SCENARIO_CPU_HIGH_WATER = 0.75

# Promotion floors. Validator can require more when variance is high.
PROMOTE_INITIAL_MIN_RESULTS = 15
PROMOTE_MEDIUM_MIN_RESULTS = 25
RELEARN_MIN_MEDIUM_RESULTS = 12
PROMOTION_MIN_PROFIT_FACTOR = 1.08
CHALLENGER_CONFIRM_MIN_RESULTS = 6
PROMOTION_WIN_EDGE_OVER_BREAKEVEN = 0.03
REAL_DEMOTION_STOP_STREAK = 2

# Post-result analysis is non-blocking and never holds symbol locks.
POST_RESULT_MIN_MINUTES = 120
POST_RESULT_MAX_MINUTES = 10080

# Symbol failure handling
SYMBOL_ERROR_COOLDOWN_AFTER = 3
SYMBOL_ERROR_REPLACE_AFTER = 12
SYMBOL_COOLDOWN_SECONDS = 300
REJECT_LOG_RATE_SECONDS = 60

# SQLite
RUNTIME_SCHEMA_VERSION = 2
LEARNING_SCHEMA_VERSION = 3
SQLITE_BUSY_TIMEOUT_MS = 5000

# Fixed analysis-tool names and initial weights.
BASE_TOOL_WEIGHTS = {
    "market_structure": 0.18,
    "ema": 0.16,
    "rsi": 0.12,
    "macd": 0.12,
    "adx_dmi": 0.12,
    "relative_volume": 0.10,
    "btc_eth_context": 0.12,
    "atr_natr": 0.08,
}

ENTRY_TIMEFRAMES = ("5m", "15m", "30m", "1H", "4H")
CONTEXT_TIMEFRAMES = ("30m", "1H", "4H", "1D")

# Endpoints can be overridden without code changes.
TOOBIT_PATH_EXCHANGE_INFO = os.getenv("TOOBIT_PATH_EXCHANGE_INFO", "/api/v1/exchangeInfo")
TOOBIT_PATH_BALANCE = os.getenv("TOOBIT_PATH_BALANCE", "/api/v1/futures/balance")
TOOBIT_PATH_POSITIONS = os.getenv("TOOBIT_PATH_POSITIONS", "/api/v1/futures/positions")
TOOBIT_PATH_OPEN_ORDERS = os.getenv("TOOBIT_PATH_OPEN_ORDERS", "/api/v1/futures/openOrders")
TOOBIT_PATH_MARGIN_MODE = os.getenv("TOOBIT_PATH_MARGIN_MODE", "/api/v1/futures/marginType")
TOOBIT_PATH_LEVERAGE = os.getenv("TOOBIT_PATH_LEVERAGE", "/api/v1/futures/leverage")
TOOBIT_PATH_POSITION_SETTINGS = os.getenv("TOOBIT_PATH_POSITION_SETTINGS", "/api/v1/futures/accountLeverage")
TOOBIT_PATH_ORDER = os.getenv("TOOBIT_PATH_ORDER", "/api/v1/futures/order")
TOOBIT_PATH_MARK_PRICE = os.getenv("TOOBIT_PATH_MARK_PRICE", "/api/v1/futures/markPrice")
TOOBIT_PATH_HISTORY_POSITIONS = os.getenv("TOOBIT_PATH_HISTORY_POSITIONS", "/api/v1/futures/historyPositions")
TOOBIT_PATH_ORDER_HISTORY = os.getenv("TOOBIT_PATH_ORDER_HISTORY", "/api/v1/futures/historyOrders")
TOOBIT_PATH_ORDER_HISTORY_ALT = os.getenv("TOOBIT_PATH_ORDER_HISTORY_ALT", "/api/v1/futures/order/history")

# Optional external macro/news blackout feed. Empty means disabled, not silently fabricated.
NEWS_CALENDAR_URL = os.getenv("NEWS_CALENDAR_URL", "").strip()
NEWS_BLOCK_BEFORE_MINUTES = 5
NEWS_BLOCK_AFTER_MINUTES = 5

# Candidate pool is intentionally larger than 100. At startup the registry keeps exactly
# 100 contracts live on Toobit and at least two public market-data sources.
# OKX stays primary; Bybit and Binance Futures are independent fallbacks.
CANDIDATE_BASE_ASSETS = tuple(dict.fromkeys("""
BTC ETH SOL XRP BNB DOGE ADA AVAX LINK DOT LTC BCH TRX TON SUI APT NEAR ATOM FIL ETC UNI AAVE ARB OP INJ SEI TIA RUNE ICP XLM HBAR ALGO VET THETA FTM POL MATIC EOS EGLD KAS STX IMX GRT LDO MKR COMP SNX CRV DYDX GMX PENDLE JUP WIF FLOKI ORDI SATS NOT ENA STRK ZK ZRO WLD TAO RNDR RENDER FET AGIX OCEAN ASI AXS SAND MANA GALA CHZ ENJ FLOW APE BLUR MAGIC GMT YGG ILV ENS SUSHI 1INCH ZRX KNC BAT LRC CELO CFX MINA ROSE ZIL IOTA IOTX QTUM NEO DASH ZEC XMR KAVA KSM WAVES ONDO OM JASMY ACH COTI SKL MASK API3 ARKM CYBER BIGTIME MEME PEOPLE BOME TURBO BRETT POPCAT MEW CATI HMSTR NEIRO ACT PNUT GOAT VIRTUAL MOVE SONIC S AIOZ IO CORE WOO XEC RVN ICX ONE ANKR CELR BAND NMR STORJ SSV RSR REZ ALT AEVO DYM MANTA METIS ZETA BLAST PORTAL PIXEL PYTH JTO JOE CAKE RAY SRM LPT AUDIO SUPER C98 HIGH ACE XAI NFP MAVIA AI EDU ID HOOK RDNT ARPA BADGER BAL UMA YFI OXT CTSI DUSK RLC POLYX GLM GAS ONG
""".split()))

# Multiplier contracts (for example 1000TOKEN) are never mapped as equivalent.
# Explicit rebrand families are accepted only through the lists below.
SYMBOL_EQUIVALENT_BASES: dict[str, frozenset[str]] = {
    "POL": frozenset({"POL", "MATIC"}),
    "RENDER": frozenset({"RENDER", "RNDR"}),
    "SONIC": frozenset({"SONIC", "S"}),
}

SYMBOL_ALIAS_OVERRIDES: dict[str, dict[str, tuple[str, ...]]] = {
    "BTC": {"okx": ("BTC-USDT-SWAP",), "bybit": ("BTCUSDT",), "binance": ("BTCUSDT",), "toobit": ("BTC-SWAP-USDT", "BTCUSDT")},
    "ETH": {"okx": ("ETH-USDT-SWAP",), "bybit": ("ETHUSDT",), "binance": ("ETHUSDT",), "toobit": ("ETH-SWAP-USDT", "ETHUSDT")},
    "POL": {"okx": ("POL-USDT-SWAP", "MATIC-USDT-SWAP"), "bybit": ("POLUSDT", "MATICUSDT"), "binance": ("POLUSDT", "MATICUSDT"), "toobit": ("POL-SWAP-USDT", "MATIC-SWAP-USDT", "POLUSDT", "MATICUSDT")},
    "RENDER": {"okx": ("RENDER-USDT-SWAP", "RNDR-USDT-SWAP"), "bybit": ("RENDERUSDT", "RNDRUSDT"), "binance": ("RENDERUSDT", "RNDRUSDT"), "toobit": ("RENDER-SWAP-USDT", "RNDR-SWAP-USDT", "RENDERUSDT", "RNDRUSDT")},
    "SONIC": {"okx": ("S-USDT-SWAP", "SONIC-USDT-SWAP"), "bybit": ("SUSDT", "SONICUSDT"), "binance": ("SUSDT", "SONICUSDT"), "toobit": ("S-SWAP-USDT", "SONIC-SWAP-USDT", "SUSDT", "SONICUSDT")},
}
