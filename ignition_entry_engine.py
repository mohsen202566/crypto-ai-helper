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

        if candle.label == "IGNITION_START" and stage.ok_for_real:
            return IgnitionEntryResult("IGNITION_READY", min(20, candle.score + stage.score_bonus), tuple(reasons))

        if candle.label == "PRE_IGNITION_WATCH":
            return IgnitionEntryResult("PRE_WATCH", min(16, candle.score + max(0, stage.score_bonus)), tuple(reasons))

        if not stage.ok_for_real and candle.score >= 7:
            reasons.append("مرحله حرکت برای real مناسب نیست، اما برای واچ/یادگیری حذف کامل نمی‌شود.")
            return IgnitionEntryResult("PRE_WATCH", min(12, candle.score), tuple(reasons))

        return IgnitionEntryResult("NO_ENTRY", max(0, candle.score + min(0, stage.score_bonus)), tuple(reasons))
