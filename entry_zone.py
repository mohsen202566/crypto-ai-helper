from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from indicators import IndicatorSnapshot

Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class EntryZoneResult:
    ok: bool
    status: str
    lower: float
    upper: float
    range_position: float
    reason: str


class EntryZoneEngine:
    """Finds whether 1H price is still in a good pullback zone.

    LONG must be near a 1H floor/pullback, not near the local high after the move.
    SHORT must be near a 1H ceiling/pullback, not near the local low after the move.
    """

    def analyze(self, snapshot: IndicatorSnapshot, direction: Direction, entry: float) -> EntryZoneResult:
        if entry <= 0:
            return EntryZoneResult(False, "NO_ENTRY_ZONE", 0.0, 0.0, 0.0, "قیمت ورود نامعتبر است.")
        high = max(snapshot.recent_high, snapshot.swing_high, snapshot.high)
        low = min(snapshot.recent_low, snapshot.swing_low, snapshot.low)
        width = max(high - low, entry * 0.000001)
        pos = (entry - low) / width
        atr = max(snapshot.atr, entry * 0.0005)

        if direction == "LONG":
            floor = max(low, min(snapshot.swing_low, snapshot.low))
            pullback_anchor = min(x for x in (snapshot.ema20, snapshot.ema50, snapshot.vwap) if x > 0)
            zone_lower = floor + atr * 0.05
            zone_upper = max(zone_lower, pullback_anchor + atr * 0.45)
            late = (
                pos >= 0.70
                or (entry - snapshot.ema20 > atr * 0.85 and entry - snapshot.vwap > atr * 0.85)
                or (snapshot.close > snapshot.open and snapshot.body_pct >= 0.64 and snapshot.volume_ratio >= 1.35)
                or snapshot.rsi >= 68
            )
            too_early = pos <= 0.06 and entry < min(snapshot.ema20, snapshot.ema50) - atr * 0.45
            if late:
                return EntryZoneResult(False, "LATE_ENTRY", zone_lower, zone_upper, pos, "جهت لانگ تایید است، اما حرکت شروع شده؛ قیمت نزدیک سقف/دور از کف 1H است.")
            if too_early:
                return EntryZoneResult(False, "TOO_EARLY", zone_lower, zone_upper, pos, "قیمت هنوز زیر ناحیه امن لانگ است؛ احتمال ادامه ریزش/شکستن کف وجود دارد.")
            if entry > zone_upper * 1.006:
                return EntryZoneResult(False, "LATE_ENTRY", zone_lower, zone_upper, pos, "قیمت بالاتر از بازه ورود لانگ است؛ ورود دنبال‌کردن کندل می‌شود.")
            return EntryZoneResult(True, "GOOD_ENTRY", zone_lower, zone_upper, pos, "ورود لانگ داخل ناحیه کف/پولبک 1H است و حرکت هنوز دیر نشده.")

        ceiling = min(high, max(snapshot.swing_high, snapshot.high))
        pullback_anchor = max(x for x in (snapshot.ema20, snapshot.ema50, snapshot.vwap) if x > 0)
        zone_upper = ceiling - atr * 0.05
        zone_lower = min(zone_upper, pullback_anchor - atr * 0.45)
        late = (
            pos <= 0.30
            or (snapshot.ema20 - entry > atr * 0.85 and snapshot.vwap - entry > atr * 0.85)
            or (snapshot.close < snapshot.open and snapshot.body_pct >= 0.64 and snapshot.volume_ratio >= 1.35)
            or snapshot.rsi <= 32
        )
        too_early = pos >= 0.94 and entry > max(snapshot.ema20, snapshot.ema50) + atr * 0.45
        if late:
            return EntryZoneResult(False, "LATE_ENTRY", zone_lower, zone_upper, pos, "جهت شورت تایید است، اما حرکت شروع شده؛ قیمت نزدیک کف/دور از سقف 1H است.")
        if too_early:
            return EntryZoneResult(False, "TOO_EARLY", zone_lower, zone_upper, pos, "قیمت هنوز بالای ناحیه امن شورت است؛ احتمال ادامه پامپ/شکستن سقف وجود دارد.")
        if entry < zone_lower * 0.994:
            return EntryZoneResult(False, "LATE_ENTRY", zone_lower, zone_upper, pos, "قیمت پایین‌تر از بازه ورود شورت است؛ ورود دنبال‌کردن ریزش می‌شود.")
        return EntryZoneResult(True, "GOOD_ENTRY", zone_lower, zone_upper, pos, "ورود شورت داخل ناحیه سقف/پولبک 1H است و حرکت هنوز دیر نشده.")
