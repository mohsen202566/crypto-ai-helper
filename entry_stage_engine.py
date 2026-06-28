from __future__ import annotations

from dataclasses import dataclass

from indicators import IndicatorSnapshot
from scorer import Direction


@dataclass(frozen=True)
class EntryStageResult:
    stage_pct: float
    ok_for_real: bool
    score_bonus: int
    reasons: tuple[str, ...]


class EntryStageEngine:
    def analyze(self, snapshot: IndicatorSnapshot, direction: Direction) -> EntryStageResult:
        atr = max(snapshot.atr, snapshot.close * 0.0001)
        if direction == "LONG":
            move_from_base = max(0.0, snapshot.close - min(snapshot.ema20, snapshot.vwap, snapshot.recent_low))
        else:
            move_from_base = max(0.0, max(snapshot.ema20, snapshot.vwap, snapshot.recent_high) - snapshot.close)
        stage_pct = min(100.0, (move_from_base / max(atr * 4.0, snapshot.close * 0.001)) * 100.0)
        reasons: list[str] = [f"Entry Stage={stage_pct:.1f}%"]
        if stage_pct <= 15:
            return EntryStageResult(stage_pct, True, 4, tuple(reasons + ["ورود در مرحله شروع حرکت است."]))
        if stage_pct <= 25:
            return EntryStageResult(stage_pct, True, 1, tuple(reasons + ["ورود هنوز قابل قبول ولی حساس است."]))
        if stage_pct <= 40:
            return EntryStageResult(stage_pct, False, -5, tuple(reasons + ["حرکت جلو رفته؛ real ممنوع می‌شود."]))
        return EntryStageResult(stage_pct, False, -10, tuple(reasons + ["ورود وسط/آخر حرکت است."]))
