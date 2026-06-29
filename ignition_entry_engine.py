from __future__ import annotations

from dataclasses import dataclass

from candle_hunter_engine import CandleHunterResult
from entry_precision_engine import EntryPrecisionResult
from scorer import EntryState


@dataclass(frozen=True)
class IgnitionEntryResult:
    state: EntryState
    score: int
    reasons: tuple[str, ...]


class IgnitionEntryEngine:
    def analyze(self, candle: CandleHunterResult, precision: EntryPrecisionResult) -> IgnitionEntryResult:
        reasons = list(candle.reasons) + list(precision.reasons)
        base_score = max(0, candle.score)
        if precision.state == "WAIT":
            return IgnitionEntryResult("PRECISION_WAIT", min(10, base_score), tuple(reasons))
        if candle.label == "REVERSAL_BUILDING":
            return IgnitionEntryResult("REVERSAL_BUILDING", min(16, base_score + 3), tuple(reasons))
        if candle.label == "IGNITION_START" and precision.precision_pct >= 78:
            return IgnitionEntryResult("EARLY_IGNITION", min(16, base_score + 2), tuple(reasons))
        if candle.label == "IGNITION_START":
            return IgnitionEntryResult("GOOD_ENTRY", min(15, base_score), tuple(reasons))
        if candle.label == "POWER_BUILDING":
            return IgnitionEntryResult("POWER_BUILDING", min(14, base_score), tuple(reasons))
        if candle.label == "EXHAUSTION":
            return IgnitionEntryResult("EXHAUSTION_RISK", min(8, base_score), tuple(reasons))
        if candle.label == "PRE_IGNITION_WATCH":
            return IgnitionEntryResult("PRECISION_WAIT", min(9, base_score), tuple(reasons))
        return IgnitionEntryResult("NO_ENTRY", max(0, min(6, candle.score)), tuple(reasons))
