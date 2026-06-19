# -*- coding: utf-8 -*-
"""
market_scanner.py

Market breadth / overview helper for the crypto futures bot.

Purpose:
- Scan a limited list of coins and estimate overall market breadth.
- Provide a short Persian status report for Telegram.
- Work as a soft market-context layer only; it must not block signals by itself.

Compatibility:
- Keeps old public functions:
    get_market_breadth
    get_market_status_text
- Adds:
    get_market_breadth_profile
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

try:
    from analysis import get_klines, add_indicators, trend_direction  # type: ignore
except Exception:
    get_klines = None
    add_indicators = None
    trend_direction = None

try:
    from coins_fa import COINS_FA  # type: ignore
except Exception:
    COINS_FA = {}

MARKET_STATUS_CACHE = {
    "time": 0,
    "text": None,
    "data": None,
}

CACHE_SECONDS = 300
MAX_MARKET_SCAN_SYMBOLS = 100
MIN_CHECKED_FOR_STRONG_BIAS = 8


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _safe_pct(part: int, total: int) -> int:
    if total <= 0:
        return 0
    return int(round((part / total) * 100))


def _unique_symbols():
    values = []
    try:
        if isinstance(COINS_FA, dict):
            values = list(COINS_FA.values())
        elif isinstance(COINS_FA, (list, tuple, set)):
            values = list(COINS_FA)
    except Exception:
        values = []

    out = []
    seen = set()
    for item in values:
        sym = str(item or "").upper().replace("/", "").replace("-", "").strip()
        if not sym:
            continue
        if not sym.endswith("USDT"):
            continue
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    return sorted(out)[:MAX_MARKET_SCAN_SYMBOLS]


def _market_label(trend: str) -> str:
    if trend in ["bullish", "weak_bullish"]:
        return "صعودی"
    if trend in ["bearish", "weak_bearish"]:
        return "نزولی"
    return "رنج"


def _normalize_trend(value: Any) -> str:
    t = str(value or "").lower().strip()
    if t in {"bullish", "strong_bullish", "weak_bullish", "uptrend", "long"}:
        return "bullish" if t != "weak_bullish" else "weak_bullish"
    if t in {"bearish", "strong_bearish", "weak_bearish", "downtrend", "short"}:
        return "bearish" if t != "weak_bearish" else "weak_bearish"
    return "range"


def _analyze_symbol_market(symbol: str) -> Optional[str]:
    """Return bullish/bearish/range for one symbol. Never raises."""
    if not get_klines or not add_indicators or not trend_direction:
        return None

    try:
        df_30m = add_indicators(get_klines(symbol, "30m"))
        df_15m = add_indicators(get_klines(symbol, "15m"))

        if df_30m is None or df_15m is None:
            return None
        if len(df_30m) < 30 or len(df_15m) < 30:
            return None

        trend_30m = _normalize_trend(trend_direction(df_30m))
        trend_15m = _normalize_trend(trend_direction(df_15m))

        # Stronger agreement: both TFs align.
        if trend_30m in ["bullish", "weak_bullish"] and trend_15m in ["bullish", "weak_bullish"]:
            return "bullish"

        if trend_30m in ["bearish", "weak_bearish"] and trend_15m in ["bearish", "weak_bearish"]:
            return "bearish"

        # If one TF is directional and the other is range, keep it weak/range for breadth.
        # This avoids overclaiming market bias from incomplete alignment.
        return "range"

    except Exception:
        return None


def _empty_breadth() -> Dict[str, Any]:
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
        "confidence": "LOW",
        "soft_score_long": 0,
        "soft_score_short": 0,
    }


def _classify_breadth(bullish: int, bearish: int, ranging: int, checked: int) -> Dict[str, Any]:
    bullish_pct = _safe_pct(bullish, checked)
    bearish_pct = _safe_pct(bearish, checked)
    range_pct = _safe_pct(ranging, checked)

    confidence = "LOW"
    if checked >= 30:
        confidence = "HIGH"
    elif checked >= MIN_CHECKED_FOR_STRONG_BIAS:
        confidence = "MEDIUM"

    bias = "neutral"
    bias_text = "خنثی"
    power = "ضعیف"
    soft_long = 0
    soft_short = 0

    # Soft context only. Do not hard-block trades.
    if checked < MIN_CHECKED_FOR_STRONG_BIAS:
        bias = "unknown"
        bias_text = "نامشخص"
        power = "ضعیف"
    elif bullish_pct >= 65:
        bias = "bullish"
        bias_text = "صعودی"
        power = "قوی" if bullish_pct >= 75 else "متوسط"
        soft_long = 3 if bullish_pct >= 75 else 2
    elif bearish_pct >= 65:
        bias = "bearish"
        bias_text = "نزولی"
        power = "قوی" if bearish_pct >= 75 else "متوسط"
        soft_short = 3 if bearish_pct >= 75 else 2
    elif range_pct >= 55:
        bias = "range"
        bias_text = "رنج"
        power = "ضعیف"
    elif bullish_pct > bearish_pct:
        bias = "weak_bullish"
        bias_text = "رنج متمایل به صعود"
        power = "متوسط"
        soft_long = 1
    elif bearish_pct > bullish_pct:
        bias = "weak_bearish"
        bias_text = "رنج متمایل به نزول"
        power = "متوسط"
        soft_short = 1

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
        "confidence": confidence,
        "soft_score_long": soft_long,
        "soft_score_short": soft_short,
    }


def get_market_breadth() -> Dict[str, Any]:
    symbols = _unique_symbols()
    if not symbols:
        return _empty_breadth()

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
        return _empty_breadth()

    return _classify_breadth(bullish, bearish, ranging, checked)


def get_market_breadth_profile() -> Dict[str, Any]:
    """Alias/profile for scanner/AI layers that want soft numeric context."""
    data = get_market_breadth()
    return {
        **data,
        "market_breadth_bias": data.get("bias", "unknown"),
        "market_breadth_power": data.get("power", "ضعیف"),
        "market_breadth_confidence": data.get("confidence", "LOW"),
        "is_soft_layer": True,
    }


def get_market_status_text() -> str:
    now = int(time.time())

    if MARKET_STATUS_CACHE.get("text") and now - _safe_int(MARKET_STATUS_CACHE.get("time"), 0) < CACHE_SECONDS:
        return str(MARKET_STATUS_CACHE["text"])

    data = get_market_breadth()
    MARKET_STATUS_CACHE["time"] = now
    MARKET_STATUS_CACHE["data"] = data

    if data.get("checked", 0) == 0:
        text = (
            "📊 وضعیت بازار\n\n"
            "داده کافی برای محاسبه وضعیت بازار دریافت نشد.\n"
            "چند دقیقه بعد دوباره امتحان کن."
        )
        MARKET_STATUS_CACHE["text"] = text
        return text

    text = (
        "📊 وضعیت بازار\n\n"
        f"تعداد ارزهای بررسی‌شده: {data['checked']}\n"
        f"🟢 صعودی: {data['bullish']} ارز | {data['bullish_pct']}٪\n"
        f"🔴 نزولی: {data['bearish']} ارز | {data['bearish_pct']}٪\n"
        f"⚪ رنج: {data['range']} ارز | {data['range_pct']}٪\n\n"
        f"نتیجه کلی: بازار {data['bias_text']} است.\n"
        f"قدرت وضعیت: {data['power']}\n"
        f"اعتماد گزارش: {data.get('confidence', 'LOW')}\n\n"
        "این گزارش فقط وضعیت کلی بازار را نشان می‌دهد و به تنهایی سیگنال ورود نیست."
    )

    MARKET_STATUS_CACHE["text"] = text
    return text


__all__ = [
    "get_market_breadth",
    "get_market_breadth_profile",
    "get_market_status_text",
]
