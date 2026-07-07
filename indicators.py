from __future__ import annotations

from dataclasses import dataclass

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


def true_ranges(candles: list[Candle]) -> list[float]:
    trs: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            tr = c.high - c.low
        else:
            prev_close = candles[i - 1].close
            tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        trs.append(max(0.0, tr))
    return trs


def wilder_smooth(values: list[float], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= length:
        return out
    first = sum(values[1:length + 1]) / length
    out[length] = first
    prev = first
    for i in range(length + 1, len(values)):
        prev = (prev * (length - 1) + values[i]) / length
        out[i] = prev
    return out


def atr(candles: list[Candle], length: int = 14) -> list[float | None]:
    if len(candles) <= length:
        return [None] * len(candles)
    return wilder_smooth(true_ranges(candles), length)


def adx_dmi(candles: list[Candle], length: int = 14) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Return ADX, +DI, -DI using Wilder smoothing."""
    n = len(candles)
    adx: list[float | None] = [None] * n
    plus_di: list[float | None] = [None] * n
    minus_di: list[float | None] = [None] * n
    if n <= length * 2:
        return adx, plus_di, minus_di

    trs = true_ranges(candles)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up_move = candles[i].high - candles[i - 1].high
        down_move = candles[i - 1].low - candles[i].low
        plus_dm[i] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[i] = down_move if down_move > up_move and down_move > 0 else 0.0

    smoothed_tr = wilder_smooth(trs, length)
    smoothed_plus = wilder_smooth(plus_dm, length)
    smoothed_minus = wilder_smooth(minus_dm, length)

    dx: list[float | None] = [None] * n
    for i in range(n):
        tr = smoothed_tr[i]
        pdm = smoothed_plus[i]
        mdm = smoothed_minus[i]
        if tr is None or pdm is None or mdm is None or tr <= 0:
            continue
        plus = 100.0 * pdm / tr
        minus = 100.0 * mdm / tr
        plus_di[i] = plus
        minus_di[i] = minus
        denom = plus + minus
        dx[i] = 0.0 if denom <= 0 else 100.0 * abs(plus - minus) / denom

    valid_dx = [x for x in dx if x is not None]
    if len(valid_dx) < length:
        return adx, plus_di, minus_di

    # Build ADX aligned to original indexes.
    first_index = next(i for i, x in enumerate(dx) if x is not None)
    seed_index = first_index + length - 1
    if seed_index < n:
        seed_values = [float(x) for x in dx[first_index:seed_index + 1] if x is not None]
        if len(seed_values) == length:
            prev = sum(seed_values) / length
            adx[seed_index] = prev
            for i in range(seed_index + 1, n):
                if dx[i] is None:
                    continue
                prev = (prev * (length - 1) + float(dx[i])) / length
                adx[i] = prev
    return adx, plus_di, minus_di


def bollinger_band_width(values: list[float], length: int = 20, mult: float = 2.0) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < length:
        return out
    for i in range(length - 1, len(values)):
        window = values[i - length + 1:i + 1]
        mean = sum(window) / length
        variance = sum((x - mean) ** 2 for x in window) / length
        std = variance ** 0.5
        if mean > 0:
            out[i] = ((mean + mult * std) - (mean - mult * std)) / mean
    return out


@dataclass(frozen=True)
class Snapshot:
    close: float
    open: float
    high: float
    low: float
    ema20: float
    ema50: float
    ema200: float
    prev_ema20: float
    prev_ema50: float
    ema50_lookback: float
    rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    prev_macd_hist: float
    atr: float
    prev_atr: float
    adx: float
    prev_adx: float
    plus_di: float
    minus_di: float
    bb_width: float
    swing_high: float
    swing_low: float


def snapshot(candles: list[Candle], swing_lookback: int = 12, slope_lookback: int = 10) -> Snapshot:
    if len(candles) < 220:
        raise RuntimeError("کندل کافی برای EMA200 وجود ندارد")
    closes = [c.close for c in candles]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    rsi14 = rsi(closes, 14)
    macd_line, macd_sig, macd_hist = macd(closes)
    atr14 = atr(candles, 14)
    adx14, plus_di, minus_di = adx_dmi(candles, 14)
    bbw = bollinger_band_width(closes, 20)

    last = len(candles) - 1
    prev = last - 1
    slope_i = max(0, last - int(slope_lookback))
    required = [
        ema20[last], ema50[last], ema200[last], ema20[prev], ema50[prev], ema50[slope_i],
        rsi14[last], macd_line[last], macd_sig[last], macd_hist[last], macd_hist[prev],
        atr14[last], atr14[prev], adx14[last], adx14[prev], plus_di[last], minus_di[last], bbw[last],
    ]
    if any(x is None for x in required):
        raise RuntimeError("اندیکاتورها هنوز کامل نیستند")
    window = candles[max(0, last - swing_lookback): last + 1]
    c = candles[last]
    return Snapshot(
        close=c.close,
        open=c.open,
        high=c.high,
        low=c.low,
        ema20=float(ema20[last]),
        ema50=float(ema50[last]),
        ema200=float(ema200[last]),
        prev_ema20=float(ema20[prev]),
        prev_ema50=float(ema50[prev]),
        ema50_lookback=float(ema50[slope_i]),
        rsi=float(rsi14[last]),
        macd=float(macd_line[last]),
        macd_signal=float(macd_sig[last]),
        macd_hist=float(macd_hist[last]),
        prev_macd_hist=float(macd_hist[prev]),
        atr=float(atr14[last]),
        prev_atr=float(atr14[prev]),
        adx=float(adx14[last]),
        prev_adx=float(adx14[prev]),
        plus_di=float(plus_di[last]),
        minus_di=float(minus_di[last]),
        bb_width=float(bbw[last]),
        swing_high=max(c.high for c in window),
        swing_low=min(c.low for c in window),
    )
