from __future__ import annotations

from dataclasses import dataclass

from config import AI_MIN_SAMPLES_MEDIUM, AI_MIN_SAMPLES_VALID, WEIGHTS
from scorer import Direction


@dataclass(frozen=True)
class PatternMemoryResult:
    pattern_id: str
    score: int
    confidence: int
    experience: int
    adjustment: int
    expected_move_pct: float | None
    expected_mae_pct: float | None
    verdict: str
    reasons: tuple[str, ...]


class AIPatternMemory:
    def make_pattern_id(self, *, direction: Direction, entry_quality: str, candle_pattern: str, market_mode: str, precision_bucket: str) -> str:
        return "|".join((direction, entry_quality, candle_pattern, market_mode, precision_bucket))

    def analyze(self, storage, *, symbol_name: str, direction: Direction, entry_quality: str, candle_pattern: str, market_mode: str, precision_pct: float) -> PatternMemoryResult:
        precision_bucket = self._precision_bucket(precision_pct)
        pattern_id = self.make_pattern_id(direction=direction, entry_quality=entry_quality, candle_pattern=candle_pattern, market_mode=market_mode, precision_bucket=precision_bucket)
        stats = storage.ai_pattern_stats(symbol_name, direction, pattern_id)
        samples = int(stats.get("samples", 0))
        wr = float(stats.get("win_rate", 0.0))
        avg_mfe = float(stats.get("avg_mfe", 0.0))
        avg_mae = float(stats.get("avg_mae", 0.0))
        adjustment = 0
        verdict = "NEUTRAL"
        confidence = 45
        if samples >= AI_MIN_SAMPLES_VALID:
            adjustment = max(-8, min(8, int((wr - 50.0) / 5.0)))
            confidence = int(max(25, min(99, wr + min(18, samples // 6))))
            verdict = "POSITIVE" if wr >= 60 else "NEGATIVE" if wr <= 40 else "NEUTRAL"
        elif samples >= AI_MIN_SAMPLES_MEDIUM:
            adjustment = max(-4, min(4, int((wr - 50.0) / 8.0)))
            confidence = int(max(35, min(88, wr + samples // 2)))
            verdict = "POSITIVE" if wr >= 64 else "NEGATIVE" if wr <= 36 else "NEUTRAL"
        else:
            confidence = 40 + min(10, samples)
        base = 8 if samples < AI_MIN_SAMPLES_MEDIUM else 10
        score = max(0, min(WEIGHTS.ai_memory, base + adjustment))
        reasons = [f"Pattern Memory: {samples} نمونه مشابه، WR={wr:.1f}%، verdict={verdict}"]
        return PatternMemoryResult(pattern_id, score, confidence, samples, adjustment, avg_mfe or None, avg_mae or None, verdict, tuple(reasons))

    @staticmethod
    def _precision_bucket(value: float) -> str:
        if value >= 82:
            return "precision_high"
        if value >= 65:
            return "precision_good"
        if value >= 42:
            return "precision_watch"
        return "precision_wait"
