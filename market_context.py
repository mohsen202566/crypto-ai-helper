from __future__ import annotations

from dataclasses import dataclass

from indicators import IndicatorSnapshot
from scorer import Direction, DirectionState


@dataclass(frozen=True)
class MarketContextResult:
    bias: DirectionState
    score: int
    reasons: tuple[str, ...]


class MarketContextEngine:
    def analyze(self, btc_1h: IndicatorSnapshot | None, eth_1h: IndicatorSnapshot | None, direction: Direction) -> MarketContextResult:
        snapshots = [s for s in (btc_1h, eth_1h) if s is not None]
        if not snapshots:
            return MarketContextResult("NEUTRAL", 2, ("دیتای BTC/ETH نبود؛ امتیاز خنثی داده شد.",))
        raw = 0
        for s in snapshots:
            if s.close > s.ema50 and s.ema20 > s.ema50:
                raw += 1
            elif s.close < s.ema50 and s.ema20 < s.ema50:
                raw -= 1
        bias: DirectionState = "LONG" if raw > 0 else "SHORT" if raw < 0 else "NEUTRAL"
        if bias == direction:
            return MarketContextResult(bias, 3, ("BTC/ETH با جهت موافق است.",))
        if bias == "NEUTRAL":
            return MarketContextResult(bias, 2, ("بازار کلی خنثی است.",))
        return MarketContextResult(bias, 0, ("بازار کلی خلاف جهت است؛ فقط امتیاز کم شد.",))
