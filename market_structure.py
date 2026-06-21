from __future__ import annotations

"""
Market structure sensor.

Provides:
- swing high/low
- support/resistance proximity
- range/trend state
- breakout/fake breakout risk
- trap/liquidity risk
- volatility compression/expansion
- movement phase
- reversal probability

No final trading decision is made here.
"""

import math
import time
from typing import Any, Dict, List, Sequence, Optional

from diagnostics import safe


Candle = Dict[str, Any]


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def _highs(candles: Sequence[Candle]) -> List[float]:
    return [_safe_float(c.get("high")) for c in candles]


def _lows(candles: Sequence[Candle]) -> List[float]:
    return [_safe_float(c.get("low")) for c in candles]


def _closes(candles: Sequence[Candle]) -> List[float]:
    return [_safe_float(c.get("close")) for c in candles]


def _volumes(candles: Sequence[Candle]) -> List[float]:
    return [_safe_float(c.get("volume")) for c in candles]


def _atr_like(candles: Sequence[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = _safe_float(candles[i].get("high"))
        l = _safe_float(candles[i].get("low"))
        pc = _safe_float(candles[i-1].get("close"))
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    recent = trs[-period:]
    return sum(recent) / max(1, len(recent))


def swing_high(candles: Sequence[Candle], lookback: int = 20) -> float:
    highs = _highs(candles)[-lookback:]
    return max(highs) if highs else 0.0


def swing_low(candles: Sequence[Candle], lookback: int = 20) -> float:
    lows = _lows(candles)[-lookback:]
    return min(lows) if lows else 0.0


def detect_compression(candles: Sequence[Candle]) -> Dict[str, float]:
    if len(candles) < 30:
        return {"compression": 0.0, "expansion": 0.0}
    atr_short = _atr_like(candles[-10:], 10)
    atr_long = _atr_like(candles[-30:], 30)
    if atr_long <= 0:
        return {"compression": 0.0, "expansion": 0.0}
    ratio = atr_short / atr_long
    compression = max(0.0, min(1.0, 1 - ratio)) if ratio < 1 else 0.0
    expansion = max(0.0, min(1.0, ratio - 1)) if ratio > 1 else 0.0
    return {"compression": round(compression, 4), "expansion": round(expansion, 4)}


def fake_breakout_risk(candles: Sequence[Candle], sh: float, sl: float) -> float:
    if len(candles) < 3:
        return 0.0
    last = candles[-1]
    prev_close = _safe_float(candles[-2].get("close"))
    o = _safe_float(last.get("open"))
    h = _safe_float(last.get("high"))
    l = _safe_float(last.get("low"))
    c = _safe_float(last.get("close"))
    rng = max(h-l, 1e-12)
    risk = 0.0
    # sweep above resistance then close back below
    if h > sh and c < sh:
        risk += 0.55
    # sweep below support then close back above
    if l < sl and c > sl:
        risk += 0.55
    wick_top = (h - max(o, c)) / rng
    wick_bottom = (min(o, c) - l) / rng
    if wick_top > 0.45 or wick_bottom > 0.45:
        risk += 0.25
    if abs(c - o) / rng < 0.25:
        risk += 0.15
    return round(max(0.0, min(1.0, risk)), 4)


def breakout_state(candles: Sequence[Candle], sh: float, sl: float) -> str:
    if not candles:
        return "UNKNOWN"
    c = _safe_float(candles[-1].get("close"))
    h = _safe_float(candles[-1].get("high"))
    l = _safe_float(candles[-1].get("low"))
    atr = _atr_like(candles)
    buffer = atr * 0.15
    if c > sh + buffer:
        return "CLEAN_BREAK_UP"
    if c < sl - buffer:
        return "CLEAN_BREAK_DOWN"
    if h > sh and c < sh:
        return "FAKE_BREAK_UP"
    if l < sl and c > sl:
        return "FAKE_BREAK_DOWN"
    if abs(c - sh) <= buffer or abs(c - sl) <= buffer:
        return "AT_LEVEL"
    return "INSIDE_RANGE"


def liquidity_risk(candles: Sequence[Candle], sh: float, sl: float) -> float:
    if len(candles) < 10:
        return 0.0
    close = _safe_float(candles[-1].get("close"))
    atr = max(_atr_like(candles), 1e-12)
    dist_res = abs(sh - close) / atr
    dist_sup = abs(close - sl) / atr
    near_level = min(dist_res, dist_sup)
    risk = max(0.0, min(1.0, 1 - near_level / 2.5))
    return round(risk, 4)


def movement_phase(candles: Sequence[Candle]) -> str:
    if len(candles) < 15:
        return "UNKNOWN"
    closes = _closes(candles)
    recent_move = closes[-1] - closes[-5]
    larger_move = closes[-1] - closes[-15]
    atr = max(_atr_like(candles), 1e-12)
    r1 = abs(recent_move) / atr
    r2 = abs(larger_move) / atr
    if r1 < 0.35 and r2 < 0.8:
        return "COMPRESSION_OR_RANGE"
    if r1 >= 0.5 and r2 < 1.2:
        return "EARLY_MOVE"
    if r2 >= 1.2 and r1 >= 0.35:
        return "MID_MOVE"
    if r2 >= 1.5 and r1 < 0.25:
        return "EXHAUSTION"
    return "MIXED"


def reversal_probability(candles: Sequence[Candle]) -> float:
    if len(candles) < 10:
        return 0.0
    closes = _closes(candles)
    vols = _volumes(candles)
    atr = max(_atr_like(candles), 1e-12)
    move = abs(closes[-1] - closes[-8]) / atr
    last_body = abs(_safe_float(candles[-1].get("close")) - _safe_float(candles[-1].get("open")))
    last_range = max(_safe_float(candles[-1].get("high")) - _safe_float(candles[-1].get("low")), 1e-12)
    wick_ratio = 1 - min(1.0, last_body / last_range)
    vol_spike = 0.0
    if len(vols) > 8:
        avg_vol = sum(vols[-8:-1]) / 7
        if avg_vol > 0:
            vol_spike = max(0.0, min(1.0, (vols[-1] / avg_vol - 1) / 2))
    prob = 0.0
    if move > 1.5:
        prob += 0.25
    prob += wick_ratio * 0.35
    prob += vol_spike * 0.25
    if movement_phase(candles) == "EXHAUSTION":
        prob += 0.25
    return round(max(0.0, min(1.0, prob)), 4)


@safe(default={})
def analyze_structure(candles: Sequence[Candle], symbol: str = "", timeframe: str = "") -> Dict[str, Any]:
    if not candles or len(candles) < 5:
        return {"ok": False, "symbol": str(symbol).upper(), "timeframe": timeframe, "error": "not_enough_candles"}

    sh = swing_high(candles[:-1] if len(candles) > 1 else candles)
    sl = swing_low(candles[:-1] if len(candles) > 1 else candles)
    close = _safe_float(candles[-1].get("close"))
    atr = max(_atr_like(candles), 1e-12)
    comp = detect_compression(candles)
    fb = fake_breakout_risk(candles, sh, sl)
    liq = liquidity_risk(candles, sh, sl)
    bs = breakout_state(candles, sh, sl)
    phase = movement_phase(candles)

    support_dist = (close - sl) / close * 100 if close else 0.0
    resistance_dist = (sh - close) / close * 100 if close else 0.0

    return {
        "ok": True,
        "symbol": str(symbol).upper(),
        "timeframe": timeframe,
        "structure": {
            "swing_high": round(sh, 8),
            "swing_low": round(sl, 8),
            "support_near": round(sl, 8),
            "resistance_near": round(sh, 8),
            "support_distance_pct": round(support_dist, 6),
            "resistance_distance_pct": round(resistance_dist, 6),
            "sr_distance": round(min(abs(close-sh), abs(close-sl)) / atr, 6),
            "breakout_state": bs,
            "fake_breakout_risk": fb,
            "trap_risk": round(max(fb, liq * 0.75), 4),
            "liquidity_risk": liq,
            "compression": comp["compression"],
            "expansion": comp["expansion"],
            "movement_phase": phase,
            "reversal_probability": reversal_probability(candles),
            "supply_zone_distance": round(resistance_dist, 6),
            "demand_zone_distance": round(support_dist, 6),
        },
        "created_at": int(time.time()),
    }


@safe(default={})
def multi_timeframe_structure(candle_map: Dict[str, Sequence[Candle]], symbol: str = "") -> Dict[str, Any]:
    return {
        "ok": True,
        "symbol": str(symbol).upper(),
        "timeframes": {tf: analyze_structure(c, symbol, tf) for tf, c in candle_map.items()},
        "created_at": int(time.time()),
    }
