"""
market_context.py
Level 4 / 1H Smart Scalp Bot

Light market context engine.

Architecture lock:
- Builds BTC/ETH/market-mode context only.
- No final AI decision, no REAL/GHOST/REJECT, no order execution,
  no position monitoring, no JSON state writes, no Telegram text.
- Allowed project imports: constants.py, utils.py, models.py, market_data.py, technical_sensors.py only.
"""

from __future__ import annotations

from typing import Any, Optional

from constants import DIRECTION_LONG, DIRECTION_SHORT, STATUS_OK, SYSTEM_VERSION
from market_data import fetch_context_snapshots
from models import MarketContextSnapshot, MarketDataResult, MarketSnapshot, SensorSnapshot
from technical_sensors import build_sensor_snapshot
from utils import clamp, normalize_direction, safe_float, safe_str


MARKET_CONTEXT_VERSION: str = SYSTEM_VERSION


def classify_asset_bias(sensor: SensorSnapshot) -> tuple[str, float, list[str]]:
    """Classify one context asset bias from raw sensors."""
    reasons: list[str] = []
    score = 0.0

    price = safe_float(sensor.price, 0.0) or 0.0
    ema20 = safe_float(sensor.ema20, None)
    ema50 = safe_float(sensor.ema50, None)
    vwap = safe_float(sensor.vwap, None)
    macd_hist = safe_float(sensor.macd_hist, None)
    macd_slope = safe_float(sensor.macd_hist_slope, None)
    rsi = safe_float(sensor.rsi, None)
    buy = safe_float(sensor.buy_power, None)
    sell = safe_float(sensor.sell_power, None)

    if ema20 is not None:
        if price >= ema20:
            score += 14
            reasons.append("PRICE_ABOVE_EMA20")
        else:
            score -= 14
            reasons.append("PRICE_BELOW_EMA20")

    if ema20 is not None and ema50 is not None:
        if ema20 >= ema50:
            score += 12
            reasons.append("EMA_STACK_BULL")
        else:
            score -= 12
            reasons.append("EMA_STACK_BEAR")

    if vwap is not None:
        if price >= vwap:
            score += 10
            reasons.append("PRICE_ABOVE_VWAP")
        else:
            score -= 10
            reasons.append("PRICE_BELOW_VWAP")

    if macd_hist is not None:
        if macd_hist > 0:
            score += 12
            reasons.append("MACD_POSITIVE")
        elif macd_hist < 0:
            score -= 12
            reasons.append("MACD_NEGATIVE")

    if macd_slope is not None:
        if macd_slope > 0:
            score += 10
            reasons.append("MACD_SLOPE_UP")
        elif macd_slope < 0:
            score -= 10
            reasons.append("MACD_SLOPE_DOWN")

    if rsi is not None:
        if rsi >= 55:
            score += 8
            reasons.append("RSI_BULL")
        elif rsi <= 45:
            score -= 8
            reasons.append("RSI_BEAR")
        else:
            reasons.append("RSI_NEUTRAL")

    if buy is not None and sell is not None:
        gap = buy - sell
        if gap >= 8:
            score += 10
            reasons.append("POWER_BULL")
        elif gap <= -8:
            score -= 10
            reasons.append("POWER_BEAR")
        else:
            reasons.append("POWER_NEUTRAL")

    if score >= 40:
        return "STRONG_BULLISH", score, reasons
    if score >= 15:
        return "BULLISH", score, reasons
    if score <= -40:
        return "STRONG_BEARISH", score, reasons
    if score <= -15:
        return "BEARISH", score, reasons
    return "NEUTRAL", score, reasons


def bias_direction_score(bias: str, direction: str) -> float:
    """Return alignment score between asset bias and trade direction."""
    b = safe_str(bias).upper()
    d = normalize_direction(direction)

    if b == "STRONG_BULLISH":
        return 85.0 if d == DIRECTION_LONG else 20.0
    if b == "BULLISH":
        return 70.0 if d == DIRECTION_LONG else 35.0
    if b == "STRONG_BEARISH":
        return 85.0 if d == DIRECTION_SHORT else 20.0
    if b == "BEARISH":
        return 70.0 if d == DIRECTION_SHORT else 35.0
    return 50.0


def classify_market_mode(btc_bias: str, eth_bias: str, btc_sensor: Optional[SensorSnapshot] = None) -> tuple[str, bool, list[str]]:
    """Classify broad market mode."""
    reasons: list[str] = []
    bull_count = sum(1 for b in [btc_bias, eth_bias] if "BULLISH" in safe_str(b).upper())
    bear_count = sum(1 for b in [btc_bias, eth_bias] if "BEARISH" in safe_str(b).upper())

    choppy = False
    if btc_sensor is not None:
        adx = safe_float(btc_sensor.adx, None)
        atr_pct = safe_float(btc_sensor.atr_pct, None)
        if adx is not None and adx < 17:
            choppy = True
            reasons.append("BTC_ADX_LOW")
        if atr_pct is not None and atr_pct < 0.25:
            choppy = True
            reasons.append("BTC_ATR_LOW")

    if bull_count >= 2:
        reasons.append("BTC_ETH_BULLISH")
        return "BULLISH", choppy, reasons
    if bear_count >= 2:
        reasons.append("BTC_ETH_BEARISH")
        return "BEARISH", choppy, reasons
    if bull_count == 1 and bear_count == 1:
        reasons.append("BTC_ETH_MIXED")
        return "MIXED", True, reasons

    reasons.append("BTC_ETH_NEUTRAL")
    return "NEUTRAL", choppy, reasons


def market_mode_direction_score(market_mode: str, direction: str) -> float:
    """Return broad market mode alignment score."""
    mode = safe_str(market_mode).upper()
    d = normalize_direction(direction)

    if mode == "BULLISH":
        return 78.0 if d == DIRECTION_LONG else 35.0
    if mode == "BEARISH":
        return 78.0 if d == DIRECTION_SHORT else 35.0
    if mode == "MIXED":
        return 45.0
    if mode == "NEUTRAL":
        return 52.0
    return 45.0


def build_context_from_results(
    context_results: dict[str, MarketDataResult],
    direction: str,
) -> MarketContextSnapshot:
    """Build context snapshot from market_data fetch results."""
    d = normalize_direction(direction)
    reason_codes: list[str] = []
    raw: dict[str, Any] = {"assets": {}}

    btc_sensor = None
    eth_sensor = None
    btc_bias = "UNKNOWN"
    eth_bias = "UNKNOWN"
    btc_score = 0.0
    eth_score = 0.0

    for symbol, result in context_results.items():
        if result.status != STATUS_OK or result.snapshot is None:
            raw["assets"][symbol] = {"status": result.status, "error": result.error}
            continue

        sensor = build_sensor_snapshot(result.snapshot)
        bias, score, reasons = classify_asset_bias(sensor)
        raw["assets"][symbol] = {
            "bias": bias,
            "score": score,
            "reasons": reasons,
            "price": sensor.price,
            "rsi": sensor.rsi,
            "adx": sensor.adx,
        }

        if sensor.symbol == "BTCUSDT":
            btc_sensor = sensor
            btc_bias = bias
            btc_score = score
        elif sensor.symbol == "ETHUSDT":
            eth_sensor = sensor
            eth_bias = bias
            eth_score = score

    market_mode, choppy, mode_reasons = classify_market_mode(btc_bias, eth_bias, btc_sensor)
    reason_codes.extend(mode_reasons)

    btc_alignment = bias_direction_score(btc_bias, d)
    eth_alignment = bias_direction_score(eth_bias, d)
    mode_alignment = market_mode_direction_score(market_mode, d)

    context_score = (btc_alignment * 0.45) + (eth_alignment * 0.25) + (mode_alignment * 0.30)

    market_risk = 100.0 - context_score
    if choppy:
        market_risk += 15.0
        reason_codes.append("MARKET_CHOPPY")

    aligned = context_score >= 58.0

    if aligned:
        reason_codes.append("MARKET_CONTEXT_ALIGNED")
    elif context_score <= 40:
        reason_codes.append("MARKET_CONTEXT_AGAINST")
    else:
        reason_codes.append("MARKET_CONTEXT_NEUTRAL")

    return MarketContextSnapshot(
        market_mode=market_mode,
        btc_bias=btc_bias,
        eth_bias=eth_bias,
        context_score=clamp(context_score, 0.0, 100.0),
        market_risk_score=clamp(market_risk, 0.0, 100.0),
        choppy=choppy,
        aligned_with_direction=aligned,
        reason_codes=reason_codes,
        raw={
            **raw,
            "btc_raw_score": btc_score,
            "eth_raw_score": eth_score,
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
        value = safe_float(getattr(snapshot, key), -1.0)
        if value is None or not (0.0 <= value <= 100.0):
            errors.append(f"invalid_{key}")

    if not snapshot.market_mode:
        errors.append("missing_market_mode")
    if not snapshot.btc_bias:
        errors.append("missing_btc_bias")
    if not snapshot.eth_bias:
        errors.append("missing_eth_bias")

    return {
        "valid": not errors,
        "errors": errors,
        "market_mode": snapshot.market_mode,
        "btc_bias": snapshot.btc_bias,
        "eth_bias": snapshot.eth_bias,
        "context_score": snapshot.context_score,
    }


__all__ = [
    "MARKET_CONTEXT_VERSION",
    "classify_asset_bias",
    "bias_direction_score",
    "classify_market_mode",
    "market_mode_direction_score",
    "build_context_from_results",
    "build_market_context_snapshot",
    "build_market_context_from_snapshots",
    "validate_market_context_snapshot",
]
