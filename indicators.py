from __future__ import annotations

from dataclasses import dataclass

from config import MIN_ENTRY_CANDLES
from okx_data import Candle


@dataclass(frozen=True)
class IndicatorSnapshot:
    close: float
    prev_close: float
    open: float
    high: float
    low: float
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
    adx: float
    plus_di: float
    minus_di: float
    atr: float
    prev_atr: float
    vwap: float
    recent_high: float
    recent_low: float
    swing_high: float
    swing_low: float
    body_pct: float
    upper_wick_pct: float
    lower_wick_pct: float
    # Stop-forensic-only indicators. These are stored for SL investigation;
    # the normal signal decision/feature key does not use them.
    atr_percentile: float = 50.0
    choppiness: float = 50.0
    bb_width_pct: float = 0.0
    keltner_squeeze_ratio: float = 1.0
    donchian_position_pct: float = 50.0
    donchian_breakout: str = "NONE"

    @property
    def atr_pct(self) -> float:
        return self.atr / self.close if self.close > 0 else 0.0

    @property
    def rsi_delta(self) -> float:
        return self.rsi - self.prev_rsi

    @property
    def ema20_slope_pct(self) -> float:
        return (self.ema20 - self.prev_ema20) / self.prev_ema20 if self.prev_ema20 > 0 else 0.0

    @property
    def ema50_slope_pct(self) -> float:
        return (self.ema50 - self.prev_ema50) / self.prev_ema50 if self.prev_ema50 > 0 else 0.0

    @property
    def price_vs_vwap_pct(self) -> float:
        return (self.close - self.vwap) / self.close if self.close > 0 else 0.0

    @property
    def price_vs_ema20_pct(self) -> float:
        return (self.close - self.ema20) / self.close if self.close > 0 else 0.0

    @property
    def price_vs_ema50_pct(self) -> float:
        return (self.close - self.ema50) / self.close if self.close > 0 else 0.0

    @property
    def price_vs_ema200_pct(self) -> float:
        return (self.close - self.ema200) / self.close if self.close > 0 else 0.0

    @property
    def ema20_50_gap_pct(self) -> float:
        return (self.ema20 - self.ema50) / self.close if self.close > 0 else 0.0


def calculate_indicators(candles: list[Candle]) -> IndicatorSnapshot:
    if len(candles) < MIN_ENTRY_CANDLES:
        raise RuntimeError(f"برای اندیکاتورهای 5m حداقل {MIN_ENTRY_CANDLES} کندل لازم است؛ دریافت شد: {len(candles)}")
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    opens = [c.open for c in candles]
    volumes = [c.volume for c in candles]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    rsi = _rsi(closes, 14)
    adx, plus_di, minus_di = _adx_dmi(highs, lows, closes, 14)
    atr = _atr(highs, lows, closes, 14)
    vwap = _rolling_vwap(candles, 48)
    last = _last_complete_index(ema20, ema50, ema200, rsi, adx, plus_di, minus_di, atr, vwap)
    prev = _previous_complete_index(last, ema20, ema50, ema200, rsi, atr)
    win80 = candles[max(0, last - 79): last + 1]
    win20 = candles[max(0, last - 19): last + 1]
    candle_range = max(0.0, highs[last] - lows[last])
    body = abs(closes[last] - opens[last])
    top_body = max(closes[last], opens[last])
    bottom_body = min(closes[last], opens[last])
    upper_wick = max(0.0, highs[last] - top_body)
    lower_wick = max(0.0, bottom_body - lows[last])
    vol_window = [v for v in volumes[max(0, last - 20): last] if v > 0]
    avg_vol = sum(vol_window) / len(vol_window) if vol_window else 0.0
    volume_ratio = volumes[last] / avg_vol if avg_vol > 0 and volumes[last] > 0 else 1.0

    # Stop-forensic-only helpers. They are calculated from the same candle batch
    # and are NOT used in RangeLearningEngine.build_features(), so they do not
    # change the original entry logic. They only help the stop investigator explain
    # range/noise, fake breakout, squeeze, and abnormal volatility after a result.
    atr_percentile = _last_percentile([x for x in atr[max(0, last - 119): last + 1] if x is not None and x > 0], atr[last] or 0.0)
    choppiness = _choppiness(highs, lows, closes, last, 14)
    bb_width_pct = _bollinger_width_pct(closes, last, 20)
    keltner_squeeze_ratio = _keltner_squeeze_ratio(closes, atr, last, 20)
    donchian_position_pct, donchian_breakout = _donchian_position(highs, lows, closes, last, 20)

    return IndicatorSnapshot(
        close=float(closes[last]), prev_close=float(closes[prev]), open=float(opens[last]), high=float(highs[last]), low=float(lows[last]), volume=float(volumes[last]), volume_ratio=float(volume_ratio),
        ema20=float(ema20[last]), ema50=float(ema50[last]), ema200=float(ema200[last]), prev_ema20=float(ema20[prev]), prev_ema50=float(ema50[prev]), prev_ema200=float(ema200[prev]),
        rsi=float(rsi[last]), prev_rsi=float(rsi[prev]), adx=float(adx[last]), plus_di=float(plus_di[last]), minus_di=float(minus_di[last]), atr=float(atr[last]), prev_atr=float(atr[prev]), vwap=float(vwap[last]),
        recent_high=float(max(c.high for c in win80)), recent_low=float(min(c.low for c in win80)), swing_high=float(max(c.high for c in win20)), swing_low=float(min(c.low for c in win20)),
        body_pct=body / candle_range if candle_range > 0 else 0.0, upper_wick_pct=upper_wick / candle_range if candle_range > 0 else 0.0, lower_wick_pct=lower_wick / candle_range if candle_range > 0 else 0.0,
        atr_percentile=float(atr_percentile), choppiness=float(choppiness), bb_width_pct=float(bb_width_pct),
        keltner_squeeze_ratio=float(keltner_squeeze_ratio), donchian_position_pct=float(donchian_position_pct),
        donchian_breakout=str(donchian_breakout),
    )


def calculate_htf_snapshot(candles: list[Candle]) -> IndicatorSnapshot:
    if len(candles) < 80:
        candles = _extend_for_htf(candles)
    return calculate_indicators(candles)


def _extend_for_htf(candles: list[Candle]) -> list[Candle]:
    if not candles:
        return candles
    out = list(candles)
    while len(out) < 220:
        out.insert(0, out[0])
    return out


def _last_complete_index(*series: list[float | None]) -> int:
    length = min(len(values) for values in series)
    for index in range(length - 1, -1, -1):
        if all(values[index] is not None for values in series):
            return index
    raise RuntimeError("اندیکاتورها هنوز آماده نیستند.")


def _previous_complete_index(start: int, *series: list[float | None]) -> int:
    for index in range(start - 1, -1, -1):
        if all(values[index] is not None for values in series):
            return index
    raise RuntimeError("مقدار قبلی اندیکاتورها آماده نیست.")



def _last_percentile(values: list[float], current: float) -> float:
    vals = sorted(v for v in values if v is not None and v > 0)
    if not vals or current <= 0:
        return 50.0
    below = sum(1 for v in vals if v <= current)
    return max(0.0, min(100.0, 100.0 * below / len(vals)))


def _std(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5


def _bollinger_width_pct(closes: list[float], last: int, period: int = 20) -> float:
    if last + 1 < period:
        return 0.0
    win = closes[last - period + 1: last + 1]
    mid = sum(win) / len(win)
    if mid <= 0:
        return 0.0
    width = 4.0 * _std(win)  # upper-lower with 2 std on each side
    return width / mid


def _keltner_squeeze_ratio(closes: list[float], atr: list[float | None], last: int, period: int = 20) -> float:
    """Bollinger width / Keltner width. < 1 means squeeze/compression."""
    bb_width = _bollinger_width_pct(closes, last, period)
    atr_val = atr[last] if 0 <= last < len(atr) and atr[last] is not None else 0.0
    mid = sum(closes[max(0, last - period + 1): last + 1]) / max(1, min(period, last + 1))
    if atr_val <= 0 or mid <= 0:
        return 1.0
    keltner_width_pct = (4.0 * atr_val) / mid
    if keltner_width_pct <= 0:
        return 1.0
    return max(0.0, min(5.0, bb_width / keltner_width_pct))


def _donchian_position(highs: list[float], lows: list[float], closes: list[float], last: int, period: int = 20) -> tuple[float, str]:
    if last + 1 < period:
        return 50.0, "NONE"
    # Use the previous channel for breakout detection to avoid counting the current candle in its own channel.
    start_prev = max(0, last - period)
    prev_highs = highs[start_prev:last]
    prev_lows = lows[start_prev:last]
    win_highs = highs[max(0, last - period + 1): last + 1]
    win_lows = lows[max(0, last - period + 1): last + 1]
    hi = max(win_highs) if win_highs else highs[last]
    lo = min(win_lows) if win_lows else lows[last]
    rng = hi - lo
    pos = 50.0 if rng <= 0 else 100.0 * (closes[last] - lo) / rng
    breakout = "NONE"
    if prev_highs and closes[last] > max(prev_highs):
        breakout = "UP"
    elif prev_lows and closes[last] < min(prev_lows):
        breakout = "DOWN"
    return max(0.0, min(100.0, pos)), breakout


def _choppiness(highs: list[float], lows: list[float], closes: list[float], last: int, period: int = 14) -> float:
    """Choppiness Index, 0=trend, 100=chop. Used only for stop forensics."""
    import math
    if last < period:
        return 50.0
    tr_sum = 0.0
    for i in range(last - period + 1, last + 1):
        if i <= 0:
            tr = highs[i] - lows[i]
        else:
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        tr_sum += max(0.0, tr)
    hi = max(highs[last - period + 1: last + 1])
    lo = min(lows[last - period + 1: last + 1])
    denom = hi - lo
    if tr_sum <= 0 or denom <= 0 or period <= 1:
        return 50.0
    return max(0.0, min(100.0, 100.0 * math.log10(tr_sum / denom) / math.log10(period)))


def _ema(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    sma = sum(values[:period]) / period
    result[period - 1] = sma
    mult = 2.0 / (period + 1.0)
    previous = sma
    for i in range(period, len(values)):
        previous = ((values[i] - previous) * mult) + previous
        result[i] = previous
    return result


def _rsi(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains = []
    losses = []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    result[period] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = ((avg_gain * (period - 1)) + max(change, 0.0)) / period
        avg_loss = ((avg_loss * (period - 1)) + max(-change, 0.0)) / period
        result[i] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    return result


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return result
    trs = [0.0] * len(closes)
    for i in range(1, len(closes)):
        trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    previous = sum(trs[1: period + 1]) / period
    result[period] = previous
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
    atr_smooth = sum(tr[1: period + 1])
    plus_smooth = sum(plus_dm[1: period + 1])
    minus_smooth = sum(minus_dm[1: period + 1])
    dx_values: list[float | None] = [None] * length
    for i in range(period + 1, length):
        atr_smooth = atr_smooth - (atr_smooth / period) + tr[i]
        plus_smooth = plus_smooth - (plus_smooth / period) + plus_dm[i]
        minus_smooth = minus_smooth - (minus_smooth / period) + minus_dm[i]
        if atr_smooth <= 0:
            continue
        plus_di = 100.0 * (plus_smooth / atr_smooth)
        minus_di = 100.0 * (minus_smooth / atr_smooth)
        plus_di_out[i] = plus_di
        minus_di_out[i] = minus_di
        denom = plus_di + minus_di
        dx_values[i] = 100.0 * abs(plus_di - minus_di) / denom if denom > 0 else 0.0
    valid = [(i, v) for i, v in enumerate(dx_values) if v is not None]
    if len(valid) < period:
        return adx, plus_di_out, minus_di_out
    values = [float(v) for _, v in valid]
    compact = _ema(values, period)
    for ci, value in enumerate(compact):
        if value is not None:
            adx[valid[ci][0]] = value
    return adx, plus_di_out, minus_di_out


def _rolling_vwap(candles: list[Candle], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1:i + 1]
        denom = sum(max(c.volume, 0.0) for c in window)
        if denom <= 0:
            result[i] = sum(c.close for c in window) / period
        else:
            result[i] = sum(((c.high + c.low + c.close) / 3.0) * max(c.volume, 0.0) for c in window) / denom
    return result
