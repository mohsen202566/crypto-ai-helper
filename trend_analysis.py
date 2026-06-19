# -*- coding: utf-8 -*-
"""
trend_analysis.py

Soft trend / breakout helper layer for the crypto futures bot.

Goals:
- Preserve old public functions:
    detect_trendline
    detect_breakout
    trendline_score
    breakout_score
- Avoid crashes on short/dirty DataFrames.
- Keep this module as a SOFT signal helper, not a hard filter.
- Support better near-term direction detection for 5M-15M scalping by adding
  compact profile data usable by analysis.py / scanner.py.
"""

import math
from typing import Any, Dict, Tuple


REQUIRED_COLUMNS = ("high", "low", "close")
OPTIONAL_VOLUME_COLUMN = "volume"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return float(default)
        return v
    except Exception:
        return float(default)


def _has_columns(df, columns) -> bool:
    try:
        return df is not None and all(c in df.columns for c in columns)
    except Exception:
        return False


def _clean_df(df):
    """Return a compact numeric dataframe slice-safe view."""
    if not _has_columns(df, REQUIRED_COLUMNS):
        return None
    try:
        out = df.copy()
        for col in set(REQUIRED_COLUMNS + (OPTIONAL_VOLUME_COLUMN,)):
            if col in out.columns:
                out[col] = out[col].astype(float)
        out = out.dropna(subset=list(REQUIRED_COLUMNS))
        if len(out) < 5:
            return None
        return out
    except Exception:
        return None


def _ema(values, period: int):
    try:
        return values.ewm(span=period, adjust=False).mean()
    except Exception:
        return None


def detect_trendline(df, lookback=80):
    """
    Detect broad trend direction.

    Return values kept backward-compatible:
    - uptrend
    - downtrend
    - sideways
    - unknown
    """
    d = _clean_df(df)
    if d is None:
        return "unknown"

    lookback = max(12, int(lookback or 80))
    if len(d) < max(12, lookback // 2):
        return "unknown"

    recent = d.tail(min(lookback, len(d)))
    if len(recent) < 12:
        return "unknown"

    mid = max(1, len(recent) // 2)
    first = recent.iloc[:mid]
    second = recent.iloc[mid:]

    first_low = _safe_float(first["low"].min())
    second_low = _safe_float(second["low"].min())
    first_high = _safe_float(first["high"].max())
    second_high = _safe_float(second["high"].max())
    first_close = _safe_float(first["close"].iloc[-1])
    second_close = _safe_float(second["close"].iloc[-1])

    # ATR-like tolerance prevents tiny noise from flipping the trend.
    avg_range = _safe_float((recent["high"] - recent["low"]).tail(20).mean())
    last_close = max(_safe_float(recent["close"].iloc[-1]), 1e-12)
    tolerance = max(avg_range * 0.15, last_close * 0.0008)

    higher_lows = second_low > first_low + tolerance
    higher_highs = second_high > first_high + tolerance
    lower_lows = second_low < first_low - tolerance
    lower_highs = second_high < first_high - tolerance

    if higher_lows and higher_highs and second_close >= first_close - tolerance:
        return "uptrend"

    if lower_lows and lower_highs and second_close <= first_close + tolerance:
        return "downtrend"

    return "sideways"


def detect_breakout(df, lookback=30):
    """
    Detect fresh breakout or fake breakout.

    Return values kept backward-compatible:
    - bullish_breakout
    - bearish_breakout
    - fake_bullish_breakout
    - fake_bearish_breakout
    - no_breakout
    - unknown
    """
    d = _clean_df(df)
    if d is None:
        return "unknown"

    lookback = max(10, int(lookback or 30))
    if len(d) < lookback + 1:
        return "unknown"

    recent = d.tail(lookback + 1)
    last = recent.iloc[-1]
    previous = recent.iloc[:-1]

    resistance = _safe_float(previous["high"].max())
    support = _safe_float(previous["low"].min())
    close = _safe_float(last["close"])
    high = _safe_float(last["high"])
    low = _safe_float(last["low"])

    last_volume = _safe_float(last.get("volume", 0.0), 0.0)
    if "volume" in previous.columns:
        avg_volume = _safe_float(previous["volume"].mean(), 0.0)
    else:
        avg_volume = 0.0

    # If volume is unavailable, do not block detection; just require a cleaner close.
    volume_ok = True if avg_volume <= 0 else last_volume > avg_volume * 1.25

    range_avg = _safe_float((previous["high"] - previous["low"]).tail(20).mean())
    tolerance = max(range_avg * 0.08, max(close, 1e-12) * 0.0005)

    bullish_close_break = close > resistance + tolerance
    bearish_close_break = close < support - tolerance

    if bullish_close_break and volume_ok:
        return "bullish_breakout"

    if bearish_close_break and volume_ok:
        return "bearish_breakout"

    # Fake breakout / liquidity sweep style reactions.
    if high > resistance + tolerance and close < resistance:
        return "fake_bullish_breakout"

    if low < support - tolerance and close > support:
        return "fake_bearish_breakout"

    return "no_breakout"


def trendline_score(trendline):
    """
    Soft scoring only. Keep weights moderate so this helper does not dominate AI.
    Returns: (long_score, short_score)
    """
    if trendline == "uptrend":
        return 8, 0

    if trendline == "downtrend":
        return 0, 8

    return 0, 0


def breakout_score(breakout):
    """
    Soft scoring only. Fake bullish breakout favors SHORT; fake bearish favors LONG.
    Returns: (long_score, short_score)
    """
    if breakout == "bullish_breakout":
        return 10, 0

    if breakout == "bearish_breakout":
        return 0, 10

    if breakout == "fake_bullish_breakout":
        return 0, 7

    if breakout == "fake_bearish_breakout":
        return 7, 0

    return 0, 0


def get_trend_profile(df, trend_lookback: int = 80, breakout_lookback: int = 30) -> Dict[str, Any]:
    """
    New non-breaking helper for analysis/scanner.

    This provides a compact profile for AI coordination:
    - trendline
    - breakout
    - long/short soft scores
    - directional_bias
    - confidence
    """
    trendline = detect_trendline(df, trend_lookback)
    breakout = detect_breakout(df, breakout_lookback)

    tl_long, tl_short = trendline_score(trendline)
    bo_long, bo_short = breakout_score(breakout)

    long_score = tl_long + bo_long
    short_score = tl_short + bo_short

    if long_score > short_score:
        bias = "BULLISH"
    elif short_score > long_score:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    strength = abs(long_score - short_score)
    if trendline == "unknown" or breakout == "unknown":
        confidence = "LOW_DATA"
    elif strength >= 14:
        confidence = "HIGH"
    elif strength >= 7:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return {
        "trendline": trendline,
        "breakout": breakout,
        "long_score": int(long_score),
        "short_score": int(short_score),
        "directional_bias": bias,
        "confidence": confidence,
        "soft_layer": True,
        "source": "trend_analysis",
    }


# Backward-compatible alias names that future files may import.
trend_profile = get_trend_profile
detect_trend_profile = get_trend_profile
