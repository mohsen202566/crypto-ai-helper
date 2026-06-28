from __future__ import annotations

from dataclasses import dataclass

from config import MIN_NET_EDGE, SLIPPAGE_BUFFER, SPREAD_BUFFER, TOOBIT_TAKER_FEE, WEIGHTS
from scorer import Direction


@dataclass(frozen=True)
class CostResult:
    ok: bool
    net_edge: float
    estimated_cost_pct: float
    estimated_profit_usdt: float
    estimated_profit_pct: float
    score_bonus: int
    reasons: tuple[str, ...]


class CostEngine:
    def evaluate(self, *, direction: Direction, entry: float, tp: float, margin_usdt: float, leverage: int, min_profit_usdt: float, min_profit_pct: float) -> CostResult:
        if entry <= 0 or tp <= 0:
            return CostResult(False, 0.0, 0.0, 0.0, 0.0, 0, ("قیمت ورود یا TP نامعتبر است.",))
        gross_move = (tp - entry) / entry if direction == "LONG" else (entry - tp) / entry
        estimated_cost = (TOOBIT_TAKER_FEE * 2.0) + SPREAD_BUFFER + SLIPPAGE_BUFFER
        net_edge = gross_move - estimated_cost
        profit_usdt = margin_usdt * max(1, leverage) * net_edge
        profit_pct = net_edge * 100.0
        reasons = [f"Net Edge={profit_pct:.3f}% بعد از کارمزد/اسپرد/اسلیپیج.", f"سود تخمینی={profit_usdt:.2f} USDT."]
        ok = True
        if net_edge < MIN_NET_EDGE:
            reasons.append("Net Edge پایه کافی نیست.")
            ok = False
        if profit_usdt < min_profit_usdt:
            reasons.append("حداقل سود دلاری پاس نشد.")
            ok = False
        if profit_pct < min_profit_pct:
            reasons.append("حداقل درصد سود پاس نشد.")
            ok = False
        bonus = 0
        if ok:
            bonus += 5
        if net_edge >= MIN_NET_EDGE * 1.8:
            bonus += 2
        if profit_usdt >= min_profit_usdt * 1.5:
            bonus += 2
        return CostResult(ok, net_edge, estimated_cost, profit_usdt, profit_pct, min(WEIGHTS.risk_net, bonus), tuple(reasons))
