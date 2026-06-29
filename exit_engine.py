from __future__ import annotations

from dataclasses import dataclass

from storage import StoredSignal


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str


class ExitEngine:
    def analyze(self, signal: StoredSignal, price: float) -> ExitDecision:
        if signal.entry <= 0:
            return ExitDecision(False, "")
        if signal.direction == "LONG":
            progress = (price - signal.entry) / max(signal.tp - signal.entry, 1e-9)
            adverse = (signal.entry - price) / max(signal.entry - signal.sl, 1e-9)
        else:
            progress = (signal.entry - price) / max(signal.entry - signal.tp, 1e-9)
            adverse = (price - signal.entry) / max(signal.sl - signal.entry, 1e-9)
        if progress >= 0.72 and adverse <= 0:
            return ExitDecision(False, "")
        if adverse >= 0.88:
            return ExitDecision(False, "")
        return ExitDecision(False, "")
