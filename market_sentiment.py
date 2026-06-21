from __future__ import annotations

"""
Market context / sentiment sensor.

Provides:
- BTC context
- market breadth
- leader/laggard context
- Fear & Greed / Altseason / BTC dominance placeholders from supplied data
- market mode

No web/API calls are forced here. External fetchers can inject context.
"""

import math
import time
from typing import Any, Dict, List, Optional

from diagnostics import safe


def _ts() -> int:
    return int(time.time())


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


def _direction_from_score(score: float) -> str:
    if score > 0.2:
        return "BULLISH"
    if score < -0.2:
        return "BEARISH"
    return "NEUTRAL"


@safe(default={})
def btc_context(btc_features: Dict[str, Any]) -> Dict[str, Any]:
    ind = btc_features.get("indicators", btc_features or {})
    score = 0.0
    ema_state = str(ind.get("ema_state", "MIXED"))
    if "BULLISH" in ema_state:
        score += 0.35
    if "BEARISH" in ema_state:
        score -= 0.35
    score += 0.25 if _safe_float(ind.get("macd_hist")) > 0 else -0.25 if _safe_float(ind.get("macd_hist")) < 0 else 0
    score += max(-0.25, min(0.25, _safe_float(ind.get("power_3")) / 100))
    score += max(-0.20, min(0.20, _safe_float(ind.get("fresh_momentum")) * 0.25))
    score = max(-1.0, min(1.0, score))
    return {
        "btc_score": round(score, 4),
        "btc_bias": "STRONG_BULLISH" if score > 0.55 else "BULLISH" if score > 0.2 else "STRONG_BEARISH" if score < -0.55 else "BEARISH" if score < -0.2 else "NEUTRAL",
        "btc_trend": _direction_from_score(score),
    }


@safe(default={})
def market_breadth(feature_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    bullish = bearish = neutral = 0
    total = 0
    for symbol, snap in feature_map.items():
        if str(symbol).upper() == "BTCUSDT":
            continue
        ind = snap.get("indicators", snap or {})
        hint = str(ind.get("direction_hint", "NEUTRAL")).upper()
        total += 1
        if hint == "LONG":
            bullish += 1
        elif hint == "SHORT":
            bearish += 1
        else:
            neutral += 1
    total = max(1, total)
    bull_pct = bullish / total * 100
    bear_pct = bearish / total * 100
    return {
        "total": total,
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "bullish_pct": round(bull_pct, 2),
        "bearish_pct": round(bear_pct, 2),
        "neutral_pct": round(neutral / total * 100, 2),
        "breadth_bias": "BULLISH" if bull_pct - bear_pct > 15 else "BEARISH" if bear_pct - bull_pct > 15 else "MIXED",
    }


@safe(default={})
def leader_influence(symbol: str, feature_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Lightweight leader context. BTC/ETH/SOL are treated as leaders.
    Later this can be replaced by learned leader-laggard module.
    """
    leaders = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    scores = []
    leader_symbol = ""
    for leader in leaders:
        snap = feature_map.get(leader, {})
        ind = snap.get("indicators", snap or {})
        hint = str(ind.get("direction_hint", "NEUTRAL")).upper()
        power = _safe_float(ind.get("fresh_momentum")) + _safe_float(ind.get("power_3")) / 100
        val = 0.0
        if hint == "LONG":
            val = abs(power) if power != 0 else 0.2
        elif hint == "SHORT":
            val = -abs(power) if power != 0 else -0.2
        scores.append(val)
        if abs(val) == max(abs(x) for x in scores):
            leader_symbol = leader
    avg = sum(scores) / max(1, len(scores))
    return {
        "leader_symbol": leader_symbol,
        "leader_influence": round(max(-1.0, min(1.0, avg)), 4),
        "leader_bias": "BULLISH" if avg > 0.15 else "BEARISH" if avg < -0.15 else "NEUTRAL",
    }


@safe(default={})
def build_market_context(
    feature_map: Dict[str, Dict[str, Any]],
    external_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    external_context = external_context or {}
    btc = btc_context(feature_map.get("BTCUSDT", {}))
    breadth = market_breadth(feature_map)

    # Fear/Greed and Altseason are injected by future fetchers. Defaults neutral.
    fear_greed = _safe_float(external_context.get("fear_greed", 50), 50)
    altseason = _safe_float(external_context.get("altseason", 0), 0)
    btc_dominance = _safe_float(external_context.get("btc_dominance", 0), 0)

    mode_score = 0.0
    mode_score += btc.get("btc_score", 0) * 0.35
    mode_score += (breadth.get("bullish_pct", 0) - breadth.get("bearish_pct", 0)) / 100 * 0.35
    mode_score += (fear_greed - 50) / 100 * 0.15
    mode_score += max(-0.15, min(0.15, altseason / 100 * 0.15))

    market_mode = "BULLISH" if mode_score > 0.18 else "BEARISH" if mode_score < -0.18 else "RANGE"

    return {
        "ok": True,
        "market_mode": market_mode,
        "market_score": round(mode_score, 4),
        **btc,
        "market_breadth_bullish": breadth.get("bullish_pct", 0),
        "market_breadth_bearish": breadth.get("bearish_pct", 0),
        "market_breadth_neutral": breadth.get("neutral_pct", 0),
        "breadth": breadth,
        "fear_greed": round(fear_greed, 2),
        "altseason": round(altseason, 4),
        "btc_dominance": round(btc_dominance, 4),
        "created_at": _ts(),
    }


@safe(default={})
def context_for_symbol(symbol: str, feature_map: Dict[str, Dict[str, Any]], external_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ctx = build_market_context(feature_map, external_context)
    leader = leader_influence(symbol, feature_map)
    ctx.update(leader)
    ctx["symbol"] = str(symbol).upper()
    return ctx
