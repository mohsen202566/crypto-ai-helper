from __future__ import annotations

"""
17 - meta_learning.py

Meta-learning / self-audit layer for the locked Movement Hunter architecture.

Responsibilities:
- Track which AI layers were helpful or harmful over time.
- Learn layer reliability from REAL and GHOST outcomes.
- Maintain soft weights for:
  analysis_engine
  movement_hunter
  trap_engine
  state_engine
  confidence_engine
  correlation_engine
  coin_learning
  movement_memory
  movement_predictor
- Provide weights and audit summaries to ai_decision_engine.py.
- Never directly decide REAL/GHOST/REJECT.

Strictly forbidden:
- No trade execution.
- No Toobit calls.
- No Telegram.
- No Paper mode.
- No Setup flow.
- No final AI decision.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import uuid4
import math
import time

from analysis_engine import AnalysisCandidate
from movement_hunter import MovementHunterResult
from trap_engine import TrapResult
from state_engine import StateResult
from confidence_engine import ConfidenceResult
from correlation_engine import CorrelationResult
from movement_predictor import MovementPredictionResult
from data_store import save_meta_learning, store
from config import SETTINGS


JsonDict = Dict[str, Any]

SOURCE_REAL = "REAL"
SOURCE_GHOST = "GHOST"

RESULT_TP1 = "TP1"
RESULT_TP2 = "TP2"
RESULT_AI_EXIT = "AI_EXIT"
RESULT_SL = "SL"
RESULT_UNKNOWN = "UNKNOWN"

MODULE_ANALYSIS = "analysis_engine"
MODULE_MOVEMENT = "movement_hunter"
MODULE_TRAP = "trap_engine"
MODULE_STATE = "state_engine"
MODULE_CONFIDENCE = "confidence_engine"
MODULE_CORRELATION = "correlation_engine"
MODULE_COIN_LEARNING = "coin_learning"
MODULE_MOVEMENT_MEMORY = "movement_memory"
MODULE_MOVEMENT_PREDICTOR = "movement_predictor"

DEFAULT_MODULES = (
    MODULE_ANALYSIS,
    MODULE_MOVEMENT,
    MODULE_TRAP,
    MODULE_STATE,
    MODULE_CONFIDENCE,
    MODULE_CORRELATION,
    MODULE_COIN_LEARNING,
    MODULE_MOVEMENT_MEMORY,
    MODULE_MOVEMENT_PREDICTOR,
)


@dataclass(frozen=True)
class ModuleAuditRecord:
    audit_id: str
    module_name: str
    source_type: str
    result: str
    timestamp: int
    predicted_positive: bool
    outcome_positive: bool
    contribution_score: float
    confidence_before: float
    weight_before: float
    weight_after: float
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class MetaLearningRecord:
    module_name: str
    sample_count: int
    success_count: int
    failure_count: int
    real_samples: int
    ghost_samples: int
    weight: float
    reliability: float
    last_updated: int
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class MetaLearningSummary:
    module_weights: Dict[str, float]
    module_reliability: Dict[str, float]
    best_modules: Tuple[str, ...]
    weak_modules: Tuple[str, ...]
    sample_count: int
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)


def now_ts() -> int:
    return int(time.time())


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except Exception:
        return low


def result_positive(result: str) -> bool:
    return str(result).upper() in {RESULT_TP1, RESULT_TP2, RESULT_AI_EXIT}


def source_weight(source_type: str) -> float:
    if str(source_type).upper() == SOURCE_REAL:
        return safe_float(getattr(SETTINGS.learning, "real_weight", 1.0), 1.0)
    return safe_float(getattr(SETTINGS.learning, "ghost_weight", 0.7), 0.7)


class ModuleContributionExtractor:
    """
    Converts each layer output into a directional contribution.

    predicted_positive=True means the module supported the trade idea.
    If outcome is positive, that module gets rewarded.
    If outcome is negative, it gets penalized.
    """

    def extract(
        self,
        candidate: AnalysisCandidate,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
        confidence: Optional[ConfidenceResult] = None,
        correlation: Optional[CorrelationResult] = None,
        prediction: Optional[MovementPredictionResult] = None,
    ) -> Dict[str, Tuple[bool, float, Tuple[str, ...]]]:
        result: Dict[str, Tuple[bool, float, Tuple[str, ...]]] = {}

        result[MODULE_ANALYSIS] = (
            candidate.valid and candidate.quality.total_quality >= 50 and candidate.risk.total_risk < 65,
            clamp(candidate.quality.total_quality - candidate.risk.total_risk * 0.35),
            tuple(candidate.reason_codes),
        )

        if movement:
            result[MODULE_MOVEMENT] = (
                movement.valid and movement.readiness_score >= 55 and movement.freshness in {"FRESH", "MID"},
                clamp(movement.readiness_score - movement.reversal_pressure * 0.35),
                tuple(movement.reason_codes),
            )

        if trap:
            result[MODULE_TRAP] = (
                trap.valid and trap.trap_risk < 60,
                clamp(100.0 - trap.trap_risk),
                tuple(trap.reason_codes),
            )

        if state:
            result[MODULE_STATE] = (
                state.valid and state.market_state not in {"RANGE", "EXHAUSTION", "LATE"} and state.late_entry_risk < 60,
                clamp(state.state_confidence - state.late_entry_risk * 0.35 - state.exhaustion_risk * 0.35),
                tuple(state.reason_codes),
            )

        if confidence:
            result[MODULE_CONFIDENCE] = (
                confidence.valid and confidence.confidence_score >= 55 and not confidence.should_reject_if_risk_high,
                clamp(confidence.confidence_score),
                tuple(confidence.reason_codes),
            )

        if correlation:
            result[MODULE_CORRELATION] = (
                correlation.valid and correlation.exposure_risk < 65 and not correlation.should_block_if_risk_high,
                clamp(100.0 - correlation.exposure_risk),
                tuple(correlation.reason_codes),
            )

        if prediction:
            result[MODULE_MOVEMENT_PREDICTOR] = (
                prediction.valid and prediction.movement_probability >= 55 and prediction.predicted_phase not in {"RANGE", "LATE"},
                clamp(prediction.movement_probability),
                tuple(prediction.reason_codes),
            )
            result[MODULE_MOVEMENT_MEMORY] = (
                prediction.sample_count >= 5 and prediction.similarity_score >= 50,
                clamp(prediction.similarity_score),
                tuple(prediction.reason_codes),
            )

        # Coin learning is not always separately available here; represented via confidence known_sample_count.
        if confidence:
            result[MODULE_COIN_LEARNING] = (
                confidence.known_sample_count >= 5 and confidence.similar_win_rate >= 50,
                clamp(confidence.similar_win_rate),
                tuple(confidence.reason_codes),
            )

        return result


class MetaLearningState:
    """In-memory state with persistence adapter."""

    def __init__(self, records: Optional[Iterable[Any]] = None):
        self.records: Dict[str, MetaLearningRecord] = {}
        self.audits: List[ModuleAuditRecord] = []
        for module in DEFAULT_MODULES:
            self.records[module] = MetaLearningRecord(
                module_name=module,
                sample_count=0,
                success_count=0,
                failure_count=0,
                real_samples=0,
                ghost_samples=0,
                weight=1.0,
                reliability=50.0,
                last_updated=now_ts(),
                notes=(),
            )

        for item in records or []:
            try:
                record = self._coerce_record(item)
                self.records[record.module_name] = record
            except Exception:
                continue

    def _coerce_record(self, item: Any) -> MetaLearningRecord:
        if isinstance(item, MetaLearningRecord):
            return item
        if hasattr(item, "to_dict") and callable(item.to_dict):
            item = item.to_dict()
        if not isinstance(item, dict):
            item = {}
        return MetaLearningRecord(
            module_name=str(item.get("module_name", "")),
            sample_count=safe_int(item.get("sample_count")),
            success_count=safe_int(item.get("success_count")),
            failure_count=safe_int(item.get("failure_count")),
            real_samples=safe_int(item.get("real_samples")),
            ghost_samples=safe_int(item.get("ghost_samples")),
            weight=safe_float(item.get("weight"), 1.0),
            reliability=safe_float(item.get("reliability"), 50.0),
            last_updated=safe_int(item.get("last_updated"), now_ts()),
            notes=tuple(item.get("notes", ()) or ()),
        )

    def update_module(
        self,
        module_name: str,
        source_type: str,
        predicted_positive: bool,
        outcome_positive: bool,
        contribution_score: float,
        reason_codes: Sequence[str],
    ) -> ModuleAuditRecord:
        old = self.records.get(module_name) or MetaLearningRecord(
            module_name=module_name,
            sample_count=0,
            success_count=0,
            failure_count=0,
            real_samples=0,
            ghost_samples=0,
            weight=1.0,
            reliability=50.0,
            last_updated=now_ts(),
            notes=(),
        )

        weighted_success = predicted_positive == outcome_positive
        sample_count = old.sample_count + 1
        success_count = old.success_count + (1 if weighted_success else 0)
        failure_count = old.failure_count + (0 if weighted_success else 1)
        real_samples = old.real_samples + (1 if str(source_type).upper() == SOURCE_REAL else 0)
        ghost_samples = old.ghost_samples + (1 if str(source_type).upper() == SOURCE_GHOST else 0)

        reliability = success_count / sample_count * 100.0 if sample_count else 50.0

        # Smooth weight. Keep bounded so one bad streak cannot destroy a layer.
        sw = source_weight(source_type)
        delta = (1.5 if weighted_success else -1.8) * sw
        if contribution_score < 35 and predicted_positive:
            delta -= 0.8 * sw
        elif contribution_score > 70 and weighted_success:
            delta += 0.5 * sw

        weight_after = clamp(old.weight * 100.0 + delta, 35.0, 135.0) / 100.0

        notes: List[str] = []
        if sample_count < int(getattr(SETTINGS.learning, "min_samples_for_confidence", 10)):
            notes.append("LOW_SAMPLE_COUNT")
        if reliability >= 65:
            notes.append("RELIABLE_LAYER")
        elif reliability <= 40 and sample_count >= 10:
            notes.append("WEAK_LAYER")

        new_record = MetaLearningRecord(
            module_name=module_name,
            sample_count=sample_count,
            success_count=success_count,
            failure_count=failure_count,
            real_samples=real_samples,
            ghost_samples=ghost_samples,
            weight=weight_after,
            reliability=clamp(reliability),
            last_updated=now_ts(),
            notes=tuple(notes),
        )
        self.records[module_name] = new_record

        audit = ModuleAuditRecord(
            audit_id=f"audit_{uuid4().hex}",
            module_name=module_name,
            source_type=str(source_type).upper(),
            result="MATCH" if weighted_success else "MISS",
            timestamp=now_ts(),
            predicted_positive=bool(predicted_positive),
            outcome_positive=bool(outcome_positive),
            contribution_score=clamp(contribution_score),
            confidence_before=old.reliability,
            weight_before=old.weight,
            weight_after=weight_after,
            reason_codes=tuple(reason_codes),
        )
        self.audits.append(audit)

        max_records = max(500, int(getattr(SETTINGS.learning, "max_records", 20000)))
        if len(self.audits) > max_records:
            self.audits = self.audits[-max_records:]

        return audit

    def summary(self) -> MetaLearningSummary:
        weights = {name: record.weight for name, record in self.records.items()}
        reliability = {name: record.reliability for name, record in self.records.items()}
        total_samples = sum(record.sample_count for record in self.records.values())

        best = tuple(
            name for name, record in sorted(self.records.items(), key=lambda kv: kv[1].reliability, reverse=True)
            if record.sample_count >= 10 and record.reliability >= 65
        )[:5]
        weak = tuple(
            name for name, record in sorted(self.records.items(), key=lambda kv: kv[1].reliability)
            if record.sample_count >= 10 and record.reliability <= 40
        )[:5]

        notes: List[str] = []
        if total_samples < 50:
            notes.append("META_LEARNING_LOW_DATA")
        if best:
            notes.append("HAS_RELIABLE_MODULES")
        if weak:
            notes.append("HAS_WEAK_MODULES")

        return MetaLearningSummary(
            module_weights=weights,
            module_reliability=reliability,
            best_modules=best,
            weak_modules=weak,
            sample_count=total_samples,
            notes=tuple(notes),
        )


class MetaLearningEngine:
    """Main meta-learning engine."""

    def __init__(self, records: Optional[Iterable[Any]] = None):
        if records is None:
            try:
                records = store().section("meta_learning").values()
            except Exception:
                records = []
        self.state = MetaLearningState(records=records)
        self.extractor = ModuleContributionExtractor()

    def audit_outcome(
        self,
        source_type: str,
        result: str,
        candidate: AnalysisCandidate,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
        confidence: Optional[ConfidenceResult] = None,
        correlation: Optional[CorrelationResult] = None,
        prediction: Optional[MovementPredictionResult] = None,
        persist: bool = True,
    ) -> List[ModuleAuditRecord]:
        outcome_positive = result_positive(result)
        contributions = self.extractor.extract(
            candidate=candidate,
            movement=movement,
            trap=trap,
            state=state,
            confidence=confidence,
            correlation=correlation,
            prediction=prediction,
        )

        audits: List[ModuleAuditRecord] = []
        for module_name, (predicted_positive, contribution_score, reasons) in contributions.items():
            audit = self.state.update_module(
                module_name=module_name,
                source_type=source_type,
                predicted_positive=predicted_positive,
                outcome_positive=outcome_positive,
                contribution_score=contribution_score,
                reason_codes=reasons,
            )
            audits.append(audit)
            if persist:
                save_meta_learning(module_name, self.state.records[module_name].to_dict())

        return audits

    def get_weight(self, module_name: str, default: float = 1.0) -> float:
        record = self.state.records.get(module_name)
        if not record:
            return default
        return safe_float(record.weight, default)

    def get_weights(self) -> Dict[str, float]:
        return dict(self.state.summary().module_weights)

    def get_summary(self) -> MetaLearningSummary:
        return self.state.summary()


_default_engine: Optional[MetaLearningEngine] = None


def engine(records: Optional[Iterable[Any]] = None) -> MetaLearningEngine:
    global _default_engine
    if _default_engine is None or records is not None:
        _default_engine = MetaLearningEngine(records=records)
    return _default_engine


def audit_outcome(
    source_type: str,
    result: str,
    candidate: AnalysisCandidate,
    movement: Optional[MovementHunterResult] = None,
    trap: Optional[TrapResult] = None,
    state: Optional[StateResult] = None,
    confidence: Optional[ConfidenceResult] = None,
    correlation: Optional[CorrelationResult] = None,
    prediction: Optional[MovementPredictionResult] = None,
    persist: bool = True,
) -> List[ModuleAuditRecord]:
    return engine().audit_outcome(
        source_type=source_type,
        result=result,
        candidate=candidate,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        correlation=correlation,
        prediction=prediction,
        persist=persist,
    )


def get_module_weights() -> Dict[str, float]:
    return engine().get_weights()


def get_meta_learning_summary() -> MetaLearningSummary:
    return engine().get_summary()


def meta_learning_summary_for_ai() -> JsonDict:
    return get_meta_learning_summary().to_dict()
