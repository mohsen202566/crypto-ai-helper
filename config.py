"""Root config for the 1H OKX -> Toobit futures trend-pullback bot.

Everything is intentionally in the project root because the user deploys by pushing
these files to GitHub and running `git pull` on the VPS.
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
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("\"").strip("'")
                os.environ.setdefault(key, value)
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
    return value in {"1", "true", "yes", "y", "on", "فعال"}


BOT_NAME = _env("BOT_NAME", "Crypto 1H Trend Pullback Toobit Bot")
BOT_DATA_DIR = _env("BOT_DATA_DIR", "data")
BOT_DB_PATH = _env("BOT_DB_PATH", os.path.join(BOT_DATA_DIR, "crypto_1h_trend_pullback.sqlite3"))
LOG_LEVEL = _env("LOG_LEVEL", "INFO")

# Telegram
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")
OWNER_ID = _env("OWNER_ID")
TELEGRAM_POLL_TIMEOUT = _env_int("TELEGRAM_POLL_TIMEOUT", 25)

# OKX analysis data only
OKX_BASE_URL = _env("OKX_BASE_URL", "https://www.okx.com")
OKX_CANDLE_LIMIT = _env_int("OKX_CANDLE_LIMIT", 260)
OKX_REQUEST_TIMEOUT = _env_int("OKX_REQUEST_TIMEOUT", 12)

# Toobit execution - toobit_client.py reads these names directly.
TOOBIT_API_KEY = _env("TOOBIT_API_KEY")
TOOBIT_API_SECRET = _env("TOOBIT_API_SECRET", _env("TOOBIT_SECRET_KEY"))
TOOBIT_SECRET_KEY = TOOBIT_API_SECRET
TOOBIT_BASE_URL = _env("TOOBIT_BASE_URL", "https://api.toobit.com")
REQUEST_TIMEOUT = _env_int("TOOBIT_TIMEOUT_SECONDS", 12)
RECV_WINDOW = _env_int("TOOBIT_RECV_WINDOW", 5000)
DEFAULT_MARGIN_TYPE = _env("DEFAULT_MARGIN_TYPE", "ISOLATED").upper()
TOOBIT_VERIFY_AFTER_ERROR_SECONDS = _env_int("TOOBIT_VERIFY_AFTER_ERROR_SECONDS", 70)

# Toobit endpoints are configurable so the unchanged old client remains usable.
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

# Main runtime laws discussed with the user.
MAX_WATCH_SYMBOLS = _env_int("MAX_WATCH_SYMBOLS", 50)
FULL_SCAN_SECONDS = _env_int("FULL_SCAN_SECONDS", 70)
MONITOR_INTERVAL_SECONDS = _env_int("MONITOR_INTERVAL_SECONDS", 10)
SLOT_RECHECK_SECONDS = _env_int("SLOT_RECHECK_SECONDS", 70)
COIN_ERROR_COOLDOWN_SECONDS = _env_int("COIN_ERROR_COOLDOWN_SECONDS", 70)
TOOBIT_PANEL_CACHE_SECONDS = _env_int("TOOBIT_PANEL_CACHE_SECONDS", 20)

# Trade panel defaults - user can change them from Telegram.
DEFAULT_TRADE_ENABLED = _env_bool("DEFAULT_TRADE_ENABLED", False)
DEFAULT_TRADE_DOLLAR = _env_float("DEFAULT_TRADE_DOLLAR", _env_float("DEFAULT_MARGIN_USDT", 10.0))
DEFAULT_TRADE_CAPITAL = _env_float("DEFAULT_TRADE_CAPITAL", 100.0)
DEFAULT_LEVERAGE = _env_int("DEFAULT_LEVERAGE", 10)
DEFAULT_MAX_POSITIONS = _env_int("DEFAULT_MAX_POSITIONS", 3)
DEFAULT_MIN_NET_PROFIT_USDT = _env_float("DEFAULT_MIN_NET_PROFIT_USDT", 0.01)

# 1H trend-pullback strategy laws.
SIGNAL_SCORE_THRESHOLD = _env_float("SIGNAL_SCORE_THRESHOLD", 80.0)
STRONG_SCORE_THRESHOLD = _env_float("STRONG_SCORE_THRESHOLD", 85.0)
RR_NORMAL = _env_float("RR_NORMAL", 1.5)
RR_STRONG = _env_float("RR_STRONG", 2.0)
ROUND_TRIP_FEE_USDT = _env_float("ROUND_TRIP_FEE_USDT", 0.05)

SWING_LOOKBACK_4H = _env_int("SWING_LOOKBACK_4H", 8)
SWING_LOOKBACK_1H = _env_int("SWING_LOOKBACK_1H", 12)
EMA_SLOPE_LOOKBACK = _env_int("EMA_SLOPE_LOOKBACK", 10)

MIN_TREND_ADX = _env_float("MIN_TREND_ADX", 20.0)
STRONG_TREND_ADX = _env_float("STRONG_TREND_ADX", 25.0)
EXHAUSTION_ADX = _env_float("EXHAUSTION_ADX", 40.0)
FLAT_EMA_ATR_MULT = _env_float("FLAT_EMA_ATR_MULT", 0.30)

PULLBACK_LOOKBACK_1H = _env_int("PULLBACK_LOOKBACK_1H", 5)
PULLBACK_ATR_BUFFER = _env_float("PULLBACK_ATR_BUFFER", 0.20)
MIN_ENTRY_BODY_RATIO = _env_float("MIN_ENTRY_BODY_RATIO", 0.45)

MAX_DISTANCE_EMA20_ATR = _env_float("MAX_DISTANCE_EMA20_ATR", 1.50)
MAX_DISTANCE_EMA50_ATR = _env_float("MAX_DISTANCE_EMA50_ATR", 2.50)
ATR_SL_BUFFER_MULT = _env_float("ATR_SL_BUFFER_MULT", 0.20)
MIN_1H_RISK_ATR = _env_float("MIN_1H_RISK_ATR", 0.70)
MAX_1H_RISK_ATR = _env_float("MAX_1H_RISK_ATR", 2.20)
MAX_1H_SL_PCT = _env_float("MAX_1H_SL_PCT", 0.035)  # 3.5% maximum 1H structure stop.

# Backward compatible names, in case old VPS env/systemd still uses them.
MIN_4H_SL_PCT = _env_float("MIN_4H_SL_PCT", 0.008)
MAX_4H_SL_PCT = _env_float("MAX_4H_SL_PCT", 0.050)
ATR_SL_MULT = _env_float("ATR_SL_MULT", 1.20)

# Hard rule: no support/resistance filter for now.
ENABLE_SUPPORT_RESISTANCE_FILTER = False
ENABLE_AI = False
ENABLE_DCA = False
ENABLE_MARTINGALE = False
ENABLE_TRAILING_STOP = False

# 50 symbols. Keep internal name USDT style. OKX and Toobit mapping/validation happens at runtime.
DEFAULT_WATCHLIST = (
    "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,TRXUSDT,"
    "DOTUSDT,LTCUSDT,BCHUSDT,TONUSDT,SUIUSDT,APTUSDT,OPUSDT,ARBUSDT,NEARUSDT,INJUSDT,"
    "ATOMUSDT,FILUSDT,ETCUSDT,AAVEUSDT,UNIUSDT,SEIUSDT,WIFUSDT,ORDIUSDT,PEPEUSDT,JUPUSDT,"
    "TIAUSDT,WLDUSDT,FETUSDT,IMXUSDT,STXUSDT,ICPUSDT,MKRUSDT,LDOUSDT,ENSUSDT,ARUSDT,"
    "GRTUSDT,ALGOUSDT,XLMUSDT,EOSUSDT,PEOPLEUSDT,FLOKIUSDT,BONKUSDT,NOTUSDT,ONDOUSDT,PENDLEUSDT"
)

WATCHLIST = tuple(
    s.strip().upper()
    for s in _env("WATCHLIST", DEFAULT_WATCHLIST).split(",")
    if s.strip()
)[:MAX_WATCH_SYMBOLS]


@dataclass(frozen=True)
class RuntimeDefaults:
    trade_enabled: bool = DEFAULT_TRADE_ENABLED
    trade_dollar_usdt: float = DEFAULT_TRADE_DOLLAR
    trade_capital_usdt: float = DEFAULT_TRADE_CAPITAL
    leverage: int = DEFAULT_LEVERAGE
    max_positions: int = DEFAULT_MAX_POSITIONS
    min_net_profit_usdt: float = DEFAULT_MIN_NET_PROFIT_USDT
