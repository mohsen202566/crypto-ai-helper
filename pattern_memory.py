from __future__ import annotations

from dataclasses import dataclass

from scorer import Direction, PatternLabel


@dataclass(frozen=True)
class MemoryResult:
    score: int
    confidence: int
    experience: int
    adjustment: int
    expected_move_pct: float | None
    reasons: tuple[str, ...]


class PatternMemory:
    def analyze(self, storage, symbol_name: str, direction: Direction, pattern: PatternLabel, rsi: float, adx: float, volume_ratio: float) -> MemoryResult:
        stats = storage.pattern_stats(symbol_name, direction, pattern, rsi, adx, volume_ratio)
        samples = int(stats.get("samples", 0))
        wr = float(stats.get("win_rate", 0.0))
        avg_mfe = float(stats.get("avg_mfe", 0.0))
        adjustment = 0
        if samples >= 100:
            adjustment = max(-10, min(8, int((wr - 50.0) / 5.0)))
        elif samples >= 50:
            adjustment = max(-6, min(6, int((wr - 50.0) / 8.0)))
        elif samples >= 20:
            adjustment = max(-3, min(3, int((wr - 50.0) / 12.0)))
        confidence = 50
        if samples >= 20:
            confidence = int(max(35, min(99, wr + min(15, samples // 10) + adjustment)))
        score = max(0, min(12, 6 + adjustment))
        expected = avg_mfe if samples >= 20 and avg_mfe > 0 else None
        reasons = [f"AI Experience={samples} نمونه، WR={wr:.1f}%"]
        if samples < 20:
            reasons.append("نمونه کافی نیست؛ اثر AI کم است.")
        else:
            reasons.append("AI بر اساس الگوی مشابه اثر واقعی داد.")
        return MemoryResult(score, confidence, samples, adjustment, expected, tuple(reasons))
