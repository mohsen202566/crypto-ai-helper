from __future__ import annotations

from dataclasses import dataclass

from okx_data import Candle
from scorer import Direction, OrderBlockState


@dataclass(frozen=True)
class OrderBlockResult:
    state: OrderBlockState
    score: int
    distance_atr: float
    reasons: tuple[str, ...]


class OrderBlockEngine:
    def analyze(self, candles_15m: list[Candle], direction: Direction, entry: float, atr: float) -> OrderBlockResult:
        if len(candles_15m) < 20:
            return OrderBlockResult("NEUTRAL", 2, 999.0, ("کندل کافی برای OB نیست.",))
        window = candles_15m[-18:]
        ranges = [max(0.0, c.high - c.low) for c in window]
        avg_range = sum(ranges) / len(ranges) if ranges else 0.0
        atr = max(atr, avg_range, entry * 0.0001)
        bullish_zone = None
        bearish_zone = None
        for idx in range(len(window) - 2):
            c = window[idx]
            n = window[idx + 1]
            body = abs(c.close - c.open)
            if body < avg_range * 0.25:
                continue
            if c.close < c.open and n.close > n.open and n.close > c.high:
                bullish_zone = (c.low, c.high)
            if c.close > c.open and n.close < n.open and n.close < c.low:
                bearish_zone = (c.low, c.high)
        zone = bullish_zone if direction == "LONG" else bearish_zone
        opposite = bearish_zone if direction == "LONG" else bullish_zone
        if zone:
            mid = (zone[0] + zone[1]) / 2.0
            distance = abs(entry - mid) / atr
            if distance <= 1.8:
                return OrderBlockResult("WITH_SIGNAL", 4, distance, ("Order Block نزدیک و موافق سیگنال است.",))
            return OrderBlockResult("NEUTRAL", 2, distance, ("Order Block موافق دور است.",))
        if opposite:
            mid = (opposite[0] + opposite[1]) / 2.0
            distance = abs(entry - mid) / atr
            if distance <= 1.2:
                return OrderBlockResult("AGAINST_SIGNAL", 0, distance, ("Order Block مخالف نزدیک است.",))
        return OrderBlockResult("NEUTRAL", 2, 999.0, ("Order Block مهمی دیده نشد.",))
