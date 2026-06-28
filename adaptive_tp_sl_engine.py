from __future__ import annotations

from risk_engine import RiskEngine, RiskResult
from indicators import IndicatorSnapshot
from levels_engine import LevelsResult
from scorer import Direction


class AdaptiveTpSlEngine:
    def __init__(self) -> None:
        self.risk = RiskEngine()

    def build(self, *, direction: Direction, entry: float, snapshot_15m: IndicatorSnapshot, levels: LevelsResult, learned_expected_pct: float | None) -> RiskResult:
        return self.risk.build_tp_sl(direction=direction, entry=entry, snapshot_15m=snapshot_15m, levels=levels, learned_expected_pct=learned_expected_pct)
