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
        """Estimate how far the current 5m move has already travelled.

        The old version used recent_high/recent_low as the anchor. On fast trends that
        made normal watch candidates look like stage=100%, so they stayed in watch and
        never converted to a real entry. This version measures the distance from the
        nearest live mean (EMA20/VWAP), which is much better for a scalping trigger.
        """
        atr = max(float(snapshot.atr or 0.0), float(snapshot.close) * 0.0001)
        close = float(snapshot.close)
        ema20 = float(snapshot.ema20)
        vwap = float(snapshot.vwap)

        if direction == "LONG":
            # nearest live support/mean below price; do not use far recent_low as anchor
            anchor = max(ema20, vwap)
            move_from_anchor = max(0.0, close - anchor)
        else:
            # nearest live resistance/mean above price; do not use far recent_high as anchor
            anchor = min(ema20, vwap)
            move_from_anchor = max(0.0, anchor - close)

        denominator = max(atr * 5.0, close * 0.0025)
        stage_pct = min(100.0, max(0.0, (move_from_anchor / denominator) * 100.0))

        reasons: list[str] = [
            f"Entry Stage={stage_pct:.1f}%",
            "مرحله ورود با EMA20/VWAP زنده محاسبه شد؛ سقف/کف دور باعث 100% کاذب نمی‌شود.",
        ]

        if stage_pct <= 35:
            return EntryStageResult(stage_pct, True, 4, tuple(reasons + ["ورود در ناحیه شروع/تریگر است."]))
        if stage_pct <= 55:
            return EntryStageResult(stage_pct, True, 2, tuple(reasons + ["ورود هنوز قابل قبول است ولی باید کندل تایید بدهد."]))
        if stage_pct <= 75:
            return EntryStageResult(stage_pct, False, -1, tuple(reasons + ["حرکت جلو رفته؛ فقط با تریگر قوی از واچ قابل بررسی است."]))
        return EntryStageResult(stage_pct, False, -4, tuple(reasons + ["حرکت از ناحیه شروع فاصله گرفته؛ برای real سخت‌گیرانه است."]))
