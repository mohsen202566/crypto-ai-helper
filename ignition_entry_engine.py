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

        if candle.label == "EXHAUSTION":
            return IgnitionEntryResult("NO_ENTRY", 0, tuple(reasons))

        if candle.label == "IGNITION_START":
            if stage.stage_pct <= 75:
                return IgnitionEntryResult("IGNITION_READY", min(20, max(12, candle.score + stage.score_bonus)), tuple(reasons))
            reasons.append("تریگر کندلی هست ولی مرحله حرکت خیلی جلو رفته؛ برای واچ نگه‌داری می‌شود.")
            return IgnitionEntryResult("PRE_WATCH", min(15, candle.score), tuple(reasons))

        if candle.label == "PRE_IGNITION_WATCH":
            # PRE_WATCH must remain a watch state, not an alert/signalling state.
            bonus = max(0, stage.score_bonus)
            return IgnitionEntryResult("PRE_WATCH", min(17, candle.score + bonus), tuple(reasons))

        if candle.label == "MID_MOVE" and candle.score >= 7:
            reasons.append("حرکت کامل شروع نشده یا از نقطه شکار فاصله دارد؛ فقط واچ ضعیف.")
            return IgnitionEntryResult("PRE_WATCH", min(10, candle.score), tuple(reasons))

        return IgnitionEntryResult("NO_ENTRY", max(0, candle.score + min(0, stage.score_bonus)), tuple(reasons))
