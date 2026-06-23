from __future__ import annotations

"""
15 - movement_memory.py

Simplified Pattern / Movement Memory for the Level 1 / 5M crypto futures bot.

Locked goals:
- Store and summarize early pump/dump patterns.
- Learn separately per coin + direction + level/timeframe.
- Store raw technical sensor values, slopes and outcome quality.
- Provide pattern summaries to movement_predictor.py and ai_decision_engine.py.
- No final REAL / GHOST / REJECT decision.
- No trap/confidence/correlation/meta/state engine.
- No Toobit, no Telegram, no paper/setup flow.

This file is memory only.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import uuid4
import math
import time

from analysis_engine import AnalysisCandidate
from config import SETTINGS, normalize_symbol

try:
    from data_store import append_bounded, store, save_error
except Exception:  # pragma: no cover - keeps file import-safe while rewrites continue
    append_bounded = None
    store = None
    save_error = None


JsonDict = Dict[str, Any]

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"

MOVE_PUMP = "PUMP"
MOVE_DUMP = "DUMP"
MOVE_NONE = "NONE"

QUALITY_WEAK = "WEAK"
QUALITY_GOOD = "GOOD"
QUALITY_EXCELLENT = "EXCELLENT"
QUALITY_BAD = "BAD"

MAX_MOVEMENT_MEMORY_RECORDS = 20000


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


def bucket_range(value: float, step: float, prefix: str) -> str:
    v = safe_float(value)
    if step <= 0:
        step = 1.0
    low = math.floor(v / step) * step
    high = low + step
    return f"{prefix}_{round(low, 6)}_{round(high, 6)}"


def bucket_rsi(value: float) -> str:
    return bucket_range(safe_float(value, 50.0), 3.0, "RSI")


def bucket_adx(value: float) -> str:
    v = safe_float(value)
    if v < 14:
        return "ADX_UNDER_14"
    if v < 20:
        return "ADX_14_20"
    if v < 25:
        return "ADX_20_25"
    if v < 30:
        return "ADX_25_30"
    if v < 40:
        return "ADX_30_40"
    return "ADX_40_PLUS"


def bucket_signed(value: float, prefix: str, tiny: float = 0.0) -> str:
    v = safe_float(value)
    if v > tiny:
        return f"{prefix}_UP"
    if v < -tiny:
        return f"{prefix}_DOWN"
    return f"{prefix}_FLAT"


def bucket_power(value: float) -> str:
    v = safe_float(value)
    if v >= 20:
        return "POWER_STRONG_BUY"
    if v >= 6:
        return "POWER_BUY"
    if v <= -20:
        return "POWER_STRONG_SELL"
    if v <= -6:
        return "POWER_SELL"
    return "POWER_BALANCED"


def bucket_volume(value: float) -> str:
    v = safe_float(value)
    if v < 0.8:
        return "VOL_LOW"
    if v < 1.25:
        return "VOL_NORMAL"
    if v < 2.0:
        return "VOL_HIGH"
    return "VOL_SPIKE"


def bucket_atr(value: float) -> str:
    v = safe_float(value)
    if v < 0.25:
        return "ATR_TINY"
    if v < 0.65:
        return "ATR_NORMAL"
    if v < 1.2:
        return "ATR_HIGH"
    return "ATR_EXTREME"


@dataclass(frozen=True)
class PatternSignature:
    coin: str
    direction: str
    movement_type: str
    timeframe: str

    rsi_bucket: str
    rsi_slope_bucket: str
    hist_bucket: str
    hist_slope_bucket: str
    adx_bucket: str
    power_bucket: str
    volume_bucket: str
    atr_bucket: str
    vwap_state: str
    ema_state: str
    compression_bucket: str

    def key(self) -> str:
        return "|".join([
            self.coin,
            self.direction,
            self.movement_type,
            self.timeframe,
            self.rsi_bucket,
            self.rsi_slope_bucket,
            self.hist_bucket,
            self.hist_slope_bucket,
            self.adx_bucket,
            self.power_bucket,
            self.volume_bucket,
            self.atr_bucket,
            self.vwap_state,
            self.ema_state,
            self.compression_bucket,
        ])

    def soft_key(self) -> str:
        return "|".join([
            self.coin,
            self.direction,
            self.movement_type,
            self.timeframe,
            self.rsi_bucket,
            self.hist_slope_bucket,
            self.power_bucket,
            self.volume_bucket,
        ])

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class MovementMemoryRecord:
    record_id: str
    symbol: str
    direction: str
    movement_type: str
    timeframe: str
    timestamp: int

    signature_key: str
    soft_signature_key: str

    entry_price: float
    exit_price: float
    move_percent: float
    mfe_percent: float
    mae_percent: float
    duration_seconds: int
    outcome: str
    quality: str

    rsi: float
    rsi_slope: float
    rsi_acceleration: float
    macd: float
    macd_signal: float
    macd_histogram: float
    histogram_slope: float
    histogram_acceleration: float
    adx: float
    adx_slope: float
    plus_di: float
    minus_di: float
    buy_power: float
    sell_power: float
    power_delta: float
    relative_volume: float
    volume_expansion: bool
    volume_spike: bool
    atr_percent: float
    atr_slope: float
    atr_expansion: str
    atr_explosion: bool
    ema_state: str
    vwap_state: str
    vwap_distance_percent: float
    range_probability: float
    compression_score: float
    price_change_percent: float

    market_mode: str = "NEUTRAL"
    meta: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class MovementMemorySummary:
    signature_key: str
    coin: str
    movement_type: str
    direction: str

    sample_count: int
    pattern_count: int
    pattern_match_score: float
    pattern_confidence: float
    matched_pattern_id: str
    pattern_win_rate: float

    avg_move_percent: float
    expected_move_percent: float
    expected_pullback_percent: float
    avg_duration_seconds: float
    avg_mfe_percent: float
    avg_mae_percent: float

    success_rate: float
    outcome_success_rate: float
    timing_score: float
    early_success_rate: float
    fuzzy_match_score: float
    quality_label: str
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)


class PatternSignatureBuilder:
    def build(self, candidate: AnalysisCandidate) -> PatternSignature:
        m = candidate.momentum_state
        coin = normalize_symbol(candidate.symbol)
        direction = normalize_direction(candidate.direction_hint)
        movement_type = movement_type_from_direction(direction)

        return PatternSignature(
            coin=coin,
            direction=direction,
            movement_type=movement_type,
            timeframe=str(candidate.timeframe or "5m"),
            rsi_bucket=bucket_rsi(m.rsi),
            rsi_slope_bucket=bucket_signed(m.rsi_slope, "RSI_SLOPE"),
            hist_bucket=bucket_signed(m.macd_histogram, "HIST"),
            hist_slope_bucket=bucket_signed(m.histogram_slope, "HIST_SLOPE"),
            adx_bucket=bucket_adx(m.adx),
            power_bucket=bucket_power(m.power_delta),
            volume_bucket=bucket_volume(m.relative_volume),
            atr_bucket=bucket_atr(m.atr_percent),
            vwap_state=str(m.vwap_state or "UNKNOWN").upper(),
            ema_state=str(m.ema_state or "UNKNOWN").upper(),
            compression_bucket=bucket_range(m.compression_score, 20.0, "COMP"),
        )


def classify_quality(move_percent: float, mfe_percent: float, mae_percent: float, atr_percent: float) -> str:
    favorable = max(safe_float(move_percent), safe_float(mfe_percent))
    adverse = abs(safe_float(mae_percent))
    atr = max(0.05, safe_float(atr_percent))
    if favorable >= atr * 1.4 and favorable > adverse * 1.5:
        return QUALITY_EXCELLENT
    if favorable >= atr * 0.85 and favorable >= adverse:
        return QUALITY_GOOD
    if favorable >= atr * 0.45:
        return QUALITY_WEAK
    return QUALITY_BAD


def is_success(record: MovementMemoryRecord) -> bool:
    return record.quality in {QUALITY_GOOD, QUALITY_EXCELLENT} or str(record.outcome).upper() in {"TP1", "TP2", "AI_EXIT_PROFIT"}


class MovementMemoryRecordBuilder:
    def __init__(self):
        self.signature_builder = PatternSignatureBuilder()

    def build(
        self,
        candidate: AnalysisCandidate,
        exit_price: float,
        duration_seconds: int,
        outcome: str = "UNKNOWN",
        mfe_percent: float = 0.0,
        mae_percent: float = 0.0,
        meta: Optional[JsonDict] = None,
    ) -> MovementMemoryRecord:
        m = candidate.momentum_state
        direction = normalize_direction(candidate.direction_hint)
        entry_price = safe_float(getattr(candidate.sensor_snapshot, "price", 0.0))
        exit_price = safe_float(exit_price)
        move_percent = pct_move(direction, entry_price, exit_price)
        signature = self.signature_builder.build(candidate)
        quality = classify_quality(move_percent, mfe_percent, mae_percent, m.atr_percent)
        market_mode = str((candidate.market_mode or {}).get("mode", "NEUTRAL")).upper()

        return MovementMemoryRecord(
            record_id=f"movmem_{uuid4().hex}",
            symbol=signature.coin,
            direction=direction,
            movement_type=signature.movement_type,
            timeframe=signature.timeframe,
            timestamp=int(candidate.timestamp or now_ts()),
            signature_key=signature.key(),
            soft_signature_key=signature.soft_key(),
            entry_price=entry_price,
            exit_price=exit_price,
            move_percent=safe_float(move_percent),
            mfe_percent=safe_float(mfe_percent if mfe_percent else max(0.0, move_percent)),
            mae_percent=safe_float(mae_percent),
            duration_seconds=safe_int(duration_seconds),
            outcome=str(outcome or "UNKNOWN").upper(),
            quality=quality,

            rsi=safe_float(m.rsi),
            rsi_slope=safe_float(m.rsi_slope),
            rsi_acceleration=safe_float(m.rsi_acceleration),
            macd=safe_float(m.macd),
            macd_signal=safe_float(m.macd_signal),
            macd_histogram=safe_float(m.macd_histogram),
            histogram_slope=safe_float(m.histogram_slope),
            histogram_acceleration=safe_float(m.histogram_acceleration),
            adx=safe_float(m.adx),
            adx_slope=safe_float(m.adx_slope),
            plus_di=safe_float(m.plus_di),
            minus_di=safe_float(m.minus_di),
            buy_power=safe_float(m.buy_power),
            sell_power=safe_float(m.sell_power),
            power_delta=safe_float(m.power_delta),
            relative_volume=safe_float(m.relative_volume),
            volume_expansion=bool(m.volume_expansion),
            volume_spike=bool(m.volume_spike),
            atr_percent=safe_float(m.atr_percent),
            atr_slope=safe_float(m.atr_slope),
            atr_expansion=str(m.atr_expansion),
            atr_explosion=bool(m.atr_explosion),
            ema_state=str(m.ema_state),
            vwap_state=str(m.vwap_state),
            vwap_distance_percent=safe_float(m.vwap_distance_percent),
            range_probability=safe_float(m.range_probability),
            compression_score=safe_float(m.compression_score),
            price_change_percent=safe_float(m.price_change_percent),
            market_mode=market_mode,
            meta=dict(meta or {}),
        )


def _coerce_record(item: Any) -> MovementMemoryRecord:
    if isinstance(item, MovementMemoryRecord):
        return item
    if hasattr(item, "to_dict") and callable(item.to_dict):
        item = item.to_dict()
    if not isinstance(item, dict):
        item = {}

    return MovementMemoryRecord(
        record_id=str(item.get("record_id", item.get("movement_id", item.get("id", f"movmem_{uuid4().hex}")))),
        symbol=normalize_symbol(str(item.get("symbol", item.get("coin", "")))),
        direction=normalize_direction(str(item.get("direction", ""))),
        movement_type=str(item.get("movement_type", MOVE_NONE)).upper(),
        timeframe=str(item.get("timeframe", "5m")),
        timestamp=safe_int(item.get("timestamp", now_ts())),
        signature_key=str(item.get("signature_key", "")),
        soft_signature_key=str(item.get("soft_signature_key", "")),
        entry_price=safe_float(item.get("entry_price", item.get("before_price", 0.0))),
        exit_price=safe_float(item.get("exit_price", item.get("after_price", 0.0))),
        move_percent=safe_float(item.get("move_percent", 0.0)),
        mfe_percent=safe_float(item.get("mfe_percent", 0.0)),
        mae_percent=safe_float(item.get("mae_percent", 0.0)),
        duration_seconds=safe_int(item.get("duration_seconds", item.get("move_duration_seconds", 0))),
        outcome=str(item.get("outcome", "UNKNOWN")).upper(),
        quality=str(item.get("quality", QUALITY_WEAK)).upper(),
        rsi=safe_float(item.get("rsi", 50.0)),
        rsi_slope=safe_float(item.get("rsi_slope", 0.0)),
        rsi_acceleration=safe_float(item.get("rsi_acceleration", 0.0)),
        macd=safe_float(item.get("macd", 0.0)),
        macd_signal=safe_float(item.get("macd_signal", 0.0)),
        macd_histogram=safe_float(item.get("macd_histogram", 0.0)),
        histogram_slope=safe_float(item.get("histogram_slope", 0.0)),
        histogram_acceleration=safe_float(item.get("histogram_acceleration", 0.0)),
        adx=safe_float(item.get("adx", 0.0)),
        adx_slope=safe_float(item.get("adx_slope", 0.0)),
        plus_di=safe_float(item.get("plus_di", 0.0)),
        minus_di=safe_float(item.get("minus_di", 0.0)),
        buy_power=safe_float(item.get("buy_power", 0.0)),
        sell_power=safe_float(item.get("sell_power", 0.0)),
        power_delta=safe_float(item.get("power_delta", 0.0)),
        relative_volume=safe_float(item.get("relative_volume", 0.0)),
        volume_expansion=bool(item.get("volume_expansion", False)),
        volume_spike=bool(item.get("volume_spike", False)),
        atr_percent=safe_float(item.get("atr_percent", 0.0)),
        atr_slope=safe_float(item.get("atr_slope", 0.0)),
        atr_expansion=str(item.get("atr_expansion", "")),
        atr_explosion=bool(item.get("atr_explosion", False)),
        ema_state=str(item.get("ema_state", "")),
        vwap_state=str(item.get("vwap_state", "")),
        vwap_distance_percent=safe_float(item.get("vwap_distance_percent", 0.0)),
        range_probability=safe_float(item.get("range_probability", 0.0)),
        compression_score=safe_float(item.get("compression_score", 0.0)),
        price_change_percent=safe_float(item.get("price_change_percent", 0.0)),
        market_mode=str(item.get("market_mode", "NEUTRAL")).upper(),
        meta=dict(item.get("meta", {}) if isinstance(item.get("meta", {}), dict) else {}),
    )


def _bucket_similarity(a: str, b: str) -> float:
    a = str(a or "UNKNOWN")
    b = str(b or "UNKNOWN")
    if a == b:
        return 1.0
    if "UNKNOWN" in {a, b}:
        return 0.35

    a_parts = a.split("_")
    b_parts = b.split("_")
    if a_parts[:1] == b_parts[:1]:
        if any(x in a for x in ("UP", "BUY")) and any(x in b for x in ("UP", "BUY")):
            return 0.7
        if any(x in a for x in ("DOWN", "SELL")) and any(x in b for x in ("DOWN", "SELL")):
            return 0.7
        return 0.5
    return 0.0


def record_signature(record: MovementMemoryRecord) -> PatternSignature:
    return PatternSignature(
        coin=normalize_symbol(record.symbol),
        direction=normalize_direction(record.direction),
        movement_type=str(record.movement_type or MOVE_NONE).upper(),
        timeframe=str(record.timeframe or "5m"),
        rsi_bucket=bucket_rsi(record.rsi),
        rsi_slope_bucket=bucket_signed(record.rsi_slope, "RSI_SLOPE"),
        hist_bucket=bucket_signed(record.macd_histogram, "HIST"),
        hist_slope_bucket=bucket_signed(record.histogram_slope, "HIST_SLOPE"),
        adx_bucket=bucket_adx(record.adx),
        power_bucket=bucket_power(record.power_delta),
        volume_bucket=bucket_volume(record.relative_volume),
        atr_bucket=bucket_atr(record.atr_percent),
        vwap_state=str(record.vwap_state or "UNKNOWN").upper(),
        ema_state=str(record.ema_state or "UNKNOWN").upper(),
        compression_bucket=bucket_range(record.compression_score, 20.0, "COMP"),
    )


def signature_similarity(target: PatternSignature, record: MovementMemoryRecord) -> float:
    sig = record_signature(record)
    score = 0.0
    total = 0.0

    def add(weight: float, sim: float) -> None:
        nonlocal score, total
        total += weight
        score += weight * max(0.0, min(1.0, sim))

    add(25.0, 1.0 if target.coin == sig.coin else 0.0)
    add(22.0, 1.0 if target.direction == sig.direction else 0.0)
    add(8.0, 1.0 if target.timeframe == sig.timeframe else 0.4)
    add(7.0, _bucket_similarity(target.rsi_bucket, sig.rsi_bucket))
    add(8.0, _bucket_similarity(target.rsi_slope_bucket, sig.rsi_slope_bucket))
    add(7.0, _bucket_similarity(target.hist_bucket, sig.hist_bucket))
    add(10.0, _bucket_similarity(target.hist_slope_bucket, sig.hist_slope_bucket))
    add(6.0, _bucket_similarity(target.adx_bucket, sig.adx_bucket))
    add(10.0, _bucket_similarity(target.power_bucket, sig.power_bucket))
    add(6.0, _bucket_similarity(target.volume_bucket, sig.volume_bucket))
    add(5.0, _bucket_similarity(target.atr_bucket, sig.atr_bucket))
    add(3.0, 1.0 if target.vwap_state == sig.vwap_state else 0.25)
    add(3.0, 1.0 if target.ema_state == sig.ema_state else 0.25)
    add(4.0, _bucket_similarity(target.compression_bucket, sig.compression_bucket))

    if total <= 0:
        return 0.0
    return clamp(score / total * 100.0)


class MovementMemoryIndex:
    def __init__(self, records: Optional[Iterable[Any]] = None):
        self.records: List[MovementMemoryRecord] = []
        for item in records or []:
            try:
                self.records.append(_coerce_record(item))
            except Exception:
                continue

    def add(self, record: MovementMemoryRecord) -> None:
        self.records.append(record)
        max_records = max(100, int(getattr(SETTINGS.learning, "max_records", MAX_MOVEMENT_MEMORY_RECORDS)))
        if len(self.records) > max_records:
            self.records = self.records[-max_records:]

    def matching_records(self, signature: PatternSignature, min_similarity: float = 54.0, limit: int = 120) -> List[MovementMemoryRecord]:
        scored: List[Tuple[float, int, MovementMemoryRecord]] = []
        for record in self.records:
            if normalize_symbol(record.symbol) != signature.coin:
                continue
            if normalize_direction(record.direction) != signature.direction:
                continue
            sim = signature_similarity(signature, record)
            if sim >= min_similarity:
                scored.append((sim, safe_int(record.timestamp), record))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [r for _, _, r in scored[:max(1, limit)]]

    def summarize(self, signature: PatternSignature) -> MovementMemorySummary:
        records = self.matching_records(signature)
        return summarize_movement_memory(signature, records)


def timing_score(record: MovementMemoryRecord) -> float:
    score = 50.0
    duration = max(0, safe_int(record.duration_seconds))
    favorable = max(safe_float(record.move_percent), safe_float(record.mfe_percent))
    adverse = abs(safe_float(record.mae_percent))

    if duration and duration <= 300:
        score += 15.0
    elif duration <= 900:
        score += 8.0
    elif duration >= 1800:
        score -= 10.0

    if favorable > adverse * 1.25:
        score += 10.0
    elif adverse > favorable * 1.25:
        score -= 10.0

    if abs(safe_float(record.price_change_percent)) < max(1.5, safe_float(record.atr_percent) * 2.0):
        score += 5.0
    else:
        score -= 8.0

    return clamp(score)


def summarize_movement_memory(signature: PatternSignature, records: Sequence[MovementMemoryRecord]) -> MovementMemorySummary:
    total = len(records)
    if total <= 0:
        return MovementMemorySummary(
            signature_key=signature.key(),
            coin=signature.coin,
            movement_type=signature.movement_type,
            direction=signature.direction,
            sample_count=0,
            pattern_count=0,
            pattern_match_score=0.0,
            pattern_confidence=0.0,
            matched_pattern_id="",
            pattern_win_rate=0.0,
            avg_move_percent=0.0,
            expected_move_percent=0.0,
            expected_pullback_percent=0.0,
            avg_duration_seconds=300.0,
            avg_mfe_percent=0.0,
            avg_mae_percent=0.0,
            success_rate=50.0,
            outcome_success_rate=0.0,
            timing_score=0.0,
            early_success_rate=0.0,
            fuzzy_match_score=0.0,
            quality_label="LOW_DATA",
            notes=("NO_PATTERN_HISTORY",),
        )

    successes = [is_success(r) for r in records]
    sims = [signature_similarity(signature, r) for r in records]
    timings = [timing_score(r) for r in records]

    avg_move = sum(safe_float(r.move_percent) for r in records) / total
    avg_mfe = sum(safe_float(r.mfe_percent) for r in records) / total
    avg_mae = sum(safe_float(r.mae_percent) for r in records) / total
    avg_duration = sum(safe_float(r.duration_seconds) for r in records) / total

    win_rate = sum(1 for ok in successes if ok) / total * 100.0
    avg_similarity = sum(sims) / total if sims else 0.0
    avg_timing = sum(timings) / total if timings else 0.0

    early_success = [
        r for r, ok, t in zip(records, successes, timings)
        if ok and t >= 60.0
    ]
    early_success_rate = len(early_success) / total * 100.0

    pattern_confidence = clamp(
        avg_similarity * 0.38
        + win_rate * 0.28
        + avg_timing * 0.22
        + min(100.0, total * 7.0) * 0.12
    )

    pattern_match_score = clamp(
        avg_similarity * 0.55
        + avg_timing * 0.25
        + win_rate * 0.20
    )

    quality = "LOW_DATA"
    notes: List[str] = []
    if total < max(3, int(getattr(SETTINGS.pattern, "min_repeats_for_importance", 3))):
        notes.append("LOW_PATTERN_COUNT")
    elif win_rate >= 62 and early_success_rate >= 35:
        quality = "HIGH"
        notes.append("EARLY_PATTERN_WORKED")
    elif win_rate >= 50:
        quality = "MEDIUM"
        notes.append("PATTERN_WORKED_MODERATELY")
    else:
        quality = "LOW"
        notes.append("PATTERN_WEAK")

    if avg_timing >= 64:
        notes.append("TIMING_GOOD")
    elif avg_timing <= 42:
        notes.append("TIMING_WEAK_OR_LATE")

    if avg_mae > avg_mfe:
        notes.append("PULLBACK_RISK_HIGH")

    best_record = max(records, key=lambda r: signature_similarity(signature, r), default=None)
    matched_pattern_id = best_record.record_id if best_record else ""

    expected_move = max(0.0, avg_mfe * 0.88 if avg_mfe > 0 else avg_move)
    expected_pullback = max(0.0, abs(avg_mae))

    return MovementMemorySummary(
        signature_key=signature.key(),
        coin=signature.coin,
        movement_type=signature.movement_type,
        direction=signature.direction,
        sample_count=total,
        pattern_count=total,
        pattern_match_score=clamp(pattern_match_score),
        pattern_confidence=clamp(pattern_confidence),
        matched_pattern_id=matched_pattern_id,
        pattern_win_rate=clamp(win_rate),
        avg_move_percent=safe_float(avg_move),
        expected_move_percent=safe_float(expected_move),
        expected_pullback_percent=safe_float(expected_pullback),
        avg_duration_seconds=safe_float(avg_duration if avg_duration > 0 else 300.0),
        avg_mfe_percent=safe_float(avg_mfe),
        avg_mae_percent=safe_float(avg_mae),
        success_rate=clamp(pattern_confidence),
        outcome_success_rate=clamp(win_rate),
        timing_score=clamp(avg_timing),
        early_success_rate=clamp(early_success_rate),
        fuzzy_match_score=clamp(avg_similarity),
        quality_label=quality,
        notes=tuple(dict.fromkeys(notes)),
    )


class MovementMemoryEngine:
    def __init__(self, records: Optional[Iterable[Any]] = None):
        if records is None:
            records = load_persisted_movement_records()
        self.index = MovementMemoryIndex(records=records)
        self.record_builder = MovementMemoryRecordBuilder()
        self.signature_builder = PatternSignatureBuilder()

    def record_movement(
        self,
        candidate: AnalysisCandidate,
        exit_price: float,
        duration_seconds: int,
        outcome: str = "UNKNOWN",
        mfe_percent: float = 0.0,
        mae_percent: float = 0.0,
        meta: Optional[JsonDict] = None,
        persist: bool = True,
        **_: Any,
    ) -> MovementMemoryRecord:
        record = self.record_builder.build(
            candidate=candidate,
            exit_price=exit_price,
            duration_seconds=duration_seconds,
            outcome=outcome,
            mfe_percent=mfe_percent,
            mae_percent=mae_percent,
            meta=meta,
        )
        self.index.add(record)

        if persist and append_bounded is not None:
            append_bounded(
                "movement_memory",
                record.record_id,
                record.to_dict(),
                max_items=MAX_MOVEMENT_MEMORY_RECORDS,
                sort_key="timestamp",
            )
        return record

    def summarize_candidate(self, candidate: AnalysisCandidate, **_: Any) -> MovementMemorySummary:
        signature = self.signature_builder.build(candidate)
        return self.index.summarize(signature)


def load_persisted_movement_records() -> List[Any]:
    if store is None:
        return []
    try:
        return list(store().section("movement_memory").values())
    except Exception as exc:
        if save_error is not None:
            try:
                save_error("movement_memory_load", str(exc), {})
            except Exception:
                pass
        return []


_default_engine: Optional[MovementMemoryEngine] = None


def engine(records: Optional[Iterable[Any]] = None) -> MovementMemoryEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = MovementMemoryEngine(records=records)
    elif records is not None:
        existing = list(_default_engine.index.records)
        _default_engine = MovementMemoryEngine(records=[*existing, *list(records)])
    return _default_engine


def record_movement_memory(
    candidate: AnalysisCandidate,
    after_price: Optional[float] = None,
    move_duration_seconds: Optional[int] = None,
    exit_price: Optional[float] = None,
    duration_seconds: Optional[int] = None,
    outcome: str = "UNKNOWN",
    mfe_percent: float = 0.0,
    mae_percent: float = 0.0,
    meta: Optional[JsonDict] = None,
    persist: bool = True,
    **kwargs: Any,
) -> MovementMemoryRecord:
    final_exit_price = safe_float(exit_price if exit_price is not None else after_price)
    final_duration = safe_int(duration_seconds if duration_seconds is not None else move_duration_seconds)
    return engine().record_movement(
        candidate=candidate,
        exit_price=final_exit_price,
        duration_seconds=final_duration,
        outcome=outcome,
        mfe_percent=mfe_percent,
        mae_percent=mae_percent,
        meta=meta,
        persist=persist,
        **kwargs,
    )


def summarize_movement_candidate(candidate: AnalysisCandidate, **kwargs: Any) -> MovementMemorySummary:
    return engine().summarize_candidate(candidate, **kwargs)


def movement_memory_summary_for_predictor(candidate: AnalysisCandidate, **kwargs: Any) -> JsonDict:
    return summarize_movement_candidate(candidate, **kwargs).to_dict()
