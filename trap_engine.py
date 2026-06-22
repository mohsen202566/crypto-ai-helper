from __future__ import annotations

"""
09 - trap_engine.py

Trap / fake breakout / liquidity-grab detection layer for the locked Movement Hunter bot.

Responsibilities:
- Detect long traps and short traps.
- Detect fake breakouts / fake breakdowns.
- Detect liquidity grabs and stop hunts.
- Estimate trap risk and liquidity risk.
- Provide structured TrapResult to AI decision layer.

Strictly forbidden:
- No REAL/GHOST/REJECT.
- No trade execution.
- No Toobit calls.
- No Telegram.
- No persistence.
- No Paper mode.
- No Setup flow.

This file is not the final decision-maker.
It only describes trap/liquidity risk.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4
import time

from analysis_layers import SensorSnapshot
from analysis_engine import AnalysisCandidate
from movement_hunter import MovementHunterResult


JsonDict = Dict[str, Any]

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"

TRAP_LOW = "LOW"
TRAP_MEDIUM = "MEDIUM"
TRAP_HIGH = "HIGH"
TRAP_EXTREME = "EXTREME"

TRAP_NONE = "NONE"
TRAP_LONG = "LONG_TRAP"
TRAP_SHORT = "SHORT_TRAP"
TRAP_FAKE_BREAKOUT = "FAKE_BREAKOUT"
TRAP_FAKE_BREAKDOWN = "FAKE_BREAKDOWN"
TRAP_LIQUIDITY_GRAB_UP = "LIQUIDITY_GRAB_UP"
TRAP_LIQUIDITY_GRAB_DOWN = "LIQUIDITY_GRAB_DOWN"


@dataclass(frozen=True)
class TrapScore:
    fake_breakout_score: float
    fake_breakdown_score: float
    liquidity_grab_score: float
    stop_hunt_score: float
    wick_rejection_score: float
    close_quality_risk: float
    range_trap_score: float
    total_score: float

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class TrapResult:
    trap_id: str
    symbol: str
    timeframe: str
    timestamp: int
    direction_hint: str
    trap_type: str
    trap_risk: float
    trap_level: str
    liquidity_risk: float
    long_trap_probability: float
    short_trap_probability: float
    score: TrapScore
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    valid: bool = True

    def to_dict(self) -> JsonDict:
        return {
            "trap_id": self.trap_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp,
            "direction_hint": self.direction_hint,
            "trap_type": self.trap_type,
            "trap_risk": self.trap_risk,
            "trap_level": self.trap_level,
            "liquidity_risk": self.liquidity_risk,
            "long_trap_probability": self.long_trap_probability,
            "short_trap_probability": self.short_trap_probability,
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


def avg(values: Sequence[float]) -> float:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def risk_level(score: float) -> str:
    score = clamp(score)
    if score >= 85:
        return TRAP_EXTREME
    if score >= 65:
        return TRAP_HIGH
    if score >= 35:
        return TRAP_MEDIUM
    return TRAP_LOW


class FakeBreakoutDetector:
    """Detects failed breakout and failed breakdown conditions."""

    def score(self, snapshot: SensorSnapshot, direction: str) -> Tuple[float, float, List[str]]:
        fake_breakout = 0.0
        fake_breakdown = 0.0
        reasons: List[str] = []

        if snapshot.failed_breakout:
            fake_breakout += 55
            reasons.append("FAILED_BREAKOUT")
        if snapshot.failed_breakdown:
            fake_breakdown += 55
            reasons.append("FAILED_BREAKDOWN")

        if snapshot.breakout_candidate and snapshot.close_quality < 0.45:
            fake_breakout += 22
            reasons.append("BREAKOUT_WEAK_CLOSE")

        if snapshot.breakdown_candidate and snapshot.close_quality > 0.55:
            fake_breakdown += 22
            reasons.append("BREAKDOWN_STRONG_CLOSE")

        if direction == DIRECTION_LONG and snapshot.failed_breakout:
            fake_breakout += 15
            reasons.append("LONG_AGAINST_FAILED_BREAKOUT")
        elif direction == DIRECTION_SHORT and snapshot.failed_breakdown:
            fake_breakdown += 15
            reasons.append("SHORT_AGAINST_FAILED_BREAKDOWN")

        return clamp(fake_breakout), clamp(fake_breakdown), reasons


class LiquidityGrabDetector:
    """Detects wick-based liquidity grab and stop hunt pressure."""

    def score(self, snapshot: SensorSnapshot, direction: str) -> Tuple[float, float, float, List[str]]:
        liquidity = 0.0
        stop_hunt = clamp(snapshot.stop_hunt_probability)
        wick_rejection = 0.0
        reasons: List[str] = []

        if snapshot.liquidity_grab_up:
            liquidity += 55
            reasons.append("LIQUIDITY_GRAB_UP")
        if snapshot.liquidity_grab_down:
            liquidity += 55
            reasons.append("LIQUIDITY_GRAB_DOWN")

        # Direction-aware wick risk:
        # For a LONG idea, a large upper wick is rejection risk, while a lower
        # wick can be a bullish sweep/recovery and should not dry REAL signals.
        # For a SHORT idea, the opposite is true.
        if direction == DIRECTION_LONG:
            if snapshot.upper_wick_percent >= 45:
                wick_rejection += 25
                reasons.append("LONG_UPPER_WICK_REJECTION")
            if snapshot.lower_wick_percent >= 55 and not snapshot.liquidity_grab_down:
                reasons.append("LONG_LOWER_WICK_SUPPORT")
        elif direction == DIRECTION_SHORT:
            if snapshot.lower_wick_percent >= 45:
                wick_rejection += 25
                reasons.append("SHORT_LOWER_WICK_REJECTION")
            if snapshot.upper_wick_percent >= 55 and not snapshot.liquidity_grab_up:
                reasons.append("SHORT_UPPER_WICK_RESISTANCE")
        else:
            if snapshot.upper_wick_percent >= 45:
                wick_rejection += 12
                reasons.append("NEUTRAL_LARGE_UPPER_WICK")
            if snapshot.lower_wick_percent >= 45:
                wick_rejection += 12
                reasons.append("NEUTRAL_LARGE_LOWER_WICK")

        if direction == DIRECTION_LONG and snapshot.liquidity_grab_up:
            liquidity += 20
            reasons.append("LONG_AFTER_UP_SWEEP_RISK")
        elif direction == DIRECTION_SHORT and snapshot.liquidity_grab_down:
            liquidity += 20
            reasons.append("SHORT_AFTER_DOWN_SWEEP_RISK")

        if stop_hunt >= 70:
            reasons.append("HIGH_STOP_HUNT_PROBABILITY")

        return clamp(liquidity), clamp(stop_hunt), clamp(wick_rejection), reasons


class CloseQualityTrapDetector:
    """Detects bad close quality relative to intended direction."""

    def score(self, snapshot: SensorSnapshot, direction: str) -> Tuple[float, List[str]]:
        risk = 0.0
        reasons: List[str] = []

        if direction == DIRECTION_LONG:
            if snapshot.close_quality < 0.35:
                risk += 45
                reasons.append("LONG_WEAK_CLOSE")
            elif snapshot.close_quality < 0.50:
                risk += 12
                reasons.append("LONG_CLOSE_NOT_STRONG")
        elif direction == DIRECTION_SHORT:
            if snapshot.close_quality > 0.65:
                risk += 45
                reasons.append("SHORT_STRONG_CLOSE_AGAINST")
            elif snapshot.close_quality > 0.50:
                risk += 12
                reasons.append("SHORT_CLOSE_NOT_WEAK")

        return clamp(risk), reasons


class RangeTrapDetector:
    """Detects fake-move probability inside range/compression."""

    def score(self, snapshot: SensorSnapshot) -> Tuple[float, List[str]]:
        risk = 0.0
        reasons: List[str] = []

        if snapshot.range_probability >= 70:
            risk += 45
            reasons.append("HIGH_RANGE_TRAP")
        elif snapshot.range_probability >= 45:
            risk += 25
            reasons.append("MEDIUM_RANGE_TRAP")

        if snapshot.compression_score >= 70 and not snapshot.atr_explosion:
            # Compression is not always bad; for Movement Hunter it can be the
            # pre-move phase. Keep it as a soft risk, not a REAL killer.
            risk += 22
            reasons.append("COMPRESSION_SOFT_FAKE_MOVE_RISK")

        if snapshot.adx < 16 and snapshot.relative_volume < 0.9:
            risk += 25
            reasons.append("LOW_ADX_LOW_VOLUME_TRAP")

        return clamp(risk), reasons


class DirectionalTrapClassifier:
    """Classifies long-trap / short-trap probability."""

    def classify(
        self,
        snapshot: SensorSnapshot,
        direction: str,
        fake_breakout: float,
        fake_breakdown: float,
        liquidity: float,
        close_risk: float,
        range_trap: float,
    ) -> Tuple[str, float, float, List[str]]:
        reasons: List[str] = []

        long_trap = 0.0
        short_trap = 0.0

        # Long trap: price sweeps up / breakout fails / upper wick / weak close.
        if snapshot.failed_breakout:
            long_trap += 35
        if snapshot.liquidity_grab_up:
            long_trap += 30
        if snapshot.upper_wick_percent >= 45:
            long_trap += 18
        if snapshot.close_quality < 0.45:
            long_trap += 15
        if snapshot.breakout_candidate and snapshot.volume_spike and snapshot.close_quality < 0.55:
            long_trap += 15

        # Short trap: price sweeps down / breakdown fails / lower wick / strong close.
        if snapshot.failed_breakdown:
            short_trap += 35
        if snapshot.liquidity_grab_down:
            short_trap += 30
        if snapshot.lower_wick_percent >= 45:
            short_trap += 18
        if snapshot.close_quality > 0.55:
            short_trap += 15
        if snapshot.breakdown_candidate and snapshot.volume_spike and snapshot.close_quality > 0.45:
            short_trap += 15

        long_trap += range_trap * 0.20
        short_trap += range_trap * 0.20

        long_trap = clamp(long_trap)
        short_trap = clamp(short_trap)

        if long_trap >= 65 and long_trap >= short_trap:
            trap_type = TRAP_LONG
            reasons.append("LONG_TRAP_PROBABLE")
        elif short_trap >= 65 and short_trap > long_trap:
            trap_type = TRAP_SHORT
            reasons.append("SHORT_TRAP_PROBABLE")
        elif fake_breakout >= 60:
            trap_type = TRAP_FAKE_BREAKOUT
            reasons.append("FAKE_BREAKOUT_PROBABLE")
        elif fake_breakdown >= 60:
            trap_type = TRAP_FAKE_BREAKDOWN
            reasons.append("FAKE_BREAKDOWN_PROBABLE")
        elif snapshot.liquidity_grab_up:
            trap_type = TRAP_LIQUIDITY_GRAB_UP
            reasons.append("UP_LIQUIDITY_GRAB")
        elif snapshot.liquidity_grab_down:
            trap_type = TRAP_LIQUIDITY_GRAB_DOWN
            reasons.append("DOWN_LIQUIDITY_GRAB")
        else:
            trap_type = TRAP_NONE

        return trap_type, long_trap, short_trap, reasons


class TrapEngine:
    """
    Main trap engine.

    Input:
        AnalysisCandidate, MovementHunterResult optional, or SensorSnapshot.

    Output:
        TrapResult.

    This is a risk/descriptive layer only.
    """

    def __init__(self):
        self.fake = FakeBreakoutDetector()
        self.liquidity = LiquidityGrabDetector()
        self.close_quality = CloseQualityTrapDetector()
        self.range_trap = RangeTrapDetector()
        self.classifier = DirectionalTrapClassifier()

    def analyze(
        self,
        candidate_or_snapshot: AnalysisCandidate | SensorSnapshot,
        movement: Optional[MovementHunterResult] = None,
    ) -> TrapResult:
        if isinstance(candidate_or_snapshot, AnalysisCandidate):
            snapshot = candidate_or_snapshot.sensor_snapshot
            direction = candidate_or_snapshot.direction_hint
            base_warnings = list(candidate_or_snapshot.warnings)
        else:
            snapshot = candidate_or_snapshot
            direction = self._infer_direction(snapshot)
            base_warnings = list(getattr(snapshot, "warnings", ()))

        reasons: List[str] = []
        warnings: List[str] = list(base_warnings)

        fake_breakout, fake_breakdown, r = self.fake.score(snapshot, direction)
        reasons.extend(r)

        liquidity_risk, stop_hunt, wick_rejection, r = self.liquidity.score(snapshot, direction)
        reasons.extend(r)

        close_risk, r = self.close_quality.score(snapshot, direction)
        reasons.extend(r)

        range_trap, r = self.range_trap.score(snapshot)
        reasons.extend(r)

        trap_type, long_prob, short_prob, r = self.classifier.classify(
            snapshot=snapshot,
            direction=direction,
            fake_breakout=fake_breakout,
            fake_breakdown=fake_breakdown,
            liquidity=liquidity_risk,
            close_risk=close_risk,
            range_trap=range_trap,
        )
        reasons.extend(r)

        total = avg([
            fake_breakout,
            fake_breakdown,
            liquidity_risk,
            stop_hunt,
            wick_rejection,
            close_risk,
            range_trap,
        ])

        # Movement context can increase risk if movement is late/exhausted,
        # but strong fresh movement should soften trap risk so REAL does not
        # become too dry.
        if movement is not None:
            if movement.freshness in {"LATE", "DEAD"}:
                total += 12
                reasons.append("MOVEMENT_LATE_OR_DEAD_TRAP_RISK")
            if movement.reversal_pressure >= 60:
                total += 12
                reasons.append("MOVEMENT_REVERSAL_PRESSURE_TRAP_RISK")

            strong_fresh = (
                movement.freshness == "FRESH"
                and movement.readiness_score >= 65
                and movement.continuation_probability >= 55
                and total < 75
            )
            if strong_fresh:
                total -= 10
                reasons.append("FRESH_CONFIRMED_MOVEMENT_SOFTENS_TRAP_RISK")

            strong_mid = (
                movement.freshness == "MID"
                and movement.readiness_score >= 72
                and movement.continuation_probability >= 62
                and total < 70
            )
            if strong_mid:
                total -= 6
                reasons.append("STRONG_MID_MOVEMENT_SOFTENS_TRAP_RISK")

        total = clamp(total)
        liquidity_total = clamp(avg([liquidity_risk, stop_hunt, wick_rejection]))

        if total >= 65:
            warnings.append("HIGH_TRAP_RISK")
        if liquidity_total >= 65:
            warnings.append("HIGH_LIQUIDITY_RISK")
        if not snapshot.valid:
            warnings.append("INVALID_SENSOR_SNAPSHOT")

        score = TrapScore(
            fake_breakout_score=fake_breakout,
            fake_breakdown_score=fake_breakdown,
            liquidity_grab_score=liquidity_risk,
            stop_hunt_score=stop_hunt,
            wick_rejection_score=wick_rejection,
            close_quality_risk=close_risk,
            range_trap_score=range_trap,
            total_score=total,
        )

        return TrapResult(
            trap_id=f"trap_{uuid4().hex}",
            symbol=snapshot.symbol,
            timeframe=snapshot.timeframe,
            timestamp=snapshot.timestamp or int(time.time()),
            direction_hint=direction,
            trap_type=trap_type,
            trap_risk=total,
            trap_level=risk_level(total),
            liquidity_risk=liquidity_total,
            long_trap_probability=long_prob,
            short_trap_probability=short_prob,
            score=score,
            reason_codes=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
            valid=bool(snapshot.valid),
        )

    def _infer_direction(self, snapshot: SensorSnapshot) -> str:
        long_points = 0
        short_points = 0

        if snapshot.power_delta > 0:
            long_points += 1
        elif snapshot.power_delta < 0:
            short_points += 1

        if snapshot.rsi_slope > 0:
            long_points += 1
        elif snapshot.rsi_slope < 0:
            short_points += 1

        if snapshot.histogram_slope > 0:
            long_points += 1
        elif snapshot.histogram_slope < 0:
            short_points += 1

        if snapshot.vwap_state in {"ABOVE", "RECLAIM"}:
            long_points += 1
        elif snapshot.vwap_state in {"BELOW", "LOSS"}:
            short_points += 1

        if long_points > short_points:
            return DIRECTION_LONG
        if short_points > long_points:
            return DIRECTION_SHORT
        return DIRECTION_NEUTRAL


_default_engine: Optional[TrapEngine] = None


def engine() -> TrapEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = TrapEngine()
    return _default_engine


def analyze_trap(
    candidate_or_snapshot: AnalysisCandidate | SensorSnapshot,
    movement: Optional[MovementHunterResult] = None,
) -> TrapResult:
    return engine().analyze(candidate_or_snapshot, movement=movement)


def trap_engine(
    candidate_or_snapshot: AnalysisCandidate | SensorSnapshot,
    movement: Optional[MovementHunterResult] = None,
) -> TrapResult:
    return analyze_trap(candidate_or_snapshot, movement=movement)
