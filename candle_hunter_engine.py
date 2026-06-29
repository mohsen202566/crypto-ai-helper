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
    """30m entry candle brain for 1H signals."""

    def analyze(self, candles_30m: list[Candle], direction: Direction) -> CandleHunterResult:
        if len(candles_30m) < 84:
            return CandleHunterResult("NOISE", 0, ("کندل کافی برای تایم ورود 30m وجود ندارد.",))
        s = calculate_indicators(candles_30m)
        last = candles_30m[-1]
        prev = candles_30m[-2]
        atr = max(s.atr, s.close * 0.0001)
        body = abs(last.close - last.open)
        body_atr = body / atr
        range_atr = max(0.0, last.high - last.low) / atr
        reasons: list[str] = []
        points = 0

        bullish = last.close > last.open
        bearish = last.close < last.open
        green_reclaim = bullish and last.close >= max(prev.close, min(s.ema20, s.vwap, s.bb_mid) * 0.9990)
        red_rejection = bearish and last.close <= min(prev.close, max(s.ema20, s.vwap, s.bb_mid) * 1.0010)

        if direction == "LONG":
            impulse_ok = bullish and last.close >= prev.high * 0.9990
            wick_ok = s.upper_wick_pct <= 0.58
            macd_ok = s.macd_hist_slope > 0
            neutral_push = s.rsi >= 48 and s.rsi_delta > -0.10
            reversal_ok = (s.consecutive_down >= 1 or s.rsi <= 45 or s.lower_wick_pct >= 0.30) and green_reclaim and macd_ok
            band_reclaim = last.close > s.bb_mid and s.bb_position >= 0.45
        else:
            impulse_ok = bearish and last.close <= prev.low * 1.0010
            wick_ok = s.lower_wick_pct <= 0.58
            macd_ok = s.macd_hist_slope < 0
            neutral_push = s.rsi <= 52 and s.rsi_delta < 0.10
            reversal_ok = (s.consecutive_up >= 1 or s.rsi >= 55 or s.upper_wick_pct >= 0.30) and red_rejection and macd_ok
            band_reclaim = last.close < s.bb_mid and s.bb_position <= 0.55

        if reversal_ok:
            points += 14
            reasons.append("کندل 30m برگشت قابل استفاده برای ورود 1H نشان می‌دهد.")
            if 0.70 <= s.volume_ratio <= 3.60:
                points += 2
            return CandleHunterResult("REVERSAL_BUILDING", min(WEIGHTS.candle_entry, points), tuple(reasons))

        if range_atr > 2.30 and s.body_pct > 0.66 and s.volume_ratio > 3.80:
            points += 4
            reasons.append("کندل 30m خیلی بزرگ/کلایمکس است؛ حذف کامل نیست، فقط Real سخت‌تر می‌شود.")
            return CandleHunterResult("EXHAUSTION", min(WEIGHTS.candle_entry, points), tuple(reasons))

        if impulse_ok:
            points += 5; reasons.append("کندل 30m در جهت فرصت فشار دارد.")
        if 0.12 <= body_atr <= 1.80 and s.body_pct >= 0.28:
            points += 3; reasons.append("اندازه کندل 30m برای ورود 1H قابل استفاده است.")
        if wick_ok:
            points += 2
        if macd_ok:
            points += 3; reasons.append("کندل با تقویت MACD همراه است.")
        if neutral_push:
            points += 2; reasons.append("RSI تایم ورود علیه معامله نیست.")
        if band_reclaim:
            points += 2; reasons.append("Bollinger mid/position تایم ورود مناسب است.")
        if 0.65 <= s.volume_ratio <= 3.40:
            points += 2
        elif s.volume_ratio > 4.2:
            points -= 2; reasons.append("ولوم 30m خیلی انفجاری است؛ ریسک کلایمکس لحاظ شد.")

        if points >= 13:
            label: PatternLabel = "IGNITION_START"
            reasons.append("الگوی کندلی شروع/ادامه حرکت 1H تشخیص داده شد.")
        elif points >= 9:
            label = "POWER_BUILDING"
            reasons.append("قدرت در تایم ورود در حال ساخته‌شدن است.")
        elif points >= 5:
            label = "PRE_IGNITION_WATCH"
            reasons.append("کندل نزدیک شکار 1H است و بهتر است در Watch پیگیری شود.")
        else:
            label = "NOISE"
            reasons.append("کندل تایم ورود هنوز تمیز نیست؛ AI فقط نرم سخت‌تر می‌کند.")
        return CandleHunterResult(label, min(WEIGHTS.candle_entry, max(0, points)), tuple(reasons))
