from __future__ import annotations

from dataclasses import dataclass
from math import isnan
from statistics import mean, pstdev

from okx_client import Candle


def closes(candles: list[Candle]) -> list[float]:
    return [c.close for c in candles]


def highs(candles: list[Candle]) -> list[float]:
    return [c.high for c in candles]


def lows(candles: list[Candle]) -> list[float]:
    return [c.low for c in candles]


def volumes(candles: list[Candle]) -> list[float]:
    return [c.volume for c in candles]


def ema(values: list[float], period: int) -> list[float]:
    if period <= 0 or not values:
        return []
    alpha = 2 / (period + 1)
    out: list[float] = []
    current = values[0]
    for value in values:
        current = alpha * value + (1 - alpha) * current
        out.append(current)
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    if len(values) < period + 2:
        return []
    gains: list[float] = [0.0]
    losses: list[float] = [0.0]
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    out = [50.0] * period
    for i in range(period, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            out.append(100.0)
        else:
            rs = avg_gain / avg_loss
            out.append(100 - (100 / (1 + rs)))
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[float], list[float], list[float]]:
    if len(values) < slow + signal:
        return [], [], []
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    line = [f - s for f, s in zip(fast_ema, slow_ema)]
    sig = ema(line, signal)
    hist = [m - s for m, s in zip(line, sig)]
    return line, sig, hist


def atr(candles: list[Candle], period: int = 14) -> list[float]:
    if len(candles) < period + 1:
        return []
    trs: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c.high - c.low)
        else:
            prev_close = candles[i - 1].close
            trs.append(max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close)))
    out: list[float] = []
    current = sum(trs[:period]) / period
    for i, tr in enumerate(trs):
        if i < period:
            out.append(current)
        else:
            current = (current * (period - 1) + tr) / period
            out.append(current)
    return out


def adx(candles: list[Candle], period: int = 14) -> list[float]:
    if len(candles) < period * 2:
        return []
    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    trs: list[float] = [candles[0].high - candles[0].low]
    for i in range(1, len(candles)):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        prev_close = candles[i - 1].close
        trs.append(max(candles[i].high - candles[i].low, abs(candles[i].high - prev_close), abs(candles[i].low - prev_close)))

    out: list[float] = [20.0] * period
    for i in range(period, len(candles)):
        tr_sum = sum(trs[i - period + 1 : i + 1])
        if tr_sum == 0:
            out.append(0.0)
            continue
        pdi = 100 * sum(plus_dm[i - period + 1 : i + 1]) / tr_sum
        mdi = 100 * sum(minus_dm[i - period + 1 : i + 1]) / tr_sum
        dx = 100 * abs(pdi - mdi) / max(pdi + mdi, 1e-9)
        out.append(dx if len(out) == period else (out[-1] * (period - 1) + dx) / period)
    return out


def bollinger(values: list[float], period: int = 20, mult: float = 2.0) -> tuple[list[float], list[float], list[float], list[float]]:
    if len(values) < period:
        return [], [], [], []
    middle: list[float] = []
    upper: list[float] = []
    lower: list[float] = []
    width: list[float] = []
    for i in range(len(values)):
        window = values[max(0, i - period + 1) : i + 1]
        avg = mean(window)
        dev = pstdev(window) if len(window) > 1 else 0.0
        middle.append(avg)
        upper.append(avg + mult * dev)
        lower.append(avg - mult * dev)
        width.append((upper[-1] - lower[-1]) / avg * 100 if avg else 0.0)
    return middle, upper, lower, width


def supertrend_direction(candles: list[Candle], period: int = 10, multiplier: float = 3.0) -> str:
    atr_values = atr(candles, period)
    if not atr_values:
        return "نامشخص"
    last = candles[-1]
    basis = (last.high + last.low) / 2
    upper = basis + multiplier * atr_values[-1]
    lower = basis - multiplier * atr_values[-1]
    if last.close > upper:
        return "صعودی"
    if last.close < lower:
        return "نزولی"
    return "خنثی"


def recent_support(candles: list[Candle], lookback: int = 40) -> float:
    sample = candles[-lookback:] if len(candles) >= lookback else candles
    return min(c.low for c in sample)


def recent_resistance(candles: list[Candle], lookback: int = 40) -> float:
    sample = candles[-lookback:] if len(candles) >= lookback else candles
    return max(c.high for c in sample)


def clean_number(value: float) -> bool:
    return value is not None and not isnan(value) and value > 0


@dataclass(frozen=True)
class IndicatorSnapshot:
    close: float
    ema20: float
    ema50: float
    ema200: float
    rsi14: float
    macd_hist: float
    adx14: float
    atr14: float
    bb_width: float
    support: float
    resistance: float


def snapshot(candles: list[Candle]) -> IndicatorSnapshot:
    values = closes(candles)
    if len(values) < 210:
        raise RuntimeError("تعداد کندل برای محاسبه اندیکاتورها کافی نیست.")
    ema20 = ema(values, 20)[-1]
    ema50 = ema(values, 50)[-1]
    ema200 = ema(values, 200)[-1]
    rsi14 = rsi(values, 14)[-1]
    _, _, hist = macd(values)
    adx14 = adx(candles, 14)[-1]
    atr14 = atr(candles, 14)[-1]
    _, _, _, bb_width = bollinger(values)
    return IndicatorSnapshot(
        close=values[-1],
        ema20=ema20,
        ema50=ema50,
        ema200=ema200,
        rsi14=rsi14,
        macd_hist=hist[-1] if hist else 0.0,
        adx14=adx14,
        atr14=atr14,
        bb_width=bb_width[-1] if bb_width else 0.0,
        support=recent_support(candles),
        resistance=recent_resistance(candles),
    )
