from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Any

GuardLevel = Literal["ALLOW", "CAUTION", "REAL_BLOCK", "BLOCK"]


@dataclass(frozen=True)
class GuardVerdict:
    level: GuardLevel = "ALLOW"
    source: str = "GUARD"
    reason: str = ""
    min_confidence: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def blocks_signal(self) -> bool:
        return self.level == "BLOCK"

    @property
    def blocks_real(self) -> bool:
        return self.level in {"REAL_BLOCK", "BLOCK"}

    @property
    def is_caution(self) -> bool:
        return self.level in {"CAUTION", "REAL_BLOCK", "BLOCK"}


_LEVEL_WEIGHT: dict[str, int] = {
    "ALLOW": 0,
    "CAUTION": 1,
    "REAL_BLOCK": 2,
    "BLOCK": 3,
}


def strongest_verdict(verdicts: list[GuardVerdict]) -> GuardVerdict:
    active = [v for v in verdicts if v.level != "ALLOW"]
    if not active:
        return GuardVerdict()
    level = max(active, key=lambda v: _LEVEL_WEIGHT.get(v.level, 0)).level
    reasons = [f"{v.source}: {v.reason}" for v in active if v.reason]
    min_confidence = max((v.min_confidence for v in active), default=0)
    payload: dict[str, Any] = {}
    for v in active:
        payload[v.source] = v.payload
    return GuardVerdict(level=level, source="SAFETY_LAYER", reason=" | ".join(reasons), min_confidence=min_confidence, payload=payload)
