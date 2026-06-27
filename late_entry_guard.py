from __future__ import annotations

from dataclasses import dataclass

from config import WEIGHTS
from indicators import IndicatorSnapshot
from scorer import Direction


@dataclass(frozen=True)
class LateEntryResult:
    ok: bool
    score: int
    reasons: tuple[str, ...]


class LateEntryGuard:
    def check(self, snapshot: IndicatorSnapshot, direction: Direction) -> LateEntryResult:
        atr = max(snapshot.atr, snapshot.close * 0.0001)
        warnings: list[str] = []
        hard = False
        score = WEIGHTS.late_entry

        distance_ema = abs(snapshot.close - snapshot.ema20) / atr
        distance_vwap = abs(snapshot.close - snapshot.vwap) / atr
        stretched = min(distance_ema, distance_vwap)
        if stretched > 1.9:
            warnings.append("قیمت از EMA20/VWAP خیلی دور شده است.")
            score -= 5
            hard = True
        elif stretched > 1.2:
            warnings.append("قیمت کمی کشیده شده است.")
            score -= 2

        candle_range_atr = snapshot.candle_range / atr
        if candle_range_atr > 1.7 and snapshot.body_pct > 0.62:
            warnings.append("کندل ورود نسبت به ATR بزرگ است.")
            score -= 4
            hard = True
        elif candle_range_atr > 1.2:
            warnings.append("کندل ورود کمی بزرگ است.")
            score -= 2

        if direction == "LONG":
            if snapshot.rsi >= 72:
                warnings.append("RSI برای لانگ کشیده است.")
                score -= 4
                hard = True
            elif snapshot.rsi >= 68:
                warnings.append("RSI لانگ نزدیک محدوده خستگی است.")
                score -= 2
            if snapshot.consecutive_up >= 3 and stretched > 0.9:
                warnings.append("چند کندل پشت سر هم بالا رفته؛ خطر ورود آخر حرکت.")
                score -= 4
                hard = True
            if snapshot.macd_hist < snapshot.prev_macd_hist and snapshot.rsi > 62:
                warnings.append("MACD در لانگ در حال ضعیف شدن است.")
                score -= 2
        else:
            if snapshot.rsi <= 28:
                warnings.append("RSI برای شورت کشیده است.")
                score -= 4
                hard = True
            elif snapshot.rsi <= 32:
                warnings.append("RSI شورت نزدیک محدوده خستگی است.")
                score -= 2
            if snapshot.consecutive_down >= 3 and stretched > 0.9:
                warnings.append("چند کندل پشت سر هم پایین رفته؛ خطر ورود آخر حرکت.")
                score -= 4
                hard = True
            if snapshot.macd_hist > snapshot.prev_macd_hist and snapshot.rsi < 38:
                warnings.append("MACD در شورت در حال ضعیف شدن است.")
                score -= 2

        score = max(0, min(WEIGHTS.late_entry, score))
        if not warnings:
            warnings.append("ورود دیر نیست.")
        return LateEntryResult(ok=not hard, score=score, reasons=tuple(warnings))
