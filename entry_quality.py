from __future__ import annotations

from dataclasses import dataclass

from candle_hunter_engine import CandleHunterResult
from entry_precision_engine import EntryPrecisionResult
from indicators import IndicatorSnapshot
from scorer import Direction


@dataclass(frozen=True)
class EntryQualityResult:
    quality: str
    ok_for_signal: bool
    score_bonus: int
    confidence: int
    reasons: tuple[str, ...]


class EntryQualityEngine:
    """Soft entry quality for 1H helper.

    snapshot_5m is kept as a compatibility parameter name but means 30m here.
    snapshot_15m means 1H here. Nothing analytical is a full reject.
    """

    def analyze(self, *, direction: Direction, snapshot_5m: IndicatorSnapshot, snapshot_15m: IndicatorSnapshot, candle: CandleHunterResult, precision: EntryPrecisionResult) -> EntryQualityResult:
        reasons: list[str] = []
        s30 = snapshot_5m
        s1h = snapshot_15m
        score = 0
        if direction == "LONG":
            rsi_power = (s1h.rsi > 50 and s1h.rsi_delta >= -0.20) or (s30.rsi >= 48 and s30.rsi_delta > 0.20)
            rsi_main_ok = s1h.rsi_delta > -0.20 or 45 <= s1h.rsi <= 67
            macd_start = s30.macd_hist_slope >= 0 and s1h.macd_hist_slope >= -abs(s1h.macd_hist) * 0.15
            di_ok = s1h.plus_di >= s1h.minus_di or s30.rsi_delta > 0.50
            price_ok = s30.close >= min(s30.ema20, s30.vwap, s30.bb_mid)
            trend_ok = s1h.close >= s1h.ema20 or s1h.ema20 >= s1h.ema50
            exhaustion_risk = s1h.rsi > 76 and s1h.rsi_delta < -0.35 and s1h.upper_wick_pct > 0.35
        else:
            rsi_power = (s1h.rsi < 50 and s1h.rsi_delta <= 0.20) or (s30.rsi <= 52 and s30.rsi_delta < -0.20)
            rsi_main_ok = s1h.rsi_delta < 0.20 or 33 <= s1h.rsi <= 55
            macd_start = s30.macd_hist_slope <= 0 and s1h.macd_hist_slope <= abs(s1h.macd_hist) * 0.15
            di_ok = s1h.minus_di >= s1h.plus_di or s30.rsi_delta < -0.50
            price_ok = s30.close <= max(s30.ema20, s30.vwap, s30.bb_mid)
            trend_ok = s1h.close <= s1h.ema20 or s1h.ema20 <= s1h.ema50
            exhaustion_risk = s1h.rsi < 24 and s1h.rsi_delta > 0.35 and s1h.lower_wick_pct > 0.35
        if rsi_power:
            score += 3; reasons.append("RSI 1H/30m در جهت فرصت فشار قابل قبول دارد.")
        if rsi_main_ok:
            score += 2; reasons.append("RSI 1H برای ادامه/شروع حرکت مناسب است.")
        if macd_start:
            score += 3; reasons.append("MACD در ساختار 1H/30m علیه فرصت نیست.")
        if di_ok:
            score += 2; reasons.append("DI یا شتاب 30m جهت را پشتیبانی می‌کند.")
        if price_ok:
            score += 2; reasons.append("قیمت 30m نسبت به EMA/VWAP/Bollinger موقعیت قابل اجرا دارد.")
        if trend_ok:
            score += 2; reasons.append("ساختار روند 1H با ورود تضاد جدی ندارد.")
        if 0.65 <= s30.volume_ratio <= 3.6 and 0.60 <= s1h.volume_ratio <= 3.4:
            score += 2; reasons.append("ولوم 1H/30m برای شکار یک‌ساعته قابل قبول است.")
        elif s30.volume_ratio > 4.4 or s1h.volume_ratio > 4.3:
            score -= 2; reasons.append("ولوم خیلی انفجاری است؛ ریسک کلایمکس در AI لحاظ شد.")
        atr_ratio = s1h.atr / max(s1h.prev_atr, s1h.close * 0.0001)
        if 0.70 <= atr_ratio <= 2.15:
            score += 1
        elif atr_ratio > 2.50:
            score -= 2; reasons.append("ATR 1H خیلی باز شده؛ AI باید محتاط‌تر TP/SL بچیند.")
        if s1h.bb_width_pct < max(s1h.atr_pct * 2.35, 0.010) and s1h.volume_ratio >= 0.75:
            score += 1; reasons.append("Bollinger squeeze 1H ممکن است قبل حرکت باشد.")
        if precision.state == "READY":
            score += min(4, max(1, precision.score // 2))
        elif precision.state == "WATCH":
            score += 1
        elif precision.state == "WAIT":
            return EntryQualityResult("PRECISION_WAIT", False, -2, precision.confidence, tuple(reasons + list(precision.reasons)))
        if exhaustion_risk and candle.label != "REVERSAL_BUILDING":
            return EntryQualityResult("EXHAUSTION_RISK", False, -2, 45, tuple(reasons + ["ریسک مصرف‌شدن حرکت 1H دیده شد؛ حذف کامل نیست، AI فقط سخت‌تر می‌کند."]))
        if candle.label == "REVERSAL_BUILDING" and score >= 8:
            return EntryQualityResult("REVERSAL_BUILDING", True, min(7, score), 84, tuple(reasons + ["برگشت ساختاری 30m/1H قابل شکار است."]))
        if candle.label == "IGNITION_START" and score >= 11 and precision.precision_pct >= 76:
            return EntryQualityResult("EARLY_IGNITION", True, min(8, score), 90, tuple(reasons + ["AI ورود شروع حرکت 1H را تایید کرد."]))
        if candle.label == "IGNITION_START" and score >= 9:
            return EntryQualityResult("GOOD_ENTRY", True, min(7, score), 80, tuple(reasons + ["AI ورود 1H را قابل اجرا می‌داند."]))
        if candle.label == "POWER_BUILDING" and score >= 7:
            return EntryQualityResult("POWER_BUILDING", True, min(6, score), 74, tuple(reasons + ["قدرت در حال ساخت است و AI آماده سیگنال یک‌ساعته است."]))
        if score >= 6:
            return EntryQualityResult("WEAK_MOVEMENT", False, max(0, min(4, score)), 54, tuple(reasons + ["حرکت قابل مشاهده است اما AI هنوز آن را نرم‌تر/سخت‌تر یاد می‌گیرد."]))
        return EntryQualityResult("NO_ENTRY", False, 0, 30, tuple(reasons + ["AI ورود یک‌ساعته را هنوز تایید نکرد."]))
