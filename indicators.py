"""اندیکاتورها و ابزارهای تحلیل کندل."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def candles_to_df(candles: list[list[Any]]) -> pd.DataFrame:
    """کندل‌های OKX را به DataFrame مرتب‌شده از قدیم به جدید تبدیل می‌کند."""
    rows = []
    for c in candles:
        if len(c) < 6:
            continue
        rows.append({
            "ts": int(float(c[0])),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("ts").reset_index(drop=True)


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    fast = ema(series, 12)
    slow = ema(series, 26)
    line = fast - slow
    signal = ema(line, 9)
    hist = line - signal
    return line, signal, hist


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["ema20"] = ema(out["close"], 20)
    out["ema50"] = ema(out["close"], 50)
    out["ema100"] = ema(out["close"], 100)
    out["rsi14"] = rsi(out["close"], 14)
    line, signal, hist = macd(out["close"])
    out["macd"] = line
    out["macd_signal"] = signal
    out["macd_hist"] = hist
    out["vol_avg20"] = out["volume"].rolling(20).mean()
    out["atr14"] = atr(out, 14)
    return out


def last(df: pd.DataFrame) -> pd.Series:
    return df.iloc[-1]


def pct_from_high(df: pd.DataFrame, lookback: int = 24) -> float:
    if df.empty:
        return 0.0
    part = df.tail(lookback)
    high = float(part["high"].max())
    close = float(part.iloc[-1]["close"])
    if high <= 0:
        return 0.0
    return (high - close) / high * 100.0


def pct_to_recent_high(df: pd.DataFrame, lookback: int = 60) -> float:
    if df.empty:
        return 0.0
    part = df.tail(lookback)
    high = float(part["high"].max())
    close = float(part.iloc[-1]["close"])
    if close <= 0:
        return 0.0
    return (high - close) / close * 100.0


def last_n_change_pct(df: pd.DataFrame, n: int) -> float:
    if len(df) <= n:
        return 0.0
    old = float(df.iloc[-n - 1]["close"])
    new = float(df.iloc[-1]["close"])
    if old <= 0:
        return 0.0
    return (new - old) / old * 100.0


def bullish_candle(row: pd.Series) -> bool:
    return float(row["close"]) > float(row["open"])


def strong_bullish_candle(row: pd.Series) -> bool:
    rng = float(row["high"] - row["low"])
    if rng <= 0:
        return False
    body = float(row["close"] - row["open"])
    return body > 0 and body / rng >= 0.55


def volume_ratio(row: pd.Series) -> float:
    avg = float(row.get("vol_avg20") or 0)
    if avg <= 0:
        return 1.0
    return float(row.get("volume") or 0) / avg


def above_emas(row: pd.Series) -> bool:
    close = float(row["close"])
    return close > float(row.get("ema20") or 0) and close > float(row.get("ema50") or 0)


def ema_bullish(row: pd.Series) -> bool:
    return float(row.get("ema20") or 0) > float(row.get("ema50") or 0)


def near_ema_support(row: pd.Series, max_distance_pct: float = 1.8) -> bool:
    close = float(row["close"])
    if close <= 0:
        return False
    ema20 = float(row.get("ema20") or 0)
    ema50 = float(row.get("ema50") or 0)
    distances = []
    if ema20 > 0:
        distances.append(abs(close - ema20) / close * 100.0)
    if ema50 > 0:
        distances.append(abs(close - ema50) / close * 100.0)
    return bool(distances) and min(distances) <= max_distance_pct
