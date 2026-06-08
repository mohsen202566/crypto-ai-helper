# -*- coding: utf-8 -*-
import time

from analysis import get_klines, add_indicators, trend_direction
from coins_fa import COINS_FA

MARKET_STATUS_CACHE = {
    "time": 0,
    "text": None,
}

CACHE_SECONDS = 300
MAX_MARKET_SCAN_SYMBOLS = 100


def _market_label(trend):
    if trend in ["bullish", "weak_bullish"]:
        return "صعودی"
    if trend in ["bearish", "weak_bearish"]:
        return "نزولی"
    return "رنج"


def _analyze_symbol_market(symbol):
    try:
        df_30m = add_indicators(get_klines(symbol, "30m"))
        df_15m = add_indicators(get_klines(symbol, "15m"))

        trend_30m = trend_direction(df_30m)
        trend_15m = trend_direction(df_15m)

        if trend_30m in ["bullish", "weak_bullish"] and trend_15m in ["bullish", "weak_bullish"]:
            return "bullish"

        if trend_30m in ["bearish", "weak_bearish"] and trend_15m in ["bearish", "weak_bearish"]:
            return "bearish"

        return "range"

    except Exception:
        return None


def get_market_status_text():
    now = int(time.time())

    if MARKET_STATUS_CACHE["text"] and now - MARKET_STATUS_CACHE["time"] < CACHE_SECONDS:
        return MARKET_STATUS_CACHE["text"]

    symbols = sorted(list(set(COINS_FA.values())))[:MAX_MARKET_SCAN_SYMBOLS]

    bullish = 0
    bearish = 0
    ranging = 0
    checked = 0

    btc_status = "نامشخص"
    eth_status = "نامشخص"

    for symbol in symbols:
        status = _analyze_symbol_market(symbol)

        if status is None:
            continue

        checked += 1

        if status == "bullish":
            bullish += 1
        elif status == "bearish":
            bearish += 1
        else:
            ranging += 1

        if symbol == "BTCUSDT":
            btc_status = _market_label(status)
        elif symbol == "ETHUSDT":
            eth_status = _market_label(status)

    if checked == 0:
        text = (
            "📊 وضعیت بازار\n\n"
            "داده کافی برای محاسبه وضعیت بازار دریافت نشد.\n"
            "چند دقیقه بعد دوباره امتحان کن."
        )
        MARKET_STATUS_CACHE["time"] = now
        MARKET_STATUS_CACHE["text"] = text
        return text

    bullish_pct = round((bullish / checked) * 100)
    bearish_pct = round((bearish / checked) * 100)
    range_pct = round((ranging / checked) * 100)

    if bullish_pct >= 60:
        market_result = "بازار صعودی است؛ لانگ‌ها برتری دارند."
        market_power = "قوی" if bullish_pct >= 70 else "متوسط"
    elif bearish_pct >= 60:
        market_result = "بازار نزولی است؛ شورت‌ها برتری دارند."
        market_power = "قوی" if bearish_pct >= 70 else "متوسط"
    elif range_pct >= 50:
        market_result = "بازار فعلاً رنج است و جهت واضحی ندارد."
        market_power = "ضعیف"
    elif bullish_pct > bearish_pct:
        market_result = "بازار رنج متمایل به صعود است."
        market_power = "متوسط"
    elif bearish_pct > bullish_pct:
        market_result = "بازار رنج متمایل به نزول است."
        market_power = "متوسط"
    else:
        market_result = "بازار خنثی است."
        market_power = "ضعیف"

    text = (
        "📊 وضعیت بازار\n\n"
        f"🟢 صعودی: {bullish_pct}٪\n"
        f"🔴 نزولی: {bearish_pct}٪\n"
        f"⚪ رنج: {range_pct}٪\n\n"
        f"قدرت بازار: {market_power}\n\n"
        f"BTC: {btc_status}\n"
        f"ETH: {eth_status}\n\n"
        f"نتیجه:\n{market_result}"
    )

    MARKET_STATUS_CACHE["time"] = now
    MARKET_STATUS_CACHE["text"] = text

    return text
