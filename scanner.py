# -*- coding: utf-8 -*-
import time

from analysis import analyze_symbol, exchange, to_okx_symbol
from config import AUTO_SIGNAL_SCORE, AUTO_SIGNAL_COOLDOWN_MINUTES
try:
    from config import AUTO_SCAN_MAX_SYMBOLS
except Exception:
    AUTO_SCAN_MAX_SYMBOLS = 35

from coins_fa import COINS_FA


RAW_SCAN_SYMBOLS = sorted(list(set(COINS_FA.values())))
_MARKETS_CACHE = None
last_alerts = {}


def _load_okx_symbols():
    global _MARKETS_CACHE

    if _MARKETS_CACHE is not None:
        return _MARKETS_CACHE

    try:
        markets = exchange.load_markets()
        _MARKETS_CACHE = set(markets.keys())
        return _MARKETS_CACHE
    except Exception:
        _MARKETS_CACHE = set()
        return _MARKETS_CACHE


def symbol_supported(symbol):
    markets = _load_okx_symbols()
    if not markets:
        return True
    return to_okx_symbol(symbol) in markets


def build_scan_symbols():
    supported = [s for s in RAW_SCAN_SYMBOLS if symbol_supported(s)]

    # اسکن را محدود می‌کنیم تا API خشک نشود و CoinGecko/OKX خطای پشت‌سرهم ندهند.
    if AUTO_SCAN_MAX_SYMBOLS and len(supported) > AUTO_SCAN_MAX_SYMBOLS:
        supported = supported[:AUTO_SCAN_MAX_SYMBOLS]

    return supported


SCAN_SYMBOLS = build_scan_symbols()


def _safe_number(value, default=0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


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


def candle_confirmed(result):
    direction = result.get("direction")
    candle = result.get("candle_pattern")
    multi = result.get("multi_candle")

    if direction == "LONG":
        return (
            candle in ["bullish_engulfing", "bullish_pinbar", "bullish_strong"]
            or multi == "bullish"
        )

    if direction == "SHORT":
        return (
            candle in ["bearish_engulfing", "bearish_pinbar", "bearish_strong"]
            or multi == "bearish"
        )

    return False


def mtf_alignment_count(result):
    direction = result.get("direction")
    trends = result.get("trends", {})

    if direction == "LONG":
        good = ["bullish", "weak_bullish"]
    elif direction == "SHORT":
        good = ["bearish", "weak_bearish"]
    else:
        return 0

    return sum(1 for tf in ["1D", "4H", "1H", "30M"] if trends.get(tf) in good)


def soft_confirmation_bonus(result):
    bonus = 0
    direction = result.get("direction")

    if direction == "LONG":
        if result.get("vwap_status") == "above_vwap":
            bonus += 1
        if result.get("volume_profile_status") == "above_poc":
            bonus += 1
        if result.get("liquidity_grab") == "bullish_liquidity_grab":
            bonus += 1
        if result.get("stop_hunt") == "bullish_stop_hunt":
            bonus += 1
        if result.get("fvg") == "bullish_fvg":
            bonus += 1
        if result.get("order_block") == "bullish_order_block":
            bonus += 1

    if direction == "SHORT":
        if result.get("vwap_status") == "below_vwap":
            bonus += 1
        if result.get("volume_profile_status") == "below_poc":
            bonus += 1
        if result.get("liquidity_grab") == "bearish_liquidity_grab":
            bonus += 1
        if result.get("stop_hunt") == "bearish_stop_hunt":
            bonus += 1
        if result.get("fvg") == "bearish_fvg":
            bonus += 1
        if result.get("order_block") == "bearish_order_block":
            bonus += 1

    return bonus


def is_high_quality_signal(result):
    if result.get("direction") == "NO TRADE":
        return False

    # طبق درخواست: Reject و B ارسال نشوند.
    if result.get("entry_grade") not in ["A+", "A"]:
        return False

    if _safe_number(result.get("score")) < 82:
        return False

    if _safe_number(result.get("win_probability")) < 68:
        return False

    if _safe_number(result.get("risk_reward")) < 1.05:
        return False

    if result.get("risk_level") == "بالا":
        return False

    if result.get("liquidity_risk") == "بالا":
        return False

    if _safe_number(result.get("adx")) < 18:
        return False

    if result.get("spread_percent") is not None and _safe_number(result.get("spread_percent")) > 0.10:
        return False

    if is_opposite_divergence(result):
        return False

    if is_fake_breakout_against_signal(result):
        return False

    if not candle_confirmed(result) and mtf_alignment_count(result) < 3:
        return False

    if mtf_alignment_count(result) < 2:
        return False

    if soft_confirmation_bonus(result) < 1:
        return False

    return True


def is_very_safe_signal(result):
    if not is_high_quality_signal(result):
        return False

    if not result.get("very_safe"):
        return False

    if _safe_number(result.get("score")) < 88:
        return False

    if _safe_number(result.get("win_probability")) < 74:
        return False

    if _safe_number(result.get("risk_reward")) < 1.15:
        return False

    return True


def get_best_signals(limit=5, very_safe_only=False):
    results = []

    for symbol in SCAN_SYMBOLS:
        try:
            result = analyze_symbol(symbol)

            if very_safe_only:
                if is_very_safe_signal(result):
                    results.append(result)
            else:
                if is_high_quality_signal(result):
                    results.append(result)

        except Exception as e:
            msg = str(e)
            if "does not have market symbol" not in msg and "Too Many Requests" not in msg and "429" not in msg:
                print("SCAN ERROR:", symbol, msg)
            continue

    results.sort(
        key=lambda x: (
            _safe_number(x.get("win_probability")),
            _safe_number(x.get("score")),
            _safe_number(x.get("risk_reward")),
            soft_confirmation_bonus(x),
            _safe_number(x.get("adx")),
        ),
        reverse=True
    )

    return results[:limit]


def is_auto_signal(result):
    if not is_high_quality_signal(result):
        return False

    if _safe_number(result.get("score")) < max(85, AUTO_SIGNAL_SCORE):
        return False

    if _safe_number(result.get("win_probability")) < 70:
        return False

    if _safe_number(result.get("risk_reward")) < 1.10:
        return False

    return True


def should_send_auto_signal(result):
    if not is_auto_signal(result):
        return False

    key = f"{result['symbol']}_{result['direction']}"
    now = time.time()
    cooldown_seconds = AUTO_SIGNAL_COOLDOWN_MINUTES * 60

    if key in last_alerts:
        if now - last_alerts[key] < cooldown_seconds:
            return False

    last_alerts[key] = now
    return True
