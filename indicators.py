from __future__ import annotations

from dataclasses import dataclass

from okx_data import Candle


@dataclass(frozen=True)
class IndicatorSnapshot:
    rsi: float
    prev_rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    prev_macd: float
    prev_macd_signal: float
    prev_macd_hist: float
    adx: float
    prev_adx: float


def calculate_indicators(candles: list[Candle]) -> IndicatorSnapshot:
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    rsi_values = _rsi(closes, 14)
    macd_line, signal_line, hist_line = _macd(closes, 12, 26, 9)
    adx_values = _adx(highs, lows, closes, 14)
    last = _last_complete_index(rsi_values, macd_line, signal_line, hist_line, adx_values)
    prev = _previous_complete_index(last, rsi_values, macd_line, signal_line, hist_line, adx_values)
    return IndicatorSnapshot(
        rsi=float(rsi_values[last]),
        prev_rsi=float(rsi_values[prev]),
        macd=float(macd_line[last]),
        macd_signal=float(signal_line[last]),
        macd_hist=float(hist_line[last]),
        prev_macd=float(macd_line[prev]),
        prev_macd_signal=float(signal_line[prev]),
        prev_macd_hist=float(hist_line[prev]),
        adx=float(adx_values[last]),
        prev_adx=float(adx_values[prev]),
    )


def _last_complete_index(*series: list[float | None]) -> int:
    length = min(len(values) for values in series)
    for index in range(length - 1, -1, -1):
        if all(values[index] is not None for values in series):
            return index
    raise RuntimeError("اندیکاتورها هنوز مقدار کامل ندارند.")


def _previous_complete_index(start: int, *series: list[float | None]) -> int:
    for index in range(start - 1, -1, -1):
        if all(values[index] is not None for values in series):
            return index
    raise RuntimeError("مقدار قبلی اندیکاتورها کامل نیست.")


def _rsi(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    result[period] = _rsi_value(avg_gain, avg_loss)
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        result[index] = _rsi_value(avg_gain, avg_loss)
    return result


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    sma = sum(values[:period]) / period
    result[period - 1] = sma
    multiplier = 2.0 / (period + 1.0)
    previous = sma
    for index in range(period, len(values)):
        previous = ((values[index] - previous) * multiplier) + previous
        result[index] = previous
    return result


def _ema_from_optional(values: list[float | None], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    valid_indexes = [index for index, value in enumerate(values) if value is not None]
    if len(valid_indexes) < period:
        return result
    start = valid_indexes[0]
    compact = [float(values[index]) for index in valid_indexes]
    compact_ema = _ema(compact, period)
    for compact_index, value in enumerate(compact_ema):
        if value is not None:
            result[start + compact_index] = value
    return result


def _macd(values: list[float], fast: int, slow: int, signal: int) -> tuple[list[float | None], list[float | None], list[float | None]]:
    fast_ema = _ema(values, fast)
    slow_ema = _ema(values, slow)
    macd_line: list[float | None] = [None] * len(values)
    for index, slow_value in enumerate(slow_ema):
        fast_value = fast_ema[index]
        if fast_value is not None and slow_value is not None:
            macd_line[index] = fast_value - slow_value
    signal_line = _ema_from_optional(macd_line, signal)
    hist_line: list[float | None] = [None] * len(values)
    for index, macd_value in enumerate(macd_line):
        signal_value = signal_line[index]
        if macd_value is not None and signal_value is not None:
            hist_line[index] = macd_value - signal_value
    return macd_line, signal_line, hist_line


def _adx(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float | None]:
    length = len(closes)
    result: list[float | None] = [None] * length
    if length <= period * 2:
        return result
    tr = [0.0] * length
    plus_dm = [0.0] * length
    minus_dm = [0.0] * length
    for index in range(1, length):
        high_diff = highs[index] - highs[index - 1]
        low_diff = lows[index - 1] - lows[index]
        plus_dm[index] = high_diff if high_diff > low_diff and high_diff > 0 else 0.0
        minus_dm[index] = low_diff if low_diff > high_diff and low_diff > 0 else 0.0
        tr[index] = max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )
    tr_smooth = sum(tr[1 : period + 1])
    plus_smooth = sum(plus_dm[1 : period + 1])
    minus_smooth = sum(minus_dm[1 : period + 1])
    dx: list[float | None] = [None] * length
    for index in range(period, length):
        if index > period:
            tr_smooth = tr_smooth - (tr_smooth / period) + tr[index]
            plus_smooth = plus_smooth - (plus_smooth / period) + plus_dm[index]
            minus_smooth = minus_smooth - (minus_smooth / period) + minus_dm[index]
        if tr_smooth == 0:
            continue
        plus_di = 100.0 * (plus_smooth / tr_smooth)
        minus_di = 100.0 * (minus_smooth / tr_smooth)
        denom = plus_di + minus_di
        if denom == 0:
            continue
        dx[index] = 100.0 * abs(plus_di - minus_di) / denom
    first_adx_index = period * 2
    first_dx_values = [value for value in dx[period:first_adx_index] if value is not None]
    if len(first_dx_values) < period:
        return result
    adx_value = sum(first_dx_values[-period:]) / period
    result[first_adx_index - 1] = adx_value
    for index in range(first_adx_index, length):
        if dx[index] is None:
            continue
        adx_value = ((adx_value * (period - 1)) + float(dx[index])) / period
        result[index] = adx_value
    return result
