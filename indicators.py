from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from okx_data import Candle


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < period:
            out.append(None)
        else:
            out.append(sum(values[i + 1 - period:i + 1]) / period)
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    if not values:
        return []
    out: list[float | None] = [None] * len(values)
    k = 2 / (period + 1)
    e: float | None = None
    for i, v in enumerate(values):
        if e is None:
            if i + 1 >= period:
                e = sum(values[i + 1 - period:i + 1]) / period
                out[i] = e
        else:
            e = v * k + e * (1 - k)
            out[i] = e
    return out


def atr(candles: list[Candle], period: int = 14) -> list[float | None]:
    trs: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            trs.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return ema(trs, period)


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
        if i >= period:
            g = sum(gains[i - period:i]) / period
            l = sum(losses[i - period:i]) / period
            if l == 0:
                out[i] = 100.0
            else:
                rs = g / l
                out[i] = 100 - (100 / (1 + rs))
    return out


def vwap(candles: list[Candle], period: int = 48) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(candles)):
        if i + 1 < period:
            out.append(None)
            continue
        win = candles[i + 1 - period:i + 1]
        denom = sum(max(0.0, c.volume) for c in win)
        if denom <= 0:
            out.append(None)
        else:
            out.append(sum(((c.high + c.low + c.close) / 3) * c.volume for c in win) / denom)
    return out


@dataclass(frozen=True)
class Snapshot:
    close: float
    atr: float
    atr_avg: float
    ema20: float
    ema50: float
    ema200: float
    rsi: float
    vwap: float
    vol_avg: float
    swing_high: float
    swing_low: float


def snapshot(candles: list[Candle], swing_lookback: int = 12) -> Snapshot:
    if len(candles) < 30:
        raise ValueError("not enough candles")
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    atrs = atr(candles, 14)
    ema20s = ema(closes, 20)
    ema50s = ema(closes, 50)
    ema200s = ema(closes, 200)
    rsis = rsi(closes, 14)
    vwaps = vwap(candles, min(48, max(10, len(candles)//2)))
    idx = len(candles) - 1
    win = candles[-max(2, swing_lookback):]
    atr_vals = [x for x in atrs[-60:] if x is not None]
    return Snapshot(
        close=closes[-1],
        atr=float(atrs[idx] or (candles[-1].high - candles[-1].low)),
        atr_avg=float(mean(atr_vals)) if atr_vals else float(candles[-1].high - candles[-1].low),
        ema20=float(ema20s[idx] or closes[-1]),
        ema50=float(ema50s[idx] or closes[-1]),
        ema200=float(ema200s[idx] or ema50s[idx] or closes[-1]),
        rsi=float(rsis[idx] or 50.0),
        vwap=float(vwaps[idx] or closes[-1]),
        vol_avg=float(mean(volumes[-20:])) if len(volumes) >= 20 else float(mean(volumes)),
        swing_high=max(c.high for c in win),
        swing_low=min(c.low for c in win),
    )
