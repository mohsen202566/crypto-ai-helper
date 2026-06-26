"""
Market context engine for Crypto AI Helper bot.

Locked responsibility:
- Uses OKX market data only.
- Checks broad market context before coin analysis.
- Uses optional sentiment/fear-greed input only as a lightweight filter.
- No Toobit, no Telegram, no order execution, no AI, no TP/SL.
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
    return list(reversed(candles))


def build_market_context(
    btc_candles: Sequence[Candle],
    alt_market_candles: Sequence[Candle] | None = None,
    fear_greed_value: int | None = None,
) -> MarketContext:
    """Build lightweight market context from OKX candles.

    btc_candles is required.
    alt_market_candles is optional and can be a broad alt-market proxy.
    fear_greed_value is optional, from 0 to 100, and is only a lightweight risk filter.
    """
    sentiment_state = _sentiment_state(fear_greed_value)

    if len(btc_candles) < 20:
        return MarketContext(
            btc_direction="neutral",
            market_direction="neutral",
            volatility_state="caution",
            sentiment_state=sentiment_state,
            trade_permission="caution",
            long_bias=0.0,
            short_bias=0.0,
            reason="داده BTC کافی نیست",
        )

    btc_direction = _direction_from_candles(btc_candles)
    market_direction = _direction_from_candles(alt_market_candles) if alt_market_candles else btc_direction
    volatility_state = _volatility_state(btc_candles)
    permission = _trade_permission(btc_direction, market_direction, volatility_state, sentiment_state)
    long_bias, short_bias = _bias_from_context(
        btc_direction,
        market_direction,
        volatility_state,
        sentiment_state,
    )

    return MarketContext(
        btc_direction=btc_direction,
        market_direction=market_direction,
        volatility_state=volatility_state,
        sentiment_state=sentiment_state,
        trade_permission=permission,
        long_bias=long_bias,
        short_bias=short_bias,
        reason=_make_reason(
            btc_direction,
            market_direction,
            volatility_state,
            sentiment_state,
            permission,
        ),
    )


def _direction_from_candles(candles: Sequence[Candle]) -> Direction:
    recent = list(candles)[-12:]
    if len(recent) < 6:
        return "neutral"

    last_close = recent[-1].close
    start_close = recent[0].close
    change_pct = _pct_change(start_close, last_close)

    enough_points = len(recent) >= 8
    highs_up = recent[-1].high > recent[-4].high > recent[-8].high if enough_points else False
    lows_up = recent[-1].low > recent[-4].low > recent[-8].low if enough_points else False
    highs_down = recent[-1].high < recent[-4].high < recent[-8].high if enough_points else False
    lows_down = recent[-1].low < recent[-4].low < recent[-8].low if enough_points else False

    if change_pct > 0.35 and (highs_up or lows_up):
        return "bullish"
    if change_pct < -0.35 and (highs_down or lows_down):
        return "bearish"
    if change_pct > 0.65:
        return "bullish"
    if change_pct < -0.65:
        return "bearish"
    return "neutral"


def _volatility_state(candles: Sequence[Candle]) -> RiskState:
    recent = list(candles)[-20:]
    ranges = [abs(c.high - c.low) / c.close * 100.0 for c in recent if c.close > 0]
    if len(ranges) < 10:
        return "caution"

    avg_range = sum(ranges[:-3]) / max(len(ranges[:-3]), 1)
    latest_range = sum(ranges[-3:]) / 3

    if latest_range > avg_range * 3.0:
        return "blocked"
    if latest_range > avg_range * 1.8 or latest_range < avg_range * 0.45:
        return "caution"
    return "normal"


def _sentiment_state(fear_greed_value: int | None) -> RiskState:
    if fear_greed_value is None:
        return "normal"
    value = int(fear_greed_value)
    if not 0 <= value <= 100:
        return "caution"
    if value <= 15 or value >= 85:
        return "caution"
    return "normal"


def _trade_permission(
    btc: Direction,
    market: Direction,
    volatility: RiskState,
    sentiment: RiskState,
) -> RiskState:
    if volatility == "blocked":
        return "blocked"
    if btc == "neutral" and market == "neutral":
        return "caution"
    if volatility == "caution" or sentiment == "caution":
        return "caution"
    return "normal"


def _bias_from_context(
    btc: Direction,
    market: Direction,
    volatility: RiskState,
    sentiment: RiskState,
) -> tuple[float, float]:
    long_bias = 0.0
    short_bias = 0.0

    if btc == "bullish":
        long_bias += 8.0
    elif btc == "bearish":
        short_bias += 8.0

    if market == "bullish":
        long_bias += 5.0
    elif market == "bearish":
        short_bias += 5.0

    if volatility == "caution":
        long_bias -= 2.0
        short_bias -= 2.0
    elif volatility == "blocked":
        long_bias -= 10.0
        short_bias -= 10.0

    if sentiment == "caution":
        long_bias -= 1.0
        short_bias -= 1.0

    return max(long_bias, -15.0), max(short_bias, -15.0)


def _make_reason(
    btc: Direction,
    market: Direction,
    volatility: RiskState,
    sentiment: RiskState,
    permission: RiskState,
) -> str:
    return (
        f"BTC={btc} | Market={market} | Volatility={volatility} | "
        f"Sentiment={sentiment} | Permission={permission}"
    )


def _pct_change(old: float, new: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old * 100.0


__all__ = [
    "Candle",
    "MarketContext",
    "parse_okx_candles",
    "build_market_context",
]
