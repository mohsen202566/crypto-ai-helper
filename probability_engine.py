"""
Probability engine for Crypto AI Helper bot.

Locked responsibility:
- Converts coin_analyzer + market_context outputs into probabilities.
- No indicators, no API, no Telegram, no Toobit, no TP/SL, no learning.

Design lock:
- Small, simple, strong.
- One responsibility only.
- Prefer NO_TRADE over a weak or late direction.
- Penalize conflict, late/exhausted movement, and market-context blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from coin_analyzer import CoinAnalysis
from config import ANALYZER_WEIGHTS
from market_context import MarketContext


@dataclass(frozen=True)
class _NeutralSection:
    direction: str = "neutral"
    score: float = 0.0


@dataclass(frozen=True)
class ProbabilityResult:
    symbol: str
    long_probability: float
    short_probability: float
    no_trade_probability: float
    confidence: float
    agreement_score: float
    preferred_direction: str
    reason: str


_SECTION_NAMES = (
    "structure",
    "momentum",
    "volume",
    "acceleration",
    "volatility_atr",
    "candle_price_action",
    "liquidity",
    # Optional 15m/30m sections. They stay neutral until coin_analyzer provides them.
    "ema_slope",
    "rsi_slope",
    "market_structure",
    "breakout_confirmation",
    "consolidation",
    "liquidity_sweep",
)

_CORE_SECTIONS = (
    "structure",
    "market_structure",
    "ema_slope",
    "rsi_slope",
    "momentum",
    "acceleration",
    "breakout_confirmation",
    "candle_price_action",
)
_CONFIRMATION_SECTIONS = ("volume", "volatility_atr")
_BLOCK_SECTIONS = ("consolidation", "liquidity_sweep")
_MIN_TRADE_PROBABILITY = 62.0
_MIN_CONFIDENCE = 75.0
_MIN_DIRECTION_EDGE = 8.0
_MIN_AGREEMENT = 70.0

_FALLBACK_WEIGHTS = {
    "structure": 18.0,
    "market_structure": 18.0,
    "ema_slope": 16.0,
    "rsi_slope": 14.0,
    "momentum": 10.0,
    "acceleration": 8.0,
    "breakout_confirmation": 10.0,
    "candle_price_action": 6.0,
    "liquidity": 6.0,
    "volume": 5.0,
    "volatility_atr": 5.0,
    "consolidation": 0.0,
    "liquidity_sweep": 0.0,
}


def calculate_probabilities(analysis: CoinAnalysis, context: MarketContext) -> ProbabilityResult:
    sections = _sections(analysis)

    long_raw = _permission_adjusted_raw(
        analysis.weighted_long_score + context.long_bias,
        context.trade_permission,
    )
    short_raw = _permission_adjusted_raw(
        analysis.weighted_short_score + context.short_bias,
        context.trade_permission,
    )

    bullish = _direction_alignment(sections, "bullish")
    bearish = _direction_alignment(sections, "bearish")
    neutral = _direction_alignment(sections, "neutral")

    long_quality = _direction_quality(
        raw_score=long_raw,
        own_alignment=bullish,
        opposite_alignment=bearish,
        neutral_alignment=neutral,
        direction="LONG",
        sections=sections,
    )
    short_quality = _direction_quality(
        raw_score=short_raw,
        own_alignment=bearish,
        opposite_alignment=bullish,
        neutral_alignment=neutral,
        direction="SHORT",
        sections=sections,
    )

    agreement = _agreement_score_from_alignment(bullish, bearish, neutral)
    edge = abs(long_quality - short_quality)
    confidence = _confidence(long_quality, short_quality, agreement, context)

    long_probability = _probability_from_quality(long_quality, confidence)
    short_probability = _probability_from_quality(short_quality, confidence)

    no_trade = _no_trade_probability(
        long_probability=long_probability,
        short_probability=short_probability,
        confidence=confidence,
        agreement=agreement,
        edge=edge,
        context=context,
    )

    preferred = _preferred_direction(
        long_probability=long_probability,
        short_probability=short_probability,
        no_trade_probability=no_trade,
        confidence=confidence,
        agreement=agreement,
        edge=edge,
        context=context,
    )

    if preferred == "NO_TRADE":
        long_probability, short_probability = _soften_trade_probabilities(long_probability, short_probability)
        no_trade = _clamp(max(no_trade, 100.0 - max(long_probability, short_probability)), 0.0, 100.0)

    return ProbabilityResult(
        symbol=analysis.symbol,
        long_probability=round(long_probability, 2),
        short_probability=round(short_probability, 2),
        no_trade_probability=round(no_trade, 2),
        confidence=round(confidence, 2),
        agreement_score=round(agreement, 2),
        preferred_direction=preferred,
        reason=_reason(preferred, long_probability, short_probability, no_trade, confidence, agreement, edge, context),
    )


def _sections(analysis: CoinAnalysis) -> dict[str, Any]:
    neutral = _NeutralSection()
    return {name: getattr(analysis, name, neutral) for name in _SECTION_NAMES}


def _permission_adjusted_raw(value: float, trade_permission: str) -> float:
    if trade_permission == "blocked":
        value -= 35.0
    elif trade_permission == "caution":
        value -= 10.0
    return _clamp(value, 0.0, 100.0)


def _direction_quality(
    *,
    raw_score: float,
    own_alignment: float,
    opposite_alignment: float,
    neutral_alignment: float,
    direction: str,
    sections: dict[str, Any],
) -> float:
    # 15m/30m mode: direction alignment is more important than raw score.
    quality = raw_score * 0.35 + own_alignment * 0.70
    quality -= opposite_alignment * 0.60
    quality -= neutral_alignment * 0.25
    quality -= _late_or_exhaustion_penalty(direction, sections)
    quality -= _core_conflict_penalty(direction, sections)
    quality -= _confirmation_penalty(direction, sections)
    quality -= _range_or_sweep_penalty(direction, sections)
    return _clamp(quality, 0.0, 100.0)


def _late_or_exhaustion_penalty(direction: str, sections: dict[str, Any]) -> float:
    """Simple guard: strong direction needs fresh momentum and acceleration.

    This does not calculate indicators. It only uses analyzer sections already produced
    upstream, keeping this file small and responsibility-safe.
    """
    wanted = "bullish" if direction == "LONG" else "bearish"
    opposite = "bearish" if direction == "LONG" else "bullish"

    penalty = 0.0
    momentum = sections["momentum"]
    acceleration = sections["acceleration"]
    candle = sections["candle_price_action"]
    liquidity = sections["liquidity"]
    ema_slope = sections["ema_slope"]
    rsi_slope = sections["rsi_slope"]
    market_structure = sections["market_structure"]
    breakout = sections["breakout_confirmation"]

    if _section_direction(momentum) != wanted:
        penalty += 8.0
    if _section_direction(acceleration) != wanted:
        penalty += 9.0
    if _section_direction(ema_slope) == opposite and _section_score(ema_slope) >= 55.0:
        penalty += 12.0
    if _section_direction(rsi_slope) == opposite and _section_score(rsi_slope) >= 55.0:
        penalty += 10.0
    if _section_direction(market_structure) == opposite and _section_score(market_structure) >= 55.0:
        penalty += 14.0
    if _section_direction(breakout) == opposite and _section_score(breakout) >= 55.0:
        penalty += 12.0
    if _section_direction(candle) == opposite and _section_score(candle) >= 55.0:
        penalty += 7.0
    if _section_direction(liquidity) == opposite and _section_score(liquidity) >= 60.0:
        penalty += 8.0

    if _section_direction(momentum) == wanted and _section_score(momentum) < 45.0:
        penalty += 5.0
    if _section_direction(acceleration) == wanted and _section_score(acceleration) < 45.0:
        penalty += 6.0
    if _section_direction(ema_slope) == wanted and _section_score(ema_slope) < 50.0:
        penalty += 5.0
    if _section_direction(rsi_slope) == wanted and _section_score(rsi_slope) < 50.0:
        penalty += 5.0

    return penalty


def _core_conflict_penalty(direction: str, sections: dict[str, Any]) -> float:
    wanted = "bullish" if direction == "LONG" else "bearish"
    opposite = "bearish" if direction == "LONG" else "bullish"

    aligned = 0
    conflicted = 0
    for name in _CORE_SECTIONS:
        section_direction = _section_direction(sections[name])
        if section_direction == wanted:
            aligned += 1
        elif section_direction == opposite:
            conflicted += 1

    if conflicted >= 4:
        return 24.0
    if conflicted >= 3:
        return 18.0
    if conflicted >= 2 and aligned <= 1:
        return 14.0
    if conflicted >= 1 and aligned == 0:
        return 10.0
    return 0.0


def _confirmation_penalty(direction: str, sections: dict[str, Any]) -> float:
    """ATR and volume confirm quality; they must not create direction alone."""
    wanted = "bullish" if direction == "LONG" else "bearish"
    penalty = 0.0
    for name in _CONFIRMATION_SECTIONS:
        section = sections[name]
        section_direction = _section_direction(section)
        score = _section_score(section)
        if section_direction == "neutral" or score < 45.0:
            penalty += 5.0
        elif section_direction != wanted and score >= 55.0:
            penalty += 7.0
    return penalty


def _range_or_sweep_penalty(direction: str, sections: dict[str, Any]) -> float:
    """Strong range or liquidity-sweep warnings should push the setup to NO_TRADE."""
    wanted = "bullish" if direction == "LONG" else "bearish"
    opposite = "bearish" if direction == "LONG" else "bullish"
    penalty = 0.0

    consolidation = sections["consolidation"]
    if _section_direction(consolidation) == "neutral" and _section_score(consolidation) >= 60.0:
        penalty += 18.0

    liquidity_sweep = sections["liquidity_sweep"]
    sweep_direction = _section_direction(liquidity_sweep)
    sweep_score = _section_score(liquidity_sweep)
    if sweep_direction == opposite and sweep_score >= 55.0:
        penalty += 16.0
    elif sweep_direction != wanted and sweep_score >= 70.0:
        penalty += 10.0

    return penalty


def _agreement_score(analysis: CoinAnalysis) -> float:
    sections = _sections(analysis)
    bullish = _direction_alignment(sections, "bullish")
    bearish = _direction_alignment(sections, "bearish")
    neutral = _direction_alignment(sections, "neutral")
    return _agreement_score_from_alignment(bullish, bearish, neutral)


def _agreement_score_from_alignment(bullish: float, bearish: float, neutral: float) -> float:
    dominant = max(bullish, bearish)
    conflict = min(bullish, bearish)
    if dominant <= 0:
        return 30.0
    score = dominant - conflict * 0.85 - neutral * 0.25
    return _clamp(score, 0.0, 100.0)


def _direction_alignment(sections: dict[str, Any], direction: str) -> float:
    total = 0.0
    for name, section in sections.items():
        if _section_direction(section) == direction:
            total += _section_weight(name) * (_section_score(section) / 100.0)
    return _clamp(total, 0.0, 100.0)


def _section_weight(name: str) -> float:
    return float(ANALYZER_WEIGHTS.get(name, _FALLBACK_WEIGHTS.get(name, 0.0)))


def _section_direction(section: Any) -> str:
    return str(getattr(section, "direction", "neutral") or "neutral").lower()


def _section_score(section: Any) -> float:
    return _clamp(float(getattr(section, "score", 0.0) or 0.0), 0.0, 100.0)


def _confidence(long_quality: float, short_quality: float, agreement: float, context: MarketContext) -> float:
    edge = abs(long_quality - short_quality)
    base = 35.0 + agreement * 0.40 + edge * 0.45 + max(long_quality, short_quality) * 0.15

    if context.trade_permission == "normal":
        base += 5.0
    elif context.trade_permission == "caution":
        base -= 9.0
    else:
        base -= 35.0

    return _clamp(base, 0.0, 100.0)


def _probability_from_quality(quality: float, confidence: float) -> float:
    probability = quality * 0.70 + confidence * 0.30
    return _clamp(probability, 0.0, 95.0)


def _no_trade_probability(
    *,
    long_probability: float,
    short_probability: float,
    confidence: float,
    agreement: float,
    edge: float,
    context: MarketContext,
) -> float:
    best = max(long_probability, short_probability)
    no_trade = 100.0 - best

    if context.trade_permission == "blocked":
        no_trade += 45.0
    elif context.trade_permission == "caution":
        no_trade += 12.0

    if confidence < _MIN_CONFIDENCE:
        no_trade += (_MIN_CONFIDENCE - confidence) * 0.80
    if agreement < _MIN_AGREEMENT:
        no_trade += (_MIN_AGREEMENT - agreement) * 0.60
    if edge < _MIN_DIRECTION_EDGE:
        no_trade += (_MIN_DIRECTION_EDGE - edge) * 1.60

    return _clamp(no_trade, 0.0, 100.0)


def _preferred_direction(
    *,
    long_probability: float,
    short_probability: float,
    no_trade_probability: float,
    confidence: float,
    agreement: float,
    edge: float,
    context: MarketContext,
) -> str:
    if context.trade_permission == "blocked":
        return "NO_TRADE"

    best = max(long_probability, short_probability)
    if (
        best < _MIN_TRADE_PROBABILITY
        or confidence < _MIN_CONFIDENCE
        or agreement < _MIN_AGREEMENT
        or edge < _MIN_DIRECTION_EDGE
    ):
        return "NO_TRADE"
    if no_trade_probability >= best:
        return "NO_TRADE"

    if long_probability > short_probability:
        return "LONG"
    if short_probability > long_probability:
        return "SHORT"
    return "NO_TRADE"


def _soften_trade_probabilities(long_probability: float, short_probability: float) -> tuple[float, float]:
    return _clamp(long_probability, 0.0, 49.0), _clamp(short_probability, 0.0, 49.0)


def _reason(
    preferred: str,
    long_p: float,
    short_p: float,
    no_trade_p: float,
    confidence: float,
    agreement: float,
    edge: float,
    context: MarketContext,
) -> str:
    gates = (
        f"Gates: MinProb={_MIN_TRADE_PROBABILITY:.0f} | "
        f"MinConfidence={_MIN_CONFIDENCE:.0f} | "
        f"MinAgreement={_MIN_AGREEMENT:.0f} | "
        f"MinEdge={_MIN_DIRECTION_EDGE:.0f}"
    )
    return (
        f"Preferred={preferred} | Long={long_p:.2f}% | Short={short_p:.2f}% | "
        f"NoTrade={no_trade_p:.2f}% | Confidence={confidence:.2f}% | "
        f"Agreement={agreement:.2f}% | Edge={edge:.2f}% | {gates} | {context.reason}"
    )


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


__all__ = ["ProbabilityResult", "calculate_probabilities"]
