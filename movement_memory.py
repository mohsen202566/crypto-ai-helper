from __future__ import annotations

"""
15 - movement_memory.py

Pre-move pump/dump memory layer for the locked Movement Hunter architecture.

Responsibilities:
- Store conditions that existed before meaningful pumps/dumps.
- Learn separately per coin + direction + market state + condition.
- Track move percent, duration, MFE/MAE, trap/range/context.
- Provide similarity summaries to movement_predictor.py and ai_decision_engine.py.
- Feed Movement Memory persistence through data_store.py.

Strictly forbidden:
- No REAL/GHOST/REJECT decision.
- No trade execution.
- No Toobit calls.
- No Telegram.
- No Paper mode.
- No Setup flow.

This file is memory only. It does not decide final signals.
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
from data_store import save_movement_memory, append_bounded, store, save_error
from config import SETTINGS


JsonDict = Dict[str, Any]

MAX_MOVEMENT_MEMORY_RECORDS = 20000

MOVE_PUMP = "PUMP"
MOVE_DUMP = "DUMP"
MOVE_NONE = "NONE"

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"

QUALITY_LOW = "LOW"
QUALITY_MEDIUM = "MEDIUM"
QUALITY_HIGH = "HIGH"


@dataclass(frozen=True)
class PreMoveSignature:
    coin: str
    movement_type: str
    direction: str
    market_state: str
    freshness: str
    rsi_bucket: str
    adx_bucket: str
    hist_bucket: str
    atr_bucket: str
    volume_bucket: str
    power_bucket: str
    trap_bucket: str
    range_bucket: str
    state_bucket: str

    def key(self) -> str:
        return "|".join(
            [
                self.coin,
                self.movement_type,
                self.direction,
                self.market_state,
                self.freshness,
                self.rsi_bucket,
                self.adx_bucket,
                self.hist_bucket,
                self.atr_bucket,
                self.volume_bucket,
                self.power_bucket,
                self.trap_bucket,
                self.range_bucket,
                self.state_bucket,
            ]
        )

    def soft_key(self) -> str:
        return "|".join(
            [
                self.coin,
                self.movement_type,
                self.direction,
                self.market_state,
                self.freshness,
                self.rsi_bucket,
                self.power_bucket,
                self.range_bucket,
            ]
        )

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class MovementMemoryRecord:
    movement_id: str
    coin: str
    movement_type: str
    direction: str
    signature_key: str
    soft_signature_key: str
    timestamp: int

    before_price: float
    after_price: float
    move_percent: float
    move_duration_seconds: int
    mfe_percent: float
    mae_percent: float

    market_state: str
    movement_phase: str
    freshness: str
    confidence_level: str

    rsi: float
    rsi_slope: float
    macd_histogram: float
    histogram_slope: float
    histogram_acceleration: float
    adx: float
    atr_percent: float
    relative_volume: float
    power_delta: float
    vwap_state: str
    ema_state: str
    trap_risk: float
    liquidity_risk: float
    range_probability: float
    reversal_probability: float
    quality_score: float
    risk_score: float
    movement_score: float
    confidence_score: float

    meta: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class MovementMemorySummary:
    signature_key: str
    coin: str
    movement_type: str
    sample_count: int
    avg_move_percent: float
    avg_duration_seconds: float
    avg_mfe_percent: float
    avg_mae_percent: float
    success_rate: float
    quality_label: str
    notes: Tuple[str, ...] = field(default_factory=tuple)

    # Extra hunter metrics. They do not change the public responsibility of
    # this file, but they let movement_predictor / ai_decision_engine know
    # whether a pattern was only profitable late, or actually detected early.
    outcome_success_rate: float = 0.0
    timing_score: float = 0.0
    early_success_rate: float = 0.0
    fuzzy_match_score: float = 0.0

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


def normalize_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().replace("-", "").replace("/", "").replace("_", "").strip()
    if s and not s.endswith("USDT") and len(s) <= 14:
        s += "USDT"
    return s


def normalize_direction(direction: str) -> str:
    d = str(direction or "").upper().strip()
    if d in {"LONG", "BUY"}:
        return DIRECTION_LONG
    if d in {"SHORT", "SELL"}:
        return DIRECTION_SHORT
    return DIRECTION_NEUTRAL


def movement_type_from_direction(direction: str) -> str:
    d = normalize_direction(direction)
    if d == DIRECTION_LONG:
        return MOVE_PUMP
    if d == DIRECTION_SHORT:
        return MOVE_DUMP
    return MOVE_NONE


def pct_move(direction: str, start_price: float, end_price: float) -> float:
    start = safe_float(start_price)
    end = safe_float(end_price)
    if start <= 0 or end <= 0:
        return 0.0
    if normalize_direction(direction) == DIRECTION_SHORT:
        return (start - end) / start * 100.0
    return (end - start) / start * 100.0


def bucket_rsi(value: float) -> str:
    v = safe_float(value, 50.0)
    if v < 25:
        return "RSI_EXTREME_LOW"
    if v < 35:
        return "RSI_LOW"
    if v < 45:
        return "RSI_LOW_MID"
    if v < 55:
        return "RSI_MID"
    if v < 65:
        return "RSI_HIGH_MID"
    if v < 75:
        return "RSI_HIGH"
    return "RSI_EXTREME_HIGH"


def bucket_adx(value: float) -> str:
    v = safe_float(value)
    if v < 14:
        return "ADX_VERY_WEAK"
    if v < 20:
        return "ADX_WEAK"
    if v < 28:
        return "ADX_NORMAL"
    if v < 40:
        return "ADX_STRONG"
    return "ADX_EXTREME"


def bucket_signed(value: float, prefix: str, small: float = 0.0, medium: float = 0.0001) -> str:
    v = safe_float(value)
    if v > medium:
        return f"{prefix}_STRONG_UP"
    if v > small:
        return f"{prefix}_UP"
    if v < -medium:
        return f"{prefix}_STRONG_DOWN"
    if v < -small:
        return f"{prefix}_DOWN"
    return f"{prefix}_FLAT"


def bucket_atr(value: float) -> str:
    v = safe_float(value)
    if v < 0.25:
        return "ATR_TINY"
    if v < 0.60:
        return "ATR_NORMAL"
    if v < 1.20:
        return "ATR_HIGH"
    if v < 2.50:
        return "ATR_EXTREME"
    return "ATR_DANGER"


def bucket_volume(value: float) -> str:
    v = safe_float(value)
    if v < 0.7:
        return "VOL_LOW"
    if v < 1.2:
        return "VOL_NORMAL"
    if v < 2.0:
        return "VOL_HIGH"
    if v < 4.0:
        return "VOL_SPIKE"
    return "VOL_EXTREME"


def bucket_power(value: float) -> str:
    v = safe_float(value)
    if v >= 35:
        return "POWER_STRONG_BUY"
    if v >= 12:
        return "POWER_BUY"
    if v <= -35:
        return "POWER_STRONG_SELL"
    if v <= -12:
        return "POWER_SELL"
    return "POWER_BALANCED"


def bucket_percent(value: float, prefix: str) -> str:
    v = clamp(value)
    if v < 25:
        return f"{prefix}_LOW"
    if v < 50:
        return f"{prefix}_MID"
    if v < 75:
        return f"{prefix}_HIGH"
    return f"{prefix}_EXTREME"


class PreMoveSignatureBuilder:
    """Builds a stable condition signature before pump/dump moves."""

    def build(
        self,
        candidate: AnalysisCandidate,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
    ) -> PreMoveSignature:
        s = candidate.sensor_snapshot
        coin = normalize_symbol(candidate.symbol)
        direction = normalize_direction(candidate.direction_hint)
        movement_type = movement_type_from_direction(direction)

        market_state = state.market_state if state else str(getattr(s, "market_state", "UNKNOWN"))
        freshness = movement.freshness if movement else "UNKNOWN"
        trap_risk = trap.trap_risk if trap else 0.0
        range_probability = state.range_probability if state else s.range_probability
        state_bucket = bucket_percent(state.state_confidence if state else 0.0, "STATE")

        return PreMoveSignature(
            coin=coin,
            movement_type=movement_type,
            direction=direction,
            market_state=str(market_state),
            freshness=str(freshness),
            rsi_bucket=bucket_rsi(s.rsi),
            adx_bucket=bucket_adx(s.adx),
            hist_bucket=bucket_signed(s.histogram_slope, "HIST"),
            atr_bucket=bucket_atr(s.atr_percent),
            volume_bucket=bucket_volume(s.relative_volume),
            power_bucket=bucket_power(s.power_delta),
            trap_bucket=bucket_percent(trap_risk, "TRAP"),
            range_bucket=bucket_percent(range_probability, "RANGE"),
            state_bucket=state_bucket,
        )


class MovementMemoryRecordBuilder:
    """Builds MovementMemoryRecord from candidate and observed move outcome."""

    def __init__(self):
        self.signature_builder = PreMoveSignatureBuilder()

    def build(
        self,
        candidate: AnalysisCandidate,
        after_price: float,
        move_duration_seconds: int,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
        confidence: Optional[ConfidenceResult] = None,
        mfe_percent: float = 0.0,
        mae_percent: float = 0.0,
        meta: Optional[JsonDict] = None,
    ) -> MovementMemoryRecord:
        s = candidate.sensor_snapshot
        direction = normalize_direction(candidate.direction_hint)
        before_price = safe_float(s.price)
        after_price = safe_float(after_price)
        move_percent = pct_move(direction, before_price, after_price)
        signature = self.signature_builder.build(candidate, movement=movement, trap=trap, state=state)

        return MovementMemoryRecord(
            movement_id=f"movmem_{uuid4().hex}",
            coin=signature.coin,
            movement_type=signature.movement_type,
            direction=signature.direction,
            signature_key=signature.key(),
            soft_signature_key=signature.soft_key(),
            timestamp=candidate.timestamp or now_ts(),
            before_price=before_price,
            after_price=after_price,
            move_percent=move_percent,
            move_duration_seconds=safe_int(move_duration_seconds),
            mfe_percent=safe_float(mfe_percent if mfe_percent else max(0.0, move_percent)),
            mae_percent=safe_float(mae_percent),
            market_state=signature.market_state,
            movement_phase=movement.movement_phase if movement else "UNKNOWN",
            freshness=movement.freshness if movement else "UNKNOWN",
            confidence_level=confidence.confidence_level if confidence else "UNKNOWN",
            rsi=safe_float(s.rsi),
            rsi_slope=safe_float(s.rsi_slope),
            macd_histogram=safe_float(s.macd_histogram),
            histogram_slope=safe_float(s.histogram_slope),
            histogram_acceleration=safe_float(s.histogram_acceleration),
            adx=safe_float(s.adx),
            atr_percent=safe_float(s.atr_percent),
            relative_volume=safe_float(s.relative_volume),
            power_delta=safe_float(s.power_delta),
            vwap_state=str(s.vwap_state),
            ema_state=str(s.ema_state),
            trap_risk=safe_float(trap.trap_risk if trap else 0.0),
            liquidity_risk=safe_float(trap.liquidity_risk if trap else 0.0),
            range_probability=safe_float(state.range_probability if state else s.range_probability),
            reversal_probability=safe_float(state.reversal_probability if state else 0.0),
            quality_score=safe_float(candidate.quality.total_quality),
            risk_score=safe_float(candidate.risk.total_risk),
            movement_score=safe_float(movement.readiness_score if movement else 0.0),
            confidence_score=safe_float(confidence.confidence_score if confidence else 0.0),
            meta=dict(meta or {}),
        )


def _bucket_family(bucket: str) -> str:
    """Return the part before the last bucket qualifier for fuzzy matching."""
    b = str(bucket or "UNKNOWN")
    if "_" not in b:
        return b
    return "_".join(b.split("_")[:-1])


def _bucket_similarity(a: str, b: str) -> float:
    """Soft bucket comparison: exact > same family > opposite/unknown."""
    a = str(a or "UNKNOWN")
    b = str(b or "UNKNOWN")
    if a == b:
        return 1.0
    if "UNKNOWN" in {a, b}:
        return 0.35
    if _bucket_family(a) == _bucket_family(b):
        return 0.68

    # Directional buckets such as HIST_UP/HIST_STRONG_UP or POWER_BUY/STRONG_BUY
    # should still count as nearby even when the exact strength differs.
    bullish_tokens = ("UP", "BUY", "HIGH")
    bearish_tokens = ("DOWN", "SELL", "LOW")
    if any(t in a for t in bullish_tokens) and any(t in b for t in bullish_tokens):
        return 0.55
    if any(t in a for t in bearish_tokens) and any(t in b for t in bearish_tokens):
        return 0.55
    return 0.0


def _record_signature(record: MovementMemoryRecord) -> PreMoveSignature:
    """Rebuild a comparable signature from a stored memory record."""
    return PreMoveSignature(
        coin=normalize_symbol(record.coin),
        movement_type=str(record.movement_type or MOVE_NONE).upper(),
        direction=normalize_direction(record.direction),
        market_state=str(record.market_state or "UNKNOWN"),
        freshness=str(record.freshness or "UNKNOWN"),
        rsi_bucket=bucket_rsi(record.rsi),
        adx_bucket=bucket_adx(record.adx),
        hist_bucket=bucket_signed(record.histogram_slope, "HIST"),
        atr_bucket=bucket_atr(record.atr_percent),
        volume_bucket=bucket_volume(record.relative_volume),
        power_bucket=bucket_power(record.power_delta),
        trap_bucket=bucket_percent(record.trap_risk, "TRAP"),
        range_bucket=bucket_percent(record.range_probability, "RANGE"),
        state_bucket=bucket_percent(record.confidence_score, "STATE"),
    )


def signature_similarity(target: PreMoveSignature, record: MovementMemoryRecord) -> float:
    """Fuzzy condition similarity for Movement Hunter.

    Old behavior was exact signature -> soft signature -> coin/direction. That
    misses near-identical conditions. This score lets the AI use nearby memories
    like DOGE LONG RSI 54 vs RSI 56, while still keeping coin+direction as the
    anchor so unrelated markets do not dominate.
    """
    r = _record_signature(record)
    score = 0.0
    total = 0.0

    def add(weight: float, value: float) -> None:
        nonlocal score, total
        total += weight
        score += weight * max(0.0, min(1.0, value))

    add(22.0, 1.0 if target.coin == r.coin else 0.0)
    add(20.0, 1.0 if target.direction == r.direction else 0.0)
    add(8.0, 1.0 if target.movement_type == r.movement_type else 0.0)
    add(8.0, 1.0 if target.market_state == r.market_state else (0.55 if "UNKNOWN" in {target.market_state, r.market_state} else 0.25))

    # Fresh/MID are close for hunters; LATE/DEAD are not close to FRESH.
    if target.freshness == r.freshness:
        fresh_sim = 1.0
    elif {target.freshness, r.freshness} <= {"FRESH", "MID", "UNKNOWN"}:
        fresh_sim = 0.62
    else:
        fresh_sim = 0.15
    add(8.0, fresh_sim)

    add(6.0, _bucket_similarity(target.rsi_bucket, r.rsi_bucket))
    add(5.0, _bucket_similarity(target.adx_bucket, r.adx_bucket))
    add(9.0, _bucket_similarity(target.hist_bucket, r.hist_bucket))
    add(5.0, _bucket_similarity(target.atr_bucket, r.atr_bucket))
    add(6.0, _bucket_similarity(target.volume_bucket, r.volume_bucket))
    add(9.0, _bucket_similarity(target.power_bucket, r.power_bucket))
    add(4.0, _bucket_similarity(target.trap_bucket, r.trap_bucket))
    add(4.0, _bucket_similarity(target.range_bucket, r.range_bucket))
    add(4.0, _bucket_similarity(target.state_bucket, r.state_bucket))

    if total <= 0:
        return 0.0
    return clamp(score / total * 100.0)


class MovementMemoryIndex:
    """Fast in-memory index for pre-move memory similarity."""

    def __init__(self, records: Optional[Iterable[Any]] = None):
        self.records: List[MovementMemoryRecord] = []
        for record in records or []:
            try:
                self.records.append(self._coerce_record(record))
            except Exception:
                continue

    def _coerce_record(self, item: Any) -> MovementMemoryRecord:
        if isinstance(item, MovementMemoryRecord):
            return item
        if hasattr(item, "to_dict") and callable(item.to_dict):
            item = item.to_dict()
        if not isinstance(item, dict):
            item = {}

        return MovementMemoryRecord(
            movement_id=str(item.get("movement_id", item.get("id", f"movmem_{uuid4().hex}"))),
            coin=normalize_symbol(item.get("coin", item.get("symbol", ""))),
            movement_type=str(item.get("movement_type", MOVE_NONE)).upper(),
            direction=normalize_direction(item.get("direction", "")),
            signature_key=str(item.get("signature_key", "")),
            soft_signature_key=str(item.get("soft_signature_key", "")),
            timestamp=safe_int(item.get("timestamp", now_ts())),
            before_price=safe_float(item.get("before_price")),
            after_price=safe_float(item.get("after_price")),
            move_percent=safe_float(item.get("move_percent")),
            move_duration_seconds=safe_int(item.get("move_duration_seconds")),
            mfe_percent=safe_float(item.get("mfe_percent")),
            mae_percent=safe_float(item.get("mae_percent")),
            market_state=str(item.get("market_state", "UNKNOWN")),
            movement_phase=str(item.get("movement_phase", "UNKNOWN")),
            freshness=str(item.get("freshness", "UNKNOWN")),
            confidence_level=str(item.get("confidence_level", "UNKNOWN")),
            rsi=safe_float(item.get("rsi"), 50.0),
            rsi_slope=safe_float(item.get("rsi_slope")),
            macd_histogram=safe_float(item.get("macd_histogram")),
            histogram_slope=safe_float(item.get("histogram_slope")),
            histogram_acceleration=safe_float(item.get("histogram_acceleration")),
            adx=safe_float(item.get("adx")),
            atr_percent=safe_float(item.get("atr_percent")),
            relative_volume=safe_float(item.get("relative_volume")),
            power_delta=safe_float(item.get("power_delta")),
            vwap_state=str(item.get("vwap_state", "UNKNOWN")),
            ema_state=str(item.get("ema_state", "UNKNOWN")),
            trap_risk=safe_float(item.get("trap_risk")),
            liquidity_risk=safe_float(item.get("liquidity_risk")),
            range_probability=safe_float(item.get("range_probability")),
            reversal_probability=safe_float(item.get("reversal_probability")),
            quality_score=safe_float(item.get("quality_score")),
            risk_score=safe_float(item.get("risk_score")),
            movement_score=safe_float(item.get("movement_score")),
            confidence_score=safe_float(item.get("confidence_score")),
            meta=dict(item.get("meta", {}) if isinstance(item.get("meta", {}), dict) else {}),
        )

    def add(self, record: MovementMemoryRecord) -> None:
        self.records.append(record)
        max_records = max(100, int(getattr(SETTINGS.learning, "max_records", 20000)))
        if len(self.records) > max_records:
            self.records = self.records[-max_records:]

    def by_signature(self, signature_key: str) -> List[MovementMemoryRecord]:
        return [r for r in self.records if r.signature_key == signature_key]

    def by_soft_signature(self, soft_key: str) -> List[MovementMemoryRecord]:
        return [r for r in self.records if r.soft_signature_key == soft_key]

    def by_coin_direction(self, coin: str, direction: str) -> List[MovementMemoryRecord]:
        c = normalize_symbol(coin)
        d = normalize_direction(direction)
        return [r for r in self.records if r.coin == c and r.direction == d]

    def by_fuzzy_similarity(self, signature: PreMoveSignature, min_similarity: float = 54.0, limit: int = 80) -> List[MovementMemoryRecord]:
        scored: List[Tuple[float, int, MovementMemoryRecord]] = []
        for idx, record in enumerate(self.records):
            # Keep coin+direction anchored. Similarity across another coin can be
            # added later as sector memory, but this file learns per coin/direction.
            if normalize_symbol(record.coin) != signature.coin:
                continue
            if normalize_direction(record.direction) != signature.direction:
                continue
            sim = signature_similarity(signature, record)
            if sim >= min_similarity:
                # Prefer more similar and more recent records.
                scored.append((sim, safe_int(record.timestamp), record))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [r for _, _, r in scored[:max(1, limit)]]

    def summarize(self, signature: PreMoveSignature) -> MovementMemorySummary:
        exact = self.by_signature(signature.key())
        soft = self.by_soft_signature(signature.soft_key())
        fuzzy = self.by_fuzzy_similarity(signature)

        # Use exact matches first, but enrich with near matches. This fixes the
        # old all-or-nothing behavior and lets the AI learn from similar RSI/ADX/
        # power/volume conditions instead of pretending they are unrelated.
        combined: List[MovementMemoryRecord] = []
        seen: set[str] = set()
        for group in (exact, soft, fuzzy):
            for record in group:
                if record.movement_id in seen:
                    continue
                combined.append(record)
                seen.add(record.movement_id)

        if not combined:
            # Last fallback stays per coin+direction, but cap it so ancient or
            # unrelated states do not overwhelm the current signature.
            combined = sorted(
                self.by_coin_direction(signature.coin, signature.direction),
                key=lambda r: safe_int(r.timestamp),
                reverse=True,
            )[:80]

        return summarize_movement_memory(signature, combined)


def _record_outcome_success(record: MovementMemoryRecord) -> bool:
    """Was the direction eventually profitable enough to matter?"""
    favorable = max(safe_float(record.move_percent), safe_float(record.mfe_percent))
    adverse = abs(safe_float(record.mae_percent))
    min_move = max(0.18, min(0.75, safe_float(record.atr_percent) * 0.55))
    return favorable >= min_move and favorable >= adverse * 0.75


def _record_timing_score(record: MovementMemoryRecord) -> float:
    """How early/useful was the detection timing? 0-100.

    Result matters, but Movement Hunter also needs timing. A winning setup that
    was detected late should score lower than a fresh setup that gave enough MFE
    quickly.
    """
    score = 50.0
    freshness = str(record.freshness or "UNKNOWN").upper()
    phase = str(record.movement_phase or "UNKNOWN").upper()
    duration = max(0, safe_int(record.move_duration_seconds))

    if freshness == "FRESH":
        score += 22
    elif freshness == "MID":
        score += 8
    elif freshness in {"LATE", "DEAD"}:
        score -= 22

    if phase in {"START", "EARLY"}:
        score += 18
    elif phase in {"MIDDLE", "MID"}:
        score += 3
    elif phase in {"LATE", "EXHAUSTION", "RANGE"}:
        score -= 18

    # The target style is 5M-15M hunting. Reward moves that developed fast,
    # but do not punish slightly longer wins too hard.
    if 0 < duration <= 300:
        score += 12
    elif duration <= 900:
        score += 6
    elif duration >= 1800:
        score -= 8

    # MFE before MAE means timing and entry quality were useful.
    if safe_float(record.mfe_percent) > abs(safe_float(record.mae_percent)) * 1.25:
        score += 8
    elif abs(safe_float(record.mae_percent)) > safe_float(record.mfe_percent) * 1.25:
        score -= 8

    return clamp(score)


def _record_composite_success_score(record: MovementMemoryRecord) -> float:
    """Combine outcome and timing for Movement Hunter memory."""
    outcome_ok = _record_outcome_success(record)
    timing = _record_timing_score(record)
    favorable = max(safe_float(record.move_percent), safe_float(record.mfe_percent))
    adverse = abs(safe_float(record.mae_percent))

    if outcome_ok:
        # Result is primary, timing decides how valuable the pattern is.
        score = 68.0 + timing * 0.32
        if favorable >= adverse * 1.8:
            score += 4.0
        return clamp(score)

    # Bad result must remain bad even if the initial timing looked early.
    score = timing * 0.35
    if adverse > favorable * 1.5:
        score -= 10.0
    return clamp(score)


def summarize_movement_memory(signature: PreMoveSignature, records: Sequence[MovementMemoryRecord]) -> MovementMemorySummary:
    total = len(records)
    if total == 0:
        return MovementMemorySummary(
            signature_key=signature.key(),
            coin=signature.coin,
            movement_type=signature.movement_type,
            sample_count=0,
            avg_move_percent=0.0,
            avg_duration_seconds=0.0,
            avg_mfe_percent=0.0,
            avg_mae_percent=0.0,
            success_rate=50.0,
            quality_label="LOW_DATA",
            notes=("NO_PREMOVE_HISTORY",),
            outcome_success_rate=0.0,
            timing_score=0.0,
            early_success_rate=0.0,
            fuzzy_match_score=0.0,
        )

    avg_move = sum(safe_float(r.move_percent) for r in records) / total
    avg_duration = sum(safe_float(r.move_duration_seconds) for r in records) / total
    avg_mfe = sum(safe_float(r.mfe_percent) for r in records) / total
    avg_mae = sum(safe_float(r.mae_percent) for r in records) / total

    outcome_successes = [_record_outcome_success(r) for r in records]
    timing_scores = [_record_timing_score(r) for r in records]
    composite_scores = [_record_composite_success_score(r) for r in records]
    similarity_scores = [signature_similarity(signature, r) for r in records]

    outcome_success_rate = sum(1 for ok in outcome_successes if ok) / total * 100.0
    timing_score = sum(timing_scores) / total if timing_scores else 0.0
    success_rate = sum(composite_scores) / total if composite_scores else 50.0
    fuzzy_match_score = sum(similarity_scores) / total if similarity_scores else 0.0

    early_success_items = [
        r for r, ok, t in zip(records, outcome_successes, timing_scores)
        if ok and t >= 62.0
    ]
    early_success_rate = len(early_success_items) / total * 100.0

    notes: List[str] = []
    if total < int(getattr(SETTINGS.learning, "min_samples_for_confidence", 10)):
        quality = "LOW_DATA"
        notes.append("LOW_SAMPLE_COUNT")
    elif success_rate >= 68 and outcome_success_rate >= 58 and avg_move > 0:
        quality = QUALITY_HIGH
        notes.append("PREMOVE_PATTERN_WORKED_WITH_TIMING")
    elif success_rate <= 42 or outcome_success_rate <= 38:
        quality = QUALITY_LOW
        notes.append("PREMOVE_PATTERN_WEAK_OR_LATE")
    else:
        quality = QUALITY_MEDIUM

    if timing_score >= 65:
        notes.append("TIMING_GOOD_EARLY_PATTERN")
    elif timing_score <= 42:
        notes.append("TIMING_LATE_OR_WEAK_PATTERN")

    if early_success_rate >= 45 and total >= 5:
        notes.append("EARLY_SUCCESS_PATTERN_FOUND")
    if fuzzy_match_score >= 70:
        notes.append("FUZZY_SIMILARITY_STRONG")
    elif fuzzy_match_score < 55:
        notes.append("FUZZY_SIMILARITY_WEAK")

    if avg_mae > avg_mfe:
        notes.append("ADVERSE_MOVE_LARGER_THAN_FAVORABLE")

    return MovementMemorySummary(
        signature_key=signature.key(),
        coin=signature.coin,
        movement_type=signature.movement_type,
        sample_count=total,
        avg_move_percent=avg_move,
        avg_duration_seconds=avg_duration,
        avg_mfe_percent=avg_mfe,
        avg_mae_percent=avg_mae,
        success_rate=clamp(success_rate),
        quality_label=quality,
        notes=tuple(dict.fromkeys(notes)),
        outcome_success_rate=clamp(outcome_success_rate),
        timing_score=clamp(timing_score),
        early_success_rate=clamp(early_success_rate),
        fuzzy_match_score=clamp(fuzzy_match_score),
    )


class MovementMemoryEngine:
    """
    Main Movement Memory Engine.

    Stores and summarizes pre-pump/pre-dump conditions.
    """

    def __init__(self, records: Optional[Iterable[Any]] = None):
        if records is None:
            try:
                records = store().section("movement_memory").values()
            except Exception:
                records = []
        self.index = MovementMemoryIndex(records=records)
        self.record_builder = MovementMemoryRecordBuilder()
        self.signature_builder = PreMoveSignatureBuilder()

    def record_movement(
        self,
        candidate: AnalysisCandidate,
        after_price: float,
        move_duration_seconds: int,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
        confidence: Optional[ConfidenceResult] = None,
        mfe_percent: float = 0.0,
        mae_percent: float = 0.0,
        meta: Optional[JsonDict] = None,
        persist: bool = True,
    ) -> MovementMemoryRecord:
        record = self.record_builder.build(
            candidate=candidate,
            after_price=after_price,
            move_duration_seconds=move_duration_seconds,
            movement=movement,
            trap=trap,
            state=state,
            confidence=confidence,
            mfe_percent=mfe_percent,
            mae_percent=mae_percent,
            meta=meta,
        )
        self.index.add(record)
        if persist:
            append_bounded('movement_memory', record.movement_id, record.to_dict(), max_items=MAX_MOVEMENT_MEMORY_RECORDS, sort_key='timestamp')
        return record

    def summarize_candidate(
        self,
        candidate: AnalysisCandidate,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
    ) -> MovementMemorySummary:
        signature = self.signature_builder.build(candidate, movement=movement, trap=trap, state=state)
        return self.index.summarize(signature)


_default_engine: Optional[MovementMemoryEngine] = None


def _load_persisted_movement_records() -> List[Any]:
    """Load persisted movement-memory records so summaries survive restarts."""
    try:
        return list(store().section("movement_memory").values())
    except Exception as exc:
        try:
            save_error("movement_memory_load", str(exc), {})
        except Exception:
            pass
        return []


def engine(records: Optional[Iterable[Any]] = None) -> MovementMemoryEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = MovementMemoryEngine(records=_load_persisted_movement_records())
    elif records is not None:
        existing = list(_default_engine.index.records)
        _default_engine = MovementMemoryEngine(records=[*existing, *list(records)])
    return _default_engine


def record_movement_memory(
    candidate: AnalysisCandidate,
    after_price: float,
    move_duration_seconds: int,
    movement: Optional[MovementHunterResult] = None,
    trap: Optional[TrapResult] = None,
    state: Optional[StateResult] = None,
    confidence: Optional[ConfidenceResult] = None,
    mfe_percent: float = 0.0,
    mae_percent: float = 0.0,
    meta: Optional[JsonDict] = None,
    persist: bool = True,
) -> MovementMemoryRecord:
    return engine().record_movement(
        candidate=candidate,
        after_price=after_price,
        move_duration_seconds=move_duration_seconds,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        mfe_percent=mfe_percent,
        mae_percent=mae_percent,
        meta=meta,
        persist=persist,
    )


def summarize_movement_candidate(
    candidate: AnalysisCandidate,
    movement: Optional[MovementHunterResult] = None,
    trap: Optional[TrapResult] = None,
    state: Optional[StateResult] = None,
) -> MovementMemorySummary:
    return engine().summarize_candidate(candidate, movement=movement, trap=trap, state=state)


def movement_memory_summary_for_predictor(
    candidate: AnalysisCandidate,
    movement: Optional[MovementHunterResult] = None,
    trap: Optional[TrapResult] = None,
    state: Optional[StateResult] = None,
) -> JsonDict:
    return summarize_movement_candidate(candidate, movement=movement, trap=trap, state=state).to_dict()
