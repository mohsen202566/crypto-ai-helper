"""
technical_sensors.py
Level 4 / 1H Smart Scalp Bot

Raw technical sensor engine.

Architecture lock:
- Calculates raw indicators/sensors only.
- No AI decision, no REAL/GHOST/REJECT decision, no TP/SL decision,
  no exchange trading, no JSON state writes, no Telegram text.
- Allowed project imports: constants.py, utils.py, models.py only.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from constants import SYSTEM_VERSION
from models import Candle, MarketSnapshot, SensorSnapshot
from utils import (
    clamp,
    normalize_symbol,
    safe_float,
    safe_str,
)


TECHNICAL_SENSORS_VERSION: str = SYSTEM_VERSION


# =============================================================================
# Basic series helpers
# =============================================================================

def candles_to_closes(candles: list[Candle]) -> list[float]:
    return [safe_float(c.close, 0.0) or 0.0 for c in candles]


def candles_to_highs(candles: list[Candle]) -> list[float]:
    return [safe_float(c.high, 0.0) or 0.0 for c in candles]


def candles_to_lows(candles: list[Candle]) -> list[float]:
    return [safe_float(c.low, 0.0) or 0.0 for c in candles]


def candles_to_volumes(candles: list[Candle]) -> list[float]:
    return [safe_float(c.volume, 0.0) or 0.0 for c in candles]


def last_value(values: list[float], default: Optional[float] = None) -> Optional[float]:
    if not values:
        return default
    value = safe_float(values[-1], default)
    return value


def sma(values: list[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    chunk = values[-period:]
    return sum(chunk) / period


def ema_series(values: list[float], period: int) -> list[float]:
    """Return EMA series with first EMA seeded from first value."""
    if period <= 0 or not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result: list[float] = []
    current = safe_float(values[0], 0.0) or 0.0
    result.append(current)
    for value in values[1:]:
        v = safe_float(value, current) or current
        current = (v * alpha) + (current * (1.0 - alpha))
        result.append(current)
    return result


def ema(values: list[float], period: int) -> Optional[float]:
    series = ema_series(values, period)
    return last_value(series, None)


def slope(values: list[float], lookback: int = 2) -> Optional[float]:
    """Return simple slope from N bars ago to latest."""
    if lookback <= 0 or len(values) <= lookback:
        return None
    latest = safe_float(values[-1], None)
    old = safe_float(values[-1 - lookback], None)
    if latest is None or old is None:
        return None
    return latest - old


def pct_slope(values: list[float], lookback: int = 2) -> Optional[float]:
    """Return percent slope from N bars ago to latest."""
    if lookback <= 0 or len(values) <= lookback:
        return None
    latest = safe_float(values[-1], None)
    old = safe_float(values[-1 - lookback], None)
    if latest is None or old is None or old == 0:
        return None
    return ((latest - old) / abs(old)) * 100.0


# =============================================================================
# Indicators
# =============================================================================

def rsi_series(closes: list[float], period: int = 14) -> list[float]:
    """Calculate RSI series using Wilder smoothing."""
    if period <= 0 or len(closes) < period + 1:
        return []

    gains: list[float] = []
    losses: list[float] = []

    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    result: list[float] = []
    first = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
    result.append(first)

    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0.0)
        loss = abs(min(change, 0.0))

        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(100.0 - (100.0 / (1.0 + rs)))

    return result


def rsi(closes: list[float], period: int = 14) -> Optional[float]:
    return last_value(rsi_series(closes, period), None)


def macd_values(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, Any]:
    """Return MACD, signal, histogram and histogram slope."""
    if len(closes) < slow + signal:
        return {"macd": None, "signal": None, "hist": None, "hist_slope": None, "hist_series": []}

    fast_ema = ema_series(closes, fast)
    slow_ema = ema_series(closes, slow)
    if not fast_ema or not slow_ema:
        return {"macd": None, "signal": None, "hist": None, "hist_slope": None, "hist_series": []}

    # Align same length.
    n = min(len(fast_ema), len(slow_ema))
    macd_line = [fast_ema[-n + i] - slow_ema[-n + i] for i in range(n)]

    signal_line = ema_series(macd_line, signal)
    n2 = min(len(macd_line), len(signal_line))
    hist = [macd_line[-n2 + i] - signal_line[-n2 + i] for i in range(n2)]

    return {
        "macd": last_value(macd_line, None),
        "signal": last_value(signal_line, None),
        "hist": last_value(hist, None),
        "hist_slope": slope(hist, 2),
        "hist_series": hist,
    }


def true_range(current: Candle, previous_close: float) -> float:
    high = safe_float(current.high, 0.0) or 0.0
    low = safe_float(current.low, 0.0) or 0.0
    return max(
        high - low,
        abs(high - previous_close),
        abs(low - previous_close),
    )


def atr_series(candles: list[Candle], period: int = 14) -> list[float]:
    """Calculate ATR series using Wilder smoothing."""
    if period <= 0 or len(candles) < period + 1:
        return []

    trs: list[float] = []
    for i in range(1, len(candles)):
        previous_close = safe_float(candles[i - 1].close, 0.0) or 0.0
        trs.append(true_range(candles[i], previous_close))

    if len(trs) < period:
        return []

    atr_values: list[float] = []
    current_atr = sum(trs[:period]) / period
    atr_values.append(current_atr)

    for tr in trs[period:]:
        current_atr = ((current_atr * (period - 1)) + tr) / period
        atr_values.append(current_atr)

    return atr_values


def atr(candles: list[Candle], period: int = 14) -> Optional[float]:
    return last_value(atr_series(candles, period), None)


def adx_values(candles: list[Candle], period: int = 14) -> dict[str, Any]:
    """Calculate ADX with +DI and -DI."""
    if period <= 0 or len(candles) < (period * 2) + 1:
        return {"adx": None, "plus_di": None, "minus_di": None, "adx_series": []}

    tr_list: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []

    for i in range(1, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]

        up_move = (safe_float(cur.high, 0.0) or 0.0) - (safe_float(prev.high, 0.0) or 0.0)
        down_move = (safe_float(prev.low, 0.0) or 0.0) - (safe_float(cur.low, 0.0) or 0.0)

        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        tr_list.append(true_range(cur, safe_float(prev.close, 0.0) or 0.0))

    if len(tr_list) < period:
        return {"adx": None, "plus_di": None, "minus_di": None, "adx_series": []}

    tr_smooth = sum(tr_list[:period])
    plus_smooth = sum(plus_dm[:period])
    minus_smooth = sum(minus_dm[:period])

    dx_values: list[float] = []
    plus_di_latest: Optional[float] = None
    minus_di_latest: Optional[float] = None

    for i in range(period, len(tr_list)):
        if i > period:
            tr_smooth = tr_smooth - (tr_smooth / period) + tr_list[i]
            plus_smooth = plus_smooth - (plus_smooth / period) + plus_dm[i]
            minus_smooth = minus_smooth - (minus_smooth / period) + minus_dm[i]

        if tr_smooth == 0:
            plus_di = 0.0
            minus_di = 0.0
        else:
            plus_di = 100.0 * (plus_smooth / tr_smooth)
            minus_di = 100.0 * (minus_smooth / tr_smooth)

        plus_di_latest = plus_di
        minus_di_latest = minus_di

        denom = plus_di + minus_di
        dx = 0.0 if denom == 0 else 100.0 * abs(plus_di - minus_di) / denom
        dx_values.append(dx)

    if len(dx_values) < period:
        return {"adx": None, "plus_di": plus_di_latest, "minus_di": minus_di_latest, "adx_series": []}

    adx_series_values: list[float] = []
    current_adx = sum(dx_values[:period]) / period
    adx_series_values.append(current_adx)

    for dx in dx_values[period:]:
        current_adx = ((current_adx * (period - 1)) + dx) / period
        adx_series_values.append(current_adx)

    return {
        "adx": last_value(adx_series_values, None),
        "plus_di": plus_di_latest,
        "minus_di": minus_di_latest,
        "adx_series": adx_series_values,
    }


def vwap(candles: list[Candle], lookback: int = 50) -> Optional[float]:
    """Calculate simple rolling VWAP over last N candles."""
    if not candles:
        return None

    sample = candles[-lookback:] if lookback > 0 else candles
    pv_sum = 0.0
    vol_sum = 0.0

    for candle in sample:
        high = safe_float(candle.high, 0.0) or 0.0
        low = safe_float(candle.low, 0.0) or 0.0
        close = safe_float(candle.close, 0.0) or 0.0
        volume = safe_float(candle.volume, 0.0) or 0.0
        typical = (high + low + close) / 3.0
        pv_sum += typical * volume
        vol_sum += volume

    if vol_sum <= 0:
        return None
    return pv_sum / vol_sum


# =============================================================================
# Candle / power sensors
# =============================================================================

def candle_body_pct(candle: Candle) -> float:
    high = safe_float(candle.high, 0.0) or 0.0
    low = safe_float(candle.low, 0.0) or 0.0
    open_ = safe_float(candle.open, 0.0) or 0.0
    close = safe_float(candle.close, 0.0) or 0.0
    rng = high - low
    if rng <= 0:
        return 0.0
    return abs(close - open_) / rng


def upper_wick_pct(candle: Candle) -> float:
    high = safe_float(candle.high, 0.0) or 0.0
    low = safe_float(candle.low, 0.0) or 0.0
    open_ = safe_float(candle.open, 0.0) or 0.0
    close = safe_float(candle.close, 0.0) or 0.0
    rng = high - low
    if rng <= 0:
        return 0.0
    return (high - max(open_, close)) / rng


def lower_wick_pct(candle: Candle) -> float:
    high = safe_float(candle.high, 0.0) or 0.0
    low = safe_float(candle.low, 0.0) or 0.0
    open_ = safe_float(candle.open, 0.0) or 0.0
    close = safe_float(candle.close, 0.0) or 0.0
    rng = high - low
    if rng <= 0:
        return 0.0
    return (min(open_, close) - low) / rng


def buy_sell_power(candles: list[Candle], lookback: int = 20) -> dict[str, float]:
    """
    Estimate buy/sell power from candle body direction and volume.

    Output percentages sum approximately to 100.
    """
    if not candles:
        return {"buy_power": 50.0, "sell_power": 50.0}

    sample = candles[-lookback:] if lookback > 0 else candles
    buy = 0.0
    sell = 0.0

    for candle in sample:
        open_ = safe_float(candle.open, 0.0) or 0.0
        close = safe_float(candle.close, 0.0) or 0.0
        high = safe_float(candle.high, 0.0) or 0.0
        low = safe_float(candle.low, 0.0) or 0.0
        volume = max(0.0, safe_float(candle.volume, 0.0) or 0.0)
        rng = max(high - low, 1e-12)

        body_bias = (close - open_) / rng
        if body_bias >= 0:
            buy += volume * (0.5 + min(0.5, abs(body_bias)))
            sell += volume * (0.5 - min(0.5, abs(body_bias)))
        else:
            sell += volume * (0.5 + min(0.5, abs(body_bias)))
            buy += volume * (0.5 - min(0.5, abs(body_bias)))

    total = buy + sell
    if total <= 0:
        return {"buy_power": 50.0, "sell_power": 50.0}

    return {
        "buy_power": (buy / total) * 100.0,
        "sell_power": (sell / total) * 100.0,
    }


def volume_ratio(candles: list[Candle], short: int = 5, long: int = 30) -> Optional[float]:
    """Return recent average volume / longer average volume."""
    volumes = candles_to_volumes(candles)
    short_avg = sma(volumes, short)
    long_avg = sma(volumes, long)
    if short_avg is None or long_avg is None or long_avg == 0:
        return None
    return short_avg / long_avg


# =============================================================================
# Snapshot builder
# =============================================================================

def build_sensor_snapshot(snapshot: MarketSnapshot) -> SensorSnapshot:
    """Build SensorSnapshot from MarketSnapshot candles."""
    candles = list(snapshot.candles or [])
    closes = candles_to_closes(candles)
    latest = candles[-1] if candles else Candle(timeframe=snapshot.timeframe)

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    vwap_value = vwap(candles, 50)
    rsi_values = rsi_series(closes, 14)
    rsi_value = last_value(rsi_values, None)
    rsi_slope_value = slope(rsi_values, 2)
    macd = macd_values(closes)
    atr_value = atr(candles, 14)
    price = safe_float(snapshot.current_price, 0.0) or candles_to_closes(candles)[-1] if candles else 0.0
    atr_pct = None if atr_value is None or price <= 0 else (atr_value / price) * 100.0
    adx = adx_values(candles, 14)
    power = buy_sell_power(candles, 20)
    vol_ratio = volume_ratio(candles, 5, 30)

    return SensorSnapshot(
        symbol=snapshot.symbol,
        timeframe=snapshot.timeframe,
        price=price,
        ema20=ema20,
        ema50=ema50,
        vwap=vwap_value,
        rsi=rsi_value,
        rsi_slope=rsi_slope_value,
        macd=macd["macd"],
        macd_signal=macd["signal"],
        macd_hist=macd["hist"],
        macd_hist_slope=macd["hist_slope"],
        adx=adx["adx"],
        atr=atr_value,
        atr_pct=atr_pct,
        buy_power=power["buy_power"],
        sell_power=power["sell_power"],
        volume_ratio=vol_ratio,
        candle_body_pct=candle_body_pct(latest),
        upper_wick_pct=upper_wick_pct(latest),
        lower_wick_pct=lower_wick_pct(latest),
        raw={
            "plus_di": adx["plus_di"],
            "minus_di": adx["minus_di"],
            "candle_count": len(candles),
            "source": snapshot.source,
            "ok": snapshot.ok,
            "error": snapshot.error,
        },
    )


def build_sensor_snapshot_from_candles(
    symbol: str,
    timeframe: str,
    candles: list[Candle],
) -> SensorSnapshot:
    """Build SensorSnapshot directly from candles for tests/backfills."""
    from models import MarketSnapshot  # local import of allowed module avoids circular runtime concerns

    ordered = sorted(candles, key=lambda c: c.timestamp)
    price = safe_float(ordered[-1].close, 0.0) if ordered else 0.0
    snapshot = MarketSnapshot(
        symbol=normalize_symbol(symbol),
        timeframe=safe_str(timeframe),
        candles=ordered,
        current_price=price or 0.0,
        ok=bool(ordered),
        source="OFFLINE",
        error="" if ordered else "no_candles",
    )
    return build_sensor_snapshot(snapshot)


def validate_sensor_snapshot(sensor: SensorSnapshot) -> dict[str, Any]:
    """Lightweight validation for a sensor snapshot."""
    errors: list[str] = []

    if not sensor.symbol:
        errors.append("missing_symbol")
    if not sensor.timeframe:
        errors.append("missing_timeframe")
    if sensor.price <= 0:
        errors.append("invalid_price")
    if sensor.rsi is not None and not (0 <= sensor.rsi <= 100):
        errors.append("invalid_rsi")
    if sensor.buy_power is not None and not (0 <= sensor.buy_power <= 100):
        errors.append("invalid_buy_power")
    if sensor.sell_power is not None and not (0 <= sensor.sell_power <= 100):
        errors.append("invalid_sell_power")

    return {
        "valid": not errors,
        "errors": errors,
        "symbol": sensor.symbol,
        "timeframe": sensor.timeframe,
    }


__all__ = [
    "TECHNICAL_SENSORS_VERSION",
    "candles_to_closes",
    "candles_to_highs",
    "candles_to_lows",
    "candles_to_volumes",
    "last_value",
    "sma",
    "ema_series",
    "ema",
    "slope",
    "pct_slope",
    "rsi_series",
    "rsi",
    "macd_values",
    "true_range",
    "atr_series",
    "atr",
    "adx_values",
    "vwap",
    "candle_body_pct",
    "upper_wick_pct",
    "lower_wick_pct",
    "buy_sell_power",
    "volume_ratio",
    "build_sensor_snapshot",
    "build_sensor_snapshot_from_candles",
    "validate_sensor_snapshot",
]
