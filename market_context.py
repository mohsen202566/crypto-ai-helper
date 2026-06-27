"""
Market context engine for Crypto AI Helper bot.

Locked responsibility:
- Uses OKX market data only.
- Checks broad market context before coin analysis.
- No Fear & Greed, no external sentiment, no Toobit, no Telegram.
- No order execution, no AI, no TP/SL.

15m/30m fast-mode lock:
- Market context is a light risk filter, not the main direction engine.
- BTC and broad-market candles may add small bias only.
- Volatility can block only extreme/noisy conditions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

Direction = Literal["bullish", "bearish", "neutral"]
RiskState = Literal["normal", "caution", "blocked"]


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class MarketContext:
    btc_direction: Direction
    market_direction: Direction
    volatility_state: RiskState
    # Kept for backward compatibility with older modules, but locked to "normal".
    sentiment_state: RiskState
    trade_permission: RiskState
    long_bias: float
    short_bias: float
    reason: str


def parse_okx_candles(raw_candles: Sequence[Sequence[str]]) -> list[Candle]:
    """Convert OKX candle arrays to Candle objects.

    OKX returns newest-first arrays. The returned list is oldest-first.
    Expected fields: ts, open, high, low, close, volume, ...
    """
    candles: list[Candle] = []
    for row in raw_candles:
        if len(row) < 6:
            continue
        try:
            candles.append(
                Candle(
                    timestamp=int(float(row[0])),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        except (TypeError, ValueError):
            continue
    return list(reversed(candles))


def build_market_context(
    btc_candles: Sequence[Candle],
    alt_market_candles: Sequence[Candle] | None = None,
    fear_greed_value: int | None = None,
) -> MarketContext:
    """Build lightweight market context from OKX candles.

    fear_greed_value is intentionally ignored. It remains in the signature only to
    avoid breaking older callers while the 15m/30m system removes sentiment from
    the trading decision path.
    """
    _ = fear_greed_value
    sentiment_state: RiskState = "normal"

    if len(btc_candles) < 20:
        return MarketContext(
            btc_direction="neutral",
            market_direction="neutral",
            volatility_state="caution",
            sentiment_state=sentiment_state,
            trade_permission="caution",
            long_bias=0.0,
            short_bias=0.0,
            reason="BTC candles are not enough; cautious mode",
        )

    btc_direction = _direction_from_candles(btc_candles)
    market_direction = _direction_from_candles(alt_market_candles) if alt_market_candles else btc_direction
    volatility_state = _volatility_state(btc_candles)
    permission = _trade_permission(btc_direction, market_direction, volatility_state)
    long_bias, short_bias = _bias_from_context(btc_direction, market_direction, volatility_state)

    return MarketContext(
        btc_direction=btc_direction,
        market_direction=market_direction,
        volatility_state=volatility_state,
        sentiment_state=sentiment_state,
        trade_permission=permission,
        long_bias=long_bias,
        short_bias=short_bias,
        reason=_make_reason(btc_direction, market_direction, volatility_state, permission),
    )


def _direction_from_candles(candles: Sequence[Candle] | None) -> Direction:
    """Detect broad market direction without making it over-dominant.

    Uses recent closes and simple structure. This is intentionally slower and
    lighter than coin-level 15m/30m direction, so it only provides small bias.
    """
    if not candles:
        return "neutral"
    recent = list(candles)[-20:]
    if len(recent) < 12:
        return "neutral"

    last_close = recent[-1].close
    close_8 = recent[-8].close
    close_16 = recent[-16].close if len(recent) >= 16 else recent[0].close

    short_change = _pct_change(close_8, last_close)
    medium_change = _pct_change(close_16, last_close)

    highs = [c.high for c in recent[-12:]]
    lows = [c.low for c in recent[-12:]]
    first_high = max(highs[:6])
    second_high = max(highs[6:])
    first_low = min(lows[:6])
    second_low = min(lows[6:])

    structure_up = second_high > first_high and second_low > first_low
    structure_down = second_high < first_high and second_low < first_low

    if short_change > 0.25 and medium_change > 0.35 and structure_up:
        return "bullish"
    if short_change < -0.25 and medium_change < -0.35 and structure_down:
        return "bearish"
    if medium_change > 0.75 and short_change > 0:
        return "bullish"
    if medium_change < -0.75 and short_change < 0:
        return "bearish"
    return "neutral"


def _volatility_state(candles: Sequence[Candle]) -> RiskState:
    """Classify volatility for fast 15m/30m mode.

    Low volatility is caution, not direction. Extreme range expansion is blocked
    because it often creates late entries and fake direction.
    """
    recent = list(candles)[-24:]
    ranges = [abs(c.high - c.low) / c.close * 100.0 for c in recent if c.close > 0]
    if len(ranges) < 12:
        return "caution"

    baseline = sum(ranges[:-4]) / max(len(ranges[:-4]), 1)
    latest = sum(ranges[-4:]) / 4
    last_range = ranges[-1]

    if baseline <= 0:
        return "caution"
    if latest > baseline * 3.2 or last_range > baseline * 4.0:
        return "blocked"
    if latest > baseline * 2.0 or latest < baseline * 0.50:
        return "caution"
    return "normal"


def _trade_permission(btc: Direction, market: Direction, volatility: RiskState) -> RiskState:
    if volatility == "blocked":
        return "blocked"
    if volatility == "caution":
        return "caution"
    # If both are flat, do not block; let coin analyzer reject weak/range setups.
    if btc == "neutral" and market == "neutral":
        return "caution"
    return "normal"


def _bias_from_context(btc: Direction, market: Direction, volatility: RiskState) -> tuple[float, float]:
    """Small directional bias only; coin_analyzer remains the direction source."""
    long_bias = 0.0
    short_bias = 0.0

    if btc == "bullish":
        long_bias += 4.0
    elif btc == "bearish":
        short_bias += 4.0

    if market == "bullish":
        long_bias += 3.0
    elif market == "bearish":
        short_bias += 3.0

    if volatility == "caution":
        long_bias -= 3.0
        short_bias -= 3.0
    elif volatility == "blocked":
        long_bias -= 15.0
        short_bias -= 15.0

    return _clamp(long_bias, -15.0, 10.0), _clamp(short_bias, -15.0, 10.0)


def _make_reason(btc: Direction, market: Direction, volatility: RiskState, permission: RiskState) -> str:
    return f"BTC={btc} | Market={market} | Volatility={volatility} | Sentiment=removed | Permission={permission}"


def _pct_change(old: float, new: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old * 100.0


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


__all__ = [
    "Candle",
    "MarketContext",
    "parse_okx_candles",
    "build_market_context",
]
