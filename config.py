from __future__ import annotations

import os
from dataclasses import dataclass


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _env_int(*names: str, default: int) -> int:
    value = _env_first(*names, default=str(default))
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _env_float(*names: str, default: float) -> float:
    value = _env_first(*names, default=str(default))
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _env_bool(*names: str, default: bool) -> bool:
    value = _env_first(*names, default="1" if default else "0").strip().lower()
    return value in {"1", "true", "yes", "on", "enabled", "فعال"}


@dataclass(frozen=True)
class ScoreWeights:
    direction_1h: int = 40
    setup_15m: int = 18
    entry_5m: int = 4
    late_entry: int = 10
    risk_reward_net: int = 15
    market_quality: int = 6
    bias_4h: int = 7


DATA_DIR = _env_first("BOT_DATA_DIR", default="data")
DB_PATH = _env_first("BOT_DB_PATH", default=os.path.join(DATA_DIR, "bot.sqlite3"))

TELEGRAM_BOT_TOKEN = _env_first("TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
TELEGRAM_CHAT_ID = _env_first("TELEGRAM_CHAT_ID", "OWNER_ID")
OWNER_ID = _env_first("OWNER_ID", "TELEGRAM_CHAT_ID")

OKX_BASE_URL = _env_first("OKX_BASE_URL", default="https://www.okx.com").rstrip("/")
OKX_CANDLE_LIMIT = _env_int("OKX_CANDLE_LIMIT", default=260)

TIMEFRAME_4H = "4H"
TIMEFRAME_1H = "1H"
TIMEFRAME_15M = "15m"
TIMEFRAME_5M = "5m"
TIMEFRAMES = (TIMEFRAME_4H, TIMEFRAME_1H, TIMEFRAME_15M, TIMEFRAME_5M)
MARKET_CONTEXT_SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")

SCAN_INTERVAL_SECONDS = _env_int("SCAN_INTERVAL_SECONDS", default=120)
MONITOR_INTERVAL_SECONDS = _env_int("MONITOR_INTERVAL_SECONDS", default=8)
SIGNAL_THRESHOLD = _env_int("SIGNAL_THRESHOLD", "ACCEPT_SCORE", default=75)

WEIGHTS = ScoreWeights()

DEFAULT_TRADE_ENABLED = _env_bool("DEFAULT_TRADE_ENABLED", default=False)
DEFAULT_MARGIN_USDT = _env_float("DEFAULT_MARGIN_USDT", default=10.0)
DEFAULT_LEVERAGE = _env_int("DEFAULT_LEVERAGE", default=5)
DEFAULT_MAX_POSITIONS = _env_int("DEFAULT_MAX_POSITIONS", default=3)

TOOBIT_TAKER_FEE = _env_float("TOOBIT_TAKER_FEE", "TOBIT_TAKER_FEE", default=0.0006)
TOOBIT_MAKER_FEE = _env_float("TOOBIT_MAKER_FEE", "TOBIT_MAKER_FEE", default=0.0002)
SPREAD_BUFFER = _env_float("SPREAD_BUFFER", default=0.0004)
SLIPPAGE_BUFFER = _env_float("SLIPPAGE_BUFFER", default=0.0005)
MIN_NET_EDGE = _env_float("MIN_NET_EDGE", default=0.0015)
MIN_RISK_REWARD = _env_float("MIN_RISK_REWARD", default=1.15)

MAX_OPEN_SIGNAL_PER_SYMBOL = 1
BOT_NAME = _env_first("BOT_NAME", default="Forex Futures AI Bot")


def ensure_runtime_config() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN یا BOT_TOKEN داخل .env تنظیم نشده است.")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID یا OWNER_ID داخل .env تنظیم نشده است.")
