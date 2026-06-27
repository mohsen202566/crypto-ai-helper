from __future__ import annotations

from dataclasses import dataclass

from config import WEIGHTS
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
            return MarketContextResult("NEUTRAL", max(2, WEIGHTS.market_quality // 2), ("دیتای BTC/ETH برای بازار کلی نبود؛ امتیاز خنثی داده شد.",))
        raw = 0
        for s in snapshots:
            if s.close > s.ema50 and s.ema20 > s.ema50:
                raw += 1
            elif s.close < s.ema50 and s.ema20 < s.ema50:
                raw -= 1
        if raw > 0:
            bias: DirectionState = "LONG"
        elif raw < 0:
            bias = "SHORT"
        else:
            bias = "NEUTRAL"
        if bias == direction:
            score = WEIGHTS.market_quality
            reason = "جهت کلی BTC/ETH با سیگنال موافق است."
        elif bias == "NEUTRAL":
            score = max(2, WEIGHTS.market_quality // 2)
            reason = "بازار کلی خنثی است."
        else:
            score = 1
            reason = "بازار کلی خلاف جهت سیگنال است؛ فقط امتیاز کیفیت بازار کم شد."
        return MarketContextResult(bias, score, (reason,))
