from __future__ import annotations

from dataclasses import dataclass

from config import WEIGHTS
from indicators import calculate_indicators
from okx_data import Candle
from scorer import Direction, PatternLabel


@dataclass(frozen=True)
class CandleHunterResult:
    label: PatternLabel
    score: int
    reasons: tuple[str, ...]


class CandleHunterEngine:
    def analyze(self, candles_5m: list[Candle], direction: Direction) -> CandleHunterResult:
        if len(candles_5m) < 84:
            return CandleHunterResult("NOISE", 0, ("کندل کافی برای شکار وجود ندارد.",))
        s = calculate_indicators(candles_5m)
        last = candles_5m[-1]
        prev = candles_5m[-2]
        atr = max(s.atr, s.close * 0.0001)
        body = abs(last.close - last.open)
        body_atr = body / atr
        range_atr = max(0.0, last.high - last.low) / atr
        reasons: list[str] = []
        if direction == "LONG":
            same_push = s.consecutive_up
            candle_ok = last.close > last.open and last.close > prev.high
            wick_ok = s.upper_wick_pct <= 0.42
            rsi_ok = s.rsi <= 68
            macd_ok = s.macd_hist >= s.prev_macd_hist
        else:
            same_push = s.consecutive_down
            candle_ok = last.close < last.open and last.close < prev.low
            wick_ok = s.lower_wick_pct <= 0.42
            rsi_ok = s.rsi >= 32
            macd_ok = s.macd_hist <= s.prev_macd_hist
        if same_push >= 3:
            return CandleHunterResult("LATE_CHASE", 0, ("چند کندل هم‌جهت قبل از ورود دیده شد؛ ورود دیر است.",))
        if range_atr > 1.9 and s.body_pct > 0.65 and s.volume_ratio > 2.7:
            return CandleHunterResult("EXHAUSTION", 0, ("کندل بزرگ همراه حجم خیلی زیاد؛ احتمال خستگی/کلایمکس.",))
        if not rsi_ok:
            return CandleHunterResult("LATE_CHASE", 2, ("RSI برای شکار شروع حرکت کشیده شده است.",))
        points = 0
        if candle_ok:
            points += 7
            reasons.append("کندل 5m شکست میکرو و جهت درست دارد.")
        if 0.35 <= body_atr <= 1.25 and s.body_pct >= 0.45:
            points += 5
            reasons.append("اندازه کندل برای شروع حرکت مناسب است.")
        if wick_ok:
            points += 3
        if macd_ok:
            points += 3
        if 0.9 <= s.volume_ratio <= 2.4:
            points += 2
        if points >= 14:
            label: PatternLabel = "IGNITION_START"
            reasons.append("الگوی کندلی شروع حرکت تشخیص داده شد.")
        elif points >= 8:
            label = "PRE_IGNITION_WATCH"
            reasons.append("کندل نزدیک شروع است ولی تریگر کامل نیست.")
        else:
            label = "NOISE"
            reasons.append("کندل هنوز شکار قطعی نیست.")
        return CandleHunterResult(label, min(WEIGHTS.candle_entry, points), tuple(reasons))
