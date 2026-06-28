from __future__ import annotations

from dataclasses import dataclass

from candle_hunter_engine import CandleHunterResult
from entry_stage_engine import EntryStageResult
from scorer import EntryState


@dataclass(frozen=True)
class IgnitionEntryResult:
    state: EntryState
    score: int
    reasons: tuple[str, ...]


class IgnitionEntryEngine:
    def analyze(self, candle: CandleHunterResult, stage: EntryStageResult) -> IgnitionEntryResult:
        reasons = list(candle.reasons) + list(stage.reasons)
        if candle.label in {"LATE_CHASE", "MID_MOVE", "EXHAUSTION", "PULLBACK"} or not stage.ok_for_real:
            return IgnitionEntryResult("LATE", max(0, candle.score + stage.score_bonus), tuple(reasons))
        if candle.label == "IGNITION_START":
            return IgnitionEntryResult("IGNITION_READY", min(20, candle.score + stage.score_bonus), tuple(reasons))
        if candle.label == "PRE_IGNITION_WATCH":
            return IgnitionEntryResult("PRE_WATCH", min(14, candle.score + max(0, stage.score_bonus)), tuple(reasons))
        return IgnitionEntryResult("NO_ENTRY", max(0, candle.score), tuple(reasons))
