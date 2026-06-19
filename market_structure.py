# -*- coding: utf-8 -*-
"""
market_structure.py

Lightweight market-structure helper for the crypto futures bot.

Purpose:
- Detect HH/HL, LH/LL, range, compression and possible structure shift.
- Return simple backward-compatible values for old analysis/scanner calls.
- Keep this as a SOFT layer: it only gives bias/score/context and never blocks signals.

Backward-compatible public functions kept:
    find_swings(df, lookback=3)
    detect_market_structure(df)
    structure_score(structure)

New optional helper:
    get_market_structure_profile(df, lookback=3)
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return float(default)
        return v
    except Exception:
        return float(default)


def _has_columns(df: Any, cols: Tuple[str, ...]) -> bool:
    try:
        return df is not None and all(c in df.columns for c in cols) and len(df) > 0
    except Exception:
        return False


def find_swings(df, lookback: int = 3):
    """Return last 5 swing highs and lows as plain float lists.

    This function intentionally preserves the old return shape:
        highs, lows
    """
    if not _has_columns(df, ("high", "low")):
        return [], []

    try:
        lookback = max(1, int(lookback))
        if len(df) < lookback * 2 + 3:
            return [], []
    except Exception:
        lookback = 3

    highs: List[float] = []
    lows: List[float] = []

    for i in range(lookback, len(df) - lookback):
        current_high = _safe_float(df["high"].iloc[i])
        current_low = _safe_float(df["low"].iloc[i])
        if current_high <= 0 or current_low <= 0:
            continue

        is_high = True
        is_low = True
        for j in range(1, lookback + 1):
            if current_high <= _safe_float(df["high"].iloc[i - j]) or current_high <= _safe_float(df["high"].iloc[i + j]):
                is_high = False
            if current_low >= _safe_float(df["low"].iloc[i - j]) or current_low >= _safe_float(df["low"].iloc[i + j]):
                is_low = False
            if not is_high and not is_low:
                break

        if is_high:
            highs.append(current_high)
        if is_low:
            lows.append(current_low)

    return highs[-5:], lows[-5:]


def _recent_close(df) -> float:
    if not _has_columns(df, ("close",)):
        return 0.0
    return _safe_float(df["close"].iloc[-1])


def _range_width_pct(highs: List[float], lows: List[float], close: float) -> float:
    if not highs or not lows or close <= 0:
        return 0.0
    hi = max(highs[-3:])
    lo = min(lows[-3:])
    if hi <= 0 or lo <= 0 or hi <= lo:
        return 0.0
    return round((hi - lo) / close * 100.0, 4)


def _breakout_bias(df, highs: List[float], lows: List[float]) -> str:
    """Detect if recent close is breaking the latest local structure.

    This does not replace signal direction; it only gives context.
    """
    close = _recent_close(df)
    if close <= 0 or not highs or not lows:
        return "NONE"
    recent_resistance = max(highs[-3:])
    recent_support = min(lows[-3:])
    if recent_resistance > 0 and close > recent_resistance:
        return "BULLISH_BREAKOUT"
    if recent_support > 0 and close < recent_support:
        return "BEARISH_BREAKDOWN"
    return "NONE"


def _structure_from_swings(highs: List[float], lows: List[float]) -> str:
    if len(highs) < 2 or len(lows) < 2:
        return "unknown"

    higher_high = highs[-1] > highs[-2]
    higher_low = lows[-1] > lows[-2]
    lower_high = highs[-1] < highs[-2]
    lower_low = lows[-1] < lows[-2]

    if higher_high and higher_low:
        return "bullish_structure"
    if lower_high and lower_low:
        return "bearish_structure"
    if higher_high and lower_low:
        return "expansion_structure"
    if lower_high and higher_low:
        return "compression_structure"
    return "range_structure"


def detect_market_structure(df):
    """Backward-compatible simple structure label.

    Old callers expect one of:
        bullish_structure / bearish_structure / range_structure / unknown
    We keep those names and only add safe extra labels where useful.
    """
    highs, lows = find_swings(df)
    structure = _structure_from_swings(highs, lows)

    # For old code, avoid making compression/expansion act like a hard direction.
    if structure in {"compression_structure", "expansion_structure"}:
        return "range_structure"
    return structure


def get_market_structure_profile(df, lookback: int = 3) -> Dict[str, Any]:
    """Return a richer soft market-structure profile for AI/scanner use.

    The profile is intentionally conservative:
    - Strong directional score only when both highs/lows agree.
    - Compression/range are returned as caution/context, not rejection.
    - Breakout/breakdown is soft context and can be learned by AI layers.
    """
    highs, lows = find_swings(df, lookback=lookback)
    raw_structure = _structure_from_swings(highs, lows)
    close = _recent_close(df)
    width_pct = _range_width_pct(highs, lows, close)
    breakout = _breakout_bias(df, highs, lows)

    bullish_score, bearish_score = structure_score(raw_structure)

    # Compression often precedes a move; do not force direction, just mark readiness.
    compression = raw_structure == "compression_structure"
    expansion = raw_structure == "expansion_structure"

    if breakout == "BULLISH_BREAKOUT":
        bullish_score += 4
    elif breakout == "BEARISH_BREAKDOWN":
        bearish_score += 4

    if raw_structure == "range_structure":
        state = "RANGE"
    elif compression:
        state = "COMPRESSION"
    elif expansion:
        state = "EXPANSION"
    elif raw_structure == "bullish_structure":
        state = "BULLISH"
    elif raw_structure == "bearish_structure":
        state = "BEARISH"
    else:
        state = "UNKNOWN"

    if bullish_score > bearish_score:
        bias = "BULLISH"
    elif bearish_score > bullish_score:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    return {
        "available": bool(highs and lows),
        "structure": detect_market_structure(df),
        "raw_structure": raw_structure,
        "state": state,
        "bias": bias,
        "bullish_score": int(max(0, min(20, bullish_score))),
        "bearish_score": int(max(0, min(20, bearish_score))),
        "recent_swing_highs": highs,
        "recent_swing_lows": lows,
        "range_width_pct": width_pct,
        "breakout_bias": breakout,
        "compression": compression,
        "expansion": expansion,
        "soft_layer": True,
        "source": "market_structure_v2",
    }


def structure_score(structure):
    """Backward-compatible score tuple: (long_score, short_score).

    Keep this moderate. Market structure is a context layer, not a hard signal.
    """
    structure = str(structure or "").lower().strip()
    if structure == "bullish_structure":
        return 12, 0
    if structure == "bearish_structure":
        return 0, 12
    if structure == "compression_structure":
        return 3, 3
    if structure == "expansion_structure":
        return 2, 2
    return 0, 0
