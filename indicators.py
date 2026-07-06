from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from okx_data import Candle


def ema(values: list[float], length: int) -> list[float | None]:
    if not values:
        return []
    out: list[float | None] = [None] * len(values)
    if len(values) < length:
        return out
    sma = sum(values[:length]) / length
    out[length - 1] = sma
    alpha = 2 / (length + 1)
    prev = sma
    for i in range(length, len(values)):
        prev = values[i] * alpha + prev * (1 - alpha)
        out[i] = prev
    return out


def rsi(values: list[float], length: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= length:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, length + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / length
    avg_loss = losses / length
    out[length] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    for i in range(length + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (length - 1) + gain) / length
        avg_loss = (avg_loss * (length - 1) + loss) / length
        out[i] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    return out


def macd(values: list[float]) -> tuple[list[float | None], list[float | None], list[float | None]]:
    e12 = ema(values, 12)
    e26 = ema(values, 26)
    line: list[float | None] = [None] * len(values)
    for i, (a, b) in enumerate(zip(e12, e26)):
        if a is not None and b is not None:
            line[i] = a - b
    compact = [x for x in line if x is not None]
    sig_compact = ema(compact, 9)
    signal: list[float | None] = [None] * len(values)
    hist: list[float | None] = [None] * len(values)
    j = 0
    for i, x in enumerate(line):
        if x is None:
            continue
        sig = sig_compact[j]
        if sig is not None:
            signal[i] = sig
            hist[i] = x - sig
        j += 1
    return line, signal, hist


def atr(candles: list[Candle], length: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(candles)
    if len(candles) <= length:
        return out
    trs: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            tr = c.high - c.low
        else:
            prev_close = candles[i - 1].close
            tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        trs.append(max(0.0, tr))
    first = sum(trs[1:length + 1]) / length
    out[length] = first
    prev = first
    for i in range(length + 1, len(candles)):
        prev = (prev * (length - 1) + trs[i]) / length
        out[i] = prev
    return out


@dataclass(frozen=True)
class Snapshot:
    close: float
    high: float
    low: float
    ema20: float
    ema50: float
    ema200: float
    prev_ema50: float
    prev_ema200: float
    rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    prev_macd_hist: float
    atr: float
    swing_high: float
    swing_low: float


def snapshot(candles: list[Candle], swing_lookback: int = 12) -> Snapshot:
    if len(candles) < 220:
        raise RuntimeError("کندل کافی برای EMA200 وجود ندارد")
    closes = [c.close for c in candles]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    rsi14 = rsi(closes, 14)
    macd_line, macd_sig, macd_hist = macd(closes)
    atr14 = atr(candles, 14)

    last = len(candles) - 1
    prev = last - 1
    required = [ema20[last], ema50[last], ema200[last], ema50[prev], ema200[prev], rsi14[last], macd_line[last], macd_sig[last], macd_hist[last], macd_hist[prev], atr14[last]]
    if any(x is None for x in required):
        raise RuntimeError("اندیکاتورها هنوز کامل نیستند")
    window = candles[max(0, last - swing_lookback): last + 1]
    return Snapshot(
        close=candles[last].close,
        high=candles[last].high,
        low=candles[last].low,
        ema20=float(ema20[last]),
        ema50=float(ema50[last]),
        ema200=float(ema200[last]),
        prev_ema50=float(ema50[prev]),
        prev_ema200=float(ema200[prev]),
        rsi=float(rsi14[last]),
        macd=float(macd_line[last]),
        macd_signal=float(macd_sig[last]),
        macd_hist=float(macd_hist[last]),
        prev_macd_hist=float(macd_hist[prev]),
        atr=float(atr14[last]),
        swing_high=max(c.high for c in window),
        swing_low=min(c.low for c in window),
    )
