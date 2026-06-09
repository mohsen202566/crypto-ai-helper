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


def get_market_breadth():
    symbols = sorted(list(set(COINS_FA.values())))[:MAX_MARKET_SCAN_SYMBOLS]

    bullish = 0
    bearish = 0
    ranging = 0
    checked = 0

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

    if checked == 0:
        return {
            "checked": 0,
            "bullish": 0,
            "bearish": 0,
            "range": 0,
            "bullish_pct": 0,
            "bearish_pct": 0,
            "range_pct": 0,
            "bias": "unknown",
            "bias_text": "نامشخص",
            "power": "ضعیف",
        }

    bullish_pct = round((bullish / checked) * 100)
    bearish_pct = round((bearish / checked) * 100)
    range_pct = round((ranging / checked) * 100)

    if bullish_pct >= 60:
        bias = "bullish"
        bias_text = "صعودی"
        power = "قوی" if bullish_pct >= 70 else "متوسط"
    elif bearish_pct >= 60:
        bias = "bearish"
        bias_text = "نزولی"
        power = "قوی" if bearish_pct >= 70 else "متوسط"
    elif range_pct >= 50:
        bias = "range"
        bias_text = "رنج"
        power = "ضعیف"
    elif bullish_pct > bearish_pct:
        bias = "weak_bullish"
        bias_text = "رنج متمایل به صعود"
        power = "متوسط"
    elif bearish_pct > bullish_pct:
        bias = "weak_bearish"
        bias_text = "رنج متمایل به نزول"
        power = "متوسط"
    else:
        bias = "neutral"
        bias_text = "خنثی"
        power = "ضعیف"

    return {
        "checked": checked,
        "bullish": bullish,
        "bearish": bearish,
        "range": ranging,
        "bullish_pct": bullish_pct,
        "bearish_pct": bearish_pct,
        "range_pct": range_pct,
        "bias": bias,
        "bias_text": bias_text,
        "power": power,
    }


def get_market_status_text():
    now = int(time.time())

    if MARKET_STATUS_CACHE["text"] and now - MARKET_STATUS_CACHE["time"] < CACHE_SECONDS:
        return MARKET_STATUS_CACHE["text"]

    data = get_market_breadth()

    if data["checked"] == 0:
        text = (
            "📊 وضعیت بازار\n\n"
            "داده کافی برای محاسبه وضعیت بازار دریافت نشد.\n"
            "چند دقیقه بعد دوباره امتحان کن."
        )
        MARKET_STATUS_CACHE["time"] = now
        MARKET_STATUS_CACHE["text"] = text
        return text

    text = (
        "📊 وضعیت بازار\n\n"
        f"تعداد ارزهای بررسی‌شده: {data['checked']}\n"
        f"🟢 صعودی: {data['bullish']} ارز | {data['bullish_pct']}٪\n"
        f"🔴 نزولی: {data['bearish']} ارز | {data['bearish_pct']}٪\n"
        f"⚪ رنج: {data['range']} ارز | {data['range_pct']}٪\n\n"
        f"نتیجه کلی: بازار {data['bias_text']} است.\n"
        f"قدرت وضعیت: {data['power']}\n\n"
        "این گزارش فقط وضعیت کلی بازار را نشان می‌دهد و به تنهایی سیگنال ورود نیست."
    )

    MARKET_STATUS_CACHE["time"] = now
    MARKET_STATUS_CACHE["text"] = text
    return text
