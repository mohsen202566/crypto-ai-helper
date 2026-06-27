from __future__ import annotations

from dataclasses import dataclass

from config import WEIGHTS
from indicators import IndicatorSnapshot
from scorer import Direction, DirectionState


@dataclass(frozen=True)
class SetupResult:
    state: DirectionState
    score: int
    reasons: tuple[str, ...]


class SetupEngine:
    def analyze(self, snapshot: IndicatorSnapshot, direction: Direction) -> SetupResult:
        points = 0
        reasons: list[str] = []
        if direction == "LONG":
            if snapshot.close >= snapshot.ema50:
                points += 4
            if snapshot.close >= snapshot.ema20 or snapshot.low <= snapshot.ema20 <= snapshot.high:
                points += 3
                reasons.append("15m نزدیک/بالای EMA20 است.")
            if 45 <= snapshot.rsi <= 67:
                points += 4
            if snapshot.macd_hist >= snapshot.prev_macd_hist:
                points += 3
            if snapshot.plus_di >= snapshot.minus_di:
                points += 2
            if snapshot.volume_ratio >= 0.7:
                points += 2
            state: DirectionState = "LONG" if points >= 10 else "NEUTRAL"
        else:
            if snapshot.close <= snapshot.ema50:
                points += 4
            if snapshot.close <= snapshot.ema20 or snapshot.low <= snapshot.ema20 <= snapshot.high:
                points += 3
                reasons.append("15m نزدیک/پایین EMA20 است.")
            if 33 <= snapshot.rsi <= 55:
                points += 4
            if snapshot.macd_hist <= snapshot.prev_macd_hist:
                points += 3
            if snapshot.minus_di >= snapshot.plus_di:
                points += 2
            if snapshot.volume_ratio >= 0.7:
                points += 2
            state = "SHORT" if points >= 10 else "NEUTRAL"
        score = min(WEIGHTS.setup_15m, int(points))
        if state == "NEUTRAL":
            reasons.append("15m تأیید کامل نیست ولی شرط سنگین هم نیست.")
        else:
            reasons.append("15m setup را تایید می‌کند.")
        return SetupResult(state=state, score=score, reasons=tuple(reasons))
