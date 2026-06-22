from __future__ import annotations

"""
12 - correlation_engine.py

Correlation / exposure risk engine for the locked Movement Hunter architecture.

Responsibilities:
- Classify symbols into correlation groups / sectors.
- Estimate exposure concentration risk before AI decision.
- Detect same-direction crowding risk.
- Provide CorrelationResult to ai_decision_engine.py.
- Keep REAL/GHOST/REJECT decision authority out of this file.

Strictly forbidden:
- No REAL/GHOST/REJECT.
- No trade execution.
- No Toobit calls.
- No Telegram.
- No persistence.
- No Paper mode.
- No Setup flow.

This file only describes correlation and exposure risk.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import uuid4
import time

from analysis_engine import AnalysisCandidate
from config import SETTINGS


JsonDict = Dict[str, Any]

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"

EXPOSURE_LOW = "LOW"
EXPOSURE_MEDIUM = "MEDIUM"
EXPOSURE_HIGH = "HIGH"
EXPOSURE_EXTREME = "EXTREME"

GROUP_BTC = "BTC_CORE"
GROUP_ETH = "ETH_BETA"
GROUP_L1 = "L1"
GROUP_L2 = "L2"
GROUP_MEME = "MEME"
GROUP_DEFI = "DEFI"
GROUP_AI = "AI"
GROUP_STORAGE = "STORAGE"
GROUP_EXCHANGE = "EXCHANGE"
GROUP_UNKNOWN = "UNKNOWN"


DEFAULT_GROUPS: Dict[str, str] = {
    "BTCUSDT": GROUP_BTC,

    "ETHUSDT": GROUP_ETH,
    "LDOUSDT": GROUP_ETH,
    "ETCUSDT": GROUP_ETH,

    "SOLUSDT": GROUP_L1,
    "BNBUSDT": GROUP_L1,
    "ADAUSDT": GROUP_L1,
    "AVAXUSDT": GROUP_L1,
    "DOTUSDT": GROUP_L1,
    "ATOMUSDT": GROUP_L1,
    "NEARUSDT": GROUP_L1,
    "APTUSDT": GROUP_L1,
    "SUIUSDT": GROUP_L1,
    "TRXUSDT": GROUP_L1,
    "XRPUSDT": GROUP_L1,
    "LTCUSDT": GROUP_L1,

    "ARBUSDT": GROUP_L2,
    "OPUSDT": GROUP_L2,
    "MATICUSDT": GROUP_L2,
    "POLUSDT": GROUP_L2,

    "DOGEUSDT": GROUP_MEME,
    "1000SHIBUSDT": GROUP_MEME,
    "1000PEPEUSDT": GROUP_MEME,
    "1000FLOKIUSDT": GROUP_MEME,
    "WIFUSDT": GROUP_MEME,
    "BONKUSDT": GROUP_MEME,

    "UNIUSDT": GROUP_DEFI,
    "AAVEUSDT": GROUP_DEFI,
    "MKRUSDT": GROUP_DEFI,
    "LINKUSDT": GROUP_DEFI,

    "FETUSDT": GROUP_AI,
    "RNDRUSDT": GROUP_AI,
    "TAOUSDT": GROUP_AI,
    "WLDUSDT": GROUP_AI,

    "FILUSDT": GROUP_STORAGE,
    "ARUSDT": GROUP_STORAGE,
}


@dataclass(frozen=True)
class PositionExposure:
    symbol: str
    direction: str
    margin_usdt: float = 0.0
    notional_usdt: float = 0.0
    group: str = GROUP_UNKNOWN
    status: str = "OPEN"

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class CorrelationScore:
    group_exposure_score: float
    same_direction_score: float
    total_positions_score: float
    btc_dependency_score: float
    market_crowding_score: float
    total_score: float

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class CorrelationResult:
    correlation_id: str
    symbol: str
    group: str
    direction_hint: str
    timestamp: int
    exposure_level: str
    exposure_risk: float
    group_open_count: int
    same_direction_count: int
    total_open_count: int
    max_group_allowed: int
    max_direction_allowed: int
    should_reduce_priority: bool
    should_block_if_risk_high: bool
    score: CorrelationScore
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    valid: bool = True

    def to_dict(self) -> JsonDict:
        return {
            "correlation_id": self.correlation_id,
            "symbol": self.symbol,
            "group": self.group,
            "direction_hint": self.direction_hint,
            "timestamp": self.timestamp,
            "exposure_level": self.exposure_level,
            "exposure_risk": self.exposure_risk,
            "group_open_count": self.group_open_count,
            "same_direction_count": self.same_direction_count,
            "total_open_count": self.total_open_count,
            "max_group_allowed": self.max_group_allowed,
            "max_direction_allowed": self.max_direction_allowed,
            "should_reduce_priority": self.should_reduce_priority,
            "should_block_if_risk_high": self.should_block_if_risk_high,
            "score": self.score.to_dict(),
            "reason_codes": list(self.reason_codes),
            "warnings": list(self.warnings),
            "valid": self.valid,
        }


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except Exception:
        return low


def normalize_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().replace("-", "").replace("/", "").replace("_", "").strip()
    if s and not s.endswith("USDT") and len(s) <= 12:
        s += "USDT"
    return s


def normalize_direction(direction: str) -> str:
    d = str(direction or "").upper().strip()
    if d in {"BUY", "LONG"}:
        return DIRECTION_LONG
    if d in {"SELL", "SHORT"}:
        return DIRECTION_SHORT
    return DIRECTION_NEUTRAL


def exposure_level(score: float) -> str:
    score = clamp(score)
    if score >= 85:
        return EXPOSURE_EXTREME
    if score >= 65:
        return EXPOSURE_HIGH
    if score >= 35:
        return EXPOSURE_MEDIUM
    return EXPOSURE_LOW


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_position(item: Any, groups: Dict[str, str]) -> PositionExposure:
    if isinstance(item, PositionExposure):
        return item

    if hasattr(item, "to_dict") and callable(item.to_dict):
        item = item.to_dict()
    elif hasattr(item, "__dict__") and not isinstance(item, dict):
        item = dict(item.__dict__)

    if not isinstance(item, dict):
        item = {}

    symbol = normalize_symbol(item.get("symbol", item.get("coin", "")))
    direction = normalize_direction(item.get("direction", item.get("side", "")))
    group = item.get("group") or groups.get(symbol, GROUP_UNKNOWN)

    return PositionExposure(
        symbol=symbol,
        direction=direction,
        margin_usdt=_safe_float(item.get("margin_usdt", item.get("margin", 0.0))),
        notional_usdt=_safe_float(item.get("notional_usdt", item.get("notional", 0.0))),
        group=str(group),
        status=str(item.get("status", "OPEN")).upper(),
    )


class CorrelationGroupMapper:
    """Maps symbols to correlation groups."""

    def __init__(self, groups: Optional[Dict[str, str]] = None):
        self.groups = dict(DEFAULT_GROUPS)
        if groups:
            self.groups.update({normalize_symbol(k): str(v) for k, v in groups.items()})

    def group_for(self, symbol: str) -> str:
        symbol = normalize_symbol(symbol)
        if symbol in self.groups:
            return self.groups[symbol]

        # Fallback heuristics.
        if symbol.startswith("1000") or symbol.replace("USDT", "") in {"DOGE", "SHIB", "PEPE", "FLOKI", "WIF", "BONK"}:
            return GROUP_MEME
        if symbol in {"ARBUSDT", "OPUSDT", "MATICUSDT", "POLUSDT"}:
            return GROUP_L2
        return GROUP_UNKNOWN

    def with_group(self, position: Any) -> PositionExposure:
        p = _safe_position(position, self.groups)
        if p.group == GROUP_UNKNOWN:
            return PositionExposure(
                symbol=p.symbol,
                direction=p.direction,
                margin_usdt=p.margin_usdt,
                notional_usdt=p.notional_usdt,
                group=self.group_for(p.symbol),
                status=p.status,
            )
        return p


class ExposureCounter:
    """Counts current exposure against candidate symbol/direction."""

    CLOSED_STATUSES = {"TP2", "AI_EXIT", "SL", "CLOSED", "CANCELLED", "REJECTED"}

    def __init__(self, group_mapper: Optional[CorrelationGroupMapper] = None):
        self.group_mapper = group_mapper or CorrelationGroupMapper()

    def open_positions(self, positions: Optional[Iterable[Any]]) -> List[PositionExposure]:
        result: List[PositionExposure] = []
        for item in positions or []:
            p = self.group_mapper.with_group(item)
            if p.status.upper() not in self.CLOSED_STATUSES:
                result.append(p)
        return result

    def counts(self, symbol: str, direction: str, positions: Optional[Iterable[Any]]) -> Dict[str, int]:
        symbol = normalize_symbol(symbol)
        direction = normalize_direction(direction)
        group = self.group_mapper.group_for(symbol)
        open_positions = self.open_positions(positions)

        return {
            "group_open_count": sum(1 for p in open_positions if p.group == group),
            "same_direction_count": sum(1 for p in open_positions if p.direction == direction),
            "total_open_count": len(open_positions),
            "same_group_same_direction_count": sum(1 for p in open_positions if p.group == group and p.direction == direction),
            "btc_core_count": sum(1 for p in open_positions if p.group == GROUP_BTC),
            "eth_beta_count": sum(1 for p in open_positions if p.group == GROUP_ETH),
        }


class CorrelationRiskScorer:
    """Scores exposure concentration risk."""

    def score(
        self,
        symbol: str,
        direction: str,
        open_positions: Optional[Iterable[Any]] = None,
        market_context: Optional[Any] = None,
    ) -> tuple[CorrelationScore, Dict[str, int], List[str]]:
        reasons: List[str] = []
        mapper = CorrelationGroupMapper()
        counter = ExposureCounter(mapper)

        symbol = normalize_symbol(symbol)
        direction = normalize_direction(direction)
        group = mapper.group_for(symbol)
        counts = counter.counts(symbol, direction, open_positions)

        max_group = max(1, int(getattr(SETTINGS.risk, "max_same_correlation_group", 2)))
        max_direction = max(1, int(getattr(SETTINGS.risk, "max_same_direction_positions", 3)))
        max_positions = max(1, int(getattr(SETTINGS.trading, "max_positions", 5)))

        group_open = counts["group_open_count"]
        same_direction = counts["same_direction_count"]
        total_open = counts["total_open_count"]

        group_score = clamp((group_open / max_group) * 100.0)
        same_dir_score = clamp((same_direction / max_direction) * 100.0)
        total_score = clamp((total_open / max_positions) * 100.0)

        if group_open >= max_group:
            reasons.append("CORRELATION_GROUP_LIMIT_REACHED")
        elif group_open == max_group - 1:
            reasons.append("CORRELATION_GROUP_NEAR_LIMIT")

        if same_direction >= max_direction:
            reasons.append("SAME_DIRECTION_LIMIT_REACHED")
        elif same_direction == max_direction - 1:
            reasons.append("SAME_DIRECTION_NEAR_LIMIT")

        if total_open >= max_positions:
            reasons.append("MAX_POSITIONS_REACHED")

        btc_dependency = 0.0
        if group in {GROUP_L1, GROUP_L2, GROUP_MEME, GROUP_ETH}:
            btc_dependency += 25
        if counts["btc_core_count"] > 0:
            btc_dependency += 20
            reasons.append("BTC_CORE_POSITION_ALREADY_OPEN")

        market_crowding = 0.0
        if market_context is not None:
            breadth = _get_context_value(market_context, "market_breadth", 50.0)
            btc_trend = str(_get_context_value(market_context, "btc_trend", "NEUTRAL")).upper()
            if direction == DIRECTION_LONG and btc_trend in {"BEARISH", "STRONG_BEARISH"}:
                market_crowding += 25
                reasons.append("LONG_AGAINST_BTC_CONTEXT")
            elif direction == DIRECTION_SHORT and btc_trend in {"BULLISH", "STRONG_BULLISH"}:
                market_crowding += 25
                reasons.append("SHORT_AGAINST_BTC_CONTEXT")

            if breadth >= 75 and direction == DIRECTION_LONG:
                market_crowding += 12
                reasons.append("BULLISH_MARKET_CROWDING")
            elif breadth <= 25 and direction == DIRECTION_SHORT:
                market_crowding += 12
                reasons.append("BEARISH_MARKET_CROWDING")

        final = clamp(
            group_score * 0.32
            + same_dir_score * 0.25
            + total_score * 0.20
            + btc_dependency * 0.13
            + market_crowding * 0.10
        )

        score = CorrelationScore(
            group_exposure_score=group_score,
            same_direction_score=same_dir_score,
            total_positions_score=total_score,
            btc_dependency_score=clamp(btc_dependency),
            market_crowding_score=clamp(market_crowding),
            total_score=final,
        )

        counts["max_group_allowed"] = max_group
        counts["max_direction_allowed"] = max_direction
        counts["max_positions_allowed"] = max_positions
        counts["group"] = group  # type: ignore[assignment]

        return score, counts, reasons


def _get_context_value(context: Any, key: str, default: Any = None) -> Any:
    if context is None:
        return default
    if isinstance(context, dict):
        return context.get(key, default)
    if hasattr(context, "to_dict") and callable(context.to_dict):
        try:
            return context.to_dict().get(key, default)
        except Exception:
            pass
    return getattr(context, key, default)


class CorrelationEngine:
    """
    Main correlation exposure engine.

    Input:
        AnalysisCandidate and current open positions.

    Output:
        CorrelationResult.

    It does not decide REAL/GHOST/REJECT.
    """

    def __init__(self, group_mapper: Optional[CorrelationGroupMapper] = None):
        self.group_mapper = group_mapper or CorrelationGroupMapper()
        self.scorer = CorrelationRiskScorer()

    def analyze(
        self,
        candidate: AnalysisCandidate,
        open_positions: Optional[Iterable[Any]] = None,
        market_context: Optional[Any] = None,
    ) -> CorrelationResult:
        symbol = normalize_symbol(candidate.symbol)
        direction = normalize_direction(candidate.direction_hint)
        group = self.group_mapper.group_for(symbol)

        score, counts, reasons = self.scorer.score(
            symbol=symbol,
            direction=direction,
            open_positions=open_positions,
            market_context=market_context or candidate.market_context,
        )

        risk = clamp(score.total_score)
        level = exposure_level(risk)

        warnings: List[str] = []
        reduce_priority = False
        block_if_high = False

        # Soft exposure policy for Movement Hunter:
        # Correlation should reduce priority before it blocks. The final
        # REAL/GHOST/REJECT decision remains in ai_decision_engine.py.
        if risk >= 75:
            reduce_priority = True
            warnings.append("HIGH_CORRELATION_EXPOSURE_SOFT")
        if risk >= 92:
            block_if_high = True
            warnings.append("EXTREME_CORRELATION_EXPOSURE")

        if counts["group_open_count"] >= counts["max_group_allowed"]:
            reduce_priority = True
            warnings.append("GROUP_EXPOSURE_LIMIT_SOFT")
            # Do not block only because the group limit is reached; allow AI
            # to route strong fresh movement to REAL or weak ones to GHOST.
            if risk >= 92:
                block_if_high = True
        if counts["same_direction_count"] >= counts["max_direction_allowed"]:
            reduce_priority = True
            warnings.append("SAME_DIRECTION_EXPOSURE_LIMIT_SOFT")
            if risk >= 92:
                block_if_high = True

        if not candidate.valid:
            warnings.append("INVALID_CANDIDATE")

        return CorrelationResult(
            correlation_id=f"corr_{uuid4().hex}",
            symbol=symbol,
            group=group,
            direction_hint=direction,
            timestamp=candidate.timestamp or int(time.time()),
            exposure_level=level,
            exposure_risk=risk,
            group_open_count=int(counts["group_open_count"]),
            same_direction_count=int(counts["same_direction_count"]),
            total_open_count=int(counts["total_open_count"]),
            max_group_allowed=int(counts["max_group_allowed"]),
            max_direction_allowed=int(counts["max_direction_allowed"]),
            should_reduce_priority=reduce_priority,
            should_block_if_risk_high=block_if_high,
            score=score,
            reason_codes=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
            valid=bool(candidate.valid),
        )


_default_engine: Optional[CorrelationEngine] = None


def engine() -> CorrelationEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = CorrelationEngine()
    return _default_engine


def analyze_correlation(
    candidate: AnalysisCandidate,
    open_positions: Optional[Iterable[Any]] = None,
    market_context: Optional[Any] = None,
) -> CorrelationResult:
    return engine().analyze(
        candidate=candidate,
        open_positions=open_positions,
        market_context=market_context,
    )


def correlation_engine(
    candidate: AnalysisCandidate,
    open_positions: Optional[Iterable[Any]] = None,
    market_context: Optional[Any] = None,
) -> CorrelationResult:
    return analyze_correlation(
        candidate=candidate,
        open_positions=open_positions,
        market_context=market_context,
    )
