import time

from analysis import analyze_symbol
from config import AUTO_SIGNAL_SCORE, AUTO_SIGNAL_COOLDOWN_MINUTES
from coins_fa import COINS_FA


SCAN_SYMBOLS = sorted(list(set(COINS_FA.values())))

last_alerts = {}


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

    count = 0

    for tf in ["1D", "4H", "1H", "30M"]:
        if trends.get(tf) in good:
            count += 1

    return count


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

    if result.get("entry_grade") not in ["A+", "A", "B"]:
        return False

    if result.get("score", 0) < 82:
        return False

    if result.get("win_probability", 0) < 68:
        return False

    if result.get("risk_reward", 0) < 1.2:
        return False

    if result.get("risk_level") == "بالا":
        return False

    if result.get("liquidity_risk") == "بالا":
        return False

    if result.get("adx", 0) < 20:
        return False

    if result.get("spread_percent") is not None:
        if result.get("spread_percent") > 0.08:
            return False

    if is_opposite_divergence(result):
        return False

    if is_fake_breakout_against_signal(result):
        return False

    if not candle_confirmed(result):
        return False

    if mtf_alignment_count(result) < 2:
        return False

    if soft_confirmation_bonus(result) < 1:
        return False

    return True


def is_auto_signal(result):
    if not is_high_quality_signal(result):
        return False

    if result.get("score", 0) < max(85, AUTO_SIGNAL_SCORE):
        return False

    if result.get("win_probability", 0) < 70:
        return False

    # بعد از اصلاح TP با حمایت/مقاومت، R/R ممکنه کمی پایین‌تر بیاد
    # ولی TP واقعی‌تر میشه، پس 1.2 برای سیگنال خودکار منطقی‌تره
    if result.get("risk_reward", 0) < 1.2:
        return False

    return True


def get_best_signals(limit=5):
    results = []

    for symbol in SCAN_SYMBOLS:
        try:
            result = analyze_symbol(symbol)

            if is_high_quality_signal(result):
                results.append(result)

        except Exception as e:
            print("SCAN ERROR:", symbol, str(e))
            continue

    results.sort(
        key=lambda x: (
            x.get("win_probability", 0),
            x.get("score", 0),
            x.get("risk_reward", 0),
            soft_confirmation_bonus(x),
            x.get("adx", 0)
        ),
        reverse=True
    )

    return results[:limit]


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
