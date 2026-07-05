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
        """Direction gate for 5m entries.

        User rule: the 5m indicator range may only be traded when the 4H and
        1H symbol trend agree with the candidate direction, and BTC/ETH 1H do
        not fight that direction. 1D is kept in the result for compatibility,
        but it is no longer part of the gate.
        """
        s1d = self._bias(symbol_1d)
        s4h = self._bias(symbol_4h)
        s1h = self._bias(symbol_1h)
        btc = self._bias(btc_1h)
        eth = self._bias(eth_1h)
        reasons: list[str] = []

        symbol_ok = s4h == direction and s1h == direction
        btc_ok = btc in {direction, "NEUTRAL"}
        eth_ok = eth in {direction, "NEUTRAL"}
        context_ok = btc_ok and eth_ok
        context_confirmations = sum(1 for b in (btc, eth) if b == direction)

        if symbol_ok and context_confirmations == 2:
            alignment = "FULL"
            reasons.append("4H/1H و BTC/ETH هم‌جهت کامل هستند.")
        elif symbol_ok and context_ok:
            alignment = "GOOD"
            reasons.append("4H/1H هم‌جهت هستند و BTC/ETH خلاف جهت نیستند.")
        else:
            alignment = "BAD"
            if not symbol_ok:
                reasons.append("Direction Gate: تایم‌فریم‌های 4H و 1H جهت را تایید نکردند.")
            if not btc_ok or not eth_ok:
                reasons.append("Direction Gate: BTC یا ETH خلاف جهت است.")

        normal_ok = symbol_ok and context_ok
        real_ok = normal_ok and alignment in {"FULL", "GOOD"}
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
