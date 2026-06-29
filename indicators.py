"""محاسبه اندیکاتورهای کلاسیک ربات."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from . import config
from .utils import candle_age_seconds, safe_float


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _adx(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().fillna(0)


def candles_to_df(candles: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    if df.empty:
        raise ValueError("کندلی برای محاسبه اندیکاتور وجود ندارد")
    for col in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce").fillna(0).astype("int64")
    return df


def calculate_indicators(candles: list[dict[str, Any]]) -> dict[str, Any]:
    df = candles_to_df(candles)
    if len(df) < 60:
        raise ValueError("برای محاسبه اندیکاتور حداقل ۶۰ کندل لازم است")

    df["ema_fast"] = df["close"].ewm(span=config.EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=config.EMA_SLOW, adjust=False).mean()

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cumulative_volume = df["volume"].replace(0, np.nan).cumsum()
    df["vwap"] = (typical_price * df["volume"]).cumsum() / cumulative_volume
    df["vwap"] = df["vwap"].ffill().bfill()

    df["volume_ma"] = df["volume"].rolling(config.VOLUME_MA_PERIOD, min_periods=config.VOLUME_MA_PERIOD).mean()
    df["rsi"] = _rsi(df["close"], config.RSI_PERIOD)
    df["atr"] = _atr(df, config.ATR_PERIOD)
    df["adx"] = _adx(df, config.ADX_PERIOD)

    rolling_mid = df["close"].rolling(config.BOLLINGER_PERIOD, min_periods=config.BOLLINGER_PERIOD).mean()
    rolling_std = df["close"].rolling(config.BOLLINGER_PERIOD, min_periods=config.BOLLINGER_PERIOD).std(ddof=0)
    df["bb_mid"] = rolling_mid
    df["bb_upper"] = rolling_mid + config.BOLLINGER_STD * rolling_std
    df["bb_lower"] = rolling_mid - config.BOLLINGER_STD * rolling_std

    latest = df.iloc[-1]
    previous = df.iloc[-2]
    current_volume = safe_float(latest["volume"])
    age = max(1, candle_age_seconds(int(latest["open_time"])))
    elapsed_ratio = min(1.0, age / config.TIMEFRAME_SECONDS)
    projected_volume = current_volume / max(0.05, elapsed_ratio)
    volume_ma = safe_float(latest["volume_ma"])
    volume_multiplier = projected_volume / volume_ma if volume_ma > 0 else 0.0
    close = safe_float(latest["close"])
    atr = safe_float(latest["atr"])
    atr_percent = (atr / close * 100) if close > 0 else 0.0

    return {
        "open_time": int(latest["open_time"]),
        "open": safe_float(latest["open"]),
        "high": safe_float(latest["high"]),
        "low": safe_float(latest["low"]),
        "close": close,
        "volume": current_volume,
        "projected_volume": projected_volume,
        "volume_ma": volume_ma,
        "volume_multiplier": volume_multiplier,
        "ema_fast": safe_float(latest["ema_fast"]),
        "ema_slow": safe_float(latest["ema_slow"]),
        "ema_fast_prev": safe_float(previous["ema_fast"]),
        "ema_slow_prev": safe_float(previous["ema_slow"]),
        "vwap": safe_float(latest["vwap"]),
        "rsi": safe_float(latest["rsi"]),
        "rsi_prev": safe_float(previous["rsi"]),
        "atr": atr,
        "atr_percent": atr_percent,
        "adx": safe_float(latest["adx"]),
        "bb_mid": safe_float(latest["bb_mid"]),
        "bb_upper": safe_float(latest["bb_upper"]),
        "bb_lower": safe_float(latest["bb_lower"]),
        "candle_age": age,
        "confirm": str(latest.get("confirm", "0")),
    }
