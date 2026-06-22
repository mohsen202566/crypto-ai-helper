from __future__ import annotations

"""
14 - ghost_manager.py

Ghost Signal Manager for the locked Movement Hunter bot.

Responsibilities:
- Create and monitor GHOST decisions for learning.
- Track whether ghost would hit TP1, TP2, AI_EXIT or SL.
- Feed ghost outcomes into coin_learning.py.
- Keep ghost learning separate from real trading while sharing the learning database.
- Provide ghost statistics and open/closed ghost state.

Strictly forbidden:
- No REAL trade execution.
- No Toobit private order calls.
- No Telegram sending.
- No REAL/GHOST/REJECT final AI decision.
- No Paper mode.
- No Setup flow.

This file manages ghost records only after AI decides GHOST.
"""

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import uuid4
import time
import math

from analysis_engine import AnalysisCandidate, DirectionScore, RiskProfile, QualityProfile
from analysis_layers import SensorSnapshot
from movement_hunter import MovementHunterResult
from trap_engine import TrapResult
from state_engine import StateResult
from confidence_engine import ConfidenceResult
from coin_learning import SOURCE_GHOST, learn_outcome
from meta_learning import audit_outcome
from movement_memory import record_movement_memory
from data_store import save_ghost, prune_section, store, save_error
from config import SETTINGS


JsonDict = Dict[str, Any]

MAX_GHOST_RECORDS = 20000

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"

GHOST_OPEN = "OPEN"
GHOST_TP1 = "TP1"
GHOST_TP2 = "TP2"
GHOST_AI_EXIT = "AI_EXIT"
GHOST_SL = "SL"
GHOST_EXPIRED = "EXPIRED"
GHOST_CLOSED = "CLOSED"

RESULT_TP1 = "TP1"
RESULT_TP2 = "TP2"
RESULT_AI_EXIT = "AI_EXIT"
RESULT_SL = "SL"
RESULT_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class GhostRecord:
    ghost_id: str
    decision_id: str
    candidate_id: str
    symbol: str
    direction: str
    entry: float
    tp1: float
    tp2: float
    sl: float
    created_at: int
    expires_at: int
    status: str = GHOST_OPEN
    result: str = RESULT_UNKNOWN
    closed_at: int = 0
    tp1_hit: bool = False
    tp2_hit: bool = False
    ai_exit_hit: bool = False
    sl_hit: bool = False
    mfe_percent: float = 0.0
    mae_percent: float = 0.0
    max_price: float = 0.0
    min_price: float = 0.0
    last_price: float = 0.0
    monitor_count: int = 0
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    meta: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class GhostMonitorResult:
    ghost_id: str
    symbol: str
    direction: str
    status: str
    result: str
    closed: bool
    price: float
    mfe_percent: float
    mae_percent: float
    reason: str
    learning_record_id: str = ""

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class GhostStats:
    total: int
    open_count: int
    closed_count: int
    tp1_count: int
    tp2_count: int
    ai_exit_count: int
    sl_count: int
    expired_count: int
    win_rate: float

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


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except Exception:
        return low


def normalize_direction(direction: str) -> str:
    d = str(direction or "").upper().strip()
    if d in {"LONG", "BUY"}:
        return DIRECTION_LONG
    if d in {"SHORT", "SELL"}:
        return DIRECTION_SHORT
    return d


def pct_move(direction: str, entry: float, price: float) -> float:
    entry = safe_float(entry)
    price = safe_float(price)
    if entry <= 0 or price <= 0:
        return 0.0
    if normalize_direction(direction) == DIRECTION_LONG:
        return (price - entry) / entry * 100.0
    return (entry - price) / entry * 100.0


def adverse_pct(direction: str, entry: float, price: float) -> float:
    return -pct_move(direction, entry, price)


def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        try:
            return obj.to_dict().get(key, default)
        except Exception:
            pass
    return getattr(obj, key, default)


def _as_dict(obj: Any) -> JsonDict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        try:
            data = obj.to_dict()
            return dict(data) if isinstance(data, dict) else {}
        except Exception:
            return {}
    try:
        return dict(getattr(obj, "__dict__", {}))
    except Exception:
        return {}


def _dataclass_kwargs(cls: Any, data: JsonDict) -> JsonDict:
    try:
        allowed = {f.name for f in fields(cls)}
        return {k: v for k, v in dict(data or {}).items() if k in allowed}
    except Exception:
        return {}


def _coerce_direction_score(data: Any, fallback_direction: str = "") -> DirectionScore:
    d = _as_dict(data)
    direction = normalize_direction(d.get("direction_hint") or fallback_direction)
    if direction not in {DIRECTION_LONG, DIRECTION_SHORT}:
        direction = normalize_direction(fallback_direction)
    long_score = safe_float(d.get("long_score"), 55.0 if direction == DIRECTION_LONG else 45.0)
    short_score = safe_float(d.get("short_score"), 55.0 if direction == DIRECTION_SHORT else 45.0)
    bias = str(d.get("bias") or ("BULLISH" if direction == DIRECTION_LONG else "BEARISH" if direction == DIRECTION_SHORT else "NEUTRAL"))
    return DirectionScore(
        long_score=long_score,
        short_score=short_score,
        direction_hint=direction or "NEUTRAL",
        bias=bias,
        gap=safe_float(d.get("gap"), abs(long_score - short_score)),
    )


def _coerce_quality_profile(data: Any) -> QualityProfile:
    d = _as_dict(data)
    total = safe_float(d.get("total_quality"), 50.0)
    level = str(d.get("quality_level") or ("HIGH" if total >= 70 else "MEDIUM" if total >= 45 else "LOW"))
    return QualityProfile(
        trend_quality=safe_float(d.get("trend_quality"), total),
        momentum_quality=safe_float(d.get("momentum_quality"), total),
        volatility_quality=safe_float(d.get("volatility_quality"), total),
        volume_quality=safe_float(d.get("volume_quality"), total),
        power_quality=safe_float(d.get("power_quality"), total),
        candle_quality=safe_float(d.get("candle_quality"), total),
        total_quality=total,
        quality_level=level,
    )


def _coerce_risk_profile(data: Any) -> RiskProfile:
    d = _as_dict(data)
    total = safe_float(d.get("total_risk"), 50.0)
    level = str(d.get("risk_level") or ("HIGH" if total >= 65 else "MEDIUM" if total >= 35 else "LOW"))
    return RiskProfile(
        range_risk=safe_float(d.get("range_risk"), total),
        trap_risk=safe_float(d.get("trap_risk"), total),
        exhaustion_risk=safe_float(d.get("exhaustion_risk"), total),
        late_move_risk=safe_float(d.get("late_move_risk"), total),
        liquidity_risk=safe_float(d.get("liquidity_risk"), total),
        total_risk=total,
        risk_level=level,
    )


def _coerce_sensor_snapshot(data: Any, ghost: Optional["GhostRecord"] = None) -> SensorSnapshot:
    d = _as_dict(data)
    if ghost is not None:
        d.setdefault("symbol", ghost.symbol)
        d.setdefault("timeframe", "5m")
        d.setdefault("timestamp", ghost.created_at or now_ts())
        d.setdefault("price", ghost.entry)
        d.setdefault("previous_close", ghost.entry)
    try:
        return SensorSnapshot(**_dataclass_kwargs(SensorSnapshot, d))
    except Exception:
        return SensorSnapshot(
            symbol=str(d.get("symbol", ghost.symbol if ghost else "")),
            timeframe=str(d.get("timeframe", "5m")),
            timestamp=int(d.get("timestamp", ghost.created_at if ghost else now_ts()) or now_ts()),
            price=safe_float(d.get("price", ghost.entry if ghost else 0.0)),
            previous_close=safe_float(d.get("previous_close", ghost.entry if ghost else 0.0)),
        )


def _coerce_candidate_from_dict(data: Any, ghost: Optional["GhostRecord"] = None) -> Optional[AnalysisCandidate]:
    d = _as_dict(data)
    if not d and ghost is None:
        return None
    symbol = str(d.get("symbol") or (ghost.symbol if ghost else ""))
    direction = normalize_direction(d.get("direction_hint") or (ghost.direction if ghost else ""))
    sensor = _coerce_sensor_snapshot(d.get("sensor_snapshot", {}), ghost=ghost)
    direction_score = _coerce_direction_score(d.get("direction_score", {}), fallback_direction=direction)
    quality = _coerce_quality_profile(d.get("quality", {}))
    risk = _coerce_risk_profile(d.get("risk", {}))
    return AnalysisCandidate(
        candidate_id=str(d.get("candidate_id") or (ghost.candidate_id if ghost else f"cand_ghost_{uuid4().hex}")),
        symbol=symbol,
        timeframe=str(d.get("timeframe") or sensor.timeframe or "5m"),
        timestamp=int(d.get("timestamp") or sensor.timestamp or (ghost.created_at if ghost else now_ts())),
        direction_hint=direction,
        bias=str(d.get("bias") or direction_score.bias),
        direction_score=direction_score,
        quality=quality,
        risk=risk,
        sensor_snapshot=sensor,
        market_context=dict(d.get("market_context", {}) if isinstance(d.get("market_context", {}), dict) else {}),
        reason_codes=tuple(d.get("reason_codes", ()) or ("GHOST_RESTORED_CANDIDATE",)),
        warnings=tuple(d.get("warnings", ()) or ()),
        valid=bool(d.get("valid", True)),
    )


def restore_candidate_from_ghost(ghost: "GhostRecord") -> Optional[AnalysisCandidate]:
    """Restore enough candidate context for learning after process restart.

    Runtime caches are lost on restart, so every Ghost must carry/rebuild
    candidate data from persisted metadata. If no full snapshot exists, create
    a minimal candidate from the ghost itself so TP/SL outcomes still teach
    coin_learning and movement_memory.
    """
    meta = ghost.meta if isinstance(ghost.meta, dict) else {}
    for key in ("candidate", "analysis_candidate", "candidate_snapshot"):
        restored = _coerce_candidate_from_dict(meta.get(key), ghost=ghost)
        if restored is not None:
            return restored

    decision = meta.get("decision", {}) if isinstance(meta.get("decision", {}), dict) else {}
    decision_candidate = decision.get("candidate") if isinstance(decision.get("candidate"), dict) else None
    if decision_candidate:
        restored = _coerce_candidate_from_dict(decision_candidate, ghost=ghost)
        if restored is not None:
            return restored

    # Last-resort fallback for older persisted ghosts that only contain entry/TP/SL.
    return _coerce_candidate_from_dict({}, ghost=ghost)


class GhostFactory:
    """Creates GhostRecord objects from AI GHOST decisions or candidate metadata."""

    def create(
        self,
        decision_id: str,
        candidate: AnalysisCandidate,
        entry: float,
        tp1: float,
        tp2: float,
        sl: float,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
        confidence: Optional[ConfidenceResult] = None,
        ttl_seconds: Optional[int] = None,
        meta: Optional[JsonDict] = None,
    ) -> GhostRecord:
        created = now_ts()
        ttl = int(ttl_seconds or getattr(SETTINGS.monitor, "ghost_monitor_ttl_seconds", 3600))
        if ttl <= 0:
            ttl = 3600

        reasons: List[str] = []
        reasons.extend(list(candidate.reason_codes))
        if movement:
            reasons.extend(list(movement.reason_codes))
        if trap:
            reasons.extend(list(trap.reason_codes))
        if state:
            reasons.extend(list(state.reason_codes))
        if confidence:
            reasons.extend(list(confidence.reason_codes))

        full_meta: JsonDict = dict(meta or {})
        # Persist context snapshots so Ghost learning survives bot restarts.
        full_meta.setdefault("candidate", candidate.to_dict() if hasattr(candidate, "to_dict") else _as_dict(candidate))
        if movement is not None:
            full_meta.setdefault("movement", movement.to_dict() if hasattr(movement, "to_dict") else _as_dict(movement))
        if trap is not None:
            full_meta.setdefault("trap", trap.to_dict() if hasattr(trap, "to_dict") else _as_dict(trap))
        if state is not None:
            full_meta.setdefault("state", state.to_dict() if hasattr(state, "to_dict") else _as_dict(state))
        if confidence is not None:
            full_meta.setdefault("confidence", confidence.to_dict() if hasattr(confidence, "to_dict") else _as_dict(confidence))

        record = GhostRecord(
            ghost_id=f"ghost_{uuid4().hex}",
            decision_id=str(decision_id),
            candidate_id=candidate.candidate_id,
            symbol=candidate.symbol,
            direction=normalize_direction(candidate.direction_hint),
            entry=safe_float(entry or candidate.sensor_snapshot.price),
            tp1=safe_float(tp1),
            tp2=safe_float(tp2),
            sl=safe_float(sl),
            created_at=created,
            expires_at=created + ttl,
            status=GHOST_OPEN,
            result=RESULT_UNKNOWN,
            max_price=safe_float(entry or candidate.sensor_snapshot.price),
            min_price=safe_float(entry or candidate.sensor_snapshot.price),
            last_price=safe_float(entry or candidate.sensor_snapshot.price),
            reason_codes=tuple(dict.fromkeys(reasons)),
            meta=full_meta,
        )
        return record


class GhostOutcomeEvaluator:
    """Evaluates ghost TP/SL/expiry using latest market price."""

    def evaluate(self, ghost: GhostRecord, price: float) -> GhostRecord:
        price = safe_float(price)
        if price <= 0 or ghost.entry <= 0:
            return ghost

        direction = normalize_direction(ghost.direction)
        max_price = max(ghost.max_price or price, price)
        min_price = min(ghost.min_price or price, price)

        mfe = max(
            safe_float(ghost.mfe_percent),
            pct_move(direction, ghost.entry, max_price if direction == DIRECTION_LONG else min_price),
        )
        mae = max(
            safe_float(ghost.mae_percent),
            adverse_pct(direction, ghost.entry, min_price if direction == DIRECTION_LONG else max_price),
        )

        status = ghost.status
        result = ghost.result
        closed_at = ghost.closed_at
        tp1_hit = ghost.tp1_hit
        tp2_hit = ghost.tp2_hit
        ai_exit_hit = ghost.ai_exit_hit
        sl_hit = ghost.sl_hit

        if status == GHOST_OPEN:
            if direction == DIRECTION_LONG:
                if ghost.sl > 0 and price <= ghost.sl:
                    status = GHOST_SL
                    result = RESULT_SL
                    sl_hit = True
                    closed_at = now_ts()
                elif ghost.tp2 > 0 and price >= ghost.tp2:
                    status = GHOST_TP2
                    result = RESULT_TP2
                    tp1_hit = True
                    tp2_hit = True
                    closed_at = now_ts()
                elif ghost.tp1 > 0 and price >= ghost.tp1:
                    status = GHOST_TP1
                    result = RESULT_TP1
                    tp1_hit = True
                    # keep closed on TP1 for learning; future versions can trail ghost to TP2.
                    closed_at = now_ts()
            else:
                if ghost.sl > 0 and price >= ghost.sl:
                    status = GHOST_SL
                    result = RESULT_SL
                    sl_hit = True
                    closed_at = now_ts()
                elif ghost.tp2 > 0 and price <= ghost.tp2:
                    status = GHOST_TP2
                    result = RESULT_TP2
                    tp1_hit = True
                    tp2_hit = True
                    closed_at = now_ts()
                elif ghost.tp1 > 0 and price <= ghost.tp1:
                    status = GHOST_TP1
                    result = RESULT_TP1
                    tp1_hit = True
                    closed_at = now_ts()

            if status == GHOST_OPEN and ghost.expires_at and now_ts() >= ghost.expires_at:
                status = GHOST_EXPIRED
                result = RESULT_UNKNOWN
                closed_at = now_ts()

        return GhostRecord(
            ghost_id=ghost.ghost_id,
            decision_id=ghost.decision_id,
            candidate_id=ghost.candidate_id,
            symbol=ghost.symbol,
            direction=ghost.direction,
            entry=ghost.entry,
            tp1=ghost.tp1,
            tp2=ghost.tp2,
            sl=ghost.sl,
            created_at=ghost.created_at,
            expires_at=ghost.expires_at,
            status=status,
            result=result,
            closed_at=closed_at,
            tp1_hit=tp1_hit,
            tp2_hit=tp2_hit,
            ai_exit_hit=ai_exit_hit,
            sl_hit=sl_hit,
            mfe_percent=mfe,
            mae_percent=mae,
            max_price=max_price,
            min_price=min_price,
            last_price=price,
            monitor_count=ghost.monitor_count + 1,
            reason_codes=ghost.reason_codes,
            meta=ghost.meta,
        )


class GhostLearningAdapter:
    """Converts closed ghost outcomes into coin_learning records."""

    def learn(
        self,
        ghost: GhostRecord,
        candidate: AnalysisCandidate,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
        confidence: Optional[ConfidenceResult] = None,
        persist: bool = True,
    ) -> str:
        if ghost.result == RESULT_UNKNOWN:
            return ""

        record = learn_outcome(
            source_type=SOURCE_GHOST,
            candidate=candidate,
            result=ghost.result,
            movement=movement,
            trap=trap,
            state=state,
            confidence=confidence,
            entry_price=ghost.entry,
            exit_price=ghost.last_price,
            realized_pnl=0.0,
            realized_pnl_percent=pct_move(ghost.direction, ghost.entry, ghost.last_price),
            mfe_percent=ghost.mfe_percent,
            mae_percent=ghost.mae_percent,
            holding_seconds=max(0, (ghost.closed_at or now_ts()) - ghost.created_at),
            meta={
                "ghost_id": ghost.ghost_id,
                "decision_id": ghost.decision_id,
                "tp1": ghost.tp1,
                "tp2": ghost.tp2,
                "sl": ghost.sl,
                "monitor_count": ghost.monitor_count,
                "note": "GHOST_LEARNING_NO_REAL_ORDER",
            },
            persist=persist,
        )

        # Meta Learning: teach the AI which layers helped or hurt this GHOST outcome.
        try:
            audit_outcome(
                source_type=SOURCE_GHOST,
                result=ghost.result,
                candidate=candidate,
                movement=movement,
                trap=trap,
                state=state,
                confidence=confidence,
                correlation=None,
                prediction=None,
                persist=persist,
            )
        except Exception:
            pass

        # Movement Memory: store pre-pump/pre-dump conditions so the AI learns
        # to hunt similar moves before or at the start, not in the middle/end.
        try:
            record_movement_memory(
                candidate=candidate,
                after_price=ghost.last_price,
                move_duration_seconds=max(0, (ghost.closed_at or now_ts()) - ghost.created_at),
                movement=movement,
                trap=trap,
                state=state,
                confidence=confidence,
                mfe_percent=ghost.mfe_percent,
                mae_percent=ghost.mae_percent,
                meta={
                    "source_type": SOURCE_GHOST,
                    "ghost_id": ghost.ghost_id,
                    "decision_id": ghost.decision_id,
                    "result": ghost.result,
                },
                persist=persist,
            )
        except Exception:
            pass

        return record.learning_id


class GhostManager:
    """
    Main Ghost Manager.

    It stores and monitors ghosts.
    It does not execute real trades.
    """

    def __init__(self):
        self.factory = GhostFactory()
        self.evaluator = GhostOutcomeEvaluator()
        self.learning = GhostLearningAdapter()
        self._candidate_cache: Dict[str, AnalysisCandidate] = {}
        self._context_cache: Dict[str, Dict[str, Any]] = {}

    def create_ghost(
        self,
        decision_id: str,
        candidate: AnalysisCandidate,
        entry: float,
        tp1: float,
        tp2: float,
        sl: float,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
        confidence: Optional[ConfidenceResult] = None,
        ttl_seconds: Optional[int] = None,
        meta: Optional[JsonDict] = None,
        persist: bool = True,
    ) -> GhostRecord:
        ghost = self.factory.create(
            decision_id=decision_id,
            candidate=candidate,
            entry=entry,
            tp1=tp1,
            tp2=tp2,
            sl=sl,
            movement=movement,
            trap=trap,
            state=state,
            confidence=confidence,
            ttl_seconds=ttl_seconds,
            meta=meta,
        )
        self._candidate_cache[ghost.ghost_id] = candidate
        self._context_cache[ghost.ghost_id] = {
            "movement": movement,
            "trap": trap,
            "state": state,
            "confidence": confidence,
        }
        if persist:
            save_ghost(ghost.ghost_id, ghost.to_dict())
        try:
            prune_section('ghosts', MAX_GHOST_RECORDS, sort_key='created_at')
        except Exception:
            pass
        return ghost

    def monitor_ghost(
        self,
        ghost: GhostRecord | Dict[str, Any],
        price: float,
        candidate: Optional[AnalysisCandidate] = None,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
        confidence: Optional[ConfidenceResult] = None,
        persist: bool = True,
        learn: bool = True,
    ) -> GhostMonitorResult:
        record = self._coerce_ghost(ghost)
        updated = self.evaluator.evaluate(record, price)

        learning_id = ""
        closed = updated.status != GHOST_OPEN

        if closed and learn:
            candidate = candidate or self._candidate_cache.get(updated.ghost_id)
            cached = self._context_cache.get(updated.ghost_id, {})
            movement = movement or cached.get("movement")
            trap = trap or cached.get("trap")
            state = state or cached.get("state")
            confidence = confidence or cached.get("confidence")

            if candidate is None:
                candidate = restore_candidate_from_ghost(updated)

            if candidate is not None:
                try:
                    learning_id = self.learning.learn(
                        ghost=updated,
                        candidate=candidate,
                        movement=movement,
                        trap=trap,
                        state=state,
                        confidence=confidence,
                        persist=persist,
                    )
                except Exception as exc:
                    try:
                        save_error("ghost_learning", str(exc), {"ghost": updated.to_dict()})
                    except Exception:
                        pass

        if learning_id:
            updated_meta = dict(updated.meta or {})
            updated_meta["learning_record_id"] = learning_id
            updated = GhostRecord(
                ghost_id=updated.ghost_id, decision_id=updated.decision_id, candidate_id=updated.candidate_id,
                symbol=updated.symbol, direction=updated.direction, entry=updated.entry, tp1=updated.tp1,
                tp2=updated.tp2, sl=updated.sl, created_at=updated.created_at, expires_at=updated.expires_at,
                status=updated.status, result=updated.result, closed_at=updated.closed_at, tp1_hit=updated.tp1_hit,
                tp2_hit=updated.tp2_hit, ai_exit_hit=updated.ai_exit_hit, sl_hit=updated.sl_hit,
                mfe_percent=updated.mfe_percent, mae_percent=updated.mae_percent, max_price=updated.max_price,
                min_price=updated.min_price, last_price=updated.last_price, monitor_count=updated.monitor_count,
                reason_codes=updated.reason_codes, meta=updated_meta,
            )

        if persist:
            save_ghost(updated.ghost_id, updated.to_dict())

        reason = "OPEN"
        if updated.status == GHOST_TP1:
            reason = "GHOST_TP1_HIT"
        elif updated.status == GHOST_TP2:
            reason = "GHOST_TP2_HIT"
        elif updated.status == GHOST_SL:
            reason = "GHOST_SL_HIT"
        elif updated.status == GHOST_EXPIRED:
            reason = "GHOST_EXPIRED"
        elif updated.status == GHOST_AI_EXIT:
            reason = "GHOST_AI_EXIT"

        return GhostMonitorResult(
            ghost_id=updated.ghost_id,
            symbol=updated.symbol,
            direction=updated.direction,
            status=updated.status,
            result=updated.result,
            closed=closed,
            price=safe_float(price),
            mfe_percent=updated.mfe_percent,
            mae_percent=updated.mae_percent,
            reason=reason,
            learning_record_id=learning_id,
        )

    def open_ghosts(self) -> List[GhostRecord]:
        records = store().open_ghosts()
        return [self._coerce_ghost(r) for r in records]

    def stats(self, ghosts: Optional[Iterable[Any]] = None) -> GhostStats:
        records = [self._coerce_ghost(g) for g in (ghosts if ghosts is not None else store().section("ghosts").values())]
        total = len(records)
        open_count = sum(1 for g in records if g.status == GHOST_OPEN)
        tp1 = sum(1 for g in records if g.result == RESULT_TP1)
        tp2 = sum(1 for g in records if g.result == RESULT_TP2)
        ai_exit = sum(1 for g in records if g.result == RESULT_AI_EXIT)
        sl = sum(1 for g in records if g.result == RESULT_SL)
        expired = sum(1 for g in records if g.status == GHOST_EXPIRED)
        closed = total - open_count
        wins = tp1 + tp2 + ai_exit
        losses = sl
        wr = (wins / (wins + losses) * 100.0) if (wins + losses) else 0.0

        return GhostStats(
            total=total,
            open_count=open_count,
            closed_count=closed,
            tp1_count=tp1,
            tp2_count=tp2,
            ai_exit_count=ai_exit,
            sl_count=sl,
            expired_count=expired,
            win_rate=clamp(wr),
        )

    def _coerce_ghost(self, item: GhostRecord | Dict[str, Any]) -> GhostRecord:
        if isinstance(item, GhostRecord):
            return item
        if hasattr(item, "to_dict") and callable(item.to_dict):
            item = item.to_dict()
        if not isinstance(item, dict):
            item = {}

        return GhostRecord(
            ghost_id=str(item.get("ghost_id", item.get("id", f"ghost_{uuid4().hex}"))),
            decision_id=str(item.get("decision_id", "")),
            candidate_id=str(item.get("candidate_id", "")),
            symbol=str(item.get("symbol", "")),
            direction=normalize_direction(str(item.get("direction", ""))),
            entry=safe_float(item.get("entry")),
            tp1=safe_float(item.get("tp1")),
            tp2=safe_float(item.get("tp2")),
            sl=safe_float(item.get("sl")),
            created_at=int(item.get("created_at", now_ts())),
            expires_at=int(item.get("expires_at", now_ts() + 3600)),
            status=str(item.get("status", GHOST_OPEN)).upper(),
            result=str(item.get("result", RESULT_UNKNOWN)).upper(),
            closed_at=int(item.get("closed_at", 0) or 0),
            tp1_hit=bool(item.get("tp1_hit", False)),
            tp2_hit=bool(item.get("tp2_hit", False)),
            ai_exit_hit=bool(item.get("ai_exit_hit", False)),
            sl_hit=bool(item.get("sl_hit", False)),
            mfe_percent=safe_float(item.get("mfe_percent")),
            mae_percent=safe_float(item.get("mae_percent")),
            max_price=safe_float(item.get("max_price", item.get("entry", 0.0))),
            min_price=safe_float(item.get("min_price", item.get("entry", 0.0))),
            last_price=safe_float(item.get("last_price", item.get("entry", 0.0))),
            monitor_count=int(item.get("monitor_count", 0) or 0),
            reason_codes=tuple(item.get("reason_codes", ()) or ()),
            meta=dict(item.get("meta", {}) if isinstance(item.get("meta", {}), dict) else {}),
        )


_default_manager: Optional[GhostManager] = None


def manager() -> GhostManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = GhostManager()
    return _default_manager


def create_ghost(
    decision_id: str,
    candidate: AnalysisCandidate,
    entry: float,
    tp1: float,
    tp2: float,
    sl: float,
    movement: Optional[MovementHunterResult] = None,
    trap: Optional[TrapResult] = None,
    state: Optional[StateResult] = None,
    confidence: Optional[ConfidenceResult] = None,
    ttl_seconds: Optional[int] = None,
    meta: Optional[JsonDict] = None,
    persist: bool = True,
) -> GhostRecord:
    return manager().create_ghost(
        decision_id=decision_id,
        candidate=candidate,
        entry=entry,
        tp1=tp1,
        tp2=tp2,
        sl=sl,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        ttl_seconds=ttl_seconds,
        meta=meta,
        persist=persist,
    )


def monitor_ghost(
    ghost: GhostRecord | Dict[str, Any],
    price: float,
    candidate: Optional[AnalysisCandidate] = None,
    movement: Optional[MovementHunterResult] = None,
    trap: Optional[TrapResult] = None,
    state: Optional[StateResult] = None,
    confidence: Optional[ConfidenceResult] = None,
    persist: bool = True,
    learn: bool = True,
) -> GhostMonitorResult:
    return manager().monitor_ghost(
        ghost=ghost,
        price=price,
        candidate=candidate,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        persist=persist,
        learn=learn,
    )


def ghost_stats() -> GhostStats:
    return manager().stats()
