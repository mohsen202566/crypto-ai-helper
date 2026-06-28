from __future__ import annotations

from dataclasses import dataclass

from okx_data import Candle


@dataclass(frozen=True)
class LevelsResult:
    support: float
    resistance: float
    swing_low: float
    swing_high: float


class LevelsEngine:
    def detect(self, candles: list[Candle], entry: float, lookback: int = 80) -> LevelsResult:
        window = candles[-lookback:] if len(candles) >= lookback else candles
        if len(window) < 10:
            raise RuntimeError("کندل کافی برای تشخیص حمایت/مقاومت وجود ندارد.")
        lows = [c.low for c in window]
        highs = [c.high for c in window]
        swing_low = min(lows)
        swing_high = max(highs)
        below = [x for x in lows if x < entry]
        above = [x for x in highs if x > entry]
        support = max(below) if below else swing_low
        resistance = min(above) if above else swing_high
        if support >= entry:
            support = swing_low
        if resistance <= entry:
            resistance = swing_high
        return LevelsResult(float(support), float(resistance), float(swing_low), float(swing_high))
