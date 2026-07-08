"""Root config for the 5M ICE OKX -> Toobit futures bot.

Rules kept from the older bots:
- OKX is used for market data, analysis, and result monitoring.
- Toobit is used only for real execution through the unchanged toobit_client.py.
- Telegram commands/panel/statistics stay Persian and root-level for easy VPS deploy.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env(name, str(default))))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name, "1" if default else "0").lower()
    return value in {"1", "true", "yes", "y", "on", "فعال", "روشن"}


BOT_NAME = _env("BOT_NAME", "Crypto 5M ICE Toobit Bot")
BOT_DATA_DIR = _env("BOT_DATA_DIR", ".")
BOT_DB_PATH = _env("BOT_DB_PATH", "crypto_5m_ice.sqlite3")
LOG_LEVEL = _env("LOG_LEVEL", "INFO")

# Telegram
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")
OWNER_ID = _env("OWNER_ID")
TELEGRAM_POLL_TIMEOUT = _env_int("TELEGRAM_POLL_TIMEOUT", 25)

# OKX data only
OKX_BASE_URL = _env("OKX_BASE_URL", "https://www.okx.com")
OKX_CANDLE_LIMIT = _env_int("OKX_CANDLE_LIMIT", 260)
OKX_REQUEST_TIMEOUT = _env_int("OKX_REQUEST_TIMEOUT", 12)

# Toobit execution - unchanged toobit_client.py reads these names directly.
TOOBIT_API_KEY = _env("TOOBIT_API_KEY")
TOOBIT_API_SECRET = _env("TOOBIT_API_SECRET", _env("TOOBIT_SECRET_KEY"))
TOOBIT_SECRET_KEY = TOOBIT_API_SECRET
TOOBIT_BASE_URL = _env("TOOBIT_BASE_URL", "https://api.toobit.com")
REQUEST_TIMEOUT = _env_int("TOOBIT_TIMEOUT_SECONDS", 12)
RECV_WINDOW = _env_int("TOOBIT_RECV_WINDOW", 5000)
DEFAULT_MARGIN_TYPE = _env("DEFAULT_MARGIN_TYPE", "ISOLATED").upper()
TOOBIT_VERIFY_AFTER_ERROR_SECONDS = _env_int("TOOBIT_VERIFY_AFTER_ERROR_SECONDS", 70)
TOOBIT_PATH_BALANCE = _env("TOOBIT_PATH_BALANCE", "/api/v1/futures/balance")
TOOBIT_PATH_POSITIONS = _env("TOOBIT_PATH_POSITIONS", "/api/v1/futures/positions")
TOOBIT_PATH_OPEN_ORDERS = _env("TOOBIT_PATH_OPEN_ORDERS", "/api/v1/futures/openOrders")
TOOBIT_PATH_MARGIN_MODE = _env("TOOBIT_PATH_MARGIN_MODE", "/api/v1/futures/marginType")
TOOBIT_PATH_LEVERAGE = _env("TOOBIT_PATH_LEVERAGE", "/api/v1/futures/leverage")
TOOBIT_PATH_POSITION_SETTINGS = _env("TOOBIT_PATH_POSITION_SETTINGS", "/api/v1/futures/accountLeverage")
TOOBIT_PATH_ORDER = _env("TOOBIT_PATH_ORDER", "/api/v1/futures/order")
TOOBIT_PATH_MARK_PRICE = _env("TOOBIT_PATH_MARK_PRICE", "/api/v1/futures/markPrice")
TOOBIT_PATH_EXCHANGE_INFO = _env("TOOBIT_PATH_EXCHANGE_INFO", "/api/v1/futures/exchangeInfo")
TOOBIT_PATH_HISTORY_POSITIONS = _env("TOOBIT_PATH_HISTORY_POSITIONS", "/api/v1/futures/historyPositions")
TOOBIT_PATH_ORDER_HISTORY = _env("TOOBIT_PATH_ORDER_HISTORY", "/api/v1/futures/historyOrders")
TOOBIT_PATH_ORDER_HISTORY_ALT = _env("TOOBIT_PATH_ORDER_HISTORY_ALT", "/api/v1/futures/order/history")
TOOBIT_PATH_TODAY_PNL = _env("TOOBIT_PATH_TODAY_PNL", "/api/v1/futures/todayPnl")
TOOBIT_PATH_CLOSE_ORDER = _env("TOOBIT_PATH_CLOSE_ORDER", TOOBIT_PATH_ORDER)
TOOBIT_PARAM_TP = _env("TOOBIT_PARAM_TP", "takeProfit")
TOOBIT_PARAM_SL = _env("TOOBIT_PARAM_SL", "stopLoss")
TOOBIT_PLACE_REAL_TP = _env_bool("TOOBIT_PLACE_REAL_TP", True)
TOOBIT_PLACE_REAL_SL = _env_bool("TOOBIT_PLACE_REAL_SL", True)
TOOBIT_TP_PARAM = TOOBIT_PARAM_TP
TOOBIT_SL_PARAM = TOOBIT_PARAM_SL
TOOBIT_PANEL_CACHE_SECONDS = _env_int("TOOBIT_PANEL_CACHE_SECONDS", 20)

# Runtime laws
MAX_WATCH_SYMBOLS = _env_int("MAX_WATCH_SYMBOLS", 30)
FULL_SCAN_SECONDS = _env_int("FULL_SCAN_SECONDS", 35)
MONITOR_INTERVAL_SECONDS = _env_int("MONITOR_INTERVAL_SECONDS", 5)
SLOT_RECHECK_SECONDS = _env_int("SLOT_RECHECK_SECONDS", 70)
COIN_ERROR_COOLDOWN_SECONDS = _env_int("COIN_ERROR_COOLDOWN_SECONDS", 70)

# Trade panel defaults - editable from Telegram.
DEFAULT_TRADE_ENABLED = _env_bool("DEFAULT_TRADE_ENABLED", False)
DEFAULT_AUTO_SIGNAL_ENABLED = _env_bool("DEFAULT_AUTO_SIGNAL_ENABLED", True)
DEFAULT_TRADE_DOLLAR = _env_float("DEFAULT_TRADE_DOLLAR", _env_float("DEFAULT_MARGIN_USDT", 10.0))
DEFAULT_TRADE_CAPITAL = _env_float("DEFAULT_TRADE_CAPITAL", 100.0)
DEFAULT_LEVERAGE = _env_int("DEFAULT_LEVERAGE", 10)
DEFAULT_MAX_POSITIONS = _env_int("DEFAULT_MAX_POSITIONS", 3)
DEFAULT_MIN_NET_PROFIT_USDT = _env_float("DEFAULT_MIN_NET_PROFIT_USDT", 0.01)

# ICE-5M signal laws. One TP only. RR must never be below 1.
# The strategy is now gate-based: SIGNAL_SCORE_THRESHOLD/STRONG_SCORE_THRESHOLD are kept
# only for old DB/code compatibility and are not used for accepting signals.
SIGNAL_SCORE_THRESHOLD = _env_float("SIGNAL_SCORE_THRESHOLD", 90.0)
STRONG_SCORE_THRESHOLD = _env_float("STRONG_SCORE_THRESHOLD", 96.0)
ICE_RR = max(1.0, _env_float("ICE_RR", 1.15))
RR_NORMAL = ICE_RR
RR_STRONG = ICE_RR
ROUND_TRIP_FEE_USDT = _env_float("ROUND_TRIP_FEE_USDT", 0.05)
MIN_5M_SL_PCT = _env_float("MIN_5M_SL_PCT", 0.0012)   # 0.12%
MAX_5M_SL_PCT = _env_float("MAX_5M_SL_PCT", 0.0065)   # 0.65%
MAX_ENTRY_EXTENSION_PCT = _env_float("MAX_ENTRY_EXTENSION_PCT", 0.0025)  # 0.25% past box edge

# Compression / explosion filters
COMPRESSION_LOOKBACK_5M = _env_int("COMPRESSION_LOOKBACK_5M", 10)
COMPRESSION_MAX_RANGE_PCT = _env_float("COMPRESSION_MAX_RANGE_PCT", 0.0100)
COMPRESSION_MAX_ATR_RATIO = _env_float("COMPRESSION_MAX_ATR_RATIO", 0.85)
COMPRESSION_MIN_BOX_PCT = _env_float("COMPRESSION_MIN_BOX_PCT", 0.0015)
TRIGGER_VOLUME_RATIO = _env_float("TRIGGER_VOLUME_RATIO", 1.45)
TRIGGER_BODY_MIN_RATIO = _env_float("TRIGGER_BODY_MIN_RATIO", 0.55)
MAX_TWO_CANDLE_MOVE_PCT = _env_float("MAX_TWO_CANDLE_MOVE_PCT", 0.0075)

# Order-flow approximations from OKX public data.
ORDERBOOK_DEPTH_LEVELS = _env_int("ORDERBOOK_DEPTH_LEVELS", 20)
MAX_SPREAD_PCT = _env_float("MAX_SPREAD_PCT", 0.0006)  # 0.06%
MIN_DEPTH_USDT = _env_float("MIN_DEPTH_USDT", 25000.0)
IMBALANCE_MIN_ABS = _env_float("IMBALANCE_MIN_ABS", 0.08)
CVD_LOOKBACK_1M = _env_int("CVD_LOOKBACK_1M", 18)
DELTA_MIN_RATIO = _env_float("DELTA_MIN_RATIO", 0.12)
REQUIRE_BOOK_DIRECTION = _env_bool("REQUIRE_BOOK_DIRECTION", True)
REQUIRE_DELTA_AND_CVD_ALIGN = _env_bool("REQUIRE_DELTA_AND_CVD_ALIGN", True)
STRICT_BOOK_MIN = _env_float("STRICT_BOOK_MIN", 0.02)
STRICT_CVD_MIN = _env_float("STRICT_CVD_MIN", 0.12)
STRICT_DELTA_MIN = _env_float("STRICT_DELTA_MIN", 0.12)

# Gate-based context / anti-fake-break checks
REQUIRE_15M_CONTEXT = _env_bool("REQUIRE_15M_CONTEXT", True)
FIFTEEN_M_DANGER_PCT = _env_float("FIFTEEN_M_DANGER_PCT", 0.003)
BREAKOUT_HOLD_CHECK_ENABLED = _env_bool("BREAKOUT_HOLD_CHECK_ENABLED", True)
BREAKOUT_HOLD_BUFFER_PCT = _env_float("BREAKOUT_HOLD_BUFFER_PCT", 0.00015)

# Soft monitoring: avoids waiting for full SL when explosion fails.
SOFT_EXIT_ENABLED = _env_bool("SOFT_EXIT_ENABLED", True)
SOFT_EXIT_MINUTES = _env_int("SOFT_EXIT_MINUTES", 4)
SOFT_EXIT_MIN_R = _env_float("SOFT_EXIT_MIN_R", 0.30)
ENABLE_REAL_SOFT_EXIT_CLOSE = _env_bool("ENABLE_REAL_SOFT_EXIT_CLOSE", False)

# Disabled risky systems.
ENABLE_SUPPORT_RESISTANCE_FILTER = False
ENABLE_AI = False
ENABLE_DCA = False
ENABLE_MARTINGALE = False
ENABLE_TRAILING_STOP = False

WATCHLIST = tuple(
    s.strip().upper()
    for s in _env(
        "WATCHLIST",
        "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,TRXUSDT,"
        "TONUSDT,DOTUSDT,NEARUSDT,APTUSDT,ARBUSDT,OPUSDT,SUIUSDT,SEIUSDT,FETUSDT,INJUSDT,"
        "LTCUSDT,BCHUSDT,ETCUSDT,FILUSDT,ATOMUSDT,AAVEUSDT,UNIUSDT,1000PEPEUSDT,WIFUSDT,ORDIUSDT",
    ).split(",")
    if s.strip()
)[:MAX_WATCH_SYMBOLS]


@dataclass(frozen=True)
class RuntimeDefaults:
    trade_enabled: bool = DEFAULT_TRADE_ENABLED
    auto_signal_enabled: bool = DEFAULT_AUTO_SIGNAL_ENABLED
    trade_dollar_usdt: float = DEFAULT_TRADE_DOLLAR
    trade_capital_usdt: float = DEFAULT_TRADE_CAPITAL
    leverage: int = DEFAULT_LEVERAGE
    max_positions: int = DEFAULT_MAX_POSITIONS
    min_net_profit_usdt: float = DEFAULT_MIN_NET_PROFIT_USDT
