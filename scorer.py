from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from config import ACCEPT_SCORE, MIN_ADX, SL_PCT, TP_PCT
from indicators import IndicatorSnapshot

Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class ScoreBreakdown:
    rsi: int
    macd: int
    adx: int

    @property
    def total(self) -> int:
        return self.rsi + self.macd + self.adx


@dataclass(frozen=True)
class SignalDecision:
    accepted: bool
    direction: Direction | None
    score: int
    long_score: int
    short_score: int
    entry: float
    tp: float
    sl: float
    long_breakdown: ScoreBreakdown
    short_breakdown: ScoreBreakdown


class TechnicalScorer:
    def score(self, indicator: IndicatorSnapshot, entry_price: float) -> SignalDecision:
        long_breakdown = ScoreBreakdown(
            rsi=self._rsi_long(indicator.rsi, indicator.prev_rsi),
            macd=self._macd_long(indicator),
            adx=self._adx_score(indicator.adx),
        )
        short_breakdown = ScoreBreakdown(
            rsi=self._rsi_short(indicator.rsi, indicator.prev_rsi),
            macd=self._macd_short(indicator),
            adx=self._adx_score(indicator.adx),
        )
        long_score = long_breakdown.total if indicator.adx >= MIN_ADX else 0
        short_score = short_breakdown.total if indicator.adx >= MIN_ADX else 0
        direction: Direction | None = None
        score = 0
        if long_score >= ACCEPT_SCORE or short_score >= ACCEPT_SCORE:
            if long_score >= short_score:
                direction = "LONG"
                score = long_score
            else:
                direction = "SHORT"
                score = short_score
        tp, sl = self._prices(entry_price, direction)
        return SignalDecision(
            accepted=direction is not None,
            direction=direction,
            score=score,
            long_score=long_score,
            short_score=short_score,
            entry=entry_price,
            tp=tp,
            sl=sl,
            long_breakdown=long_breakdown,
            short_breakdown=short_breakdown,
        )

    def _rsi_long(self, value: float, previous: float) -> int:
        rising = value > previous
        if 56 <= value <= 68 and rising:
            return 30
        if 50 <= value < 56 and rising:
            return 24
        if 50 <= value <= 63:
            return 18
        if 68 < value <= 72 and rising:
            return 14
        if value > 72:
            return 8
        return 0

    def _rsi_short(self, value: float, previous: float) -> int:
        falling = value < previous
        if 32 <= value <= 44 and falling:
            return 30
        if 44 < value <= 50 and falling:
            return 24
        if 37 <= value <= 50:
            return 18
        if 28 <= value < 32 and falling:
            return 14
        if value < 28:
            return 8
        return 0

    def _macd_long(self, indicator: IndicatorSnapshot) -> int:
        score = 0
        if indicator.macd > indicator.macd_signal:
            score += 16
        if indicator.macd_hist > 0:
            score += 10
        if indicator.macd_hist > indicator.prev_macd_hist:
            score += 8
        crossed = indicator.prev_macd <= indicator.prev_macd_signal and indicator.macd > indicator.macd_signal
        improved = (indicator.macd - indicator.macd_signal) > (indicator.prev_macd - indicator.prev_macd_signal)
        if crossed or improved:
            score += 6
        return min(score, 40)

    def _macd_short(self, indicator: IndicatorSnapshot) -> int:
        score = 0
        if indicator.macd < indicator.macd_signal:
            score += 16
        if indicator.macd_hist < 0:
            score += 10
        if indicator.macd_hist < indicator.prev_macd_hist:
            score += 8
        crossed = indicator.prev_macd >= indicator.prev_macd_signal and indicator.macd < indicator.macd_signal
        improved = (indicator.macd - indicator.macd_signal) < (indicator.prev_macd - indicator.prev_macd_signal)
        if crossed or improved:
            score += 6
        return min(score, 40)

    def _adx_score(self, value: float) -> int:
        if value < MIN_ADX:
            return 0
        if value < 25:
            return 15
        if value < 35:
            return 25
        return 30

    def _prices(self, entry: float, direction: Direction | None) -> tuple[float, float]:
        if direction == "LONG":
            return entry * (1.0 + TP_PCT), entry * (1.0 - SL_PCT)
        if direction == "SHORT":
            return entry * (1.0 - TP_PCT), entry * (1.0 + SL_PCT)
        return entry, entry
