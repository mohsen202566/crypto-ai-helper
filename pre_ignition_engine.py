from __future__ import annotations

from dataclasses import dataclass

from config import WEIGHTS
from indicators import IndicatorSnapshot
from scorer import Direction, DirectionState


@dataclass(frozen=True)
class PreIgnitionResult:
    state: DirectionState
    score: int
    confidence: int
    reasons: tuple[str, ...]


class PreIgnitionEngine:
    def analyze(self, snapshot_15m: IndicatorSnapshot, snapshot_5m: IndicatorSnapshot, direction: Direction) -> PreIgnitionResult:
        points = 0
        reasons: list[str] = []
        s15 = snapshot_15m
        s5 = snapshot_5m
        if direction == "LONG":
            if 46 <= s15.rsi <= 64:
                points += 4
                reasons.append("RSI 15m در محدوده شروع قدرت لانگ است.")
            if s15.macd_hist >= s15.prev_macd_hist:
                points += 4
                reasons.append("MACD 15m تازه در حال تقویت است.")
            if 14 <= s15.adx <= 32 and s15.plus_di >= s15.minus_di:
                points += 4
                reasons.append("ADX/DI 15m شروع قدرت را تأیید می‌کند.")
            if 0.75 <= s15.volume_ratio <= 2.2:
                points += 3
            if s5.macd_hist >= s5.prev_macd_hist and s5.rsi <= 68:
                points += 3
            if s5.close >= s5.ema20 or s5.low <= s5.ema20 <= s5.high:
                points += 2
        else:
            if 36 <= s15.rsi <= 54:
                points += 4
                reasons.append("RSI 15m در محدوده شروع قدرت شورت است.")
            if s15.macd_hist <= s15.prev_macd_hist:
                points += 4
                reasons.append("MACD 15m تازه در حال تقویت شورت است.")
            if 14 <= s15.adx <= 32 and s15.minus_di >= s15.plus_di:
                points += 4
                reasons.append("ADX/DI 15m شروع قدرت شورت را تأیید می‌کند.")
            if 0.75 <= s15.volume_ratio <= 2.2:
                points += 3
            if s5.macd_hist <= s5.prev_macd_hist and s5.rsi >= 32:
                points += 3
            if s5.close <= s5.ema20 or s5.low <= s5.ema20 <= s5.high:
                points += 2
        if s15.volume_ratio > 3.2 or s5.volume_ratio > 3.5:
            points -= 4
            reasons.append("حجم خیلی انفجاری است؛ احتمال کلایمکس یا نوسان غیرعادی.")
        score = max(0, min(WEIGHTS.pre_ignition, points))
        state: DirectionState = direction if score >= 12 else "NEUTRAL"
        confidence = int(min(100, score / max(1, WEIGHTS.pre_ignition) * 100))
        if state == "NEUTRAL":
            reasons.append("پیش‌قدرت کافی برای شکار کامل نیست.")
        else:
            reasons.append("پیش‌قدرت شکار فعال است.")
        return PreIgnitionResult(state, score, confidence, tuple(reasons))
