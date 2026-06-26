"""
Locked configuration for Crypto AI Helper bot.

This file must contain only static/runtime settings and validators.
No technical analysis, no trading execution, no Telegram UI, no AI logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Literal, Tuple


# =========================
# Project identity
# =========================
BOT_NAME = "CryptoAIHelperBot"
QUOTE_ASSET = "USDT"
TIMEFRAME = "1H"
TARGET_HOLD_MINUTES: Tuple[int, int] = (60, 90)


# =========================
# Data / execution sources
# =========================
# OKX is used only for market data and analysis.
OKX_BASE_URL = "https://www.okx.com"

# Toobit is used only for account, wallet, positions, orders, TP/SL and execution.
TOOBIT_BASE_URL = "https://api.toobit.com"


# =========================
# Locked watchlist
# =========================
@dataclass(frozen=True)
class CoinConfig:
    fa_name: str
    display_symbol: str      # Telegram / user display
    okx_symbol: str          # OKX market-data symbol
    toobit_symbol: str       # Toobit execution symbol


WATCHLIST: Dict[str, CoinConfig] = {
    "SOLUSDT": CoinConfig("سولانا", "SOLUSDT", "SOL-USDT-SWAP", "SOLUSDT"),
    "AVAXUSDT": CoinConfig("آوالانچ", "AVAXUSDT", "AVAX-USDT-SWAP", "AVAXUSDT"),
    "LINKUSDT": CoinConfig("چین‌لینک", "LINKUSDT", "LINK-USDT-SWAP", "LINKUSDT"),
    "INJUSDT": CoinConfig("اینجکتیو", "INJUSDT", "INJ-USDT-SWAP", "INJUSDT"),
    "DOGEUSDT": CoinConfig("دوج‌کوین", "DOGEUSDT", "DOGE-USDT-SWAP", "DOGEUSDT"),
    "SUIUSDT": CoinConfig("سویی", "SUIUSDT", "SUI-USDT-SWAP", "SUIUSDT"),
    "APTUSDT": CoinConfig("آپتوس", "APTUSDT", "APT-USDT-SWAP", "APTUSDT"),
}


# =========================
# Runtime intervals - seconds
# =========================
SIGNAL_SCAN_INTERVAL = 5
RESULT_CHECK_INTERVAL = 5
POSITION_CHECK_INTERVAL = 10
ACCOUNT_SYNC_INTERVAL = 20
ORDER_VERIFY_DELAY = 70
API_TIMEOUT_SECONDS = 5


# =========================
# Trading settings limits
# =========================
TRADE_DOLLAR_MIN = 1.0
TRADE_DOLLAR_MAX = 1000.0
LEVERAGE_MIN = 1
LEVERAGE_MAX = 100
TRADE_CAPITAL_MIN = 1.0
TRADE_CAPITAL_MAX = 100000.0
MAX_POSITIONS_MIN = 1
MAX_POSITIONS_MAX = 100
MIN_NET_PROFIT_MIN = 0.01
MIN_NET_PROFIT_MAX = 10000.0


# Defaults can be changed by Telegram commands and persisted by the state layer.
DEFAULT_AUTO_SIGNAL_ENABLED = True
DEFAULT_REAL_TRADE_ENABLED = False
DEFAULT_TRADE_DOLLAR = 5.0
DEFAULT_LEVERAGE = 10
DEFAULT_TRADE_CAPITAL = 100.0
DEFAULT_MAX_POSITIONS = 1
DEFAULT_MIN_NET_PROFIT_USDT = 0.10

MARGIN_MODE: Literal["isolated"] = "isolated"


# =========================
# TP / SL rules
# =========================
TP_COUNT = 1
SL_COUNT = 1
ALLOWED_RISK_REWARD = (1.5, 2.0)

# If a trade cannot satisfy one of these values logically, it must not open real Toobit trade.
MIN_RISK_REWARD = 1.5
MAX_RISK_REWARD = 2.0


# =========================
# Fee settings
# =========================
# Conservative default. Replace with exact Toobit taker/maker fee if account-specific fee API is available.
# Round trip = open fee + close fee.
DEFAULT_OPEN_FEE_RATE = 0.0006
DEFAULT_CLOSE_FEE_RATE = 0.0006
DEFAULT_ROUND_TRIP_FEE_RATE = DEFAULT_OPEN_FEE_RATE + DEFAULT_CLOSE_FEE_RATE


# =========================
# Coin move profiles - approximate starting profiles
# Percent movement expected over 1-2 hours.
# These are conservative placeholders and should later be calibrated from OKX historical candles.
# =========================
@dataclass(frozen=True)
class MoveProfile:
    weak_min: float
    weak_max: float
    normal_min: float
    normal_max: float
    strong_min: float
    strong_max: float


COIN_MOVE_PROFILE: Dict[str, MoveProfile] = {
    "SOLUSDT": MoveProfile(0.5, 0.8, 1.2, 2.0, 3.0, 5.0),
    "AVAXUSDT": MoveProfile(0.6, 0.9, 1.5, 2.3, 3.5, 5.5),
    "LINKUSDT": MoveProfile(0.4, 0.7, 1.0, 1.8, 2.5, 4.0),
    "INJUSDT": MoveProfile(0.8, 1.2, 2.0, 3.2, 4.5, 7.0),
    "DOGEUSDT": MoveProfile(0.5, 0.9, 1.3, 2.2, 3.5, 6.0),
    "SUIUSDT": MoveProfile(0.7, 1.1, 1.8, 3.0, 4.5, 7.0),
    "APTUSDT": MoveProfile(0.6, 0.9, 1.5, 2.5, 3.5, 5.5),
}


# =========================
# Coin analyzer locked weights
# =========================
ANALYZER_WEIGHTS = {
    "structure": 30,
    "momentum": 25,
    "volume": 15,
    "acceleration": 10,
    "volatility_atr": 10,
    "candle_price_action": 5,
    "liquidity": 5,
}

ACCELERATION_COMPONENTS = (
    "momentum_acceleration",
    "volume_acceleration",
    "volatility_acceleration",
)


# =========================
# AI / probability decision gates
# Balanced defaults: not too strict, not too loose.
# ai_decision.py must not validate TP/SL directly.
# =========================
MIN_DIRECTION_PROBABILITY = 70.0
MIN_CONFIDENCE = 60.0
MIN_AGREEMENT_SCORE = 60.0


# =========================
# Telegram command definitions
# =========================
CMD_TRADE = "ترید"
CMD_TRADE_ON = "ترید فعال"
CMD_TRADE_OFF = "ترید خاموش"
CMD_TRADE_DOLLAR = "ترید دلار"
CMD_TRADE_LEVERAGE = "ترید لوریج"
CMD_TRADE_CAPITAL = "سرمایه ترید"
CMD_MAX_POSITIONS = "حداکثر پوزیشن"
CMD_MIN_NET_PROFIT = "حداقل سود خالص"
CMD_STATS = "آمار"
CMD_AI = "هوش مصنوعی"
CMD_COINS = "کوین‌ها"
CMD_SETTINGS = "تنظیمات"
CMD_POSITIONS = "پوزیشن"


# =========================
# Telegram titles - locked UI text
# =========================
TITLE_TOOBIT_SIGNAL = "🏦 سیگنال توبیت"
TITLE_NORMAL_SIGNAL = "📊 سیگنال"
TITLE_TOOBIT_RESULT = "🏦 نتیجه توبیت"
TITLE_NORMAL_RESULT = "📊 نتیجه سیگنال"
TITLE_TRADE_PANEL = "⚙️ وضعیت ربات"
TITLE_STATS_PANEL = "📊 آمار ربات"

LONG_LABEL = "🟢 جهت: لانگ"
SHORT_LABEL = "🔴 جهت: شورت"


# =========================
# Validators
# =========================
def validate_trade_dollar(value: float) -> float:
    return _validate_float_range(value, TRADE_DOLLAR_MIN, TRADE_DOLLAR_MAX, "ترید دلار")


def validate_leverage(value: int) -> int:
    return _validate_int_range(value, LEVERAGE_MIN, LEVERAGE_MAX, "ترید لوریج")


def validate_trade_capital(value: float) -> float:
    return _validate_float_range(value, TRADE_CAPITAL_MIN, TRADE_CAPITAL_MAX, "سرمایه ترید")


def validate_max_positions(value: int) -> int:
    return _validate_int_range(value, MAX_POSITIONS_MIN, MAX_POSITIONS_MAX, "حداکثر پوزیشن")


def validate_min_net_profit(value: float) -> float:
    return _validate_float_range(value, MIN_NET_PROFIT_MIN, MIN_NET_PROFIT_MAX, "حداقل سود خالص")


def _validate_float_range(value: float, min_value: float, max_value: float, name: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} باید عدد باشد.") from exc
    if not min_value <= numeric <= max_value:
        raise ValueError(f"{name} باید بین {min_value} تا {max_value} باشد.")
    return numeric


def _validate_int_range(value: int, min_value: int, max_value: int, name: str) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} باید عدد صحیح باشد.") from exc
    if not min_value <= numeric <= max_value:
        raise ValueError(f"{name} باید بین {min_value} تا {max_value} باشد.")
    return numeric


def is_locked_coin(symbol: str) -> bool:
    return symbol.upper() in WATCHLIST


def get_coin(symbol: str) -> CoinConfig:
    key = symbol.upper()
    if key not in WATCHLIST:
        raise KeyError(f"کوین خارج از واچ‌لیست قفل‌شده است: {symbol}")
    return WATCHLIST[key]


# =========================
# Self-check
# =========================
def validate_locked_config() -> None:
    if len(WATCHLIST) != 7:
        raise AssertionError("واچ‌لیست باید دقیقاً ۷ کوین داشته باشد.")
    if set(WATCHLIST.keys()) != set(COIN_MOVE_PROFILE.keys()):
        raise AssertionError("COIN_MOVE_PROFILE باید دقیقاً با WATCHLIST هماهنگ باشد.")
    if sum(ANALYZER_WEIGHTS.values()) != 100:
        raise AssertionError("وزن‌های تحلیل باید جمعاً ۱۰۰ باشند.")
    if ALLOWED_RISK_REWARD != (1.5, 2.0):
        raise AssertionError("Risk/Reward فقط باید 1.5 و 2.0 باشد.")
    if MARGIN_MODE != "isolated":
        raise AssertionError("Margin mode باید همیشه isolated باشد.")
    validate_trade_dollar(DEFAULT_TRADE_DOLLAR)
    validate_leverage(DEFAULT_LEVERAGE)
    validate_trade_capital(DEFAULT_TRADE_CAPITAL)
    validate_max_positions(DEFAULT_MAX_POSITIONS)
    validate_min_net_profit(DEFAULT_MIN_NET_PROFIT_USDT)


validate_locked_config()
