from __future__ import annotations

from dataclasses import dataclass

from config import WEIGHTS
from indicators import IndicatorSnapshot
from scorer import Direction, DirectionState


@dataclass(frozen=True)
class DirectionResult:
    state: DirectionState
    score: int
    confidence: int
    raw_strength: int
    reasons: tuple[str, ...]


class DirectionEngine:
    def analyze_1h(self, snapshot: IndicatorSnapshot) -> DirectionResult:
        raw, reasons = self._raw_strength(snapshot)
        if raw >= 55:
            state: DirectionState = "LONG"
        elif raw <= -55:
            state = "SHORT"
        else:
            state = "NEUTRAL"
        confidence = min(100, int(abs(raw)))
        if state == "NEUTRAL":
            score = min(24, int(confidence * 0.35))
            reasons = (*reasons, "1H جهت واضح ندارد؛ بازار خنثی یا نویزی است.")
        else:
            score = int(round(26 + (min(confidence, 100) / 100) * 14))
            score = min(WEIGHTS.direction_1h, max(25, score))
        return DirectionResult(state=state, score=score, confidence=confidence, raw_strength=raw, reasons=tuple(reasons))

    def analyze_4h_bias(self, snapshot: IndicatorSnapshot, direction: Direction) -> DirectionResult:
        raw, reasons = self._raw_strength(snapshot)
        if raw >= 45:
            state: DirectionState = "LONG"
        elif raw <= -45:
            state = "SHORT"
        else:
            state = "NEUTRAL"
        confidence = min(100, int(abs(raw)))
        if state == direction:
            score = WEIGHTS.bias_4h
            reasons = (*reasons, "4H با جهت سیگنال موافق است.")
        elif state == "NEUTRAL":
            score = max(2, WEIGHTS.bias_4h // 2)
            reasons = (*reasons, "4H خنثی است؛ رد کامل نمی‌شود.")
        else:
            score = 0
            reasons = (*reasons, "4H خلاف جهت است؛ فقط امتیاز جهت بزرگ‌تر حذف شد.")
        return DirectionResult(state=state, score=score, confidence=confidence, raw_strength=raw, reasons=tuple(reasons))

    def _raw_strength(self, s: IndicatorSnapshot) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []
        atr_pct = s.atr / s.close if s.close > 0 else 0.0
        slope50 = s.ema50_slope_pct
        slope200 = s.ema200_slope_pct

        if s.close > s.ema50:
            score += 16
            reasons.append("قیمت بالای EMA50 است.")
        else:
            score -= 16
            reasons.append("قیمت پایین EMA50 است.")

        if s.ema20 > s.ema50:
            score += 10
        else:
            score -= 10

        if s.close > s.ema200:
            score += 10
        else:
            score -= 10

        if slope50 > max(0.00005, atr_pct * 0.02):
            score += 14
            reasons.append("شیب EMA50 مثبت است.")
        elif slope50 < -max(0.00005, atr_pct * 0.02):
            score -= 14
            reasons.append("شیب EMA50 منفی است.")
        else:
            reasons.append("EMA50 تقریباً صاف است.")

        if slope200 > 0:
            score += 5
        elif slope200 < 0:
            score -= 5

        di_gap = abs(s.plus_di - s.minus_di)
        if s.plus_di > s.minus_di and di_gap >= 3:
            score += 12
            reasons.append("+DI از -DI قوی‌تر است.")
        elif s.minus_di > s.plus_di and di_gap >= 3:
            score -= 12
            reasons.append("-DI از +DI قوی‌تر است.")

        if s.adx >= 18:
            add = min(10, int((s.adx - 15) / 2))
            if s.plus_di > s.minus_di:
                score += add
            elif s.minus_di > s.plus_di:
                score -= add
            reasons.append(f"ADX={s.adx:.1f} نشان می‌دهد بازار خیلی مرده نیست.")
        else:
            reasons.append(f"ADX={s.adx:.1f} ضعیف است.")

        if 50 <= s.rsi <= 68:
            score += 8
        elif 32 <= s.rsi <= 50:
            score -= 8
        elif s.rsi > 72:
            score -= 4
        elif s.rsi < 28:
            score += 4

        if s.macd_hist > 0 and s.macd_hist >= s.prev_macd_hist:
            score += 7
        elif s.macd_hist < 0 and s.macd_hist <= s.prev_macd_hist:
            score -= 7

        # Very stretched price should reduce confidence because direction may be late.
        if s.atr > 0 and abs(s.close - s.ema20) > 2.2 * s.atr:
            score = int(score * 0.85)
            reasons.append("قیمت از EMA20 خیلی دور است؛ قدرت جهت کمی کم شد.")

        return max(-100, min(100, int(score))), reasons
