"""
market_context.py
Level 4 / 1H Smart Scalp Bot

Light market context engine.

Architecture lock:
- Builds BTC/ETH/market-mode context only.
- No final AI decision, no REAL/GHOST/REJECT, no order execution,
  no position monitoring, no JSON state writes, no Telegram text.
- Allowed project imports: constants.py, utils.py, models.py, market_data.py, technical_sensors.py only.

Core rule:
- Hunt the start of a pump/dump movement, not the middle/end of it.
- Context must not wait only for old confirmed EMA/MACD trend.
- Fresh context evidence from BTC/ETH should support early entries:
  RSI slope turn, MACD histogram acceleration, power shift, volume/ATR expansion,
  EMA/VWAP reclaim/loss, and healthy participation.
- Late/choppy/overheated/weak-participation context must raise market risk.
"""

from __future__ import annotations

from typing import Any, Optional

from constants import DIRECTION_LONG, DIRECTION_SHORT, STATUS_OK, SYSTEM_VERSION
from market_data import fetch_context_snapshots
from models import MarketContextSnapshot, MarketDataResult, MarketSnapshot, SensorSnapshot
from technical_sensors import build_sensor_snapshot
from utils import clamp, normalize_direction, safe_float, safe_str


MARKET_CONTEXT_VERSION: str = SYSTEM_VERSION


# =============================================================================
# Safe helpers
# =============================================================================

def _num(value: Any, default: float = 0.0) -> float:
    """Return safe float while preserving valid 0.0 values."""
    parsed = safe_float(value, None)
    if parsed is None:
        return float(default)
    return float(parsed)


def _directional(value: Any, direction: str, default: float = 0.0) -> float:
    """Normalize a signed value so positive means aligned with direction."""
    d = normalize_direction(direction)
    v = _num(value, default)
    if d == DIRECTION_LONG:
        return v
    if d == DIRECTION_SHORT:
        return -v
    return 0.0


def _directional_power_gap(sensor: SensorSnapshot, direction: str) -> float:
    """Return buy/sell power gap in requested direction."""
    d = normalize_direction(direction)
    buy = _num(getattr(sensor, "buy_power", None), 50.0)
    sell = _num(getattr(sensor, "sell_power", None), 50.0)
    if d == DIRECTION_LONG:
        return buy - sell
    if d == DIRECTION_SHORT:
        return sell - buy
    return 0.0


# =============================================================================
# Fresh context / asset bias
# =============================================================================

def score_asset_fresh_context(sensor: SensorSnapshot, direction: str) -> tuple[float, bool, list[str]]:
    """
    Score early context support for a direction.

    This is intentionally start-aware: it rewards acceleration and pressure even
    before the full classic trend is confirmed.
    """
    d = normalize_direction(direction)
    score = 40.0
    reasons: list[str] = []

    price = _num(getattr(sensor, "price", None), 0.0)
    ema20 = safe_float(getattr(sensor, "ema20", None), None)
    vwap = safe_float(getattr(sensor, "vwap", None), None)
    rsi = safe_float(getattr(sensor, "rsi", None), None)
    rsi_slope = _directional(getattr(sensor, "rsi_slope", None), d, 0.0)
    macd_hist = _directional(getattr(sensor, "macd_hist", None), d, 0.0)
    macd_slope = _directional(getattr(sensor, "macd_hist_slope", None), d, 0.0)
    power_gap = _directional_power_gap(sensor, d)
    volume = safe_float(getattr(sensor, "volume_ratio", None), None)
    atr_pct = safe_float(getattr(sensor, "atr_pct", None), None)
    adx = safe_float(getattr(sensor, "adx", None), None)

    if rsi_slope >= 0.18:
        score += 14.0
        reasons.append("CTX_FRESH_RSI_SLOPE_STRONG")
    elif rsi_slope >= 0.05:
        score += 8.0
        reasons.append("CTX_FRESH_RSI_SLOPE_OK")
    else:
        score -= 8.0
        reasons.append("CTX_FRESH_RSI_SLOPE_WEAK")

    if macd_slope > 0:
        score += 16.0
        reasons.append("CTX_FRESH_MACD_ACCEL")
        if macd_hist <= 0:
            score += 4.0
            reasons.append("CTX_EARLY_BEFORE_MACD_CROSS")
    else:
        score -= 12.0
        reasons.append("CTX_MACD_ACCEL_WEAK")

    if power_gap >= 12:
        score += 12.0
        reasons.append("CTX_POWER_SHIFT_STRONG")
    elif power_gap >= 4:
        score += 7.0
        reasons.append("CTX_POWER_SHIFT_OK")
    elif power_gap <= -8:
        score -= 12.0
        reasons.append("CTX_POWER_AGAINST")
    else:
        score -= 3.0
        reasons.append("CTX_POWER_NEUTRAL")

    if volume is not None:
        if 1.10 <= volume <= 1.90:
            score += 9.0
            reasons.append("CTX_VOLUME_EXPANSION_START")
        elif volume > 1.90:
            score += 3.0
            reasons.append("CTX_VOLUME_HIGH_WATCH_CLIMAX")
        elif volume >= 0.85:
            score += 3.0
            reasons.append("CTX_VOLUME_ACCEPTABLE")
        else:
            score -= 8.0
            reasons.append("CTX_VOLUME_WEAK")
    else:
        reasons.append("CTX_VOLUME_MISSING")

    if atr_pct is not None:
        if 0.25 <= atr_pct <= 1.80:
            score += 5.0
            reasons.append("CTX_ATR_HEALTHY")
        elif atr_pct < 0.18:
            score -= 7.0
            reasons.append("CTX_ATR_TOO_DEAD")
        elif atr_pct > 2.20:
            score -= 5.0
            reasons.append("CTX_ATR_TOO_EXTENDED")

    if ema20 is not None:
        if (d == DIRECTION_LONG and price >= ema20) or (d == DIRECTION_SHORT and price <= ema20):
            score += 6.0
            reasons.append("CTX_EMA20_ALIGNED")
        elif macd_slope > 0 and power_gap >= 4:
            score += 2.0
            reasons.append("CTX_EMA20_NOT_RECLAIMED_BUT_PRESSURE")
        else:
            score -= 6.0
            reasons.append("CTX_EMA20_AGAINST")

    if vwap is not None:
        if (d == DIRECTION_LONG and price >= vwap) or (d == DIRECTION_SHORT and price <= vwap):
            score += 5.0
            reasons.append("CTX_VWAP_ALIGNED")
        elif macd_slope > 0 and power_gap >= 4:
            score += 1.0
            reasons.append("CTX_VWAP_NOT_RECLAIMED_BUT_PRESSURE")
        else:
            score -= 5.0
            reasons.append("CTX_VWAP_AGAINST")

    if rsi is not None:
        if d == DIRECTION_LONG and rsi >= 74 and rsi_slope <= 0.08:
            score -= 12.0
            reasons.append("CTX_LONG_OVERHEATED_FADING")
        elif d == DIRECTION_SHORT and rsi <= 26 and rsi_slope <= 0.08:
            score -= 12.0
            reasons.append("CTX_SHORT_OVERHEATED_FADING")

    if adx is not None:
        if adx >= 18:
            score += 3.0
            reasons.append("CTX_ADX_ACCEPTABLE")
        elif macd_slope > 0 and power_gap >= 4:
            reasons.append("CTX_ADX_LOW_BUT_STARTING")
        else:
            score -= 4.0
            reasons.append("CTX_ADX_LOW")

    final = clamp(score, 0.0, 100.0)
    active = final >= 60.0 and macd_slope > 0 and power_gap >= 3.0 and (volume is None or volume >= 0.80)
    return final, active, reasons


def classify_asset_bias(sensor: SensorSnapshot) -> tuple[str, float, list[str]]:
    """Classify one context asset bias from raw sensors with less late confirmation bias."""
    reasons: list[str] = []
    score = 0.0

    price = _num(getattr(sensor, "price", None), 0.0)
    ema20 = safe_float(getattr(sensor, "ema20", None), None)
    ema50 = safe_float(getattr(sensor, "ema50", None), None)
    vwap = safe_float(getattr(sensor, "vwap", None), None)
    macd_hist = safe_float(getattr(sensor, "macd_hist", None), None)
    macd_slope = safe_float(getattr(sensor, "macd_hist_slope", None), None)
    rsi = safe_float(getattr(sensor, "rsi", None), None)
    rsi_slope = safe_float(getattr(sensor, "rsi_slope", None), None)
    buy = safe_float(getattr(sensor, "buy_power", None), None)
    sell = safe_float(getattr(sensor, "sell_power", None), None)
    volume = safe_float(getattr(sensor, "volume_ratio", None), None)

    if ema20 is not None:
        if price >= ema20:
            score += 12.0
            reasons.append("PRICE_ABOVE_EMA20")
        else:
            score -= 12.0
            reasons.append("PRICE_BELOW_EMA20")

    if ema20 is not None and ema50 is not None:
        if ema20 >= ema50:
            score += 10.0
            reasons.append("EMA_STACK_BULL")
        else:
            score -= 10.0
            reasons.append("EMA_STACK_BEAR")

    if vwap is not None:
        if price >= vwap:
            score += 9.0
            reasons.append("PRICE_ABOVE_VWAP")
        else:
            score -= 9.0
            reasons.append("PRICE_BELOW_VWAP")

    if macd_hist is not None:
        if macd_hist > 0:
            score += 10.0
            reasons.append("MACD_POSITIVE")
        elif macd_hist < 0:
            score -= 10.0
            reasons.append("MACD_NEGATIVE")

    if macd_slope is not None:
        if macd_slope > 0:
            score += 12.0
            reasons.append("MACD_SLOPE_UP")
        elif macd_slope < 0:
            score -= 12.0
            reasons.append("MACD_SLOPE_DOWN")

    if rsi is not None:
        if rsi >= 55:
            score += 7.0
            reasons.append("RSI_BULL")
        elif rsi <= 45:
            score -= 7.0
            reasons.append("RSI_BEAR")
        else:
            reasons.append("RSI_NEUTRAL")

    if rsi_slope is not None:
        if rsi_slope > 0.05:
            score += 5.0
            reasons.append("RSI_SLOPE_UP")
        elif rsi_slope < -0.05:
            score -= 5.0
            reasons.append("RSI_SLOPE_DOWN")

    if buy is not None and sell is not None:
        gap = buy - sell
        if gap >= 8:
            score += 10.0
            reasons.append("POWER_BULL")
        elif gap <= -8:
            score -= 10.0
            reasons.append("POWER_BEAR")
        elif gap >= 3:
            score += 4.0
            reasons.append("POWER_SOFT_BULL")
        elif gap <= -3:
            score -= 4.0
            reasons.append("POWER_SOFT_BEAR")
        else:
            reasons.append("POWER_NEUTRAL")

    if volume is not None:
        if volume >= 1.10:
            reasons.append("PARTICIPATION_ACTIVE")
        elif volume < 0.75:
            score *= 0.86
            reasons.append("PARTICIPATION_WEAK")

    score = clamp(score, -100.0, 100.0)
    if score >= 40:
        return "STRONG_BULLISH", score, reasons
    if score >= 15:
        return "BULLISH", score, reasons
    if score <= -40:
        return "STRONG_BEARISH", score, reasons
    if score <= -15:
        return "BEARISH", score, reasons
    return "NEUTRAL", score, reasons


def bias_direction_score(bias: str, direction: str, fresh_score: float = 50.0, fresh_active: bool = False) -> float:
    """Return alignment score between asset bias and trade direction, with fresh-start support."""
    b = safe_str(bias).upper()
    d = normalize_direction(direction)
    fresh = _num(fresh_score, 50.0)

    if b == "STRONG_BULLISH":
        base = 85.0 if d == DIRECTION_LONG else 20.0
    elif b == "BULLISH":
        base = 70.0 if d == DIRECTION_LONG else 35.0
    elif b == "STRONG_BEARISH":
        base = 85.0 if d == DIRECTION_SHORT else 20.0
    elif b == "BEARISH":
        base = 70.0 if d == DIRECTION_SHORT else 35.0
    else:
        base = 50.0

    if fresh_active:
        base += 8.0
    elif fresh >= 60.0:
        base += 4.0
    elif fresh <= 38.0:
        base -= 5.0

    return clamp(base, 0.0, 100.0)


def classify_market_mode(
    btc_bias: str,
    eth_bias: str,
    btc_sensor: Optional[SensorSnapshot] = None,
    btc_fresh_active: bool = False,
    eth_fresh_active: bool = False,
) -> tuple[str, bool, list[str]]:
    """Classify broad market mode with early movement awareness."""
    reasons: list[str] = []
    bull_count = sum(1 for b in [btc_bias, eth_bias] if "BULLISH" in safe_str(b).upper())
    bear_count = sum(1 for b in [btc_bias, eth_bias] if "BEARISH" in safe_str(b).upper())

    choppy = False
    if btc_sensor is not None:
        adx = safe_float(getattr(btc_sensor, "adx", None), None)
        atr_pct = safe_float(getattr(btc_sensor, "atr_pct", None), None)
        macd_slope = safe_float(getattr(btc_sensor, "macd_hist_slope", None), None)
        buy = safe_float(getattr(btc_sensor, "buy_power", None), None)
        sell = safe_float(getattr(btc_sensor, "sell_power", None), None)
        power_gap = abs((buy or 50.0) - (sell or 50.0)) if buy is not None and sell is not None else 0.0

        # Low ADX/ATR is choppy only if there is no fresh pressure starting.
        fresh_context = btc_fresh_active or eth_fresh_active or ((macd_slope or 0.0) != 0 and power_gap >= 4)
        if adx is not None and adx < 17 and not fresh_context:
            choppy = True
            reasons.append("BTC_ADX_LOW")
        elif adx is not None and adx < 17:
            reasons.append("BTC_ADX_LOW_BUT_FRESH_PRESSURE")

        if atr_pct is not None and atr_pct < 0.25 and not fresh_context:
            choppy = True
            reasons.append("BTC_ATR_LOW")
        elif atr_pct is not None and atr_pct < 0.25:
            reasons.append("BTC_ATR_LOW_BUT_EXPANDING_CONTEXT")

    if bull_count >= 2:
        reasons.append("BTC_ETH_BULLISH")
        return "BULLISH", choppy, reasons
    if bear_count >= 2:
        reasons.append("BTC_ETH_BEARISH")
        return "BEARISH", choppy, reasons
    if bull_count == 1 and bear_count == 1:
        reasons.append("BTC_ETH_MIXED")
        return "MIXED", True, reasons

    if btc_fresh_active and eth_fresh_active:
        reasons.append("BTC_ETH_FRESH_CONTEXT_ACTIVE")
        return "FRESH_START", choppy, reasons
    if btc_fresh_active or eth_fresh_active:
        reasons.append("ONE_MAJOR_FRESH_CONTEXT_ACTIVE")
        return "FRESH_START", choppy, reasons

    reasons.append("BTC_ETH_NEUTRAL")
    return "NEUTRAL", choppy, reasons


def market_mode_direction_score(market_mode: str, direction: str, fresh_context_score: float = 50.0) -> float:
    """Return broad market mode alignment score."""
    mode = safe_str(market_mode).upper()
    d = normalize_direction(direction)
    fresh = _num(fresh_context_score, 50.0)

    if mode == "BULLISH":
        return 78.0 if d == DIRECTION_LONG else 35.0
    if mode == "BEARISH":
        return 78.0 if d == DIRECTION_SHORT else 35.0
    if mode == "FRESH_START":
        return clamp(55.0 + max(0.0, fresh - 50.0) * 0.55, 45.0, 78.0)
    if mode == "MIXED":
        return 45.0
    if mode == "NEUTRAL":
        return 52.0
    return 45.0


# =============================================================================
# Builder
# =============================================================================

def build_context_from_results(
    context_results: dict[str, MarketDataResult],
    direction: str,
) -> MarketContextSnapshot:
    """Build context snapshot from market_data fetch results."""
    d = normalize_direction(direction)
    reason_codes: list[str] = []
    raw: dict[str, Any] = {"assets": {}}

    btc_sensor: Optional[SensorSnapshot] = None
    eth_sensor: Optional[SensorSnapshot] = None
    btc_bias = "UNKNOWN"
    eth_bias = "UNKNOWN"
    btc_score = 0.0
    eth_score = 0.0
    btc_fresh_score = 50.0
    eth_fresh_score = 50.0
    btc_fresh_active = False
    eth_fresh_active = False

    for symbol, result in context_results.items():
        if getattr(result, "status", None) != STATUS_OK or getattr(result, "snapshot", None) is None:
            raw["assets"][symbol] = {
                "status": getattr(result, "status", None),
                "error": getattr(result, "error", None),
            }
            continue

        sensor = build_sensor_snapshot(result.snapshot)
        bias, score, reasons = classify_asset_bias(sensor)
        fresh_score, fresh_active, fresh_reasons = score_asset_fresh_context(sensor, d)
        raw["assets"][symbol] = {
            "bias": bias,
            "score": score,
            "fresh_context_score": fresh_score,
            "fresh_context_active": fresh_active,
            "reasons": reasons,
            "fresh_reasons": fresh_reasons,
            "price": getattr(sensor, "price", None),
            "rsi": getattr(sensor, "rsi", None),
            "rsi_slope": getattr(sensor, "rsi_slope", None),
            "macd_hist": getattr(sensor, "macd_hist", None),
            "macd_hist_slope": getattr(sensor, "macd_hist_slope", None),
            "adx": getattr(sensor, "adx", None),
            "atr_pct": getattr(sensor, "atr_pct", None),
            "volume_ratio": getattr(sensor, "volume_ratio", None),
        }

        symbol_norm = safe_str(getattr(sensor, "symbol", symbol)).upper()
        if symbol_norm == "BTCUSDT":
            btc_sensor = sensor
            btc_bias = bias
            btc_score = score
            btc_fresh_score = fresh_score
            btc_fresh_active = fresh_active
        elif symbol_norm == "ETHUSDT":
            eth_sensor = sensor
            eth_bias = bias
            eth_score = score
            eth_fresh_score = fresh_score
            eth_fresh_active = fresh_active

    fresh_context_score = clamp((btc_fresh_score * 0.62) + (eth_fresh_score * 0.38), 0.0, 100.0)
    fresh_context_active = bool(btc_fresh_active or (eth_fresh_active and fresh_context_score >= 58.0))

    market_mode, choppy, mode_reasons = classify_market_mode(
        btc_bias,
        eth_bias,
        btc_sensor,
        btc_fresh_active=btc_fresh_active,
        eth_fresh_active=eth_fresh_active,
    )
    reason_codes.extend(mode_reasons)

    btc_alignment = bias_direction_score(btc_bias, d, btc_fresh_score, btc_fresh_active)
    eth_alignment = bias_direction_score(eth_bias, d, eth_fresh_score, eth_fresh_active)
    mode_alignment = market_mode_direction_score(market_mode, d, fresh_context_score)

    context_score = (
        btc_alignment * 0.40
        + eth_alignment * 0.22
        + mode_alignment * 0.23
        + fresh_context_score * 0.15
    )

    # If BTC is fresh in the requested direction, do not keep context neutral too long.
    if btc_fresh_active and fresh_context_score >= 60.0:
        context_score += 5.0
        reason_codes.append("BTC_FRESH_CONTEXT_BOOST")
    elif fresh_context_active and fresh_context_score >= 58.0:
        context_score += 3.0
        reason_codes.append("FRESH_CONTEXT_SOFT_BOOST")

    market_risk = 100.0 - context_score
    if choppy:
        # Choppy is dangerous, but fresh pressure should soften the penalty.
        penalty = 8.0 if fresh_context_active else 15.0
        market_risk += penalty
        reason_codes.append("MARKET_CHOPPY")

    if fresh_context_active and market_risk > 35.0:
        market_risk -= 5.0
        reason_codes.append("FRESH_CONTEXT_REDUCES_RISK")

    context_score = clamp(context_score, 0.0, 100.0)
    market_risk = clamp(market_risk, 0.0, 100.0)

    aligned = context_score >= 55.0 or (fresh_context_active and context_score >= 52.0)

    if aligned:
        reason_codes.append("MARKET_CONTEXT_ALIGNED")
    elif context_score <= 40:
        reason_codes.append("MARKET_CONTEXT_AGAINST")
    else:
        reason_codes.append("MARKET_CONTEXT_NEUTRAL")

    if fresh_context_score >= 65:
        reason_codes.append("FRESH_MARKET_CONTEXT_HIGH")
    elif fresh_context_score >= 55:
        reason_codes.append("FRESH_MARKET_CONTEXT_MEDIUM")
    else:
        reason_codes.append("FRESH_MARKET_CONTEXT_LOW")

    return MarketContextSnapshot(
        market_mode=market_mode,
        btc_bias=btc_bias,
        eth_bias=eth_bias,
        context_score=context_score,
        market_risk_score=market_risk,
        choppy=choppy,
        aligned_with_direction=aligned,
        reason_codes=list(dict.fromkeys(reason_codes)),
        raw={
            **raw,
            "btc_raw_score": btc_score,
            "eth_raw_score": eth_score,
            "btc_fresh_context_score": btc_fresh_score,
            "eth_fresh_context_score": eth_fresh_score,
            "fresh_context_score": fresh_context_score,
            "btc_fresh_context_active": btc_fresh_active,
            "eth_fresh_context_active": eth_fresh_active,
            "fresh_context_active": fresh_context_active,
            "direction": d,
        },
    )


def build_market_context_snapshot(
    direction: str,
    *,
    timeframe: str = "1H",
    context_results: Optional[dict[str, MarketDataResult]] = None,
) -> MarketContextSnapshot:
    """
    Build market context snapshot.

    If context_results is omitted, fetches BTC/ETH context snapshots via market_data.
    """
    if context_results is None:
        context_results = fetch_context_snapshots(timeframe=timeframe)
    return build_context_from_results(context_results, direction)


def build_market_context_from_snapshots(
    snapshots: dict[str, MarketSnapshot],
    direction: str,
) -> MarketContextSnapshot:
    """Build context from already available snapshots for tests/backfills."""
    results: dict[str, MarketDataResult] = {}
    for symbol, snapshot in snapshots.items():
        results[symbol] = MarketDataResult(
            status=STATUS_OK if snapshot.ok else "FAILED",
            symbol=symbol,
            timeframe=snapshot.timeframe,
            snapshot=snapshot,
            message="offline_context",
            error=snapshot.error,
        )
    return build_context_from_results(results, direction)


def validate_market_context_snapshot(snapshot: MarketContextSnapshot) -> dict[str, Any]:
    """Lightweight validation for market context snapshot."""
    errors: list[str] = []
    for key in ["context_score", "market_risk_score"]:
        value = safe_float(getattr(snapshot, key, None), None)
        if value is None or not (0.0 <= value <= 100.0):
            errors.append(f"invalid_{key}")

    if not getattr(snapshot, "market_mode", None):
        errors.append("missing_market_mode")
    if not getattr(snapshot, "btc_bias", None):
        errors.append("missing_btc_bias")
    if not getattr(snapshot, "eth_bias", None):
        errors.append("missing_eth_bias")

    raw = getattr(snapshot, "raw", None) or {}
    if not isinstance(raw, dict):
        errors.append("invalid_raw")
    else:
        for key in ["fresh_context_score"]:
            value = safe_float(raw.get(key), None)
            if value is None or not (0.0 <= value <= 100.0):
                errors.append(f"invalid_raw_{key}")

    return {
        "valid": not errors,
        "errors": errors,
        "market_mode": getattr(snapshot, "market_mode", None),
        "btc_bias": getattr(snapshot, "btc_bias", None),
        "eth_bias": getattr(snapshot, "eth_bias", None),
        "context_score": getattr(snapshot, "context_score", None),
    }


__all__ = [
    "MARKET_CONTEXT_VERSION",
    "score_asset_fresh_context",
    "classify_asset_bias",
    "bias_direction_score",
    "classify_market_mode",
    "market_mode_direction_score",
    "build_context_from_results",
    "build_market_context_snapshot",
    "build_market_context_from_snapshots",
    "validate_market_context_snapshot",
]
