from __future__ import annotations

from dataclasses import dataclass

from config import ESTIMATED_FIXED_ROUND_FEE_USDT, MIN_REAL_NET_PROFIT_USDT, SLIPPAGE_BUFFER, SPREAD_BUFFER, TOOBIT_TAKER_FEE
from scorer import Direction


@dataclass(frozen=True)
class CostResult:
    ok: bool
    net_edge: float
    estimated_cost_pct: float
    estimated_profit_usdt: float
    estimated_net_profit_usdt: float
    estimated_profit_pct: float
    score: int
    reasons: tuple[str, ...]


class CostEngine:
    def evaluate(self, *, direction: Direction, entry: float, tp: float, margin_usdt: float, leverage: int) -> CostResult:
        if entry <= 0 or tp <= 0:
            return CostResult(False, 0.0, 0.0, 0.0, 0.0, 0.0, 0, ("قیمت ورود یا TP نامعتبر است.",))
        gross_move = (tp - entry) / entry if direction == "LONG" else (entry - tp) / entry
        percent_cost = (TOOBIT_TAKER_FEE * 2.0) + SPREAD_BUFFER + SLIPPAGE_BUFFER
        notional = margin_usdt * max(1, leverage)
        variable_cost_usdt = notional * percent_cost
        gross_profit_usdt = notional * gross_move
        net_profit_usdt = gross_profit_usdt - variable_cost_usdt - ESTIMATED_FIXED_ROUND_FEE_USDT
        net_edge = net_profit_usdt / max(notional, 1e-9)
        ok = net_profit_usdt >= MIN_REAL_NET_PROFIT_USDT
        score = 4 if ok else 0
        reasons = [
            f"سود کل تخمینی={gross_profit_usdt:.4f} USDT.",
            f"سود خالص تخمینی بعد از fee/slippage={net_profit_usdt:.4f} USDT.",
            "برای Real حداقل سود خالص تایید شد." if ok else f"برای Real سود خالص باید حداقل {MIN_REAL_NET_PROFIT_USDT:.2f} USDT باشد.",
        ]
        return CostResult(ok, net_edge, percent_cost, gross_profit_usdt, net_profit_usdt, gross_move * 100.0, score, tuple(reasons))
