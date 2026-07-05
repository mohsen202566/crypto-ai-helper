from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from indicators import snapshot
from okx_client import Candle

Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class PowerResult:
    passed: bool
    strength: str
    score: int
    reason: str
    open_room_percent: float


def analyze_power(*, direction: Direction, h1: list[Candle], m15: list[Candle]) -> PowerResult:
    h1s = snapshot(h1)
    m15s = snapshot(m15)
    score = 0
    reasons: list[str] = []

    if h1s.adx14 >= 25:
        score += 2
        reasons.append("قدرت روند یک‌ساعته خوب")
    elif h1s.adx14 >= 20:
        score += 1
        reasons.append("قدرت روند یک‌ساعته قابل قبول")

    if direction == "LONG":
        if 53 <= h1s.rsi14 <= 72:
            score += 2
        if 52 <= m15s.rsi14 <= 72:
            score += 2
        if h1s.macd_hist > 0 and m15s.macd_hist > 0:
            score += 1
        open_room = max(0.0, (h1s.resistance - h1s.close) / h1s.close * 100)
    else:
        if 28 <= h1s.rsi14 <= 47:
            score += 2
        if 28 <= m15s.rsi14 <= 48:
            score += 2
        if h1s.macd_hist < 0 and m15s.macd_hist < 0:
            score += 1
        open_room = max(0.0, (h1s.close - h1s.support) / h1s.close * 100)

    atr_percent = h1s.atr14 / h1s.close * 100
    if atr_percent >= 0.35:
        score += 1
    if open_room >= max(0.4, atr_percent * 1.2):
        score += 2
        reasons.append("فضای سود مناسب")
    else:
        reasons.append("فضای سود کم")

    if score >= 7:
        strength = "خیلی قوی" if score >= 9 else "قوی"
        return PowerResult(True, strength, score, "، ".join(reasons), open_room)
    return PowerResult(False, "ضعیف", score, "حرکت برای ادامه و سود مناسب کافی نیست.", open_room)
