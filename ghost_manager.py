from __future__ import annotations

"""
14 - ghost_manager.py

Light Ghost Manager for the simplified Level 1 / 5M crypto futures bot.

Locked goals:
- Manage only GHOST records after ai_decision_engine decides GHOST.
- Monitor ghost TP1 / TP2 / SL / expiry.
- Learn from closed ghosts through coin_learning.py and movement_memory.py.
- Keep learning separated by source_type=GHOST.
- No REAL trade execution.
- No Toobit private order calls.
- No Telegram sending.
- No final REAL/GHOST/REJECT decision.
- No paper/setup flow.
- No trap/state/confidence/meta/correlation/movement_hunter dependency.

This file manages ghost records only.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4
import math
import time

from ai_decision_engine import AIDecision
from analysis_engine import AnalysisCandidate
from coin_learning import SOURCE_GHOST, learn_outcome
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
GHOST_SL = "SL"
GHOST_EXPIRED = "EXPIRED"
GHOST_AI_EXIT = "AI_EXIT"

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


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, safe_float(value, low)))


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
    return max(0.0, -pct_move(direction, entry, price))


def obj_to_dict(obj: Any) -> JsonDict:
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


def decision_id_from_any(decision: Any) -> str:
    if decision is None:
        return ""
    if isinstance(decision, str):
        return decision
    return str(getattr(decision, "decision_id", "") or obj_to_dict(decision).get("decision_id", ""))


def restore_candidate_from_ghost(ghost: GhostRecord) -> Optional[AnalysisCandidate]:
    """Restore candidate snapshot from ghost metadata after restart."""
    meta = ghost.meta if isinstance(ghost.meta, dict) else {}
    candidate_data = meta.get("candidate") or meta.get("analysis_candidate") or meta.get("candidate_snapshot")
    if not isinstance(candidate_data, dict):
        return None

    try:
        from analysis_layers import SensorSnapshot
        from analysis_engine import (
            SensorDirectionHint,
            SensorMomentumState,
            AnalysisCandidate,
        )

        sensor_data = candidate_data.get("sensor_snapshot", {}) or {}
        direction_data = candidate_data.get("sensor_direction", {}) or {}
        momentum_data = candidate_data.get("momentum_state", {}) or {}

        sensor_snapshot = SensorSnapshot(**{
            k: v for k, v in sensor_data.items()
            if k in getattr(SensorSnapshot, "__dataclass_fields__", {})
        })

        sensor_direction = SensorDirectionHint(**{
            k: v for k, v in direction_data.items()
            if k in getattr(SensorDirectionHint, "__dataclass_fields__", {})
        })

        momentum_state = SensorMomentumState(**{
            k: v for k, v in momentum_data.items()
            if k in getattr(SensorMomentumState, "__dataclass_fields__", {})
        })

        return AnalysisCandidate(
            candidate_id=str(candidate_data.get("candidate_id", ghost.candidate_id)),
            symbol=str(candidate_data.get("symbol", ghost.symbol)),
            timeframe=str(candidate_data.get("timeframe", "5m")),
            timestamp=safe_int(candidate_data.get("timestamp", ghost.created_at)),
            direction_hint=normalize_direction(candidate_data.get("direction_hint", ghost.direction)),
            bias=str(candidate_data.get("bias", "")),
            sensor_direction=sensor_direction,
            momentum_state=momentum_state,
            sensor_snapshot=sensor_snapshot,
            market_mode=dict(candidate_data.get("market_mode", {}) if isinstance(candidate_data.get("market_mode", {}), dict) else {}),
            reason_codes=tuple(candidate_data.get("reason_codes", ()) or ()),
            warnings=tuple(candidate_data.get("warnings", ()) or ()),
            valid=bool(candidate_data.get("valid", True)),
        )
    except Exception:
        return None


class GhostFactory:
    def create(
        self,
        decision: AIDecision | str,
        candidate: AnalysisCandidate,
        entry: float,
        tp1: float,
        tp2: float,
        sl: float,
        ttl_seconds: Optional[int] = None,
        meta: Optional[JsonDict] = None,
    ) -> GhostRecord:
        created = now_ts()
        ttl = safe_int(ttl_seconds or getattr(SETTINGS.monitor, "ghost_monitor_ttl_seconds", 3600), 3600)
        if ttl <= 0:
            ttl = 3600

        entry_price = safe_float(entry or getattr(candidate.sensor_snapshot, "price", 0.0))
        full_meta: JsonDict = dict(meta or {})
        full_meta.setdefault("candidate", candidate.to_dict() if hasattr(candidate, "to_dict") else obj_to_dict(candidate))
        if not isinstance(decision, str):
            full_meta.setdefault("decision", decision.to_dict() if hasattr(decision, "to_dict") else obj_to_dict(decision))

        decision_id = decision_id_from_any(decision)

        return GhostRecord(
            ghost_id=f"ghost_{uuid4().hex}",
            decision_id=decision_id,
            candidate_id=str(candidate.candidate_id),
            symbol=str(candidate.symbol),
            direction=normalize_direction(candidate.direction_hint),
            entry=entry_price,
            tp1=safe_float(tp1),
            tp2=safe_float(tp2),
            sl=safe_float(sl),
            created_at=created,
            expires_at=created + ttl,
            status=GHOST_OPEN,
            result=RESULT_UNKNOWN,
            max_price=entry_price,
            min_price=entry_price,
            last_price=entry_price,
            reason_codes=tuple(dict.fromkeys(list(candidate.reason_codes))),
            meta=full_meta,
        )


class GhostOutcomeEvaluator:
    def evaluate(self, ghost: GhostRecord, price: float) -> GhostRecord:
        price = safe_float(price)
        if price <= 0 or ghost.entry <= 0:
            return ghost

        direction = normalize_direction(ghost.direction)
        max_price = max(safe_float(ghost.max_price, ghost.entry), price)
        min_price = min(safe_float(ghost.min_price, ghost.entry), price)

        favorable_price = max_price if direction == DIRECTION_LONG else min_price
        adverse_price = min_price if direction == DIRECTION_LONG else max_price

        mfe = max(safe_float(ghost.mfe_percent), pct_move(direction, ghost.entry, favorable_price))
        mae = max(safe_float(ghost.mae_percent), adverse_pct(direction, ghost.entry, adverse_price))

        status = ghost.status
        result = ghost.result
        closed_at = ghost.closed_at
        tp1_hit = ghost.tp1_hit
        tp2_hit = ghost.tp2_hit
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
                    closed_at = now_ts()
            elif direction == DIRECTION_SHORT:
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

            if status == GHOST_OPEN and ghost.expires_at > 0 and now_ts() >= ghost.expires_at:
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
            ai_exit_hit=ghost.ai_exit_hit,
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
    def learn(
        self,
        ghost: GhostRecord,
        candidate: AnalysisCandidate,
        persist: bool = True,
    ) -> str:
        if ghost.result == RESULT_UNKNOWN:
            return ""

        holding_seconds = max(0, (ghost.closed_at or now_ts()) - ghost.created_at)
        pnl_percent = pct_move(ghost.direction, ghost.entry, ghost.last_price)

        learning_record = learn_outcome(
            source_type=SOURCE_GHOST,
            candidate=candidate,
            result=ghost.result,
            entry_price=ghost.entry,
            exit_price=ghost.last_price,
            realized_pnl=0.0,
            realized_pnl_percent=pnl_percent,
            mfe_percent=ghost.mfe_percent,
            mae_percent=ghost.mae_percent,
            holding_seconds=holding_seconds,
            meta={
                "ghost_id": ghost.ghost_id,
                "decision_id": ghost.decision_id,
                "tp1": ghost.tp1,
                "tp2": ghost.tp2,
                "sl": ghost.sl,
                "monitor_count": ghost.monitor_count,
                "source_note": "GHOST_LEARNING_NO_REAL_ORDER",
            },
            persist=persist,
        )

        try:
            record_movement_memory(
                candidate=candidate,
                exit_price=ghost.last_price,
                duration_seconds=holding_seconds,
                outcome=ghost.result,
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

        return str(getattr(learning_record, "learning_id", ""))


class GhostManager:
    def __init__(self):
        self.factory = GhostFactory()
        self.evaluator = GhostOutcomeEvaluator()
        self.learning = GhostLearningAdapter()
        self._candidate_cache: Dict[str, AnalysisCandidate] = {}

    def create_ghost(
        self,
        decision: AIDecision | str,
        candidate: AnalysisCandidate,
        entry: float,
        tp1: float,
        tp2: float,
        sl: float,
        ttl_seconds: Optional[int] = None,
        meta: Optional[JsonDict] = None,
        persist: bool = True,
        **_: Any,
    ) -> GhostRecord:
        ghost = self.factory.create(
            decision=decision,
            candidate=candidate,
            entry=entry,
            tp1=tp1,
            tp2=tp2,
            sl=sl,
            ttl_seconds=ttl_seconds,
            meta=meta,
        )
        self._candidate_cache[ghost.ghost_id] = candidate

        if persist:
            save_ghost(ghost.ghost_id, ghost.to_dict())
            try:
                prune_section("ghosts", MAX_GHOST_RECORDS, sort_key="created_at")
            except Exception:
                pass

        return ghost

    def monitor_ghost(
        self,
        ghost: GhostRecord | Dict[str, Any],
        price: float,
        candidate: Optional[AnalysisCandidate] = None,
        persist: bool = True,
        learn: bool = True,
        **_: Any,
    ) -> GhostMonitorResult:
        record = self.coerce_ghost(ghost)
        updated = self.evaluator.evaluate(record, price)

        learning_id = ""
        closed = updated.status != GHOST_OPEN

        if closed and learn and updated.result != RESULT_UNKNOWN:
            candidate = candidate or self._candidate_cache.get(updated.ghost_id) or restore_candidate_from_ghost(updated)

            if candidate is not None:
                try:
                    learning_id = self.learning.learn(
                        ghost=updated,
                        candidate=candidate,
                        persist=persist,
                    )
                except Exception as exc:
                    try:
                        save_error("ghost_learning", str(exc), {"ghost": updated.to_dict()})
                    except Exception:
                        pass

        if learning_id:
            meta = dict(updated.meta or {})
            meta["learning_record_id"] = learning_id
            updated = GhostRecord(**{**updated.to_dict(), "meta": meta})

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
        return [self.coerce_ghost(r) for r in records]

    def stats(self, ghosts: Optional[Iterable[Any]] = None) -> GhostStats:
        source = ghosts if ghosts is not None else store().section("ghosts").values()
        records = [self.coerce_ghost(g) for g in source]

        total = len(records)
        open_count = sum(1 for g in records if g.status == GHOST_OPEN)
        closed_count = total - open_count
        tp1 = sum(1 for g in records if g.result == RESULT_TP1)
        tp2 = sum(1 for g in records if g.result == RESULT_TP2)
        ai_exit = sum(1 for g in records if g.result == RESULT_AI_EXIT)
        sl = sum(1 for g in records if g.result == RESULT_SL)
        expired = sum(1 for g in records if g.status == GHOST_EXPIRED)

        wins = tp1 + tp2 + ai_exit
        losses = sl
        wr = wins / (wins + losses) * 100.0 if (wins + losses) else 0.0

        return GhostStats(
            total=total,
            open_count=open_count,
            closed_count=closed_count,
            tp1_count=tp1,
            tp2_count=tp2,
            ai_exit_count=ai_exit,
            sl_count=sl,
            expired_count=expired,
            win_rate=clamp(wr),
        )

    def coerce_ghost(self, item: GhostRecord | Dict[str, Any]) -> GhostRecord:
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
            created_at=safe_int(item.get("created_at", now_ts())),
            expires_at=safe_int(item.get("expires_at", now_ts() + 3600)),
            status=str(item.get("status", GHOST_OPEN)).upper(),
            result=str(item.get("result", RESULT_UNKNOWN)).upper(),
            closed_at=safe_int(item.get("closed_at", 0)),
            tp1_hit=bool(item.get("tp1_hit", False)),
            tp2_hit=bool(item.get("tp2_hit", False)),
            ai_exit_hit=bool(item.get("ai_exit_hit", False)),
            sl_hit=bool(item.get("sl_hit", False)),
            mfe_percent=safe_float(item.get("mfe_percent")),
            mae_percent=safe_float(item.get("mae_percent")),
            max_price=safe_float(item.get("max_price", item.get("entry", 0.0))),
            min_price=safe_float(item.get("min_price", item.get("entry", 0.0))),
            last_price=safe_float(item.get("last_price", item.get("entry", 0.0))),
            monitor_count=safe_int(item.get("monitor_count", 0)),
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
    decision: AIDecision | str,
    candidate: AnalysisCandidate,
    entry: float,
    tp1: float,
    tp2: float,
    sl: float,
    ttl_seconds: Optional[int] = None,
    meta: Optional[JsonDict] = None,
    persist: bool = True,
    **kwargs: Any,
) -> GhostRecord:
    return manager().create_ghost(
        decision=decision,
        candidate=candidate,
        entry=entry,
        tp1=tp1,
        tp2=tp2,
        sl=sl,
        ttl_seconds=ttl_seconds,
        meta=meta,
        persist=persist,
        **kwargs,
    )


def monitor_ghost(
    ghost: GhostRecord | Dict[str, Any],
    price: float,
    candidate: Optional[AnalysisCandidate] = None,
    persist: bool = True,
    learn: bool = True,
    **kwargs: Any,
) -> GhostMonitorResult:
    return manager().monitor_ghost(
        ghost=ghost,
        price=price,
        candidate=candidate,
        persist=persist,
        learn=learn,
        **kwargs,
    )


def ghost_stats() -> GhostStats:
    return manager().stats()
