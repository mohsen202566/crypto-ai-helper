from __future__ import annotations

from ai_judge import AIJudge
from ai_pattern_memory import AIPatternMemory, PatternMemoryResult
from ai_range_memory import AIRangeMemory, RangeMemoryResult
from ai_shadow_tester import AIShadowTester
from scorer import Direction


class LearningEngine:
    def __init__(self) -> None:
        self.pattern_memory = AIPatternMemory()
        self.range_memory = AIRangeMemory()
        self.shadow = AIShadowTester()
        self.judge = AIJudge()

    def analyze_pattern(self, storage, *, symbol_name: str, direction: Direction, entry_quality: str, candle_pattern: str, market_mode: str, precision_pct: float) -> PatternMemoryResult:
        return self.pattern_memory.analyze(storage, symbol_name=symbol_name, direction=direction, entry_quality=entry_quality, candle_pattern=candle_pattern, market_mode=market_mode, precision_pct=precision_pct)

    def analyze_range(self, storage, *, symbol_name: str, direction: Direction, snapshot_5m, snapshot_15m, entry_quality: str, candle_pattern: str) -> RangeMemoryResult:
        return self.range_memory.analyze(storage, symbol_name=symbol_name, direction=direction, snapshot_5m=snapshot_5m, snapshot_15m=snapshot_15m, entry_quality=entry_quality, candle_pattern=candle_pattern)

    def register_shadow(self, storage, signal_id: int, *, direction: Direction, entry: float, tp: float, sl: float) -> None:
        self.shadow.register(storage, signal_id, direction=direction, entry=entry, tp=tp, sl=sl)

    def learn_from_closed_signal(self, storage, signal_id: int) -> None:
        signal = storage.signal_dict(signal_id)
        if not signal:
            return
        judgement = self.judge.judge_closed_signal(signal)
        storage.record_ai_judgement(signal_id, judgement)
        storage.update_ai_profiles_from_signal(signal_id, judgement)
        storage.update_shadow_tests(signal_id)
