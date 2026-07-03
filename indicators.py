from __future__ import annotations

from dataclasses import dataclass

from okx_data import Candle


@dataclass(frozen=True)
class IndicatorSnapshot:
    close: float
    ema20: float
    ema50: float
    ema200: float
    vwap: float
    rsi14: float
    adx14: float
    di_plus: float
    di_minus: float
    atr14: float
    atr_pct: float
    volume_ratio: float
    swing_high: float
    swing_low: float
    dist_vwap_pct: float
    dist_ema20_pct: float
    dist_ema50_pct: float
    dist_ema200_pct: float


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2 / (period + 1)
    out = values[0]
    for value in values[1:]:
        out = value * k + out * (1 - k)
    return out


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) <= period:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        c = candles[i]
        prev = candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    return sum(trs[-period:]) / period if trs else 0.0


def adx_di(candles: list[Candle], period: int = 14) -> tuple[float, float, float]:
    if len(candles) <= period + 2:
        return 0.0, 0.0, 0.0
    trs: list[float] = []
    pdm: list[float] = []
    ndm: list[float] = []
    for i in range(1, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]
        up = cur.high - prev.high
        down = prev.low - cur.low
        pdm.append(up if up > down and up > 0 else 0.0)
        ndm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    tr = sum(trs[-period:])
    if tr <= 0:
        return 0.0, 0.0, 0.0
    di_plus = 100 * sum(pdm[-period:]) / tr
    di_minus = 100 * sum(ndm[-period:]) / tr
    dx_values: list[float] = []
    start = max(period, len(trs) - period)
    for end in range(start, len(trs) + 1):
        tr_window = sum(trs[max(0, end - period):end])
        if tr_window <= 0:
            continue
        p = 100 * sum(pdm[max(0, end - period):end]) / tr_window
        n = 100 * sum(ndm[max(0, end - period):end]) / tr_window
        den = p + n
        dx_values.append(100 * abs(p - n) / den if den else 0.0)
    adx = sum(dx_values[-period:]) / min(period, len(dx_values)) if dx_values else 0.0
    return adx, di_plus, di_minus


def build_snapshot(candles: list[Candle]) -> IndicatorSnapshot:
    if len(candles) < 205:
        raise RuntimeError(f"کندل کافی برای اندیکاتور نیست: {len(candles)}")
    closes = [c.close for c in candles]
    close = closes[-1]
    ema20 = ema(closes[-80:], 20)
    ema50 = ema(closes[-120:], 50)
    ema200 = ema(closes[-205:], 200)
    recent = candles[-80:]
    vol_sum = sum(c.volume for c in recent)
    vwap = sum(((c.high + c.low + c.close) / 3) * c.volume for c in recent) / vol_sum if vol_sum > 0 else close
    rsi14 = rsi(closes[-80:], 14)
    atr14 = atr(candles[-80:], 14)
    adx14, di_plus, di_minus = adx_di(candles[-80:], 14)
    vols = [c.volume for c in candles[-25:]]
    avg_vol = sum(vols[:-1]) / max(len(vols) - 1, 1)
    volume_ratio = candles[-1].volume / avg_vol if avg_vol > 0 else 1.0
    swings = candles[-30:]
    swing_high = max(c.high for c in swings)
    swing_low = min(c.low for c in swings)
    return IndicatorSnapshot(
        close=close,
        ema20=ema20,
        ema50=ema50,
        ema200=ema200,
        vwap=vwap,
        rsi14=rsi14,
        adx14=adx14,
        di_plus=di_plus,
        di_minus=di_minus,
        atr14=atr14,
        atr_pct=atr14 / close if close > 0 else 0.0,
        volume_ratio=volume_ratio,
        swing_high=swing_high,
        swing_low=swing_low,
        dist_vwap_pct=(close - vwap) / close if close else 0.0,
        dist_ema20_pct=(close - ema20) / close if close else 0.0,
        dist_ema50_pct=(close - ema50) / close if close else 0.0,
        dist_ema200_pct=(close - ema200) / close if close else 0.0,
    )
