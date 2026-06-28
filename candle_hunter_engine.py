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
            wick_ok = s.upper_wick_pct <= 0.48
            rsi_soft_ok = s.rsi <= 74
            rsi_sweet = 40 <= s.rsi <= 68
            macd_ok = s.macd_hist >= s.prev_macd_hist
        else:
            same_push = s.consecutive_down
            candle_ok = last.close < last.open and last.close < prev.low
            wick_ok = s.lower_wick_pct <= 0.48
            rsi_soft_ok = s.rsi >= 26
            rsi_sweet = 32 <= s.rsi <= 60
            macd_ok = s.macd_hist <= s.prev_macd_hist

        if range_atr > 2.5 and s.body_pct > 0.70 and s.volume_ratio > 3.2:
            return CandleHunterResult("EXHAUSTION", 0, ("کندل خیلی بزرگ با حجم غیرعادی؛ احتمال خستگی/کلایمکس.",))

        points = 0
        if candle_ok:
            points += 7
            reasons.append("کندل 5m شکست میکرو و جهت درست دارد.")
        if 0.22 <= body_atr <= 1.45 and s.body_pct >= 0.34:
            points += 5
            reasons.append("اندازه کندل برای شروع حرکت مناسب است.")
        if wick_ok:
            points += 3
        else:
            points -= 1
            reasons.append("ویک کندل کمی زیاد است؛ امتیاز ورود کم شد.")
        if macd_ok:
            points += 3
            reasons.append("MACD 5m هم‌جهت/در حال تقویت است.")
        if 0.70 <= s.volume_ratio <= 2.9:
            points += 2
            reasons.append("حجم برای شروع حرکت قابل قبول است.")
        if rsi_sweet:
            points += 2
        elif not rsi_soft_ok:
            points -= 3
            reasons.append("RSI از محدوده شروع حرکت فاصله گرفته؛ فقط امتیاز کم شد.")

        # Do not kill opportunities only because a few candles already moved.
        # Watchlist exists exactly to catch continuation of the first move.
        if same_push >= 6:
            points -= 6
            reasons.append("چند کندل هم‌جهت زیاد دیده شد؛ فقط با شکست تازه قابل قبول است.")
        elif same_push >= 3:
            points -= 2
            reasons.append("چند کندل هم‌جهت دیده شد؛ حساس است ولی حذف کامل نمی‌شود.")

        if range_atr > 2.0 and s.body_pct > 0.66 and s.volume_ratio > 2.7:
            points -= 4
            reasons.append("کندل بزرگ و پرحجم است؛ خطر انتهای موج، امتیاز کم شد.")

        points = max(0, points)
        micro_break = any("شکست میکرو" in reason for reason in reasons)

        if points >= 13 and micro_break and same_push <= 4:
            label: PatternLabel = "IGNITION_START"
            reasons.append("الگوی کندلی شروع حرکت/تریگر از واچ تشخیص داده شد.")
        elif points >= 6:
            label = "PRE_IGNITION_WATCH"
            reasons.append("شرایط کندلی برای واچ مناسب است؛ هنوز تریگر قطعی نیست.")
        elif same_push >= 6 or range_atr > 2.2:
            label = "MID_MOVE"
            reasons.append("حرکت از نقطه شکار فاصله گرفته؛ فقط برای یادگیری/واچ ضعیف مناسب است.")
        else:
            label = "NOISE"
            reasons.append("کندل هنوز شکار قطعی نیست.")

        return CandleHunterResult(label, min(WEIGHTS.candle_entry, points), tuple(reasons))
