"""
Final AI decision layer for Crypto AI Helper bot.

Locked responsibility:
- Logical final decision only.
- Uses probability_engine result and market_context.
- Does not validate TP/SL, fees, Toobit, Telegram, order execution, or learning.

Design lock:
- Small, simple, strong.
- One responsibility only.
- Respect probability_engine NO_TRADE.
- Prefer NO_TRADE over weak, conflicted, or late direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from config import MIN_AGREEMENT_SCORE, MIN_CONFIDENCE, MIN_DIRECTION_EDGE, MIN_DIRECTION_PROBABILITY
from market_context import MarketContext
from probability_engine import ProbabilityResult

Decision = Literal["ENTER_LONG", "ENTER_SHORT", "NO_TRADE"]
Direction = Literal["LONG", "SHORT", "NONE"]


@dataclass(frozen=True)
class AIDecision:
    decision: Decision
    direction: Direction
    confidence: float
    reason: str


def make_ai_decision(probability: ProbabilityResult, context: MarketContext) -> AIDecision:
    """Return the final logical entry decision.

    This layer is intentionally small. The heavy probability work belongs to
    probability_engine. Here we only enforce the final safety gate so a weak,
    late, conflicted, or market-blocked setup cannot become an entry.
    """
    if context.trade_permission == "blocked":
        return _no_trade(probability, "مارکت کانتکست ورود را مسدود کرده")

    if probability.preferred_direction == "NO_TRADE":
        return _no_trade(probability, "ProbabilityEngine ورود را NO_TRADE تشخیص داده")

    if probability.no_trade_probability >= _active_direction_probability(probability):
        return _no_trade(probability, "ریسک NO_TRADE از جهت فعال قوی‌تر است")

    if probability.preferred_direction == "LONG" and _direction_ok(
        probability.long_probability,
        probability.short_probability,
        probability.confidence,
        probability.agreement_score,
    ):
        return AIDecision("ENTER_LONG", "LONG", probability.confidence, _reason("لانگ", probability))

    if probability.preferred_direction == "SHORT" and _direction_ok(
        probability.short_probability,
        probability.long_probability,
        probability.confidence,
        probability.agreement_score,
    ):
        return AIDecision("ENTER_SHORT", "SHORT", probability.confidence, _reason("شورت", probability))

    return _no_trade(probability, "احتمال/اعتماد/توافق یا برتری جهت برای ورود کافی نیست")


def _direction_ok(
    direction_probability: float,
    opposite_probability: float,
    confidence: float,
    agreement: float,
) -> bool:
    return (
        direction_probability >= MIN_DIRECTION_PROBABILITY
        and confidence >= MIN_CONFIDENCE
        and agreement >= MIN_AGREEMENT_SCORE
        and (direction_probability - opposite_probability) >= MIN_DIRECTION_EDGE
    )


def _active_direction_probability(probability: ProbabilityResult) -> float:
    if probability.preferred_direction == "LONG":
        return probability.long_probability
    if probability.preferred_direction == "SHORT":
        return probability.short_probability
    return 0.0


def _no_trade(probability: ProbabilityResult, reason: str) -> AIDecision:
    return AIDecision("NO_TRADE", "NONE", probability.confidence, reason)


def _reason(label: str, probability: ProbabilityResult) -> str:
    return (
        f"ورود {label}: Preferred={probability.preferred_direction}، "
        f"Long={probability.long_probability:.2f}%، Short={probability.short_probability:.2f}%، "
        f"NoTrade={probability.no_trade_probability:.2f}%، "
        f"Confidence={probability.confidence:.2f}%، Agreement={probability.agreement_score:.2f}%، "
        f"Edge={abs(probability.long_probability - probability.short_probability):.2f}%"
    )


__all__ = ["AIDecision", "make_ai_decision"]
