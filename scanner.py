# -*- coding: utf-8 -*-
import time

from analysis import analyze_symbol, exchange, to_okx_symbol
from config import AUTO_SIGNAL_SCORE, AUTO_SIGNAL_COOLDOWN_MINUTES
try:
    from config import AUTO_SCAN_MAX_SYMBOLS
except Exception:
    AUTO_SCAN_MAX_SYMBOLS = 70

try:
    from config import AUTO_SCAN_MIN_SCORE
except Exception:
    AUTO_SCAN_MIN_SCORE = AUTO_SIGNAL_SCORE

from coins_fa import COINS_FA


RAW_SCAN_SYMBOLS = sorted(list(set(COINS_FA.values())))
_MARKETS_CACHE = None
_SUPPORTED_SYMBOLS_CACHE = None
last_alerts = {}


QUIET_ERRORS = [
    "does not have market symbol",
    "نماد",
    "قابل معامله نیست",
    "Too Many Requests",
    "429",
    "Unauthorized",
    "داده کافی",
    "اندیکاتورها کامل محاسبه نشدند",
    "timeout",
    "timed out",
    "NetworkError",
    "ExchangeNotAvailable",
]


def is_quiet_error(msg):
    msg = str(msg)
    return any(item in msg for item in QUIET_ERRORS)


def _load_okx_markets():
    global _MARKETS_CACHE

    if _MARKETS_CACHE is not None:
        return _MARKETS_CACHE

    try:
        markets = exchange.load_markets()
        _MARKETS_CACHE = markets or {}
    except Exception as e:
        print("OKX MARKET LOAD ERROR:", str(e))
        _MARKETS_CACHE = {}

    return _MARKETS_CACHE


def symbol_supported(symbol):
    markets = _load_okx_markets()

    # اگر OKX markets لود نشد، اسکن را نمی‌بندیم؛ ولی خطاها در analyze کنترل می‌شوند.
    if not markets:
        return True

    return to_okx_symbol(symbol) in markets


def build_scan_symbols():
    global _SUPPORTED_SYMBOLS_CACHE

    if _SUPPORTED_SYMBOLS_CACHE is not None:
        return _SUPPORTED_SYMBOLS_CACHE

    supported = []
    for symbol in RAW_SCAN_SYMBOLS:
        try:
            if symbol_supported(symbol):
                supported.append(symbol)
        except Exception:
            continue

    if AUTO_SCAN_MAX_SYMBOLS and len(supported) > AUTO_SCAN_MAX_SYMBOLS:
        supported = supported[:AUTO_SCAN_MAX_SYMBOLS]

    _SUPPORTED_SYMBOLS_CACHE = supported
    return _SUPPORTED_SYMBOLS_CACHE


SCAN_SYMBOLS = build_scan_symbols()


def refresh_scan_symbols():
    global _MARKETS_CACHE, _SUPPORTED_SYMBOLS_CACHE, SCAN_SYMBOLS
    _MARKETS_CACHE = None
    _SUPPORTED_SYMBOLS_CACHE = None
    SCAN_SYMBOLS = build_scan_symbols()
    return SCAN_SYMBOLS


def is_opposite_divergence(result):
    direction = result.get("direction")

    if direction == "LONG":
        return (
            result.get("rsi_divergence") == "bearish_rsi_divergence"
            or result.get("macd_divergence") == "bearish_macd_divergence"
        )

    if direction == "SHORT":
        return (
            result.get("rsi_divergence") == "bullish_rsi_divergence"
            or result.get("macd_divergence") == "bullish_macd_divergence"
        )

    return True


def is_fake_breakout_against_signal(result):
    direction = result.get("direction")

    if direction == "LONG":
        return result.get("fake_breakout") == "fake_bullish_breakout"

    if direction == "SHORT":
        return result.get("fake_breakout") == "fake_bearish_breakout"

    return True


def mtf_alignment_count(result):
    direction = result.get("direction")
    trends = result.get("trends", {}) or {}

    if direction == "LONG":
        good = ["bullish", "weak_bullish"]
    elif direction == "SHORT":
        good = ["bearish", "weak_bearish"]
    else:
        return 0

    count = 0
    for tf in ["1D", "4H", "1H", "30M"]:
        if trends.get(tf) in good:
            count += 1

    return count


def soft_confirmation_bonus(result):
    bonus = 0
    direction = result.get("direction")

    if direction == "LONG":
        checks = [
            ("vwap_status", "above_vwap"),
            ("volume_profile_status", "above_poc"),
            ("liquidity_grab", "bullish_liquidity_grab"),
            ("stop_hunt", "bullish_stop_hunt"),
            ("order_block", "bullish_order_block"),
            ("multi_candle", "bullish"),
        ]

    elif direction == "SHORT":
        checks = [
            ("vwap_status", "below_vwap"),
            ("volume_profile_status", "below_poc"),
            ("liquidity_grab", "bearish_liquidity_grab"),
            ("stop_hunt", "bearish_stop_hunt"),
            ("order_block", "bearish_order_block"),
            ("multi_candle", "bearish"),
        ]

    else:
        return 0

    for key, val in checks:
        if result.get(key) == val:
            bonus += 1

    return bonus


def is_high_quality_signal(result):
    """
    نسخه هماهنگ با analysis.py اسکالپ سریع:
    هدف، ارسال سیگنال‌های 5 تا 15 دقیقه‌ای زودتر است، نه انتظار برای تاییدهای دیرهنگام.
    """
    if not result:
        return False

    if result.get("direction") == "NO TRADE":
        return False

    if result.get("entry_grade") not in ["A+", "A"]:
        return False

    if result.get("score", 0) < AUTO_SCAN_MIN_SCORE:
        return False

    if result.get("win_probability", 0) < 60:
        return False

    if result.get("risk_reward", 0) < 0.70:
        return False

    if result.get("adx", 0) < 18:
        return False

    try:
        buy_power = float(result.get("buy_power", 50))
        sell_power = float(result.get("sell_power", 50))
    except Exception:
        buy_power = 50
        sell_power = 50

    direction = result.get("direction")
    power_gap = buy_power - sell_power

    if direction == "LONG":
        if power_gap < 6:
            return False
    elif direction == "SHORT":
        if power_gap > -6:
            return False

    spread = result.get("spread_percent")
    if spread is not None and spread > 0.12:
        return False

    if mtf_alignment_count(result) < 1:
        return False

    return True

def is_very_safe_signal(result):
    return (
        is_high_quality_signal(result)
        and result.get("score", 0) >= 92
        and result.get("win_probability", 0) >= 70
        and result.get("risk_reward", 0) >= 0.90
        and result.get("adx", 0) >= 24
    )

def analyze_symbol_safe(symbol):
    try:
        result = analyze_symbol(symbol)

        # analysis.py جدید برای خطاها NO TRADE برمی‌گرداند.
        if not result or result.get("direction") == "NO TRADE":
            return None

        return result

    except Exception as e:
        msg = str(e)
        if not is_quiet_error(msg):
            print("SCAN ANALYZE ERROR:", symbol, msg)
        return None


def get_best_signals(limit=5, very_safe_only=False):
    results = []

    for symbol in SCAN_SYMBOLS:
        result = analyze_symbol_safe(symbol)
        if not result:
            continue

        if very_safe_only:
            if is_very_safe_signal(result):
                results.append(result)
        else:
            if is_high_quality_signal(result):
                results.append(result)

    results = sorted(
        results,
        key=lambda x: (
            x.get("score", 0),
            x.get("win_probability", 0),
            x.get("risk_reward", 0),
        ),
        reverse=True,
    )

    return results[:limit]


def should_send_auto_signal(result):
    if not is_high_quality_signal(result):
        return False

    if result.get("score", 0) < AUTO_SIGNAL_SCORE:
        return False

    symbol = result.get("symbol")
    direction = result.get("direction")

    if not symbol or not direction or direction == "NO TRADE":
        return False

    now = int(time.time())
    key = f"{symbol}_{direction}"
    last = last_alerts.get(key)

    if last and now - last < AUTO_SIGNAL_COOLDOWN_MINUTES * 60:
        return False

    last_alerts[key] = now
    return True
