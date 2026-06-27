from __future__ import annotations

from dataclasses import dataclass

from okx_data import Candle


@dataclass(frozen=True)
class IndicatorSnapshot:
    close: float
    prev_close: float
    high: float
    low: float
    open: float
    volume: float
    volume_ratio: float
    ema20: float
    ema50: float
    ema200: float
    prev_ema20: float
    prev_ema50: float
    prev_ema200: float
    rsi: float
    prev_rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    prev_macd_hist: float
    adx: float
    plus_di: float
    minus_di: float
    atr: float
    prev_atr: float
    vwap: float
    recent_high: float
    recent_low: float
    candle_range: float
    body_pct: float
    consecutive_up: int
    consecutive_down: int

    @property
    def ema50_slope_pct(self) -> float:
        if self.prev_ema50 <= 0:
            return 0.0
        return (self.ema50 - self.prev_ema50) / self.prev_ema50

    @property
    def ema200_slope_pct(self) -> float:
        if self.prev_ema200 <= 0:
            return 0.0
        return (self.ema200 - self.prev_ema200) / self.prev_ema200


def calculate_indicators(candles: list[Candle]) -> IndicatorSnapshot:
    if len(candles) < 80:
        raise RuntimeError("برای محاسبه اندیکاتورها حداقل 80 کندل لازم است.")
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    opens = [c.open for c in candles]
    volumes = [c.volume for c in candles]

    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    rsi = _rsi(closes, 14)
    macd_line, macd_signal, macd_hist = _macd(closes, 12, 26, 9)
    adx, plus_di, minus_di = _adx_dmi(highs, lows, closes, 14)
    atr = _atr(highs, lows, closes, 14)
    vwap = _rolling_vwap(candles, 40)

    last = _last_complete_index(ema20, ema50, rsi, macd_line, macd_signal, macd_hist, adx, plus_di, minus_di, atr, vwap)
    prev = _previous_complete_index(last, ema20, ema50, rsi, macd_hist, adx, atr)

    # If EMA200 is not ready, use EMA50 as a conservative fallback so smaller datasets still work.
    ema200_now = ema200[last] if ema200[last] is not None else ema50[last]
    ema200_prev = ema200[prev] if ema200[prev] is not None else ema50[prev]
    if ema200_now is None or ema200_prev is None:
        raise RuntimeError("EMA200/EMA50 هنوز آماده نیست.")

    window = candles[max(0, last - 40 + 1): last + 1]
    recent_high = max(c.high for c in window)
    recent_low = min(c.low for c in window)
    candle_range = max(0.0, highs[last] - lows[last])
    body = abs(closes[last] - opens[last])
    body_pct = body / candle_range if candle_range > 0 else 0.0
    vol_window = [v for v in volumes[max(0, last - 20): last] if v > 0]
    avg_vol = sum(vol_window) / len(vol_window) if vol_window else 0.0
    volume_ratio = (volumes[last] / avg_vol) if avg_vol > 0 and volumes[last] > 0 else 1.0

    return IndicatorSnapshot(
        close=float(closes[last]),
        prev_close=float(closes[prev]),
        high=float(highs[last]),
        low=float(lows[last]),
        open=float(opens[last]),
        volume=float(volumes[last]),
        volume_ratio=float(volume_ratio),
        ema20=float(ema20[last]),
        ema50=float(ema50[last]),
        ema200=float(ema200_now),
        prev_ema20=float(ema20[prev]),
        prev_ema50=float(ema50[prev]),
        prev_ema200=float(ema200_prev),
        rsi=float(rsi[last]),
        prev_rsi=float(rsi[prev]),
        macd=float(macd_line[last]),
        macd_signal=float(macd_signal[last]),
        macd_hist=float(macd_hist[last]),
        prev_macd_hist=float(macd_hist[prev]),
        adx=float(adx[last]),
        plus_di=float(plus_di[last]),
        minus_di=float(minus_di[last]),
        atr=float(atr[last]),
        prev_atr=float(atr[prev]),
        vwap=float(vwap[last]),
        recent_high=float(recent_high),
        recent_low=float(recent_low),
        candle_range=float(candle_range),
        body_pct=float(body_pct),
        consecutive_up=_consecutive(candles[: last + 1], up=True),
        consecutive_down=_consecutive(candles[: last + 1], up=False),
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
    valid_pairs = [(idx, value) for idx, value in enumerate(values) if value is not None]
    if len(valid_pairs) < period:
        return result
    compact = [float(value) for _, value in valid_pairs]
    compact_ema = _ema(compact, period)
    for compact_index, ema_value in enumerate(compact_ema):
        if ema_value is not None:
            original_index = valid_pairs[compact_index][0]
            result[original_index] = ema_value
    return result


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
        avg_gain = ((avg_gain * (period - 1)) + max(change, 0.0)) / period
        avg_loss = ((avg_loss * (period - 1)) + max(-change, 0.0)) / period
        result[index] = _rsi_value(avg_gain, avg_loss)
    return result


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _macd(values: list[float], fast: int, slow: int, signal: int) -> tuple[list[float | None], list[float | None], list[float | None]]:
    fast_ema = _ema(values, fast)
    slow_ema = _ema(values, slow)
    macd_line: list[float | None] = [None] * len(values)
    for i, slow_value in enumerate(slow_ema):
        if fast_ema[i] is not None and slow_value is not None:
            macd_line[i] = float(fast_ema[i]) - float(slow_value)
    signal_line = _ema_from_optional(macd_line, signal)
    hist: list[float | None] = [None] * len(values)
    for i, value in enumerate(macd_line):
        if value is not None and signal_line[i] is not None:
            hist[i] = value - float(signal_line[i])
    return macd_line, signal_line, hist


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return result
    trs: list[float] = [0.0] * len(closes)
    for i in range(1, len(closes)):
        trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    first = sum(trs[1: period + 1]) / period
    result[period] = first
    previous = first
    for i in range(period + 1, len(closes)):
        previous = ((previous * (period - 1)) + trs[i]) / period
        result[i] = previous
    return result


def _adx_dmi(highs: list[float], lows: list[float], closes: list[float], period: int) -> tuple[list[float | None], list[float | None], list[float | None]]:
    length = len(closes)
    adx: list[float | None] = [None] * length
    plus_di_out: list[float | None] = [None] * length
    minus_di_out: list[float | None] = [None] * length
    if length <= period * 2:
        return adx, plus_di_out, minus_di_out
    tr = [0.0] * length
    plus_dm = [0.0] * length
    minus_dm = [0.0] * length
    for i in range(1, length):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[i] = down_move if down_move > up_move and down_move > 0 else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

    tr_smooth = sum(tr[1: period + 1])
    plus_smooth = sum(plus_dm[1: period + 1])
    minus_smooth = sum(minus_dm[1: period + 1])
    dx: list[float | None] = [None] * length
    for i in range(period, length):
        if i > period:
            tr_smooth = tr_smooth - (tr_smooth / period) + tr[i]
            plus_smooth = plus_smooth - (plus_smooth / period) + plus_dm[i]
            minus_smooth = minus_smooth - (minus_smooth / period) + minus_dm[i]
        if tr_smooth <= 0:
            continue
        plus_di = 100.0 * plus_smooth / tr_smooth
        minus_di = 100.0 * minus_smooth / tr_smooth
        plus_di_out[i] = plus_di
        minus_di_out[i] = minus_di
        denom = plus_di + minus_di
        if denom > 0:
            dx[i] = 100.0 * abs(plus_di - minus_di) / denom

    first_adx_index = period * 2
    first_dx_values = [float(x) for x in dx[period:first_adx_index] if x is not None]
    if len(first_dx_values) < period:
        return adx, plus_di_out, minus_di_out
    value = sum(first_dx_values[-period:]) / period
    adx[first_adx_index - 1] = value
    for i in range(first_adx_index, length):
        if dx[i] is None:
            continue
        value = ((value * (period - 1)) + float(dx[i])) / period
        adx[i] = value
    return adx, plus_di_out, minus_di_out


def _rolling_vwap(candles: list[Candle], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(candles)
    for i in range(len(candles)):
        start = max(0, i - period + 1)
        window = candles[start: i + 1]
        denom = sum(max(c.volume, 0.0) for c in window)
        if denom <= 0:
            result[i] = sum((c.high + c.low + c.close) / 3.0 for c in window) / len(window)
        else:
            result[i] = sum(((c.high + c.low + c.close) / 3.0) * max(c.volume, 0.0) for c in window) / denom
    return result


def _consecutive(candles: list[Candle], *, up: bool) -> int:
    count = 0
    for candle in reversed(candles[-8:]):
        is_up = candle.close > candle.open
        is_down = candle.close < candle.open
        if (up and is_up) or ((not up) and is_down):
            count += 1
        else:
            break
    return count
