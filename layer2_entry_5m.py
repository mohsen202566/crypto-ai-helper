from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from indicators import snapshot
from okx_client import Candle

Direction = Literal["LONG", "SHORT"]
EntryKind = Literal["پولبک", "شکست"]


@dataclass(frozen=True)
class EntryResult:
    passed: bool
    direction: Direction
    entry_price: float
    entry_kind: EntryKind | None
    reason: str


def find_entry(*, direction: Direction, m5: list[Candle]) -> EntryResult:
    s = snapshot(m5)
    last = m5[-1]
    prev = m5[-2]
    bullish_candle = last.close > last.open and last.close > prev.close
    bearish_candle = last.close < last.open and last.close < prev.close

    if direction == "LONG":
        pullback_zone = 38 <= s.rsi14 <= 52
        breakout_zone = 55 <= s.rsi14 <= 70
        near_dynamic_support = last.low <= max(s.ema20, s.ema50) * 1.003 and last.close >= s.ema20
        if pullback_zone and near_dynamic_support and bullish_candle:
            return EntryResult(True, direction, last.close, "پولبک", "ورود لانگ روی پولبک پنج دقیقه تایید شد.")
        if breakout_zone and last.close > s.resistance * 0.998 and bullish_candle and s.macd_hist > 0:
            return EntryResult(True, direction, last.close, "شکست", "ورود لانگ روی شکست پنج دقیقه تایید شد.")
    else:
        pullback_zone = 48 <= s.rsi14 <= 62
        breakout_zone = 30 <= s.rsi14 <= 45
        near_dynamic_resistance = last.high >= min(s.ema20, s.ema50) * 0.997 and last.close <= s.ema20
        if pullback_zone and near_dynamic_resistance and bearish_candle:
            return EntryResult(True, direction, last.close, "پولبک", "ورود شورت روی پولبک پنج دقیقه تایید شد.")
        if breakout_zone and last.close < s.support * 1.002 and bearish_candle and s.macd_hist < 0:
            return EntryResult(True, direction, last.close, "شکست", "ورود شورت روی شکست پنج دقیقه تایید شد.")

    return EntryResult(False, direction, last.close, None, "نقطه ورود پنج دقیقه تمیز نیست.")
