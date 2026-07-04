from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from indicators import IndicatorSnapshot

Direction = Literal["LONG", "SHORT"]
Bias = Literal["LONG", "SHORT", "NEUTRAL"]


@dataclass(frozen=True)
class MarketContextResult:
    symbol_1d: Bias
    symbol_4h: Bias
    symbol_1h: Bias
    btc_bias: Bias
    eth_bias: Bias
    alignment: str
    real_ok: bool
    normal_ok: bool
    reasons: tuple[str, ...]


class MarketContextEngine:
    def analyze(self, direction: Direction, symbol_1d: IndicatorSnapshot | None, symbol_4h: IndicatorSnapshot | None, symbol_1h: IndicatorSnapshot | None, btc_1h: IndicatorSnapshot | None, eth_1h: IndicatorSnapshot | None) -> MarketContextResult:
        s1d = self._bias(symbol_1d)
        s4h = self._bias(symbol_4h)
        s1h = self._bias(symbol_1h)
        btc = self._bias(btc_1h)
        eth = self._bias(eth_1h)
        reasons: list[str] = []
        match = sum(1 for b in (s1d, s4h, s1h) if b == direction)
        opposite = sum(1 for b in (s1d, s4h, s1h) if b != "NEUTRAL" and b != direction)
        context_opposite = sum(1 for b in (btc, eth) if b != "NEUTRAL" and b != direction)
        if match >= 3:
            alignment = "FULL"
            reasons.append("1D/4H/1H هم‌جهت کامل هستند.")
        elif match >= 2 and opposite == 0:
            alignment = "GOOD"
            reasons.append("اکثر تایم‌های بالا هم‌جهت یا خنثی هستند.")
        elif match >= 1 and opposite <= 1:
            alignment = "SOFT"
            reasons.append("جهت تایم‌های بالا برای Normal نرم قابل قبول است.")
        else:
            alignment = "BAD"
            reasons.append("تایم‌های بالا خلاف جهت یا نامطمئن هستند.")
        if context_opposite == 2:
            reasons.append("BTC و ETH خلاف جهت هستند؛ Real سخت می‌شود.")
        normal_ok = alignment != "BAD" or match >= 1
        real_ok = alignment in {"FULL", "GOOD"} and context_opposite < 2
        return MarketContextResult(s1d, s4h, s1h, btc, eth, alignment, real_ok, normal_ok, tuple(reasons))

    @staticmethod
    def _bias(snapshot: IndicatorSnapshot | None) -> Bias:
        if snapshot is None:
            return "NEUTRAL"
        long_strength = 0
        short_strength = 0
        if snapshot.close > snapshot.ema50:
            long_strength += 1
        if snapshot.ema20 > snapshot.ema50:
            long_strength += 1
        if snapshot.ema50 > snapshot.ema200:
            long_strength += 1
        if snapshot.close < snapshot.ema50:
            short_strength += 1
        if snapshot.ema20 < snapshot.ema50:
            short_strength += 1
        if snapshot.ema50 < snapshot.ema200:
            short_strength += 1
        if long_strength >= 2 and long_strength > short_strength:
            return "LONG"
        if short_strength >= 2 and short_strength > long_strength:
            return "SHORT"
        return "NEUTRAL"
