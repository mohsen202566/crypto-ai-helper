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

# 15m/30m fast model lock
TIMEFRAME = "30m"
ENTRY_TIMEFRAME = "15m"
TREND_FILTER_TIMEFRAME = "1h"
TARGET_HOLD_MINUTES: Tuple[int, int] = (30, 60)
MAX_HOLD_MINUTES = 75
MIN_MAIN_CANDLES_30M = 40
MIN_ENTRY_CANDLES_15M = 40
MIN_TREND_CANDLES_1H = 20


# =========================
# Data / execution sources
# =========================
# OKX is used only for market data and analysis.
OKX_BASE_URL = "https://www.okx.com"

# Toobit is used only for account, wallet, positions, orders, TP/SL and execution.
TOOBIT_BASE_URL = "https://api.toobit.com"


# =========================
# Locked watchlist - 15 liquid/volatile coins
# =========================
@dataclass(frozen=True)
class CoinConfig:
    fa_name: str
    display_symbol: str      # Telegram / user display
    okx_symbol: str          # OKX market-data symbol
    toobit_symbol: str       # Toobit execution symbol


WATCHLIST: Dict[str, CoinConfig] = {
    "BTCUSDT": CoinConfig("بیت‌کوین", "BTCUSDT", "BTC-USDT-SWAP", "BTCUSDT"),
    "ETHUSDT": CoinConfig("اتریوم", "ETHUSDT", "ETH-USDT-SWAP", "ETHUSDT"),
    "SOLUSDT": CoinConfig("سولانا", "SOLUSDT", "SOL-USDT-SWAP", "SOLUSDT"),
    "DOGEUSDT": CoinConfig("دوج‌کوین", "DOGEUSDT", "DOGE-USDT-SWAP", "DOGEUSDT"),
    "XRPUSDT": CoinConfig("ریپل", "XRPUSDT", "XRP-USDT-SWAP", "XRPUSDT"),
    "BNBUSDT": CoinConfig("بی‌ان‌بی", "BNBUSDT", "BNB-USDT-SWAP", "BNBUSDT"),
    "SUIUSDT": CoinConfig("سویی", "SUIUSDT", "SUI-USDT-SWAP", "SUIUSDT"),
    "AVAXUSDT": CoinConfig("آوالانچ", "AVAXUSDT", "AVAX-USDT-SWAP", "AVAXUSDT"),
    "LINKUSDT": CoinConfig("چین‌لینک", "LINKUSDT", "LINK-USDT-SWAP", "LINKUSDT"),
    "INJUSDT": CoinConfig("اینجکتیو", "INJUSDT", "INJ-USDT-SWAP", "INJUSDT"),
    "APTUSDT": CoinConfig("آپتوس", "APTUSDT", "APT-USDT-SWAP", "APTUSDT"),
    "ARBUSDT": CoinConfig("آربیتروم", "ARBUSDT", "ARB-USDT-SWAP", "ARBUSDT"),
    "OPUSDT": CoinConfig("آپتیمیزم", "OPUSDT", "OP-USDT-SWAP", "OPUSDT"),
    "SEIUSDT": CoinConfig("سی", "SEIUSDT", "SEI-USDT-SWAP", "SEIUSDT"),
    "FETUSDT": CoinConfig("فچ‌ای‌آی", "FETUSDT", "FET-USDT-SWAP", "FETUSDT"),
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
# TP / SL rules - 15m/30m lock
# =========================
TP_COUNT = 1
SL_COUNT = 1
ALLOWED_RISK_REWARD = (1.2, 1.5, 2.0)

# Dynamic RR priority:
# weak -> 1.2, normal -> 1.5, strong -> 2.0
MIN_RISK_REWARD = 1.2
MAX_RISK_REWARD = 2.0
WEAK_RISK_REWARD = 1.2
NORMAL_RISK_REWARD = 1.5
STRONG_RISK_REWARD = 2.0

# SL guardrails for fast 30-60 minute trades.
MIN_SL_DISTANCE_PCT = 0.20
MAX_SL_DISTANCE_PCT = 1.80
SL_BUFFER_PCT = 0.10


# =========================
# Fee settings
# =========================
# Conservative default. Replace with exact Toobit taker/maker fee if account-specific fee API is available.
# Round trip = open fee + close fee.
DEFAULT_OPEN_FEE_RATE = 0.0006
DEFAULT_CLOSE_FEE_RATE = 0.0006
DEFAULT_ROUND_TRIP_FEE_RATE = DEFAULT_OPEN_FEE_RATE + DEFAULT_CLOSE_FEE_RATE


# =========================
# Coin move profiles
# Percent movement expected over 30-60 minutes.
# Conservative placeholders; should later be calibrated from OKX historical candles.
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
    "BTCUSDT": MoveProfile(0.25, 0.45, 0.60, 1.00, 1.30, 2.20),
    "ETHUSDT": MoveProfile(0.30, 0.55, 0.75, 1.25, 1.60, 2.70),
    "SOLUSDT": MoveProfile(0.40, 0.70, 1.00, 1.70, 2.20, 3.80),
    "DOGEUSDT": MoveProfile(0.35, 0.65, 0.90, 1.60, 2.10, 3.70),
    "XRPUSDT": MoveProfile(0.30, 0.55, 0.80, 1.35, 1.80, 3.00),
    "BNBUSDT": MoveProfile(0.25, 0.45, 0.65, 1.05, 1.40, 2.30),
    "SUIUSDT": MoveProfile(0.45, 0.80, 1.15, 1.95, 2.50, 4.30),
    "AVAXUSDT": MoveProfile(0.40, 0.75, 1.05, 1.80, 2.40, 4.00),
    "LINKUSDT": MoveProfile(0.35, 0.60, 0.90, 1.50, 1.90, 3.20),
    "INJUSDT": MoveProfile(0.55, 0.95, 1.35, 2.25, 3.00, 5.00),
    "APTUSDT": MoveProfile(0.40, 0.75, 1.05, 1.80, 2.35, 3.90),
    "ARBUSDT": MoveProfile(0.40, 0.75, 1.05, 1.80, 2.35, 3.90),
    "OPUSDT": MoveProfile(0.40, 0.75, 1.05, 1.80, 2.35, 3.90),
    "SEIUSDT": MoveProfile(0.50, 0.90, 1.25, 2.10, 2.80, 4.80),
    "FETUSDT": MoveProfile(0.45, 0.85, 1.15, 1.95, 2.60, 4.40),
}


# =========================
# Coin analyzer locked weights
# Direction must come from structure/slope/momentum/breakout.
# Volume and ATR are confirmation only, not direction makers.
# Consolidation and liquidity_sweep are blocker/penalty sections, weight=0.
# =========================
ANALYZER_WEIGHTS = {
    "structure": 10,
    "market_structure": 18,
    "ema_slope": 16,
    "rsi_slope": 14,
    "momentum": 8,
    "acceleration": 8,
    "breakout_confirmation": 10,
    "candle_price_action": 6,
    "liquidity": 5,
    "volume": 3,
    "volatility_atr": 2,
    "consolidation": 0,
    "liquidity_sweep": 0,
}

ACCELERATION_COMPONENTS = (
    "momentum_acceleration",
    "volume_acceleration",
    "volatility_acceleration",
)


# =========================
# AI / probability decision gates
# ai_decision.py must not validate TP/SL directly.
# =========================
MIN_DIRECTION_PROBABILITY = 62.0
MIN_CONFIDENCE = 75.0
MIN_AGREEMENT_SCORE = 70.0
MIN_DIRECTION_EDGE = 8.0


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
    if TIMEFRAME != "30m":
        raise AssertionError("تایم‌فریم اصلی باید 30m باشد.")
    if ENTRY_TIMEFRAME != "15m":
        raise AssertionError("تایم‌فریم ورود باید 15m باشد.")
    if TREND_FILTER_TIMEFRAME != "1h":
        raise AssertionError("فیلتر روند باید 1h باشد.")
    if TARGET_HOLD_MINUTES != (30, 60) or MAX_HOLD_MINUTES != 75:
        raise AssertionError("زمان نگهداری باید 30-60 دقیقه و خروج اجباری 75 دقیقه باشد.")
    if len(WATCHLIST) != 15:
        raise AssertionError("واچ‌لیست باید دقیقاً ۱۵ کوین داشته باشد.")
    if set(WATCHLIST.keys()) != set(COIN_MOVE_PROFILE.keys()):
        raise AssertionError("COIN_MOVE_PROFILE باید دقیقاً با WATCHLIST هماهنگ باشد.")
    if sum(ANALYZER_WEIGHTS.values()) != 100:
        raise AssertionError("وزن‌های تحلیل باید جمعاً ۱۰۰ باشند.")
    if ALLOWED_RISK_REWARD != (1.2, 1.5, 2.0):
        raise AssertionError("Risk/Reward فقط باید 1.2، 1.5 و 2.0 باشد.")
    if MIN_CONFIDENCE != 75.0 or MIN_AGREEMENT_SCORE != 70.0 or MIN_DIRECTION_EDGE != 8.0:
        raise AssertionError("گیت‌های ورود 15m/30m تغییر کرده‌اند.")
    if MARGIN_MODE != "isolated":
        raise AssertionError("Margin mode باید همیشه isolated باشد.")
    validate_trade_dollar(DEFAULT_TRADE_DOLLAR)
    validate_leverage(DEFAULT_LEVERAGE)
    validate_trade_capital(DEFAULT_TRADE_CAPITAL)
    validate_max_positions(DEFAULT_MAX_POSITIONS)
    validate_min_net_profit(DEFAULT_MIN_NET_PROFIT_USDT)


validate_locked_config()
