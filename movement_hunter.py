from __future__ import annotations

"""
08 - movement_hunter.py

Movement Hunter layer for the locked crypto futures bot architecture.

Responsibilities:
- Detect ultra-early fresh movement readiness.
- Detect pump/dump start conditions from sensors.
- Identify fresh / mid / late / exhausted movement phase.
- Detect ATR expansion, power shift, momentum ignition, range suppression.
- Produce MovementHunterResult for AI decision layer.

Strictly forbidden:
- No REAL/GHOST/REJECT.
- No trade execution.
- No Toobit calls.
- No Telegram.
- No persistence.
- No Paper mode.
- No Setup flow.

This file is not the final decision-maker.
It only describes movement readiness and movement phase.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4
import time

from analysis_layers import SensorSnapshot
from analysis_engine import AnalysisCandidate


JsonDict = Dict[str, Any]


DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"

PHASE_START = "START"
PHASE_EARLY = "EARLY"
PHASE_MIDDLE = "MIDDLE"
PHASE_LATE = "LATE"
PHASE_EXHAUSTION = "EXHAUSTION"
PHASE_RANGE = "RANGE"
PHASE_UNKNOWN = "UNKNOWN"

FRESH_FRESH = "FRESH"
FRESH_MID = "MID"
FRESH_LATE = "LATE"
FRESH_DEAD = "DEAD"
FRESH_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class MovementScore:
    early_momentum: float
    atr_expansion: float
    power_shift: float
    volume_participation: float
    breakout_readiness: float
    range_suppression: float
    exhaustion_penalty: float
    total_score: float

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class MovementHunterResult:
    movement_id: str
    symbol: str
    timeframe: str
    timestamp: int
    direction_hint: str
    movement_phase: str
    freshness: str
    readiness_score: float
    continuation_probability: float
    reversal_pressure: float
    range_probability: float
    score: MovementScore
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    valid: bool = True

    def to_dict(self) -> JsonDict:
        return {
            "movement_id": self.movement_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp,
            "direction_hint": self.direction_hint,
            "movement_phase": self.movement_phase,
            "freshness": self.freshness,
            "readiness_score": self.readiness_score,
            "continuation_probability": self.continuation_probability,
            "reversal_pressure": self.reversal_pressure,
            "range_probability": self.range_probability,
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


def _direction_from_input(candidate_or_snapshot: Any) -> str:
    if isinstance(candidate_or_snapshot, AnalysisCandidate):
        return candidate_or_snapshot.direction_hint
    return DIRECTION_NEUTRAL


class EarlyMomentumEngine:
    """
    Detects 0-candle / 1-candle / 2-candle movement ignition using sensors.

    Movement Hunter rule:
    - Do not wait until the move is obvious.
    - Detect birth-of-momentum from small but aligned RSI/MACD/Power/VWAP/volume/compression clues.
    - This layer does not decide REAL/GHOST; it only tells the AI that a move may be forming.
    """

    def score(self, snapshot: SensorSnapshot, direction: str) -> Tuple[float, List[str]]:
        score = 0.0
        reasons: List[str] = []

        # Generic participation context used by both directions.
        volume_live = bool(snapshot.volume_expansion or snapshot.volume_spike)
        atr_live = bool(snapshot.atr_expansion == "EXPANDING" or snapshot.atr_explosion or snapshot.atr_slope > 0)
        compression_live = bool(snapshot.compression_score >= 38 and snapshot.range_probability < 86)
        power_abs = abs(float(getattr(snapshot, "power_delta", 0.0) or 0.0))

        if direction == DIRECTION_LONG:
            # Ultra-early bullish birth. Keep thresholds soft but require alignment across clues.
            if snapshot.rsi_slope > 0.08:
                score += 12
                reasons.append("BIRTH_RSI_SLOPE_UP")
            if snapshot.rsi_acceleration > 0.02:
                score += 8
                reasons.append("BIRTH_RSI_ACCEL_UP")
            if snapshot.histogram_slope > 0:
                score += 15
                reasons.append("BIRTH_HIST_SLOPE_UP")
            if snapshot.histogram_acceleration > 0:
                score += 13
                reasons.append("BIRTH_HIST_ACCEL_UP")
            if snapshot.power_delta > 5:
                score += 12
                reasons.append("BIRTH_BUY_POWER_SHIFT")
            if snapshot.power_delta > 14:
                score += 8
                reasons.append("STRONG_BUY_POWER_BIRTH")
            if snapshot.vwap_state in {"RECLAIM", "ABOVE"}:
                score += 9
                reasons.append("VWAP_BULLISH_CONTEXT")
            if snapshot.failed_breakdown or snapshot.liquidity_grab_down:
                score += 10
                reasons.append("SELL_SIDE_SWEEP_RECOVERY_BIRTH")
            if snapshot.close_quality > 0.58:
                score += 5
                reasons.append("BULL_CLOSE_QUALITY_BIRTH")

            aligned_birth = (
                (snapshot.rsi_slope > 0 and snapshot.histogram_slope > 0 and snapshot.power_delta > 0)
                or (snapshot.histogram_acceleration > 0 and snapshot.power_delta > 8)
            )
            if aligned_birth:
                score += 10
                reasons.append("ALIGNED_BULLISH_BIRTH_CLUSTER")

        elif direction == DIRECTION_SHORT:
            # Ultra-early bearish birth. Same logic mirrored for dumps.
            if snapshot.rsi_slope < -0.08:
                score += 12
                reasons.append("BIRTH_RSI_SLOPE_DOWN")
            if snapshot.rsi_acceleration < -0.02:
                score += 8
                reasons.append("BIRTH_RSI_ACCEL_DOWN")
            if snapshot.histogram_slope < 0:
                score += 15
                reasons.append("BIRTH_HIST_SLOPE_DOWN")
            if snapshot.histogram_acceleration < 0:
                score += 13
                reasons.append("BIRTH_HIST_ACCEL_DOWN")
            if snapshot.power_delta < -5:
                score += 12
                reasons.append("BIRTH_SELL_POWER_SHIFT")
            if snapshot.power_delta < -14:
                score += 8
                reasons.append("STRONG_SELL_POWER_BIRTH")
            if snapshot.vwap_state in {"LOSS", "BELOW"}:
                score += 9
                reasons.append("VWAP_BEARISH_CONTEXT")
            if snapshot.failed_breakout or snapshot.liquidity_grab_up:
                score += 10
                reasons.append("BUY_SIDE_SWEEP_REVERSAL_BIRTH")
            if snapshot.close_quality < 0.42:
                score += 5
                reasons.append("BEAR_CLOSE_QUALITY_BIRTH")

            aligned_birth = (
                (snapshot.rsi_slope < 0 and snapshot.histogram_slope < 0 and snapshot.power_delta < 0)
                or (snapshot.histogram_acceleration < 0 and snapshot.power_delta < -8)
            )
            if aligned_birth:
                score += 10
                reasons.append("ALIGNED_BEARISH_BIRTH_CLUSTER")

        if volume_live:
            score += 9 if snapshot.volume_expansion else 0
            score += 9 if snapshot.volume_spike else 0
            reasons.append("LIVE_VOLUME_PARTICIPATION")

        # Compression before breakout is valuable even before full ATR expansion.
        if compression_live and (power_abs >= 8 or volume_live or atr_live):
            score += 12
            reasons.append("COMPRESSION_WITH_EARLY_PARTICIPATION")
        elif compression_live and power_abs >= 5:
            score += 7
            reasons.append("EARLY_SQUEEZE_PRESSURE")

        if atr_live and power_abs >= 5:
            score += 6
            reasons.append("ATR_POWER_IGNITION_COMBO")

        return clamp(score), reasons

class ATRMovementEngine:
    """ATR expansion / volatility ignition detection."""

    def score(self, snapshot: SensorSnapshot) -> Tuple[float, List[str]]:
        score = 0.0
        reasons: List[str] = []

        if snapshot.atr_explosion:
            score += 48
            reasons.append("ATR_EXPLOSION")
        elif snapshot.atr_expansion == "EXPANDING":
            score += 34
            reasons.append("ATR_EXPANDING")
        elif snapshot.atr_expansion == "SHRINKING":
            score -= 5
            reasons.append("ATR_SHRINKING_SOFT")

        if snapshot.atr_slope > 0:
            score += 12
            reasons.append("ATR_SLOPE_UP")

        if snapshot.compression_score >= 60 and snapshot.atr_slope > 0:
            score += 8
            reasons.append("PRE_EXPANSION_ATR_FROM_COMPRESSION")

        if snapshot.atr_percent > 0:
            score += min(20.0, snapshot.atr_percent * 8.0)

        return clamp(score), reasons


class BreakoutReadinessEngine:
    """Detects movement readiness without making trade decisions."""

    def score(self, snapshot: SensorSnapshot, direction: str) -> Tuple[float, List[str]]:
        score = 0.0
        reasons: List[str] = []

        if direction == DIRECTION_LONG:
            if snapshot.breakout_candidate:
                score += 24
                reasons.append("BREAKOUT_CANDIDATE")
            if snapshot.failed_breakdown:
                score += 14
                reasons.append("FAILED_BREAKDOWN_RECOVERY")
            if snapshot.liquidity_grab_down:
                score += 10
                reasons.append("LIQUIDITY_GRAB_DOWN_REVERSAL")
        elif direction == DIRECTION_SHORT:
            if snapshot.breakdown_candidate:
                score += 24
                reasons.append("BREAKDOWN_CANDIDATE")
            if snapshot.failed_breakout:
                score += 14
                reasons.append("FAILED_BREAKOUT_REVERSAL")
            if snapshot.liquidity_grab_up:
                score += 10
                reasons.append("LIQUIDITY_GRAB_UP_REVERSAL")

        return clamp(score), reasons


class RangeSuppressionEngine:
    """Range is the main enemy of REAL movement hunting."""

    def score(self, snapshot: SensorSnapshot) -> Tuple[float, List[str]]:
        reasons: List[str] = []
        penalty = clamp(snapshot.range_probability)

        if snapshot.range_probability >= 70:
            reasons.append("HIGH_RANGE_PROBABILITY")
        elif snapshot.range_probability >= 45:
            reasons.append("MEDIUM_RANGE_PROBABILITY")

        # Keep range as a risk factor, but do not let normal compression kill every scalp setup.
        # Only strong compression without any ATR expansion should create a heavy range penalty.
        if snapshot.compression_score >= 82 and not snapshot.atr_explosion and snapshot.atr_expansion != "EXPANDING":
            penalty = max(penalty, 62.0)
            reasons.append("COMPRESSION_WITHOUT_EXPLOSION_SOFT")
        elif snapshot.compression_score >= 60 and (snapshot.atr_expansion == "EXPANDING" or snapshot.atr_slope > 0):
            penalty = min(penalty, 45.0)
            reasons.append("COMPRESSION_CAN_BE_PRE_MOVE")

        # Low ADX + low volume is a warning, not an automatic signal killer.
        if snapshot.adx < 13 and snapshot.relative_volume < 0.65 and snapshot.atr_expansion != "EXPANDING":
            penalty = max(penalty, 55.0)
            reasons.append("WEAK_ADX_LOW_VOLUME_RANGE_SOFT")

        return clamp(penalty), reasons


class ExhaustionPressureEngine:
    """Detects late or tired movement."""

    def score(self, snapshot: SensorSnapshot, direction: str) -> Tuple[float, List[str]]:
        penalty = 0.0
        reasons: List[str] = []

        if snapshot.momentum_weakness:
            penalty += 35
            reasons.append("MOMENTUM_WEAKNESS")

        if direction == DIRECTION_LONG and snapshot.bull_exhaustion:
            penalty += 45
            reasons.append("BULL_EXHAUSTION")
        elif direction == DIRECTION_SHORT and snapshot.bear_exhaustion:
            penalty += 45
            reasons.append("BEAR_EXHAUSTION")

        if abs(snapshot.price_change_percent) > max(snapshot.atr_percent * 2.9, 2.0):
            penalty += 15
            reasons.append("POSSIBLY_LATE_EXTENDED_MOVE_SOFT")

        if snapshot.stop_hunt_probability >= 70:
            penalty += 15
            reasons.append("HIGH_STOP_HUNT_PRESSURE")

        return clamp(penalty), reasons


class MovementPhaseClassifier:
    """Classifies movement phase for AI.

    Goal: produce more FRESH/EARLY/PRE-MOVE descriptions and avoid calling
    forming moves LATE too early. Final REAL/GHOST still belongs to AI decision.
    """

    def classify(self, snapshot: SensorSnapshot, score: MovementScore, direction: str) -> Tuple[str, str, float, List[str]]:
        reasons: List[str] = []
        readiness = score.total_score
        reversal_pressure = clamp(score.exhaustion_penalty + snapshot.stop_hunt_probability * 0.35)

        compression_birth = (
            snapshot.compression_score >= 38
            and snapshot.range_probability < 86
            and (
                abs(snapshot.power_delta) >= 8
                or snapshot.volume_expansion
                or snapshot.volume_spike
                or snapshot.atr_expansion == "EXPANDING"
                or snapshot.atr_slope > 0
            )
        )
        momentum_birth = score.early_momentum >= 24 and abs(snapshot.power_delta) >= 5
        live_ignition = score.early_momentum >= 34 or score.atr_expansion >= 28 or score.breakout_readiness >= 20

        # Severe range only when no birth/ignition evidence exists.
        if (
            snapshot.range_probability >= 90
            and readiness < 28
            and not snapshot.atr_explosion
            and snapshot.atr_expansion != "EXPANDING"
            and not compression_birth
            and not momentum_birth
        ):
            reasons.append("PHASE_RANGE_SEVERE_NO_BIRTH")
            return PHASE_RANGE, FRESH_DEAD, reversal_pressure, reasons

        if (
            snapshot.range_probability >= 82
            and readiness < 34
            and snapshot.atr_expansion != "EXPANDING"
            and not compression_birth
            and not momentum_birth
        ):
            reasons.append("PHASE_RANGE_LATE_NO_BREAKOUT_BIRTH")
            return PHASE_RANGE, FRESH_LATE, reversal_pressure, reasons

        # Exhaustion should not kill a forming counter-move / squeeze birth.
        if score.exhaustion_penalty >= 90 and readiness < 46 and not compression_birth and not momentum_birth:
            reasons.append("PHASE_EXHAUSTION_SEVERE_CONFIRMED")
            return PHASE_EXHAUSTION, FRESH_DEAD, reversal_pressure, reasons

        if score.exhaustion_penalty >= 76 and readiness < 46 and not live_ignition:
            reasons.append("PHASE_EXHAUSTION_MODERATE_CONFIRMED")
            return PHASE_EXHAUSTION, FRESH_LATE, reversal_pressure, reasons

        # Strongest hunter states first.
        if readiness >= 62 and score.early_momentum >= 38:
            reasons.append("PHASE_EARLY_STRONG_BIRTH")
            return PHASE_EARLY, FRESH_FRESH, reversal_pressure, reasons

        if readiness >= 46 and (score.early_momentum >= 26 or compression_birth):
            reasons.append("PHASE_START_BIRTH_CONFIRMED")
            return PHASE_START, FRESH_FRESH, reversal_pressure, reasons

        # Pre-start / weak fresh state: keep it fresh for predictor/AI learning instead of labeling it LATE.
        if compression_birth or momentum_birth:
            reasons.append("PHASE_START_PRE_MOVE_BIRTH")
            return PHASE_START, FRESH_FRESH, reversal_pressure, reasons

        # MID now requires stronger readiness than before so weak early setups are not swallowed as MID.
        if readiness >= 44 and live_ignition:
            reasons.append("PHASE_MIDDLE_LIVE_MOVE")
            return PHASE_MIDDLE, FRESH_MID, reversal_pressure, reasons

        # Low readiness with some early clues remains UNKNOWN/FRESH-ish for learning, not automatic late.
        if readiness >= 24 and score.early_momentum >= 18:
            reasons.append("PHASE_UNKNOWN_EARLY_WATCH")
            return PHASE_UNKNOWN, FRESH_UNKNOWN, reversal_pressure, reasons

        if readiness >= 24:
            reasons.append("PHASE_LATE_LOW_IGNITION")
            return PHASE_LATE, FRESH_LATE, reversal_pressure, reasons

        reasons.append("PHASE_UNKNOWN")
        return PHASE_UNKNOWN, FRESH_UNKNOWN, reversal_pressure, reasons

class MovementHunter:
    """
    Main movement hunter layer.

    Input:
        AnalysisCandidate or SensorSnapshot

    Output:
        MovementHunterResult

    This is not the final AI decision.
    """

    def __init__(self):
        self.early = EarlyMomentumEngine()
        self.atr = ATRMovementEngine()
        self.breakout = BreakoutReadinessEngine()
        self.range = RangeSuppressionEngine()
        self.exhaustion = ExhaustionPressureEngine()
        self.phase = MovementPhaseClassifier()

    def analyze(self, candidate_or_snapshot: AnalysisCandidate | SensorSnapshot) -> MovementHunterResult:
        if isinstance(candidate_or_snapshot, AnalysisCandidate):
            snapshot = candidate_or_snapshot.sensor_snapshot
            direction = candidate_or_snapshot.direction_hint
            base_warnings = list(candidate_or_snapshot.warnings)
        else:
            snapshot = candidate_or_snapshot
            direction = DIRECTION_NEUTRAL
            base_warnings = list(getattr(snapshot, "warnings", ()))

        if direction not in {DIRECTION_LONG, DIRECTION_SHORT}:
            direction = self._infer_direction_from_snapshot(snapshot)

        reasons: List[str] = []
        warnings: List[str] = list(base_warnings)

        early_score, early_reasons = self.early.score(snapshot, direction)
        atr_score, atr_reasons = self.atr.score(snapshot)
        breakout_score, breakout_reasons = self.breakout.score(snapshot, direction)
        range_penalty, range_reasons = self.range.score(snapshot)
        exhaustion_penalty, exhaustion_reasons = self.exhaustion.score(snapshot, direction)

        power_shift = clamp(abs(snapshot.power_delta) * 1.6)
        volume_participation = clamp(snapshot.relative_volume * 35.0)
        if snapshot.volume_spike:
            volume_participation = max(volume_participation, 85.0)

        raw_total = (
            early_score * 0.44
            + atr_score * 0.18
            + power_shift * 0.16
            + volume_participation * 0.10
            + breakout_score * 0.12
        )

        # Balanced suppression: range/exhaustion reduce quality, but should not silence
        # every early-movement candidate before AI can classify REAL/GHOST/REJECT.
        # Birth-of-momentum bonus: avoid losing the first candle when multiple
        # early clues align before the move is obvious.
        birth_bonus = 0.0
        if early_score >= 32 and abs(snapshot.power_delta) >= 8:
            birth_bonus += 6.0
            reasons.append("BIRTH_BONUS_POWER_ALIGNED")
        if snapshot.compression_score >= 40 and snapshot.range_probability < 86 and (snapshot.volume_expansion or snapshot.atr_slope > 0 or abs(snapshot.power_delta) >= 10):
            birth_bonus += 6.0
            reasons.append("BIRTH_BONUS_SQUEEZE_CONTEXT")

        penalty = range_penalty * 0.14 + exhaustion_penalty * 0.26
        total = clamp(raw_total + birth_bonus - penalty)

        movement_score = MovementScore(
            early_momentum=early_score,
            atr_expansion=atr_score,
            power_shift=power_shift,
            volume_participation=volume_participation,
            breakout_readiness=breakout_score,
            range_suppression=range_penalty,
            exhaustion_penalty=exhaustion_penalty,
            total_score=total,
        )

        phase, freshness, reversal_pressure, phase_reasons = self.phase.classify(snapshot, movement_score, direction)

        continuation = clamp(
            total
            - reversal_pressure * 0.34
            - snapshot.range_probability * 0.14
            + (10 if snapshot.atr_explosion else 0)
            + (6 if (snapshot.compression_score >= 40 and abs(snapshot.power_delta) >= 8) else 0)
        )

        reasons.extend(early_reasons)
        reasons.extend(atr_reasons)
        reasons.extend(breakout_reasons)
        reasons.extend(range_reasons)
        reasons.extend(exhaustion_reasons)
        reasons.extend(phase_reasons)

        if range_penalty >= 70:
            warnings.append("RANGE_SUPPRESSION_ACTIVE")
        if exhaustion_penalty >= 60:
            warnings.append("EXHAUSTION_SUPPRESSION_ACTIVE")
        if not snapshot.valid:
            warnings.append("INVALID_SENSOR_SNAPSHOT")

        return MovementHunterResult(
            movement_id=f"move_{uuid4().hex}",
            symbol=snapshot.symbol,
            timeframe=snapshot.timeframe,
            timestamp=snapshot.timestamp or int(time.time()),
            direction_hint=direction,
            movement_phase=phase,
            freshness=freshness,
            readiness_score=total,
            continuation_probability=continuation,
            reversal_pressure=reversal_pressure,
            range_probability=clamp(snapshot.range_probability),
            score=movement_score,
            reason_codes=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
            valid=bool(snapshot.valid),
        )

    def _infer_direction_from_snapshot(self, snapshot: SensorSnapshot) -> str:
        long_points = 0
        short_points = 0

        if snapshot.rsi_slope > 0:
            long_points += 1
        elif snapshot.rsi_slope < 0:
            short_points += 1

        if snapshot.histogram_slope > 0:
            long_points += 1
        elif snapshot.histogram_slope < 0:
            short_points += 1

        if snapshot.power_delta > 0:
            long_points += 1
        elif snapshot.power_delta < 0:
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


_default_hunter: Optional[MovementHunter] = None


def hunter() -> MovementHunter:
    global _default_hunter
    if _default_hunter is None:
        _default_hunter = MovementHunter()
    return _default_hunter


def analyze_movement(candidate_or_snapshot: AnalysisCandidate | SensorSnapshot) -> MovementHunterResult:
    return hunter().analyze(candidate_or_snapshot)


def movement_hunter(candidate_or_snapshot: AnalysisCandidate | SensorSnapshot) -> MovementHunterResult:
    return analyze_movement(candidate_or_snapshot)
