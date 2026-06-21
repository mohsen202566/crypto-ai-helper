from __future__ import annotations

"""
Multi-timeframe trend context.

This module is a sensor/context module.
It does not create signals. It tells AI how timeframes align.

Hierarchy for user's scalp architecture:
5M = primary entry trigger
15M = entry readiness
30M = setup quality
1H = direction confirmation
4H = macro context
"""

import math
import time
from typing import Any, Dict, List, Optional

from diagnostics import safe


TF_WEIGHTS = {
    "5m": 0.34,
    "15m": 0.26,
    "30m": 0.18,
    "1h": 0.14,
    "4h": 0.08,
    "5M": 0.34,
    "15M": 0.26,
    "30M": 0.18,
    "1H": 0.14,
    "4H": 0.08,
}


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def _tf_score(tf_snapshot: Dict[str, Any]) -> float:
    ind = tf_snapshot.get("indicators", tf_snapshot or {})
    score = 0.0
    ema_state = str(ind.get("ema_state", "MIXED"))
    if ema_state == "STRONG_BULLISH":
        score += 1.0
    elif ema_state == "BULLISH":
        score += 0.55
    elif ema_state == "STRONG_BEARISH":
        score -= 1.0
    elif ema_state == "BEARISH":
        score -= 0.55

    macd_hist = _safe_float(ind.get("macd_hist"))
    score += 0.35 if macd_hist > 0 else -0.35 if macd_hist < 0 else 0.0

    rsi = _safe_float(ind.get("rsi"), 50)
    if rsi > 55:
        score += min(0.35, (rsi - 55) / 45)
    elif rsi < 45:
        score -= min(0.35, (45 - rsi) / 45)

    power = _safe_float(ind.get("power_3"))
    score += max(-0.35, min(0.35, power / 100))

    fresh = _safe_float(ind.get("fresh_momentum"))
    score += max(-0.35, min(0.35, fresh * 0.35))

    return round(max(-1.0, min(1.0, score)), 4)


@safe(default={})
def analyze_trend(mtf_features: Dict[str, Any]) -> Dict[str, Any]:
    tfs = mtf_features.get("timeframes", mtf_features or {})
    tf_scores = {}
    weighted = 0.0
    total_w = 0.0
    for tf, snap in tfs.items():
        if not isinstance(snap, dict) or not snap.get("ok", True):
            continue
        s = _tf_score(snap)
        w = TF_WEIGHTS.get(tf, TF_WEIGHTS.get(str(tf).lower(), 0.1))
        tf_scores[tf] = {"score": s, "weight": w, "direction": "LONG" if s > 0.2 else "SHORT" if s < -0.2 else "NEUTRAL"}
        weighted += s * w
        total_w += w
    final = weighted / total_w if total_w > 0 else 0.0

    direction = "LONG" if final > 0.18 else "SHORT" if final < -0.18 else "NEUTRAL"
    alignment = abs(final)
    conflict = _conflict_score(tf_scores)

    return {
        "ok": True,
        "trend_score": round(final, 4),
        "direction": direction,
        "alignment": round(alignment, 4),
        "conflict": conflict,
        "timeframes": tf_scores,
        "scalp_hierarchy": "5M>15M>30M>1H>4H",
        "created_at": int(time.time()),
    }


def _conflict_score(tf_scores: Dict[str, Dict[str, Any]]) -> float:
    longs = sum(1 for x in tf_scores.values() if x.get("direction") == "LONG")
    shorts = sum(1 for x in tf_scores.values() if x.get("direction") == "SHORT")
    total = max(1, longs + shorts)
    return round(min(longs, shorts) / total, 4)


@safe(default={})
def entry_readiness(mtf_features: Dict[str, Any], direction: str) -> Dict[str, Any]:
    tfs = mtf_features.get("timeframes", mtf_features or {})
    direction = str(direction).upper()
    score = 0.0
    notes = []
    risks = []
    for tf, weight in [("5m", 0.55), ("5M", 0.55), ("15m", 0.35), ("15M", 0.35), ("30m", 0.10), ("30M", 0.10)]:
        snap = tfs.get(tf)
        if not snap:
            continue
        s = _tf_score(snap)
        aligned = s > 0.15 if direction == "LONG" else s < -0.15
        if aligned:
            score += abs(s) * weight
            notes.append(f"{tf}_aligned")
        else:
            score -= abs(s) * weight * 0.55
            risks.append(f"{tf}_not_aligned")
    return {"direction": direction, "entry_readiness": round(max(-1, min(1, score)), 4), "notes": notes, "risks": risks}
