from __future__ import annotations

from dataclasses import dataclass

from config import WEIGHTS
from indicators import IndicatorSnapshot
from scorer import Direction, EntryState


@dataclass(frozen=True)
class EntryResult:
    state: EntryState
    score: int
    reasons: tuple[str, ...]


class EntryEngine:
    def analyze(self, snapshot: IndicatorSnapshot, direction: Direction) -> EntryResult:
        points = 0
        reasons: list[str] = []
        atr = max(snapshot.atr, snapshot.close * 0.0001)
        distance_to_ema20 = abs(snapshot.close - snapshot.ema20) / atr
        distance_to_vwap = abs(snapshot.close - snapshot.vwap) / atr

        if distance_to_ema20 <= 0.9 or distance_to_vwap <= 0.9:
            points += 1
            reasons.append("5m ورود نزدیک EMA20/VWAP است.")
        if direction == "LONG":
            if snapshot.close >= snapshot.open:
                points += 1
            if snapshot.macd_hist >= snapshot.prev_macd_hist:
                points += 1
            if 38 <= snapshot.rsi <= 68:
                points += 1
        else:
            if snapshot.close <= snapshot.open:
                points += 1
            if snapshot.macd_hist <= snapshot.prev_macd_hist:
                points += 1
            if 32 <= snapshot.rsi <= 62:
                points += 1

        if points >= 3:
            state: EntryState = "READY"
        elif points >= 1:
            state = "WAIT"
        else:
            state = "BAD"
        score = min(WEIGHTS.entry_5m, points)
        if state == "READY":
            reasons.append("5m برای تایمینگ ورود مناسب است.")
        elif state == "WAIT":
            reasons.append("5m هنوز کامل نیست؛ فقط امتیاز کمی می‌گیرد.")
        else:
            reasons.append("5m تایمینگ خوبی نشان نمی‌دهد.")
        return EntryResult(state=state, score=score, reasons=tuple(reasons))
