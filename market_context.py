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
    btc_1d: Bias
    btc_4h: Bias
    btc_1h: Bias
    btc_bias: Bias
    alignment: str
    real_ok: bool
    normal_ok: bool
    reasons: tuple[str, ...]


class MarketContextEngine:
    """Strict, simple 1H direction gate.

    The learning layer is not allowed to bypass this gate.  A signal is considered only when:
    - BTC confirms the same direction on its multi-timeframe bias,
    - the symbol 4H and 1H both confirm the direction,
    - the symbol 1D is not strongly opposite for Normal, and confirms for Real.
    """

    def analyze(
        self,
        direction: Direction,
        symbol_1d: IndicatorSnapshot | None,
        symbol_4h: IndicatorSnapshot | None,
        symbol_1h: IndicatorSnapshot | None,
        btc_1d: IndicatorSnapshot | None,
        btc_4h: IndicatorSnapshot | None,
        btc_1h: IndicatorSnapshot | None,
    ) -> MarketContextResult:
        s1d = self._bias(symbol_1d)
        s4h = self._bias(symbol_4h)
        s1h = self._bias(symbol_1h)
        b1d = self._bias(btc_1d)
        b4h = self._bias(btc_4h)
        b1h = self._bias(btc_1h)
        btc_bias = self._aggregate_btc_bias(direction, b1d, b4h, b1h)

        opposite = "SHORT" if direction == "LONG" else "LONG"
        reasons: list[str] = []

        if btc_bias != direction:
            reasons.append(f"BTC تایید {direction} نمی‌دهد؛ BTC={btc_bias} ({b1d}/{b4h}/{b1h}).")
        else:
            reasons.append(f"BTC جهت {direction} را تایید می‌کند ({b1d}/{b4h}/{b1h}).")

        if s4h != direction:
            reasons.append(f"4H ارز تایید {direction} ندارد؛ 4H={s4h}.")
        if s1h != direction:
            reasons.append(f"1H ارز تایید {direction} ندارد؛ 1H={s1h}.")
        if s1d == opposite:
            reasons.append(f"1D ارز خلاف {direction} است؛ 1D={s1d}.")
        elif s1d == direction:
            reasons.append(f"1D/4H/1H ارز در مسیر {direction} قرار دارد.")
        else:
            reasons.append("1D ارز خنثی است؛ فقط Normal نرم ممکن است، Real نه.")

        normal_ok = bool(btc_bias == direction and s4h == direction and s1h == direction and s1d != opposite)
        real_ok = bool(btc_bias == direction and s1d == direction and s4h == direction and s1h == direction)

        if real_ok:
            alignment = "FULL"
            reasons.append("تایید کامل جهت: BTC + 1D + 4H + 1H هم‌جهت هستند.")
        elif normal_ok:
            alignment = "GOOD"
            reasons.append("جهت برای Normal تایید است؛ 1D خلاف نیست ولی Real کامل نیست.")
        else:
            alignment = "BAD"
            reasons.append("Direction Gate رد شد؛ قبل از ورود، جهت کافی نیست.")

        return MarketContextResult(s1d, s4h, s1h, b1d, b4h, b1h, btc_bias, alignment, real_ok, normal_ok, tuple(reasons))

    @staticmethod
    def _aggregate_btc_bias(direction: Direction, b1d: Bias, b4h: Bias, b1h: Bias) -> Bias:
        counts = {"LONG": 0, "SHORT": 0}
        for item in (b1d, b4h, b1h):
            if item in counts:
                counts[item] += 1
        if counts[direction] >= 2:
            return direction
        opposite = "SHORT" if direction == "LONG" else "LONG"
        if counts[opposite] >= 2:
            return opposite
        return "NEUTRAL"

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
        if snapshot.plus_di > snapshot.minus_di * 1.05:
            long_strength += 1
        if snapshot.close < snapshot.ema50:
            short_strength += 1
        if snapshot.ema20 < snapshot.ema50:
            short_strength += 1
        if snapshot.ema50 < snapshot.ema200:
            short_strength += 1
        if snapshot.minus_di > snapshot.plus_di * 1.05:
            short_strength += 1
        if long_strength >= 3 and long_strength > short_strength:
            return "LONG"
        if short_strength >= 3 and short_strength > long_strength:
            return "SHORT"
        return "NEUTRAL"
