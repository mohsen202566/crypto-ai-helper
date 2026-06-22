from __future__ import annotations

"""
13 - coin_learning.py

Coin + direction + condition learning layer for the locked Movement Hunter bot.

Responsibilities:
- Learn from REAL and GHOST outcomes.
- Store and summarize coin + direction + condition behavior.
- Never learn broad labels like "DOGE is bad".
- Learn conditional patterns:
  coin + direction + market_state + indicator buckets + trap/range/volatility context.
- Track TP1, TP2, AI_EXIT, SL, MFE, MAE, holding time, movement size.
- Provide learning summaries to confidence_engine.py and ai_decision_engine.py.

Strictly forbidden:
- No REAL/GHOST/REJECT decision.
- No trade execution.
- No Toobit calls.
- No Telegram.
- No Paper mode.
- No Setup flow.

This file learns and summarizes. It does not decide final signals.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import uuid4
import time
import math

from analysis_engine import AnalysisCandidate
from movement_hunter import MovementHunterResult
from trap_engine import TrapResult
from state_engine import StateResult
from confidence_engine import ConfidenceResult
from data_store import save_learning_record, append_bounded, save_coin_behavior, new_id
from config import SETTINGS


JsonDict = Dict[str, Any]

MAX_LEARNING_RECORDS = 20000

SOURCE_REAL = "REAL"
SOURCE_GHOST = "GHOST"

RESULT_TP1 = "TP1"
RESULT_TP2 = "TP2"
RESULT_AI_EXIT = "AI_EXIT"
RESULT_SL = "SL"
RESULT_OPEN = "OPEN"
RESULT_UNKNOWN = "UNKNOWN"

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"


@dataclass(frozen=True)
class ConditionKey:
    coin: str
    direction: str
    market_state: str
    rsi_bucket: str
    adx_bucket: str
    macd_bucket: str
    atr_bucket: str
    volume_bucket: str
    power_bucket: str
    vwap_state: str
    ema_state: str
    trap_bucket: str
    range_bucket: str
    freshness: str

    def key(self) -> str:
        return "|".join(
            [
                self.coin,
                self.direction,
                self.market_state,
                self.rsi_bucket,
                self.adx_bucket,
                self.macd_bucket,
                self.atr_bucket,
                self.volume_bucket,
                self.power_bucket,
                self.vwap_state,
                self.ema_state,
                self.trap_bucket,
                self.range_bucket,
                self.freshness,
            ]
        )

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class LearningRecord:
    learning_id: str
    source_type: str
    coin: str
    direction: str
    condition_key: str
    timestamp: int

    market_state: str
    movement_phase: str
    freshness: str
    confidence_level: str

    entry_price: float
    exit_price: float
    result: str
    realized_pnl: float
    realized_pnl_percent: float
    mfe_percent: float
    mae_percent: float
    holding_seconds: int

    rsi: float
    rsi_slope: float
    macd: float
    macd_histogram: float
    histogram_slope: float
    histogram_acceleration: float
    adx: float
    atr_percent: float
    relative_volume: float
    buy_power: float
    sell_power: float
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
class LearningSummary:
    condition_key: str
    coin: str
    direction: str
    sample_count: int
    real_samples: int
    ghost_samples: int
    tp1_count: int
    tp2_count: int
    ai_exit_count: int
    sl_count: int
    win_rate: float
    similar_win_rate: float
    avg_mfe_percent: float
    avg_mae_percent: float
    avg_holding_seconds: float
    avg_realized_pnl_percent: float
    risk_label: str
    confidence_hint: str
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class CoinBehaviorRecord:
    behavior_id: str
    coin: str
    direction: str
    condition_key: str
    sample_count: int
    real_samples: int
    ghost_samples: int
    tp1_count: int
    tp2_count: int
    ai_exit_count: int
    sl_count: int
    win_rate: float
    avg_mfe_percent: float
    avg_mae_percent: float
    avg_holding_seconds: float
    last_updated: int
    best_conditions: Tuple[str, ...] = field(default_factory=tuple)
    worst_conditions: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    try:
        if math.isnan(float(value)) or math.isinf(float(value)):
            return low
        return max(low, min(high, float(value)))
    except Exception:
        return low


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


def bucket_signed(value: float, prefix: str, small: float = 0.0, medium: float = 1.0) -> str:
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


def bucket_atr_percent(value: float) -> str:
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


def bucket_relative_volume(value: float) -> str:
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


def bucket_power_delta(value: float) -> str:
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


def result_is_win(result: str) -> bool:
    return str(result).upper() in {RESULT_TP1, RESULT_TP2, RESULT_AI_EXIT}


class ConditionKeyBuilder:
    """Builds coin+direction+condition keys from current candidate and AI context."""

    def build(
        self,
        candidate: AnalysisCandidate,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
    ) -> ConditionKey:
        s = candidate.sensor_snapshot
        coin = normalize_symbol(candidate.symbol)
        direction = normalize_direction(candidate.direction_hint)

        market_state = state.market_state if state else str(getattr(s, "market_state", "UNKNOWN"))
        trap_risk = trap.trap_risk if trap else 0.0
        range_probability = state.range_probability if state else s.range_probability
        freshness = movement.freshness if movement else "UNKNOWN"

        return ConditionKey(
            coin=coin,
            direction=direction,
            market_state=str(market_state),
            rsi_bucket=bucket_rsi(s.rsi),
            adx_bucket=bucket_adx(s.adx),
            macd_bucket=bucket_signed(s.histogram_slope, "HIST", small=0.0, medium=0.0001),
            atr_bucket=bucket_atr_percent(s.atr_percent),
            volume_bucket=bucket_relative_volume(s.relative_volume),
            power_bucket=bucket_power_delta(s.power_delta),
            vwap_state=str(s.vwap_state),
            ema_state=str(s.ema_state),
            trap_bucket=bucket_percent(trap_risk, "TRAP"),
            range_bucket=bucket_percent(range_probability, "RANGE"),
            freshness=str(freshness),
        )


class LearningRecordBuilder:
    """Builds immutable LearningRecord objects from outcome data."""

    def __init__(self):
        self.key_builder = ConditionKeyBuilder()

    def build(
        self,
        source_type: str,
        candidate: AnalysisCandidate,
        result: str,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
        confidence: Optional[ConfidenceResult] = None,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        realized_pnl: float = 0.0,
        realized_pnl_percent: float = 0.0,
        mfe_percent: float = 0.0,
        mae_percent: float = 0.0,
        holding_seconds: int = 0,
        meta: Optional[JsonDict] = None,
    ) -> LearningRecord:
        s = candidate.sensor_snapshot
        key = self.key_builder.build(candidate, movement=movement, trap=trap, state=state)

        return LearningRecord(
            learning_id=f"learn_{uuid4().hex}",
            source_type=str(source_type).upper(),
            coin=key.coin,
            direction=key.direction,
            condition_key=key.key(),
            timestamp=candidate.timestamp or int(time.time()),
            market_state=key.market_state,
            movement_phase=movement.movement_phase if movement else "UNKNOWN",
            freshness=movement.freshness if movement else "UNKNOWN",
            confidence_level=confidence.confidence_level if confidence else "UNKNOWN",
            entry_price=safe_float(entry_price or s.price),
            exit_price=safe_float(exit_price),
            result=str(result or RESULT_UNKNOWN).upper(),
            realized_pnl=safe_float(realized_pnl),
            realized_pnl_percent=safe_float(realized_pnl_percent),
            mfe_percent=safe_float(mfe_percent),
            mae_percent=safe_float(mae_percent),
            holding_seconds=safe_int(holding_seconds),
            rsi=safe_float(s.rsi),
            rsi_slope=safe_float(s.rsi_slope),
            macd=safe_float(s.macd),
            macd_histogram=safe_float(s.macd_histogram),
            histogram_slope=safe_float(s.histogram_slope),
            histogram_acceleration=safe_float(s.histogram_acceleration),
            adx=safe_float(s.adx),
            atr_percent=safe_float(s.atr_percent),
            relative_volume=safe_float(s.relative_volume),
            buy_power=safe_float(s.buy_power),
            sell_power=safe_float(s.sell_power),
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


class CoinLearningMemory:
    """
    In-memory learning index.

    data_store.py is used for persistence. This class provides fast summaries.
    """

    def __init__(self, records: Optional[Iterable[Any]] = None):
        self.records: List[LearningRecord] = []
        for record in records or []:
            try:
                self.records.append(self._coerce_record(record))
            except Exception:
                continue

    def _coerce_record(self, item: Any) -> LearningRecord:
        if isinstance(item, LearningRecord):
            return item
        if hasattr(item, "to_dict") and callable(item.to_dict):
            item = item.to_dict()
        if not isinstance(item, dict):
            item = {}
        return LearningRecord(
            learning_id=str(item.get("learning_id", item.get("id", new_id("learn")))),
            source_type=str(item.get("source_type", SOURCE_GHOST)).upper(),
            coin=normalize_symbol(item.get("coin", item.get("symbol", ""))),
            direction=normalize_direction(item.get("direction", "")),
            condition_key=str(item.get("condition_key", "")),
            timestamp=safe_int(item.get("timestamp", time.time())),
            market_state=str(item.get("market_state", "UNKNOWN")),
            movement_phase=str(item.get("movement_phase", "UNKNOWN")),
            freshness=str(item.get("freshness", "UNKNOWN")),
            confidence_level=str(item.get("confidence_level", "UNKNOWN")),
            entry_price=safe_float(item.get("entry_price")),
            exit_price=safe_float(item.get("exit_price")),
            result=str(item.get("result", RESULT_UNKNOWN)).upper(),
            realized_pnl=safe_float(item.get("realized_pnl")),
            realized_pnl_percent=safe_float(item.get("realized_pnl_percent")),
            mfe_percent=safe_float(item.get("mfe_percent")),
            mae_percent=safe_float(item.get("mae_percent")),
            holding_seconds=safe_int(item.get("holding_seconds")),
            rsi=safe_float(item.get("rsi"), 50.0),
            rsi_slope=safe_float(item.get("rsi_slope")),
            macd=safe_float(item.get("macd")),
            macd_histogram=safe_float(item.get("macd_histogram")),
            histogram_slope=safe_float(item.get("histogram_slope")),
            histogram_acceleration=safe_float(item.get("histogram_acceleration")),
            adx=safe_float(item.get("adx")),
            atr_percent=safe_float(item.get("atr_percent")),
            relative_volume=safe_float(item.get("relative_volume")),
            buy_power=safe_float(item.get("buy_power")),
            sell_power=safe_float(item.get("sell_power")),
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

    def add(self, record: LearningRecord) -> None:
        self.records.append(record)
        max_records = max(100, int(getattr(SETTINGS.learning, "max_records", 20000)))
        if len(self.records) > max_records:
            self.records = self.records[-max_records:]

    def similar_records(self, condition_key: str, coin: Optional[str] = None, direction: Optional[str] = None) -> List[LearningRecord]:
        result = [r for r in self.records if r.condition_key == condition_key]
        if not result and coin and direction:
            c = normalize_symbol(coin)
            d = normalize_direction(direction)
            result = [r for r in self.records if r.coin == c and r.direction == d]
        return result

    def summarize(self, condition_key: str, coin: str, direction: str) -> LearningSummary:
        records = self.similar_records(condition_key, coin=coin, direction=direction)
        if not records:
            return LearningSummary(
                condition_key=condition_key,
                coin=normalize_symbol(coin),
                direction=normalize_direction(direction),
                sample_count=0,
                real_samples=0,
                ghost_samples=0,
                tp1_count=0,
                tp2_count=0,
                ai_exit_count=0,
                sl_count=0,
                win_rate=50.0,
                similar_win_rate=50.0,
                avg_mfe_percent=0.0,
                avg_mae_percent=0.0,
                avg_holding_seconds=0.0,
                avg_realized_pnl_percent=0.0,
                risk_label="UNKNOWN",
                confidence_hint="LOW_DATA",
                notes=("NO_HISTORY",),
            )

        return summarize_records(condition_key, normalize_symbol(coin), normalize_direction(direction), records)

    def summarize_for_candidate(
        self,
        candidate: AnalysisCandidate,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
    ) -> LearningSummary:
        key = ConditionKeyBuilder().build(candidate, movement=movement, trap=trap, state=state)
        return self.summarize(key.key(), key.coin, key.direction)


def summarize_records(condition_key: str, coin: str, direction: str, records: Sequence[LearningRecord]) -> LearningSummary:
    total = len(records)
    real = sum(1 for r in records if r.source_type == SOURCE_REAL)
    ghost = sum(1 for r in records if r.source_type == SOURCE_GHOST)
    tp1 = sum(1 for r in records if r.result == RESULT_TP1)
    tp2 = sum(1 for r in records if r.result == RESULT_TP2)
    ai_exit = sum(1 for r in records if r.result == RESULT_AI_EXIT)
    sl = sum(1 for r in records if r.result == RESULT_SL)

    wins = tp1 + tp2 + ai_exit
    closed = wins + sl
    wr = (wins / closed * 100.0) if closed else 50.0

    avg_mfe = sum(r.mfe_percent for r in records) / total if total else 0.0
    avg_mae = sum(r.mae_percent for r in records) / total if total else 0.0
    avg_hold = sum(r.holding_seconds for r in records) / total if total else 0.0
    avg_pnl = sum(r.realized_pnl_percent for r in records) / total if total else 0.0

    notes: List[str] = []
    if total < int(getattr(SETTINGS.learning, "min_samples_for_confidence", 10)):
        confidence_hint = "LOW_DATA"
        notes.append("LOW_SAMPLE_COUNT")
    elif wr >= 65:
        confidence_hint = "GOOD_HISTORY"
        notes.append("CONDITION_PERFORMED_WELL")
    elif wr <= 40 and closed >= 5:
        confidence_hint = "WEAK_HISTORY"
        notes.append("CONDITION_PERFORMED_WEAK")
    else:
        confidence_hint = "MIXED_HISTORY"

    if sl >= 3 and wr <= 45:
        risk_label = "RISKY_CONDITION"
        notes.append("REPEATED_SL_PATTERN")
    elif wr >= 65 and avg_mfe > abs(avg_mae):
        risk_label = "FAVORABLE_CONDITION"
    else:
        risk_label = "NEUTRAL_CONDITION"

    return LearningSummary(
        condition_key=condition_key,
        coin=coin,
        direction=direction,
        sample_count=total,
        real_samples=real,
        ghost_samples=ghost,
        tp1_count=tp1,
        tp2_count=tp2,
        ai_exit_count=ai_exit,
        sl_count=sl,
        win_rate=clamp(wr),
        similar_win_rate=clamp(wr),
        avg_mfe_percent=avg_mfe,
        avg_mae_percent=avg_mae,
        avg_holding_seconds=avg_hold,
        avg_realized_pnl_percent=avg_pnl,
        risk_label=risk_label,
        confidence_hint=confidence_hint,
        notes=tuple(notes),
    )


def build_coin_behavior(summary: LearningSummary) -> CoinBehaviorRecord:
    best: List[str] = []
    worst: List[str] = []

    if summary.win_rate >= 65:
        best.append(summary.condition_key)
    if summary.win_rate <= 40 and summary.sample_count >= 5:
        worst.append(summary.condition_key)
    if summary.sl_count >= 3:
        worst.append("REPEATED_SL_PATTERN")

    return CoinBehaviorRecord(
        behavior_id=f"beh_{uuid4().hex}",
        coin=summary.coin,
        direction=summary.direction,
        condition_key=summary.condition_key,
        sample_count=summary.sample_count,
        real_samples=summary.real_samples,
        ghost_samples=summary.ghost_samples,
        tp1_count=summary.tp1_count,
        tp2_count=summary.tp2_count,
        ai_exit_count=summary.ai_exit_count,
        sl_count=summary.sl_count,
        win_rate=summary.win_rate,
        avg_mfe_percent=summary.avg_mfe_percent,
        avg_mae_percent=summary.avg_mae_percent,
        avg_holding_seconds=summary.avg_holding_seconds,
        last_updated=int(time.time()),
        best_conditions=tuple(best),
        worst_conditions=tuple(worst),
    )


class CoinLearningEngine:
    """
    Main learning engine.

    It builds learning records and summaries. It does not decide trades.
    """

    def __init__(self, records: Optional[Iterable[Any]] = None):
        self.memory = CoinLearningMemory(records=records)
        self.record_builder = LearningRecordBuilder()

    def learn_outcome(
        self,
        source_type: str,
        candidate: AnalysisCandidate,
        result: str,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
        confidence: Optional[ConfidenceResult] = None,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        realized_pnl: float = 0.0,
        realized_pnl_percent: float = 0.0,
        mfe_percent: float = 0.0,
        mae_percent: float = 0.0,
        holding_seconds: int = 0,
        meta: Optional[JsonDict] = None,
        persist: bool = True,
    ) -> LearningRecord:
        record = self.record_builder.build(
            source_type=source_type,
            candidate=candidate,
            result=result,
            movement=movement,
            trap=trap,
            state=state,
            confidence=confidence,
            entry_price=entry_price,
            exit_price=exit_price,
            realized_pnl=realized_pnl,
            realized_pnl_percent=realized_pnl_percent,
            mfe_percent=mfe_percent,
            mae_percent=mae_percent,
            holding_seconds=holding_seconds,
            meta=meta,
        )
        self.memory.add(record)

        if persist:
            append_bounded('learning', record.learning_id, record.to_dict(), max_items=MAX_LEARNING_RECORDS, sort_key='created_at')
            summary = self.memory.summarize(record.condition_key, record.coin, record.direction)
            behavior = build_coin_behavior(summary)
            save_coin_behavior(f"{record.coin}|{record.direction}|{record.condition_key}", behavior.to_dict())

        return record

    def summarize_candidate(
        self,
        candidate: AnalysisCandidate,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
    ) -> LearningSummary:
        return self.memory.summarize_for_candidate(candidate, movement=movement, trap=trap, state=state)


_default_engine: Optional[CoinLearningEngine] = None


def engine(records: Optional[Iterable[Any]] = None) -> CoinLearningEngine:
    global _default_engine
    if _default_engine is None or records is not None:
        _default_engine = CoinLearningEngine(records=records)
    return _default_engine


def learn_outcome(
    source_type: str,
    candidate: AnalysisCandidate,
    result: str,
    movement: Optional[MovementHunterResult] = None,
    trap: Optional[TrapResult] = None,
    state: Optional[StateResult] = None,
    confidence: Optional[ConfidenceResult] = None,
    entry_price: float = 0.0,
    exit_price: float = 0.0,
    realized_pnl: float = 0.0,
    realized_pnl_percent: float = 0.0,
    mfe_percent: float = 0.0,
    mae_percent: float = 0.0,
    holding_seconds: int = 0,
    meta: Optional[JsonDict] = None,
    persist: bool = True,
) -> LearningRecord:
    return engine().learn_outcome(
        source_type=source_type,
        candidate=candidate,
        result=result,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        entry_price=entry_price,
        exit_price=exit_price,
        realized_pnl=realized_pnl,
        realized_pnl_percent=realized_pnl_percent,
        mfe_percent=mfe_percent,
        mae_percent=mae_percent,
        holding_seconds=holding_seconds,
        meta=meta,
        persist=persist,
    )


def summarize_candidate_learning(
    candidate: AnalysisCandidate,
    movement: Optional[MovementHunterResult] = None,
    trap: Optional[TrapResult] = None,
    state: Optional[StateResult] = None,
) -> LearningSummary:
    return engine().summarize_candidate(candidate, movement=movement, trap=trap, state=state)


def coin_learning_summary_for_confidence(
    candidate: AnalysisCandidate,
    movement: Optional[MovementHunterResult] = None,
    trap: Optional[TrapResult] = None,
    state: Optional[StateResult] = None,
) -> JsonDict:
    return summarize_candidate_learning(candidate, movement=movement, trap=trap, state=state).to_dict()
