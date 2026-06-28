from __future__ import annotations

from pattern_memory import MemoryResult, PatternMemory
from scorer import Direction, PatternLabel


class LearningEngine:
    def __init__(self) -> None:
        self.memory = PatternMemory()

    def analyze(self, storage, symbol_name: str, direction: Direction, pattern: PatternLabel, rsi: float, adx: float, volume_ratio: float) -> MemoryResult:
        return self.memory.analyze(storage, symbol_name, direction, pattern, rsi, adx, volume_ratio)
