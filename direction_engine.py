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
        if raw >= 52:
            state: DirectionState = "LONG"
        elif raw <= -52:
            state = "SHORT"
        else:
            state = "NEUTRAL"
        confidence = min(100, int(abs(raw)))
        if state == "NEUTRAL":
            score = min(12, int(confidence * 0.20))
            reasons.append("1H جهت واضح ندارد؛ real trade رد می‌شود.")
        else:
            score = int(round(14 + min(confidence, 100) / 100 * 11))
            score = min(WEIGHTS.direction, max(14, score))
        return DirectionResult(state, score, confidence, raw, tuple(reasons))

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
            score = 3
            reasons.append("4H با جهت سیگنال موافق است.")
        elif state == "NEUTRAL":
            score = 2
            reasons.append("4H خنثی است؛ رد کامل نمی‌شود.")
        else:
            score = 0
            reasons.append("4H خلاف جهت است؛ فقط امتیاز کم می‌شود.")
        return DirectionResult(state, score, confidence, raw, tuple(reasons))

    def _raw_strength(self, s: IndicatorSnapshot) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []
        atr_pct = s.atr_pct
        slope50 = s.ema50_slope_pct
        slope200 = s.ema200_slope_pct
        if s.close > s.ema50:
            score += 15
            reasons.append("قیمت بالای EMA50 است.")
        else:
            score -= 15
            reasons.append("قیمت پایین EMA50 است.")
        if s.ema20 > s.ema50:
            score += 10
        else:
            score -= 10
        if s.close > s.ema200:
            score += 8
        else:
            score -= 8
        slope_gate = max(0.00004, atr_pct * 0.02)
        if slope50 > slope_gate:
            score += 13
            reasons.append("شیب EMA50 مثبت است.")
        elif slope50 < -slope_gate:
            score -= 13
            reasons.append("شیب EMA50 منفی است.")
        else:
            reasons.append("EMA50 تقریباً صاف است.")
        if slope200 > 0:
            score += 4
        elif slope200 < 0:
            score -= 4
        di_gap = abs(s.plus_di - s.minus_di)
        if s.plus_di > s.minus_di and di_gap >= 2.5:
            score += 12
            reasons.append("+DI از -DI قوی‌تر است.")
        elif s.minus_di > s.plus_di and di_gap >= 2.5:
            score -= 12
            reasons.append("-DI از +DI قوی‌تر است.")
        if s.adx >= 16:
            add = min(9, int((s.adx - 14) / 2))
            if s.plus_di > s.minus_di:
                score += add
            elif s.minus_di > s.plus_di:
                score -= add
            reasons.append(f"ADX={s.adx:.1f} بازار را خیلی مرده نشان نمی‌دهد.")
        if 50 <= s.rsi <= 66:
            score += 7
        elif 34 <= s.rsi <= 50:
            score -= 7
        elif s.rsi > 74:
            score -= 4
        elif s.rsi < 26:
            score += 4
        if s.macd_hist > 0 and s.macd_hist >= s.prev_macd_hist:
            score += 8
        elif s.macd_hist < 0 and s.macd_hist <= s.prev_macd_hist:
            score -= 8
        return max(-100, min(100, score)), reasons
