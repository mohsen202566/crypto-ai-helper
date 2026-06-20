from __future__ import annotations

"""
Market raw-feature extraction.

This module is a SENSOR, not a signal engine.
It must not decide REAL/GHOST/REJECT.
It only calculates technical features that AI Movement Hunter will consume.

Input format:
candles = [
  {"open":..., "high":..., "low":..., "close":..., "volume":..., "timestamp":...},
  ...
]

Output:
A normalized feature snapshot with exact indicator values.
"""

import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from diagnostics import safe


Candle = Dict[str, Any]


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def _ts() -> int:
    return int(time.time())


def _closes(candles: Sequence[Candle]) -> List[float]:
    return [_safe_float(c.get("close")) for c in candles]


def _highs(candles: Sequence[Candle]) -> List[float]:
    return [_safe_float(c.get("high")) for c in candles]


def _lows(candles: Sequence[Candle]) -> List[float]:
    return [_safe_float(c.get("low")) for c in candles]


def _volumes(candles: Sequence[Candle]) -> List[float]:
    return [_safe_float(c.get("volume")) for c in candles]


def sma(values: Sequence[float], period: int) -> float:
    if not values:
        return 0.0
    vals = list(values)[-period:]
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def ema_series(values: Sequence[float], period: int) -> List[float]:
    vals = list(values)
    if not vals:
        return []
    k = 2 / (period + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(values: Sequence[float], period: int) -> float:
    out = ema_series(values, period)
    return out[-1] if out else 0.0


def rsi(values: Sequence[float], period: int = 14) -> float:
    vals = list(values)
    if len(vals) < 2:
        return 50.0
    diffs = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    recent = diffs[-period:]
    if not recent:
        return 50.0
    gains = [max(0.0, d) for d in recent]
    losses = [abs(min(0.0, d)) for d in recent]
    avg_gain = sum(gains) / max(1, len(gains))
    avg_loss = sum(losses) / max(1, len(losses))
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 4)


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, float]:
    vals = list(values)
    if not vals:
        return {"macd": 0.0, "macd_signal": 0.0, "macd_hist": 0.0, "macd_slope": 0.0}
    fast_s = ema_series(vals, fast)
    slow_s = ema_series(vals, slow)
    n = min(len(fast_s), len(slow_s))
    line = [fast_s[-n + i] - slow_s[-n + i] for i in range(n)] if n else [0.0]
    sig_s = ema_series(line, signal)
    m = line[-1]
    sig = sig_s[-1] if sig_s else 0.0
    hist = m - sig
    prev_hist = line[-2] - (sig_s[-2] if len(sig_s) >= 2 else sig) if len(line) >= 2 else hist
    return {
        "macd": round(m, 8),
        "macd_signal": round(sig, 8),
        "macd_hist": round(hist, 8),
        "macd_slope": round(hist - prev_hist, 8),
    }


def atr(candles: Sequence[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high = _safe_float(candles[i].get("high"))
        low = _safe_float(candles[i].get("low"))
        prev_close = _safe_float(candles[i - 1].get("close"))
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    recent = trs[-period:]
    return round(sum(recent) / max(1, len(recent)), 8)


def adx(candles: Sequence[Candle], period: int = 14) -> float:
    if len(candles) < period + 2:
        return 0.0
    plus_dm = []
    minus_dm = []
    tr = []
    for i in range(1, len(candles)):
        h = _safe_float(candles[i].get("high"))
        l = _safe_float(candles[i].get("low"))
        ph = _safe_float(candles[i-1].get("high"))
        pl = _safe_float(candles[i-1].get("low"))
        pc = _safe_float(candles[i-1].get("close"))
        up = h - ph
        down = pl - l
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(h-l, abs(h-pc), abs(l-pc)))
    recent_tr = tr[-period:]
    tr_sum = sum(recent_tr)
    if tr_sum <= 0:
        return 0.0
    plus_di = 100 * sum(plus_dm[-period:]) / tr_sum
    minus_di = 100 * sum(minus_dm[-period:]) / tr_sum
    denom = plus_di + minus_di
    if denom <= 0:
        return 0.0
    dx = 100 * abs(plus_di - minus_di) / denom
    # lightweight ADX approximation; enough as raw sensor.
    return round(dx, 4)


def vwap(candles: Sequence[Candle], period: int = 50) -> float:
    recent = list(candles)[-period:]
    pv = 0.0
    vol = 0.0
    for c in recent:
        typical = (_safe_float(c.get("high")) + _safe_float(c.get("low")) + _safe_float(c.get("close"))) / 3
        v = _safe_float(c.get("volume"))
        pv += typical * v
        vol += v
    return round(pv / vol, 8) if vol > 0 else 0.0


def buy_sell_power(candles: Sequence[Candle], n: int = 3) -> Dict[str, float]:
    recent = list(candles)[-n:]
    buy = 0.0
    sell = 0.0
    for c in recent:
        o = _safe_float(c.get("open"))
        close = _safe_float(c.get("close"))
        high = _safe_float(c.get("high"))
        low = _safe_float(c.get("low"))
        vol = max(0.0, _safe_float(c.get("volume")))
        rng = max(high - low, 1e-12)
        body = close - o
        if body >= 0:
            buy += vol * abs(body) / rng
            sell += vol * max(0.0, (high - close) + (o - low)) / rng * 0.5
        else:
            sell += vol * abs(body) / rng
            buy += vol * max(0.0, (high - o) + (close - low)) / rng * 0.5
    total = buy + sell
    power = (buy - sell) / total * 100 if total > 0 else 0.0
    return {"buy_power": round(buy, 6), "sell_power": round(sell, 6), f"power_{n}": round(power, 6)}


def candle_quality(candles: Sequence[Candle]) -> float:
    if not candles:
        return 0.0
    c = candles[-1]
    o = _safe_float(c.get("open"))
    h = _safe_float(c.get("high"))
    l = _safe_float(c.get("low"))
    cl = _safe_float(c.get("close"))
    rng = max(h - l, 1e-12)
    body = abs(cl - o)
    wick = rng - body
    q = body / rng
    if wick > body * 2:
        q *= 0.65
    return round(max(0.0, min(1.0, q)), 4)


def fresh_momentum(candles: Sequence[Candle]) -> float:
    closes = _closes(candles)
    if len(closes) < 6:
        return 0.0
    m = macd(closes)
    r_now = rsi(closes)
    r_prev = rsi(closes[:-2]) if len(closes) > 16 else 50.0
    p2 = buy_sell_power(candles, 2).get("power_2", 0.0)
    p3 = buy_sell_power(candles, 3).get("power_3", 0.0)
    slope = _safe_float(m.get("macd_slope"))
    raw = 0.0
    raw += max(-1, min(1, slope * 5000)) * 0.30
    raw += max(-1, min(1, (r_now - r_prev) / 10)) * 0.25
    raw += max(-1, min(1, p2 / 40)) * 0.25
    raw += max(-1, min(1, p3 / 40)) * 0.20
    return round(max(-1.0, min(1.0, raw)), 4)


def volume_z_score(candles: Sequence[Candle], period: int = 30) -> float:
    vols = _volumes(candles)[-period:]
    if len(vols) < 5:
        return 0.0
    mean = sum(vols[:-1]) / max(1, len(vols)-1)
    var = sum((v - mean) ** 2 for v in vols[:-1]) / max(1, len(vols)-1)
    sd = math.sqrt(var)
    if sd <= 0:
        return 0.0
    return round((vols[-1] - mean) / sd, 4)


def ema_state_from_values(close: float, ema20: float, ema50: float, ema200: float) -> str:
    if close > ema20 > ema50 > ema200:
        return "STRONG_BULLISH"
    if close < ema20 < ema50 < ema200:
        return "STRONG_BEARISH"
    if close > ema20 and ema20 > ema50:
        return "BULLISH"
    if close < ema20 and ema20 < ema50:
        return "BEARISH"
    return "MIXED"


def direction_hint_from_features(features: Dict[str, Any]) -> str:
    score = 0.0
    score += 1 if features.get("ema_state") in {"BULLISH", "STRONG_BULLISH"} else -1 if features.get("ema_state") in {"BEARISH", "STRONG_BEARISH"} else 0
    score += 1 if _safe_float(features.get("macd_hist")) > 0 else -1 if _safe_float(features.get("macd_hist")) < 0 else 0
    score += 1 if _safe_float(features.get("power_3")) > 5 else -1 if _safe_float(features.get("power_3")) < -5 else 0
    score += 0.5 if _safe_float(features.get("fresh_momentum")) > 0.15 else -0.5 if _safe_float(features.get("fresh_momentum")) < -0.15 else 0
    if score >= 1.5:
        return "LONG"
    if score <= -1.5:
        return "SHORT"
    return "NEUTRAL"


@safe(default={})
def extract_features(candles: Sequence[Candle], symbol: str = "", timeframe: str = "") -> Dict[str, Any]:
    if not candles or len(candles) < 5:
        return {
            "ok": False,
            "symbol": str(symbol).upper(),
            "timeframe": timeframe,
            "error": "not_enough_candles",
            "indicators": {},
        }

    closes = _closes(candles)
    close = closes[-1]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    vw = vwap(candles)
    p2 = buy_sell_power(candles, 2)
    p3 = buy_sell_power(candles, 3)
    m = macd(closes)
    a = atr(candles)
    ad = adx(candles)
    r = rsi(closes)
    vdist = ((close - vw) / close * 100) if close and vw else 0.0

    indicators = {
        "close": round(close, 8),
        "rsi": r,
        **m,
        "adx": ad,
        "atr": a,
        "ema_20": round(e20, 8),
        "ema_50": round(e50, 8),
        "ema_200": round(e200, 8),
        "ema_state": ema_state_from_values(close, e20, e50, e200),
        "vwap": vw,
        "vwap_state": "ABOVE" if close > vw else "BELOW" if close < vw else "AT",
        "vwap_distance": round(vdist, 6),
        "volume": _volumes(candles)[-1],
        "volume_z": volume_z_score(candles),
        "buy_power": p3.get("buy_power", 0.0),
        "sell_power": p3.get("sell_power", 0.0),
        "power_2": p2.get("power_2", 0.0),
        "power_3": p3.get("power_3", 0.0),
        "candle_quality": candle_quality(candles),
        "fresh_momentum": fresh_momentum(candles),
    }
    indicators["early_momentum"] = round(
        (max(-1, min(1, indicators["fresh_momentum"])) * 0.45) +
        (max(-1, min(1, indicators["power_2"] / 50)) * 0.30) +
        (max(-1, min(1, indicators["macd_slope"] * 5000)) * 0.25),
        4,
    )
    indicators["direction_hint"] = direction_hint_from_features(indicators)

    return {
        "ok": True,
        "symbol": str(symbol).upper(),
        "timeframe": timeframe,
        "timestamp": int(candles[-1].get("timestamp", _ts())),
        "indicators": indicators,
    }


@safe(default={})
def multi_timeframe_features(candle_map: Dict[str, Sequence[Candle]], symbol: str = "") -> Dict[str, Any]:
    out = {"ok": True, "symbol": str(symbol).upper(), "timeframes": {}, "created_at": _ts()}
    for tf, candles in candle_map.items():
        out["timeframes"][tf] = extract_features(candles, symbol=symbol, timeframe=tf)
    return out
