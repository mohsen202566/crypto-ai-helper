from __future__ import annotations

from dataclasses import dataclass

from config import MIN_NET_EDGE, SLIPPAGE_BUFFER, SPREAD_BUFFER, TOOBIT_TAKER_FEE, WEIGHTS
from scorer import Direction


@dataclass(frozen=True)
class CostResult:
    ok: bool
    net_edge: float
    estimated_cost_pct: float
    score_bonus: int
    reasons: tuple[str, ...]


class CostEngine:
    def evaluate(self, *, direction: Direction, entry: float, tp: float) -> CostResult:
        if entry <= 0 or tp <= 0:
            return CostResult(False, 0.0, 0.0, 0, ("قیمت ورود یا TP نامعتبر است.",))
        gross_move = (tp - entry) / entry if direction == "LONG" else (entry - tp) / entry
        estimated_cost = (TOOBIT_TAKER_FEE * 2.0) + SPREAD_BUFFER + SLIPPAGE_BUFFER
        net_edge = gross_move - estimated_cost
        reasons = [f"Net Edge={net_edge * 100:.3f}% بعد از کارمزد/اسپرد/اسلیپیج."]
        if net_edge < MIN_NET_EDGE:
            reasons.append("Net Edge کافی نیست.")
            return CostResult(False, net_edge, estimated_cost, 0, tuple(reasons))
        bonus = 0
        if net_edge >= MIN_NET_EDGE:
            bonus += 2
        if net_edge >= MIN_NET_EDGE * 1.8:
            bonus += 1
        return CostResult(True, net_edge, estimated_cost, min(3, bonus), tuple(reasons))
