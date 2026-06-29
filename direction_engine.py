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
    raw: int
    reasons: tuple[str, ...]


class DirectionEngine:
    """1H direction brain.

    1H is the main setup timeframe, 4H is context, and 30m is entry timing.
    This brain is soft: neutral/weak states reduce confidence and push Watch, not a hard analytical reject.
    """

    def analyze_1h_scalp(self, snapshot_1h: IndicatorSnapshot, snapshot_30m: IndicatorSnapshot, snapshot_4h: IndicatorSnapshot | None = None) -> DirectionResult:
        long_raw, long_reasons = self._one_hour_side_strength(snapshot_1h, snapshot_30m, snapshot_4h, "LONG")
        short_raw, short_reasons = self._one_hour_side_strength(snapshot_1h, snapshot_30m, snapshot_4h, "SHORT")
        diff = abs(long_raw - short_raw)
        if long_raw >= 18 and long_raw >= short_raw + 4:
            score = min(WEIGHTS.direction, 8 + diff // 2 + long_raw // 5)
            confidence = min(96, 52 + diff * 2 + long_raw)
            return DirectionResult("LONG", score, confidence, int(long_raw), tuple(long_reasons + ["1H جهت لانگ را با تایید 30m/4H قوی‌تر نشان می‌دهد."]))
        if short_raw >= 18 and short_raw >= long_raw + 4:
            score = min(WEIGHTS.direction, 8 + diff // 2 + short_raw // 5)
            confidence = min(96, 52 + diff * 2 + short_raw)
            return DirectionResult("SHORT", score, confidence, int(-short_raw), tuple(short_reasons + ["1H جهت شورت را با تایید 30m/4H قوی‌تر نشان می‌دهد."]))
        reasons = long_reasons[:3] + short_reasons[:3] + ["1H هنوز جهت غالب کاملاً جدا نکرده؛ AI فقط Watch/Internal می‌کند."]
        return DirectionResult("NEUTRAL", min(WEIGHTS.direction, max(long_raw, short_raw) // 3), 35, int(long_raw - short_raw), tuple(reasons))

    # Backward-compatible name. In the 1H bot it means 1H+30m analysis.
    def analyze_15m_scalp(self, snapshot_15m: IndicatorSnapshot, snapshot_5m: IndicatorSnapshot) -> DirectionResult:
        return self.analyze_1h_scalp(snapshot_15m, snapshot_5m, None)

    def analyze_1h_context(self, snapshot_1h: IndicatorSnapshot, direction: Direction) -> DirectionResult:
        points = 0
        reasons: list[str] = []
        if direction == "LONG":
            if snapshot_1h.close >= snapshot_1h.ema20:
                points += 4; reasons.append("1H قیمت بالای EMA20 است.")
            if snapshot_1h.ema20 >= snapshot_1h.ema50:
                points += 4; reasons.append("1H EMA20 بالای EMA50 است.")
            if snapshot_1h.macd_hist_slope > 0:
                points += 3; reasons.append("1H MACD در حال تقویت لانگ است.")
            if snapshot_1h.plus_di >= snapshot_1h.minus_di:
                points += 3; reasons.append("1H +DI دست بالا را دارد.")
        else:
            if snapshot_1h.close <= snapshot_1h.ema20:
                points += 4; reasons.append("1H قیمت زیر EMA20 است.")
            if snapshot_1h.ema20 <= snapshot_1h.ema50:
                points += 4; reasons.append("1H EMA20 زیر EMA50 است.")
            if snapshot_1h.macd_hist_slope < 0:
                points += 3; reasons.append("1H MACD در حال تقویت شورت است.")
            if snapshot_1h.minus_di >= snapshot_1h.plus_di:
                points += 3; reasons.append("1H -DI دست بالا را دارد.")
        if snapshot_1h.adx >= 18:
            points += 2; reasons.append("ADX 1H روند/حرکت قابل استفاده نشان می‌دهد.")
        state: DirectionState = direction if points >= 7 else "NEUTRAL"
        return DirectionResult(state, min(WEIGHTS.direction, points), min(90, 35 + points * 5), points, tuple(reasons or ["1H فعلاً خنثی است؛ قفل ورود نیست."]))

    def analyze_4h_bias(self, snapshot_4h: IndicatorSnapshot, direction: Direction) -> DirectionResult:
        points = 0
        reasons: list[str] = []
        if direction == "LONG":
            if snapshot_4h.close >= snapshot_4h.ema50:
                points += 4; reasons.append("4H قیمت بالای EMA50 است.")
            if snapshot_4h.ema20_slope_pct >= -0.0004:
                points += 3; reasons.append("4H شیب EMA20 علیه لانگ نیست.")
            if snapshot_4h.macd_hist_slope >= 0 or snapshot_4h.rsi >= 48:
                points += 3; reasons.append("4H مومنتوم کلی لانگ را کامل رد نمی‌کند.")
        else:
            if snapshot_4h.close <= snapshot_4h.ema50:
                points += 4; reasons.append("4H قیمت زیر EMA50 است.")
            if snapshot_4h.ema20_slope_pct <= 0.0004:
                points += 3; reasons.append("4H شیب EMA20 علیه شورت نیست.")
            if snapshot_4h.macd_hist_slope <= 0 or snapshot_4h.rsi <= 52:
                points += 3; reasons.append("4H مومنتوم کلی شورت را کامل رد نمی‌کند.")
        state: DirectionState = direction if points >= 6 else "NEUTRAL"
        return DirectionResult(state, min(WEIGHTS.direction, points), min(88, 35 + points * 6), points, tuple(reasons or ["4H خنثی است؛ فقط Real را محتاط‌تر می‌کند."]))

    def _one_hour_side_strength(self, s1h: IndicatorSnapshot, s30: IndicatorSnapshot, s4h: IndicatorSnapshot | None, direction: Direction) -> tuple[int, list[str]]:
        pts = 0
        reasons: list[str] = []
        if direction == "LONG":
            if s1h.close > s1h.ema20:
                pts += 5; reasons.append("1H بالای EMA20 است.")
            if s1h.ema20 >= s1h.ema50:
                pts += 5; reasons.append("EMA20/50 در 1H به نفع لانگ است.")
            if s1h.close >= s1h.ema200 or s1h.ema50_slope_pct > 0:
                pts += 3; reasons.append("ساختار EMA200/EMA50 برای لانگ قابل قبول است.")
            if s1h.rsi >= 50 and s1h.rsi_delta >= -0.25:
                pts += 4; reasons.append("RSI 1H قدرت لانگ را حفظ کرده است.")
            if s30.rsi_delta > 0 or s30.rsi >= 52:
                pts += 3; reasons.append("30m ورود لانگ را پشتیبانی می‌کند.")
            if s1h.macd_hist_slope > 0 or s1h.macd_hist > 0:
                pts += 4; reasons.append("MACD 1H به نفع لانگ است.")
            if s1h.plus_di >= s1h.minus_di:
                pts += 3; reasons.append("+DI در 1H دست بالا را دارد.")
            if s1h.close >= s1h.vwap:
                pts += 2; reasons.append("قیمت بالای VWAP 24 کندلی است.")
            if s1h.bb_position >= 0.45 or s1h.close > s1h.bb_mid:
                pts += 2; reasons.append("جایگاه Bollinger برای لانگ بد نیست.")
            if s4h and s4h.close >= s4h.ema50:
                pts += 3; reasons.append("4H خلاف لانگ نیست.")
            if s1h.rsi > 76 and s1h.upper_wick_pct > 0.35:
                pts -= 3; reasons.append("لانگ ممکن است دیر/مصرف‌شده باشد؛ AI فقط نرم سخت‌تر می‌کند.")
        else:
            if s1h.close < s1h.ema20:
                pts += 5; reasons.append("1H زیر EMA20 است.")
            if s1h.ema20 <= s1h.ema50:
                pts += 5; reasons.append("EMA20/50 در 1H به نفع شورت است.")
            if s1h.close <= s1h.ema200 or s1h.ema50_slope_pct < 0:
                pts += 3; reasons.append("ساختار EMA200/EMA50 برای شورت قابل قبول است.")
            if s1h.rsi <= 50 and s1h.rsi_delta <= 0.25:
                pts += 4; reasons.append("RSI 1H ضعف/فشار شورت را حفظ کرده است.")
            if s30.rsi_delta < 0 or s30.rsi <= 48:
                pts += 3; reasons.append("30m ورود شورت را پشتیبانی می‌کند.")
            if s1h.macd_hist_slope < 0 or s1h.macd_hist < 0:
                pts += 4; reasons.append("MACD 1H به نفع شورت است.")
            if s1h.minus_di >= s1h.plus_di:
                pts += 3; reasons.append("-DI در 1H دست بالا را دارد.")
            if s1h.close <= s1h.vwap:
                pts += 2; reasons.append("قیمت زیر VWAP 24 کندلی است.")
            if s1h.bb_position <= 0.55 or s1h.close < s1h.bb_mid:
                pts += 2; reasons.append("جایگاه Bollinger برای شورت بد نیست.")
            if s4h and s4h.close <= s4h.ema50:
                pts += 3; reasons.append("4H خلاف شورت نیست.")
            if s1h.rsi < 24 and s1h.lower_wick_pct > 0.35:
                pts -= 3; reasons.append("شورت ممکن است دیر/مصرف‌شده باشد؛ AI فقط نرم سخت‌تر می‌کند.")

        if 17 <= s1h.adx <= 48:
            pts += 3; reasons.append("ADX 1H برای حرکت یک‌ساعته قابل استفاده است.")
        elif s1h.adx < 13:
            pts -= 2; reasons.append("ADX 1H کم‌جان است؛ AI بیشتر Watch می‌کند.")
        if 0.70 <= s1h.volume_ratio <= 3.20:
            pts += 2; reasons.append("ولوم 1H طبیعی/قابل استفاده است.")
        elif s1h.volume_ratio > 4.2:
            pts -= 2; reasons.append("ولوم 1H خیلی انفجاری است؛ ریسک کلایمکس لحاظ شد.")
        if s1h.bb_width_pct < s1h.atr_pct * 2.2 and s1h.volume_ratio >= 0.85:
            pts += 2; reasons.append("Bollinger squeeze/فشردگی می‌تواند قبل حرکت باشد.")
        return max(0, pts), reasons
