from __future__ import annotations

from dataclasses import dataclass

from config import WEIGHTS
from indicators import IndicatorSnapshot
from scorer import Direction


@dataclass(frozen=True)
class PreIgnitionResult:
    score: int
    confidence: int
    reasons: tuple[str, ...]


class PreIgnitionEngine:
    """1H setup-readiness brain.

    It searches for conditions before a 1H move: compression, structure alignment,
    ATR expansion starting, volume pressure, and 30m timing.
    """

    def analyze_1h_setup(self, snapshot_1h: IndicatorSnapshot, snapshot_30m: IndicatorSnapshot, direction: Direction) -> PreIgnitionResult:
        s1h = snapshot_1h
        s30 = snapshot_30m
        points = 0
        reasons: list[str] = []
        atr_ratio = s1h.atr / max(s1h.prev_atr, s1h.close * 0.0001)
        squeeze = s1h.bb_width_pct < max(s1h.atr_pct * 2.35, 0.010)
        vol_ok = 0.70 <= max(s1h.volume_ratio, s30.volume_ratio) <= 3.60
        if squeeze and s1h.volume_ratio >= 0.80:
            points += 4; reasons.append("فشردگی Bollinger قبل از حرکت 1H دیده می‌شود.")
        if 0.80 <= atr_ratio <= 1.85:
            points += 2; reasons.append("ATR 1H در محدوده سالم برای شروع/ادامه حرکت است.")
        elif atr_ratio > 2.30:
            points -= 2; reasons.append("ATR 1H بیش از حد باز شده؛ AI فقط سخت‌تر می‌کند.")
        if vol_ok:
            points += 2; reasons.append("ولوم برای تایم 1H قابل استفاده است.")
        elif s1h.volume_ratio > 4.2:
            points -= 2; reasons.append("ولوم 1H کلایمکس محتمل دارد.")

        if direction == "LONG":
            if s1h.rsi_delta > 0 or 48 <= s1h.rsi <= 64:
                points += 3; reasons.append("RSI 1H لانگ را رد نمی‌کند و رو به بهبود است.")
            if s30.rsi_delta > 0 and s30.close >= min(s30.ema20, s30.vwap):
                points += 3; reasons.append("30m برای ورود لانگ آماده‌تر شده است.")
            if s1h.macd_hist_slope > 0:
                points += 3; reasons.append("MACD 1H در حال تقویت لانگ است.")
            if s1h.close >= s1h.ema20 or s1h.close >= s1h.bb_mid:
                points += 2; reasons.append("قیمت 1H موقعیت ساختاری قابل قبول برای لانگ دارد.")
        else:
            if s1h.rsi_delta < 0 or 36 <= s1h.rsi <= 52:
                points += 3; reasons.append("RSI 1H شورت را رد نمی‌کند و رو به ضعف است.")
            if s30.rsi_delta < 0 and s30.close <= max(s30.ema20, s30.vwap):
                points += 3; reasons.append("30m برای ورود شورت آماده‌تر شده است.")
            if s1h.macd_hist_slope < 0:
                points += 3; reasons.append("MACD 1H در حال تقویت شورت است.")
            if s1h.close <= s1h.ema20 or s1h.close <= s1h.bb_mid:
                points += 2; reasons.append("قیمت 1H موقعیت ساختاری قابل قبول برای شورت دارد.")

        score = max(0, min(WEIGHTS.pre_ignition, points))
        confidence = max(25, min(92, 35 + score * 5))
        if not reasons:
            reasons.append("نشانه قوی قبل حرکت 1H هنوز کامل نیست؛ AI فقط Watch می‌کند.")
        return PreIgnitionResult(score, confidence, tuple(reasons))

    # Compatibility wrapper: in the 1H bot, snapshot_15m means 1H and snapshot_5m means 30m.
    def analyze(self, snapshot_15m: IndicatorSnapshot, snapshot_5m: IndicatorSnapshot, direction: Direction) -> PreIgnitionResult:
        return self.analyze_1h_setup(snapshot_15m, snapshot_5m, direction)
