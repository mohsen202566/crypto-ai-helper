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
            rsi_soft_ok = s.rsi <= 72
            rsi_sweet = 42 <= s.rsi <= 66
            macd_ok = s.macd_hist >= s.prev_macd_hist
        else:
            same_push = s.consecutive_down
            candle_ok = last.close < last.open and last.close < prev.low
            wick_ok = s.lower_wick_pct <= 0.42
            rsi_soft_ok = s.rsi >= 28
            rsi_sweet = 34 <= s.rsi <= 58
            macd_ok = s.macd_hist <= s.prev_macd_hist

        if range_atr > 2.3 and s.body_pct > 0.68 and s.volume_ratio > 3.0:
            return CandleHunterResult("EXHAUSTION", 0, ("کندل خیلی بزرگ با حجم غیرعادی؛ احتمال خستگی/کلایمکس.",))

        points = 0
        if candle_ok:
            points += 7
            reasons.append("کندل 5m شکست میکرو و جهت درست دارد.")
        if 0.28 <= body_atr <= 1.35 and s.body_pct >= 0.38:
            points += 5
            reasons.append("اندازه کندل برای شروع حرکت مناسب است.")
        if wick_ok:
            points += 3
        else:
            points -= 1
            reasons.append("ویک کندل کمی زیاد است؛ امتیاز ورود کم شد.")
        if macd_ok:
            points += 3
        if 0.75 <= s.volume_ratio <= 2.6:
            points += 2
        if rsi_sweet:
            points += 2
        elif not rsi_soft_ok:
            points -= 4
            reasons.append("RSI از محدوده شروع حرکت فاصله گرفته؛ فقط امتیاز کم شد.")

        if same_push >= 5:
            points -= 8
            reasons.append("چند کندل هم‌جهت زیاد دیده شد؛ برای real مناسب نیست.")
        elif same_push >= 3:
            points -= 4
            reasons.append("چند کندل هم‌جهت دیده شد؛ ورود حساس است ولی حذف کامل نمی‌شود.")

        if range_atr > 1.8 and s.body_pct > 0.65 and s.volume_ratio > 2.5:
            points -= 5
            reasons.append("کندل بزرگ و پرحجم است؛ خطر انتهای موج، امتیاز کم شد.")

        points = max(0, points)
        if points >= 14 and same_push <= 2:
            label: PatternLabel = "IGNITION_START"
            reasons.append("الگوی کندلی شروع حرکت تشخیص داده شد.")
        elif points >= 7:
            label = "PRE_IGNITION_WATCH"
            reasons.append("شرایط کندلی برای واچ مناسب است؛ هنوز تریگر قطعی نیست.")
        elif same_push >= 5 or range_atr > 2.0:
            label = "MID_MOVE"
            reasons.append("حرکت از نقطه شکار فاصله گرفته؛ برای real مناسب نیست.")
        else:
            label = "NOISE"
            reasons.append("کندل هنوز شکار قطعی نیست.")

        return CandleHunterResult(label, min(WEIGHTS.candle_entry, points), tuple(reasons))
