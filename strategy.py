"""Simple technical strategy engine for Crypto AI Helper bot.

Locked responsibility:
- Pure technical analysis only.
- Produces LONG / SHORT / NO_TRADE using simple weighted scoring.
- No Toobit, no Telegram, no state, no TP/SL, no API calls.

Design lock:
- Predictive, not reactive.
- Candles are used for indicators/structure, not as a direct color-based entry signal.
- Every indicator has LONG / NEUTRAL / SHORT zones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from config import (
    DIRECTION_MAX_OPPOSITE_SCORE,
    MIN_DIRECTION_SCORE,
    MIN_STRENGTH_SCORE,
    SCORE_BREAKOUT,
    SCORE_EMA,
    SCORE_MARKET_STRUCTURE,
    SCORE_MACD,
    SCORE_RSI,
    SCORE_ATR_EXPANSION,
    SCORE_VOLUME,
)

Direction = Literal["LONG", "SHORT", "NO_TRADE"]
Bias = Literal["LONG", "SHORT", "NEUTRAL"]
Strength = Literal["weak", "normal", "strong"]


@dataclass(frozen=True)
class StrategyResult:
    symbol: str
    direction: Direction
    strength: Strength
    long_score: float
    short_score: float
    strength_score: float
    entry_price: float
    atr: float
    reason: str


# Public API -----------------------------------------------------------------

def analyze_strategy(symbol: str, candles_5m: list[dict[str, Any]], candles_15m: list[dict[str, Any]] | None = None) -> StrategyResult:
    """Analyze one symbol with a simple 5m/15m technical model.

    Main timing is 5m. 15m is optional support for smoother structure, but this
    strategy remains lightweight and score-based.
    """
    symbol = symbol.upper()
    candles = _clean_candles(candles_5m)
    higher = _clean_candles(candles_15m or [])

    if len(candles) < 60:
        return _no_trade(symbol, 0.0, 0.0, 0.0, "کندل 5m کافی نیست")

    close = _closes(candles)
    high = _highs(candles)
    low = _lows(candles)
    volume = _volumes(candles)
    entry = close[-1]

    ema_bias = _ema_bias(close)
    rsi_value, rsi_bias = _rsi_bias(close)
    macd_bias = _macd_bias(close)
    structure_bias = _structure_bias(higher if len(higher) >= 30 else candles)

    long_score = 0.0
    short_score = 0.0
    reasons: list[str] = []

    long_score, short_score = _add_vote(long_score, short_score, ema_bias, SCORE_EMA)
    reasons.append(f"EMA={ema_bias}")

    long_score, short_score = _add_vote(long_score, short_score, rsi_bias, SCORE_RSI)
    reasons.append(f"RSI={rsi_value:.2f}/{rsi_bias}")

    long_score, short_score = _add_vote(long_score, short_score, macd_bias, SCORE_MACD)
    reasons.append(f"MACD={macd_bias}")

    long_score, short_score = _add_vote(long_score, short_score, structure_bias, SCORE_MARKET_STRUCTURE)
    reasons.append(f"Structure={structure_bias}")

    direction: Direction = "NO_TRADE"
    if long_score >= MIN_DIRECTION_SCORE and short_score <= DIRECTION_MAX_OPPOSITE_SCORE:
        direction = "LONG"
    elif short_score >= MIN_DIRECTION_SCORE and long_score <= DIRECTION_MAX_OPPOSITE_SCORE:
        direction = "SHORT"

    atr_value = _atr(high, low, close, period=14)
    strength_score = 0.0
    if direction != "NO_TRADE":
        atr_ok = _atr_expanding(high, low, close)
        volume_ok = _volume_expanding(volume)
        breakout_ok = _breakout_ok(direction, high, low, close)

        if atr_ok:
            strength_score += SCORE_ATR_EXPANSION
        if volume_ok:
            strength_score += SCORE_VOLUME
        if breakout_ok:
            strength_score += SCORE_BREAKOUT

        reasons.append(f"ATRExpansion={atr_ok}")
        reasons.append(f"VolumeMA20={volume_ok}")
        reasons.append(f"Breakout={breakout_ok}")

        if strength_score < MIN_STRENGTH_SCORE:
            direction = "NO_TRADE"
            reasons.append("StrengthScore کافی نیست")

    strength = _classify_strength(long_score, short_score, strength_score)
    if direction == "NO_TRADE":
        strength = "weak"

    return StrategyResult(
        symbol=symbol,
        direction=direction,
        strength=strength,
        long_score=round(long_score, 2),
        short_score=round(short_score, 2),
        strength_score=round(strength_score, 2),
        entry_price=float(entry),
        atr=float(atr_value),
        reason=" | ".join(reasons),
    )


# Scoring --------------------------------------------------------------------

def _add_vote(long_score: float, short_score: float, bias: Bias, weight: float) -> tuple[float, float]:
    if bias == "LONG":
        long_score += weight
    elif bias == "SHORT":
        short_score += weight
    return long_score, short_score


def _classify_strength(long_score: float, short_score: float, strength_score: float) -> Strength:
    direction_score = max(long_score, short_score)
    total = direction_score + strength_score
    if total >= 115:
        return "strong"
    if total >= 90:
        return "normal"
    return "weak"


# Indicator biases -----------------------------------------------------------

def _ema_bias(close: list[float]) -> Bias:
    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)
    if len(ema20) < 6 or len(ema50) < 6:
        return "NEUTRAL"

    gap_pct = (ema20[-1] - ema50[-1]) / close[-1] * 100.0
    slope20 = (ema20[-1] - ema20[-5]) / close[-1] * 100.0

    if gap_pct > 0.03 and slope20 > 0.01:
        return "LONG"
    if gap_pct < -0.03 and slope20 < -0.01:
        return "SHORT"
    return "NEUTRAL"


def _rsi_bias(close: list[float]) -> tuple[float, Bias]:
    values = _rsi(close, 14)
    if len(values) < 5:
        return 50.0, "NEUTRAL"
    current = values[-1]
    slope = current - values[-4]

    if current > 55.0 and slope > 0.5:
        return current, "LONG"
    if current < 45.0 and slope < -0.5:
        return current, "SHORT"
    return current, "NEUTRAL"


def _macd_bias(close: list[float]) -> Bias:
    macd_line, signal_line = _macd(close)
    if len(macd_line) < 4 or len(signal_line) < 4:
        return "NEUTRAL"

    prev_diff = macd_line[-2] - signal_line[-2]
    curr_diff = macd_line[-1] - signal_line[-1]

    # Fresh cross or very recent start. We do not use histogram as a separate signal.
    if prev_diff <= 0 and curr_diff > 0:
        return "LONG"
    if prev_diff >= 0 and curr_diff < 0:
        return "SHORT"

    # Early continuation after cross: small but clear separation.
    if curr_diff > 0 and macd_line[-1] > macd_line[-3]:
        return "LONG"
    if curr_diff < 0 and macd_line[-1] < macd_line[-3]:
        return "SHORT"
    return "NEUTRAL"


def _structure_bias(candles: list[dict[str, Any]]) -> Bias:
    if len(candles) < 20:
        return "NEUTRAL"
    highs = _highs(candles)
    lows = _lows(candles)

    swing_highs = _swing_points(highs, mode="high")[-3:]
    swing_lows = _swing_points(lows, mode="low")[-3:]
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "NEUTRAL"

    hh = swing_highs[-1] > swing_highs[-2]
    hl = swing_lows[-1] > swing_lows[-2]
    lh = swing_highs[-1] < swing_highs[-2]
    ll = swing_lows[-1] < swing_lows[-2]

    if hh and hl:
        return "LONG"
    if lh and ll:
        return "SHORT"
    return "NEUTRAL"


# Strength checks ------------------------------------------------------------

def _atr_expanding(high: list[float], low: list[float], close: list[float]) -> bool:
    if len(close) < 35:
        return False
    atr_fast = _atr_series(high, low, close, period=7)
    atr_slow = _atr_series(high, low, close, period=14)
    if not atr_fast or not atr_slow:
        return False
    return atr_fast[-1] > atr_slow[-1] * 1.03 and atr_fast[-1] > atr_fast[-4]


def _volume_expanding(volume: list[float]) -> bool:
    if len(volume) < 21:
        return False
    avg20 = sum(volume[-21:-1]) / 20.0
    return avg20 > 0 and volume[-1] >= avg20 * 1.10


def _breakout_ok(direction: Direction, high: list[float], low: list[float], close: list[float]) -> bool:
    if len(close) < 12 or direction == "NO_TRADE":
        return False
    recent_high = max(high[-11:-1])
    recent_low = min(low[-11:-1])
    if direction == "LONG":
        return close[-1] > recent_high * 0.999
    return close[-1] < recent_low * 1.001


# Math helpers ----------------------------------------------------------------

def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(value * alpha + result[-1] * (1.0 - alpha))
    return result


def _rsi(close: list[float], period: int = 14) -> list[float]:
    if len(close) <= period:
        return []
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        diff = close[i] - close[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    values: list[float] = [_rsi_from_avgs(avg_gain, avg_loss)]

    for i in range(period + 1, len(close)):
        diff = close[i] - close[i - 1]
        gain = max(diff, 0.0)
        loss = abs(min(diff, 0.0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        values.append(_rsi_from_avgs(avg_gain, avg_loss))
    return values


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _macd(close: list[float]) -> tuple[list[float], list[float]]:
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    signal_line = _ema(macd_line, 9)
    return macd_line, signal_line


def _atr(high: list[float], low: list[float], close: list[float], period: int = 14) -> float:
    series = _atr_series(high, low, close, period)
    return series[-1] if series else 0.0


def _atr_series(high: list[float], low: list[float], close: list[float], period: int = 14) -> list[float]:
    if len(close) <= period:
        return []
    tr: list[float] = []
    for i in range(1, len(close)):
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    if len(tr) < period:
        return []
    result = [sum(tr[:period]) / period]
    for value in tr[period:]:
        result.append((result[-1] * (period - 1) + value) / period)
    return result


def _swing_points(values: list[float], *, mode: Literal["high", "low"], left: int = 2, right: int = 2) -> list[float]:
    points: list[float] = []
    for i in range(left, len(values) - right):
        window = values[i - left : i + right + 1]
        if mode == "high" and values[i] == max(window):
            points.append(values[i])
        elif mode == "low" and values[i] == min(window):
            points.append(values[i])
    return points


# Candle parsing --------------------------------------------------------------

def _clean_candles(candles: list[dict[str, Any]]) -> list[dict[str, float]]:
    result: list[dict[str, float]] = []
    for item in candles:
        try:
            result.append(
                {
                    "open": float(item.get("open", item.get("o"))),
                    "high": float(item.get("high", item.get("h"))),
                    "low": float(item.get("low", item.get("l"))),
                    "close": float(item.get("close", item.get("c"))),
                    "volume": float(item.get("volume", item.get("v", 0.0))),
                }
            )
        except (TypeError, ValueError):
            continue
    return result


def _closes(candles: list[dict[str, float]]) -> list[float]:
    return [item["close"] for item in candles]


def _highs(candles: list[dict[str, float]]) -> list[float]:
    return [item["high"] for item in candles]


def _lows(candles: list[dict[str, float]]) -> list[float]:
    return [item["low"] for item in candles]


def _volumes(candles: list[dict[str, float]]) -> list[float]:
    return [item["volume"] for item in candles]


def _no_trade(symbol: str, long_score: float, short_score: float, strength_score: float, reason: str) -> StrategyResult:
    return StrategyResult(
        symbol=symbol.upper(),
        direction="NO_TRADE",
        strength="weak",
        long_score=long_score,
        short_score=short_score,
        strength_score=strength_score,
        entry_price=0.0,
        atr=0.0,
        reason=reason,
    )


__all__ = ["StrategyResult", "analyze_strategy"]
