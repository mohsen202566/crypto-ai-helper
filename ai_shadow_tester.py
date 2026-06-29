from __future__ import annotations

from dataclasses import dataclass

from scorer import Direction


@dataclass(frozen=True)
class ShadowPlan:
    name: str
    tp: float
    sl: float


class AIShadowTester:
    def build_plans(self, *, direction: Direction, entry: float, tp: float, sl: float) -> tuple[ShadowPlan, ...]:
        if entry <= 0 or tp <= 0 or sl <= 0:
            return tuple()
        if direction == "LONG":
            reward = tp - entry
            risk = entry - sl
            return (
                ShadowPlan("tp_shorter_sl_same", entry + reward * 0.72, sl),
                ShadowPlan("tp_same_sl_wider", tp, entry - risk * 1.20),
                ShadowPlan("tp_wider_sl_same", entry + reward * 1.18, sl),
            )
        reward = entry - tp
        risk = sl - entry
        return (
            ShadowPlan("tp_shorter_sl_same", entry - reward * 0.72, sl),
            ShadowPlan("tp_same_sl_wider", tp, entry + risk * 1.20),
            ShadowPlan("tp_wider_sl_same", entry - reward * 1.18, sl),
        )

    def register(self, storage, signal_id: int, *, direction: Direction, entry: float, tp: float, sl: float) -> None:
        for plan in self.build_plans(direction=direction, entry=entry, tp=tp, sl=sl):
            storage.add_shadow_test(signal_id, plan.name, plan.tp, plan.sl)
