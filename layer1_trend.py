from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from indicators import snapshot
from okx_client import Candle

Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class TrendResult:
    passed: bool
    direction: Direction | None
    quality: str
    reason: str
    score: int


def _score_timeframe(candles: list[Candle]) -> tuple[int, int, str]:
    s = snapshot(candles)
    bull = 0
    bear = 0
    notes: list[str] = []

    if s.close > s.ema50 > s.ema200:
        bull += 2
        notes.append("ساختار صعودی")
    if s.close < s.ema50 < s.ema200:
        bear += 2
        notes.append("ساختار نزولی")

    if s.rsi14 >= 55:
        bull += 1
    elif s.rsi14 <= 45:
        bear += 1

    if s.macd_hist > 0:
        bull += 1
    elif s.macd_hist < 0:
        bear += 1

    if s.adx14 >= 20:
        if bull > bear:
            bull += 1
        elif bear > bull:
            bear += 1
    else:
        notes.append("قدرت روند کم")

    return bull, bear, "، ".join(notes)


def analyze_trend(*, daily: list[Candle], h4: list[Candle], h1: list[Candle]) -> TrendResult:
    d_bull, d_bear, d_note = _score_timeframe(daily)
    h4_bull, h4_bear, h4_note = _score_timeframe(h4)
    h1_bull, h1_bear, h1_note = _score_timeframe(h1)

    bull_score = d_bull + h4_bull * 2 + h1_bull * 2
    bear_score = d_bear + h4_bear * 2 + h1_bear * 2
    diff = abs(bull_score - bear_score)

    if bull_score >= 8 and diff >= 3:
        return TrendResult(True, "LONG", "قوی" if bull_score >= 10 else "معمولی", f"جهت خرید تایید شد. {d_note} {h4_note} {h1_note}", bull_score)
    if bear_score >= 8 and diff >= 3:
        return TrendResult(True, "SHORT", "قوی" if bear_score >= 10 else "معمولی", f"جهت فروش تایید شد. {d_note} {h4_note} {h1_note}", bear_score)

    return TrendResult(False, None, "رد", "جهت یا روند برای سیگنال یک‌ساعته کافی نیست.", max(bull_score, bear_score))
