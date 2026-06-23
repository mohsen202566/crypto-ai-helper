from __future__ import annotations

"""
13 - coin_learning.py

Simplified coin learning layer for the Level 1 / 5M crypto futures bot.

Locked goals:
- Learn from REAL and GHOST outcomes.
- Learn separately per coin + direction + 5m condition.
- Store raw sensor values, MFE/MAE, realized PnL, holding time and movement quality.
- Provide learning summaries to Pattern Start Predictor and AI Decision Engine.
- WinRate is TP1 vs SL only. TP2 and AI_EXIT are separate stats.
- No final REAL / GHOST / REJECT decision.
- No trap/confidence/correlation/meta/state engine.
- No Toobit, no Telegram, no paper/setup flow.

This file learns and summarizes. It never opens trades and never sends messages.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import uuid4
import math
import time

from analysis_engine import AnalysisCandidate
from config import SETTINGS, normalize_symbol

try:
    from data_store import append_bounded, store, save_error, save_coin_behavior
except Exception:  # keeps file import-safe during staged rewrites
    append_bounded = None
    store = None
    save_error = None
    save_coin_behavior = None


JsonDict = Dict[str, Any]

MAX_LEARNING_RECORDS = 20000
STRATEGY_LEVEL_LEVEL1 = "LEVEL_1"
DEFAULT_STRATEGY_LEVEL = STRATEGY_LEVEL_LEVEL1

SOURCE_REAL = "REAL"
SOURCE_GHOST = "GHOST"

RESULT_TP1 = "TP1"
RESULT_TP2 = "TP2"
RESULT_AI_EXIT = "AI_EXIT"
RESULT_AI_EXIT_PROFIT = "AI_EXIT_PROFIT"
RESULT_SL = "SL"
RESULT_OPEN = "OPEN"
RESULT_UNKNOWN = "UNKNOWN"

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"

QUALITY_BAD = "BAD"
QUALITY_WEAK = "WEAK"
QUALITY_GOOD = "GOOD"
QUALITY_EXCELLENT = "EXCELLENT"


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


def pct_move(direction: str, entry_price: float, exit_price: float) -> float:
    entry = safe_float(entry_price)
    exit_ = safe_float(exit_price)
    if entry <= 0 or exit_ <= 0:
        return 0.0
    if normalize_direction(direction) == DIRECTION_SHORT:
        return (entry - exit_) / entry * 100.0
    return (exit_ - entry) / entry * 100.0


def bucket_range(value: float, step: float, prefix: str) -> str:
    v = safe_float(value)
    if step <= 0:
        step = 1.0
    low = math.floor(v / step) * step
    high = low + step
    return f"{prefix}_{round(low, 6)}_{round(high, 6)}"


def bucket_rsi(value: float) -> str:
    # 3-point buckets preserve examples like RSI 65-68 for DOGE LONG.
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


def result_is_winrate_result(result: str) -> bool:
    return str(result or "").upper() in {RESULT_TP1, RESULT_SL}


def result_is_win(result: str) -> bool:
    return str(result or "").upper() == RESULT_TP1


def result_is_positive(result: str, pnl_percent: float = 0.0, mfe_percent: float = 0.0, mae_percent: float = 0.0) -> bool:
    r = str(result or "").upper()
    if r in {RESULT_TP1, RESULT_TP2, RESULT_AI_EXIT_PROFIT}:
        return True
    if r == RESULT_AI_EXIT:
        return safe_float(pnl_percent) > 0 or safe_float(mfe_percent) > abs(safe_float(mae_percent))
    return False


@dataclass(frozen=True)
class ConditionKey:
    strategy_level: str
    coin: str
    direction: str
    timeframe: str
    market_mode: str
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
            self.strategy_level,
            self.coin,
            self.direction,
            self.timeframe,
            self.market_mode,
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
            self.strategy_level,
            self.coin,
            self.direction,
            self.timeframe,
            self.rsi_bucket,
            self.hist_slope_bucket,
            self.power_bucket,
            self.volume_bucket,
        ])

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class LearningRecord:
    learning_id: str
    strategy_level: str
    source_type: str
    coin: str
    direction: str
    timeframe: str
    condition_key: str
    soft_condition_key: str
    timestamp: int

    entry_price: float
    exit_price: float
    result: str
    realized_pnl: float
    realized_pnl_percent: float
    mfe_percent: float
    mae_percent: float
    holding_seconds: int
    move_percent: float
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
    pattern_match_score: float = 0.0
    pattern_id: str = ""
    meta: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class LearningSummary:
    strategy_level: str
    condition_key: str
    coin: str
    direction: str
    timeframe: str

    sample_count: int
    real_samples: int
    ghost_samples: int

    tp1_count: int
    tp2_count: int
    ai_exit_count: int
    sl_count: int
    win_rate: float
    similar_win_rate: float

    avg_move_percent: float
    expected_move_percent: float
    expected_pullback_percent: float
    best_entry_zone: str
    avg_mfe_percent: float
    avg_mae_percent: float
    avg_holding_seconds: float
    avg_realized_pnl_percent: float

    outcome_success_rate: float
    timing_score: float
    early_success_rate: float
    fuzzy_match_score: float
    pattern_match_score: float
    pattern_confidence: float
    pattern_count: int
    pattern_win_rate: float
    matched_pattern_id: str
    pattern_features: JsonDict = field(default_factory=dict)

    risk_label: str = "UNKNOWN"
    confidence_hint: str = "LOW_DATA"
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class CoinBehaviorRecord:
    behavior_id: str
    strategy_level: str
    coin: str
    direction: str
    timeframe: str
    condition_key: str
    sample_count: int
    real_samples: int
    ghost_samples: int
    tp1_count: int
    tp2_count: int
    ai_exit_count: int
    sl_count: int
    win_rate: float
    pattern_count: int
    pattern_win_rate: float
    avg_mfe_percent: float
    avg_mae_percent: float
    last_updated: int
    best_conditions: Tuple[str, ...] = field(default_factory=tuple)
    weak_conditions: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)


class ConditionKeyBuilder:
    def build(self, candidate: AnalysisCandidate) -> ConditionKey:
        m = candidate.momentum_state
        coin = normalize_symbol(candidate.symbol)
        direction = normalize_direction(candidate.direction_hint)
        market_mode = str((candidate.market_mode or {}).get("mode", "NEUTRAL")).upper()

        return ConditionKey(
            strategy_level=DEFAULT_STRATEGY_LEVEL,
            coin=coin,
            direction=direction,
            timeframe=str(candidate.timeframe or "5m"),
            market_mode=market_mode,
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


def classify_quality(result: str, move_percent: float, mfe_percent: float, mae_percent: float, atr_percent: float, pnl_percent: float = 0.0) -> str:
    r = str(result or "").upper()
    favorable = max(safe_float(move_percent), safe_float(mfe_percent))
    adverse = abs(safe_float(mae_percent))
    atr = max(0.05, safe_float(atr_percent))

    if r == RESULT_SL:
        return QUALITY_BAD
    if r in {RESULT_TP2} or (favorable >= atr * 1.55 and favorable > adverse * 1.5):
        return QUALITY_EXCELLENT
    if r in {RESULT_TP1, RESULT_AI_EXIT_PROFIT} or (favorable >= atr * 0.90 and favorable >= adverse):
        return QUALITY_GOOD
    if r == RESULT_AI_EXIT and (safe_float(pnl_percent) > 0 or favorable >= atr * 0.45):
        return QUALITY_WEAK
    if favorable >= atr * 0.45:
        return QUALITY_WEAK
    return QUALITY_BAD


class LearningRecordBuilder:
    def __init__(self):
        self.key_builder = ConditionKeyBuilder()

    def build(
        self,
        source_type: str,
        candidate: AnalysisCandidate,
        result: str,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        realized_pnl: float = 0.0,
        realized_pnl_percent: float = 0.0,
        mfe_percent: float = 0.0,
        mae_percent: float = 0.0,
        holding_seconds: int = 0,
        pattern_summary: Optional[Any] = None,
        meta: Optional[JsonDict] = None,
        **_: Any,
    ) -> LearningRecord:
        m = candidate.momentum_state
        key = self.key_builder.build(candidate)
        entry = safe_float(entry_price or getattr(candidate.sensor_snapshot, "price", 0.0))
        exit_ = safe_float(exit_price)
        move = pct_move(key.direction, entry, exit_) if exit_ > 0 else 0.0
        result_value = str(result or RESULT_UNKNOWN).upper()

        # Pattern summary is not always passed directly by ghost/position monitors.
        # Recover it from metadata so Pattern Start learning does not stay at zero.
        if pattern_summary is None and isinstance(meta, dict):
            pattern_summary = (
                meta.get("prediction")
                or meta.get("movement_prediction")
                or meta.get("pattern_summary")
                or meta.get("decision")
                or {}
            )

        pattern_score = safe_float(obj_value(pattern_summary, "pattern_match_score", 0.0), 0.0)
        pattern_id = str(obj_value(pattern_summary, "matched_pattern_id", "") or "")

        quality = classify_quality(
            result=result_value,
            move_percent=move,
            mfe_percent=mfe_percent,
            mae_percent=mae_percent,
            atr_percent=m.atr_percent,
            pnl_percent=realized_pnl_percent,
        )

        return LearningRecord(
            learning_id=f"learn_{uuid4().hex}",
            strategy_level=DEFAULT_STRATEGY_LEVEL,
            source_type=str(source_type or SOURCE_GHOST).upper(),
            coin=key.coin,
            direction=key.direction,
            timeframe=key.timeframe,
            condition_key=key.key(),
            soft_condition_key=key.soft_key(),
            timestamp=int(candidate.timestamp or now_ts()),
            entry_price=entry,
            exit_price=exit_,
            result=result_value,
            realized_pnl=safe_float(realized_pnl),
            realized_pnl_percent=safe_float(realized_pnl_percent),
            mfe_percent=safe_float(mfe_percent),
            mae_percent=safe_float(mae_percent),
            holding_seconds=safe_int(holding_seconds),
            move_percent=safe_float(move),
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
            market_mode=key.market_mode,
            pattern_match_score=pattern_score,
            pattern_id=pattern_id,
            meta=dict(meta or {}),
        )


def obj_value(obj: Optional[Any], key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def coerce_record(item: Any) -> LearningRecord:
    if isinstance(item, LearningRecord):
        return item
    if hasattr(item, "to_dict") and callable(item.to_dict):
        item = item.to_dict()
    if not isinstance(item, dict):
        item = {}

    return LearningRecord(
        learning_id=str(item.get("learning_id", item.get("id", f"learn_{uuid4().hex}"))),
        strategy_level=str(item.get("strategy_level", DEFAULT_STRATEGY_LEVEL)).upper(),
        source_type=str(item.get("source_type", SOURCE_GHOST)).upper(),
        coin=normalize_symbol(str(item.get("coin", item.get("symbol", "")))),
        direction=normalize_direction(str(item.get("direction", ""))),
        timeframe=str(item.get("timeframe", "5m")),
        condition_key=str(item.get("condition_key", "")),
        soft_condition_key=str(item.get("soft_condition_key", "")),
        timestamp=safe_int(item.get("timestamp", now_ts())),
        entry_price=safe_float(item.get("entry_price")),
        exit_price=safe_float(item.get("exit_price")),
        result=str(item.get("result", RESULT_UNKNOWN)).upper(),
        realized_pnl=safe_float(item.get("realized_pnl")),
        realized_pnl_percent=safe_float(item.get("realized_pnl_percent")),
        mfe_percent=safe_float(item.get("mfe_percent")),
        mae_percent=safe_float(item.get("mae_percent")),
        holding_seconds=safe_int(item.get("holding_seconds")),
        move_percent=safe_float(item.get("move_percent")),
        quality=str(item.get("quality", QUALITY_WEAK)).upper(),
        rsi=safe_float(item.get("rsi"), 50.0),
        rsi_slope=safe_float(item.get("rsi_slope")),
        rsi_acceleration=safe_float(item.get("rsi_acceleration")),
        macd=safe_float(item.get("macd")),
        macd_signal=safe_float(item.get("macd_signal")),
        macd_histogram=safe_float(item.get("macd_histogram")),
        histogram_slope=safe_float(item.get("histogram_slope")),
        histogram_acceleration=safe_float(item.get("histogram_acceleration")),
        adx=safe_float(item.get("adx")),
        adx_slope=safe_float(item.get("adx_slope")),
        plus_di=safe_float(item.get("plus_di")),
        minus_di=safe_float(item.get("minus_di")),
        buy_power=safe_float(item.get("buy_power")),
        sell_power=safe_float(item.get("sell_power")),
        power_delta=safe_float(item.get("power_delta")),
        relative_volume=safe_float(item.get("relative_volume")),
        volume_expansion=bool(item.get("volume_expansion", False)),
        volume_spike=bool(item.get("volume_spike", False)),
        atr_percent=safe_float(item.get("atr_percent")),
        atr_slope=safe_float(item.get("atr_slope")),
        atr_expansion=str(item.get("atr_expansion", "")),
        atr_explosion=bool(item.get("atr_explosion", False)),
        ema_state=str(item.get("ema_state", "")),
        vwap_state=str(item.get("vwap_state", "")),
        vwap_distance_percent=safe_float(item.get("vwap_distance_percent")),
        range_probability=safe_float(item.get("range_probability")),
        compression_score=safe_float(item.get("compression_score")),
        price_change_percent=safe_float(item.get("price_change_percent")),
        market_mode=str(item.get("market_mode", "NEUTRAL")).upper(),
        pattern_match_score=safe_float(item.get("pattern_match_score")),
        pattern_id=str(item.get("pattern_id", "")),
        meta=dict(item.get("meta", {}) if isinstance(item.get("meta", {}), dict) else {}),
    )


def condition_similarity(current_key: str, record_key: str) -> float:
    if not current_key or not record_key:
        return 0.0
    if current_key == record_key:
        return 100.0
    a = str(current_key).split("|")
    b = str(record_key).split("|")
    if not a or not b:
        return 0.0

    matches = sum(1 for i in range(min(len(a), len(b))) if a[i] == b[i])
    base = matches / max(len(a), len(b), 1) * 100.0

    # strategy level, coin and direction are anchors
    if len(a) > 0 and len(b) > 0 and a[0] != b[0]:
        base *= 0.20
    if len(a) > 1 and len(b) > 1 and a[1] != b[1]:
        base *= 0.25
    if len(a) > 2 and len(b) > 2 and a[2] != b[2]:
        base *= 0.35
    return clamp(base)


def record_timing_score(record: LearningRecord) -> float:
    score = 50.0
    duration = safe_int(record.holding_seconds)
    favorable = max(safe_float(record.move_percent), safe_float(record.mfe_percent))
    adverse = abs(safe_float(record.mae_percent))

    if 0 < duration <= 300:
        score += 18
    elif duration <= 900:
        score += 10
    elif duration >= 1800:
        score -= 8

    # Start-move samples should not already be far extended.
    if abs(safe_float(record.price_change_percent)) < max(1.5, safe_float(record.atr_percent) * 2.0):
        score += 6
    else:
        score -= 10

    if favorable > adverse * 1.25:
        score += 10
    elif adverse > favorable * 1.25:
        score -= 10

    if record.quality == QUALITY_EXCELLENT:
        score += 8
    elif record.quality == QUALITY_GOOD:
        score += 5
    elif record.quality == QUALITY_BAD:
        score -= 8

    return clamp(score)


def record_success(record: LearningRecord) -> bool:
    if record.quality in {QUALITY_GOOD, QUALITY_EXCELLENT}:
        return True
    return result_is_positive(record.result, record.realized_pnl_percent, record.mfe_percent, record.mae_percent)


class CoinLearningMemory:
    def __init__(self, records: Optional[Iterable[Any]] = None):
        self.records: List[LearningRecord] = []
        for item in records or []:
            try:
                self.records.append(coerce_record(item))
            except Exception:
                continue

    def add(self, record: LearningRecord) -> None:
        self.records.append(record)
        max_records = max(100, int(getattr(SETTINGS.learning, "max_records", MAX_LEARNING_RECORDS)))
        if len(self.records) > max_records:
            self.records = self.records[-max_records:]

    def matching_records(self, condition_key: str, coin: str, direction: str, min_similarity: float = 52.0, limit: int = 160) -> List[LearningRecord]:
        c = normalize_symbol(coin)
        d = normalize_direction(direction)
        scored: List[Tuple[float, int, LearningRecord]] = []

        for record in self.records:
            if normalize_symbol(record.coin) != c:
                continue
            if normalize_direction(record.direction) != d:
                continue
            sim = condition_similarity(condition_key, record.condition_key)
            if sim >= min_similarity:
                scored.append((sim, safe_int(record.timestamp), record))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [r for _, _, r in scored[:max(1, limit)]]

    def summarize(self, condition_key: str, coin: str, direction: str, timeframe: str = "5m") -> LearningSummary:
        records = self.matching_records(condition_key, coin=coin, direction=direction)
        return summarize_records(condition_key, normalize_symbol(coin), normalize_direction(direction), timeframe, records)

    def summarize_for_candidate(self, candidate: AnalysisCandidate, **_: Any) -> LearningSummary:
        key = ConditionKeyBuilder().build(candidate)
        return self.summarize(key.key(), key.coin, key.direction, key.timeframe)



def build_pattern_features(records: Sequence[LearningRecord]) -> JsonDict:
    if not records:
        return {}
    n = max(1, len(records))
    def avg(name: str) -> float:
        return sum(safe_float(getattr(r, name, 0.0)) for r in records) / n
    return {
        "rsi": round(avg("rsi"), 6),
        "rsi_slope": round(avg("rsi_slope"), 6),
        "rsi_acceleration": round(avg("rsi_acceleration"), 6),
        "macd": round(avg("macd"), 8),
        "macd_histogram": round(avg("macd_histogram"), 8),
        "histogram_slope": round(avg("histogram_slope"), 8),
        "histogram_acceleration": round(avg("histogram_acceleration"), 8),
        "adx": round(avg("adx"), 6),
        "adx_slope": round(avg("adx_slope"), 6),
        "power_delta": round(avg("power_delta"), 6),
        "relative_volume": round(avg("relative_volume"), 6),
        "atr_percent": round(avg("atr_percent"), 6),
        "atr_slope": round(avg("atr_slope"), 6),
        "compression_score": round(avg("compression_score"), 6),
    }


def infer_best_entry_zone(direction: str, avg_pullback: float, expected_move: float) -> str:
    d = normalize_direction(direction)
    pb = max(0.0, safe_float(avg_pullback))
    em = max(0.0, safe_float(expected_move))
    if em <= 0:
        return "UNKNOWN"
    if pb <= em * 0.20:
        return "IMMEDIATE_START_ZONE"
    if pb <= em * 0.45:
        return "SMALL_PULLBACK_ZONE"
    return "WAIT_FOR_RETEST_ZONE"

def summarize_records(
    condition_key: str,
    coin: str,
    direction: str,
    timeframe: str,
    records: Sequence[LearningRecord],
) -> LearningSummary:
    total = len(records)

    if total <= 0:
        return LearningSummary(
            strategy_level=DEFAULT_STRATEGY_LEVEL,
            condition_key=condition_key,
            coin=coin,
            direction=direction,
            timeframe=timeframe,
            sample_count=0,
            real_samples=0,
            ghost_samples=0,
            tp1_count=0,
            tp2_count=0,
            ai_exit_count=0,
            sl_count=0,
            win_rate=50.0,
            similar_win_rate=50.0,
            avg_move_percent=0.0,
            expected_move_percent=0.0,
            expected_pullback_percent=0.0,
            best_entry_zone="UNKNOWN",
            avg_mfe_percent=0.0,
            avg_mae_percent=0.0,
            avg_holding_seconds=0.0,
            avg_realized_pnl_percent=0.0,
            outcome_success_rate=50.0,
            timing_score=50.0,
            early_success_rate=0.0,
            fuzzy_match_score=0.0,
            pattern_match_score=0.0,
            pattern_confidence=0.0,
            pattern_count=0,
            pattern_win_rate=0.0,
            matched_pattern_id="",
            pattern_features={},
            risk_label="UNKNOWN",
            confidence_hint="LOW_DATA",
            notes=("NO_HISTORY",),
        )

    real = sum(1 for r in records if r.source_type == SOURCE_REAL)
    ghost = sum(1 for r in records if r.source_type == SOURCE_GHOST)
    tp1 = sum(1 for r in records if r.result == RESULT_TP1)
    tp2 = sum(1 for r in records if r.result == RESULT_TP2)
    ai_exit = sum(1 for r in records if r.result in {RESULT_AI_EXIT, RESULT_AI_EXIT_PROFIT})
    sl = sum(1 for r in records if r.result == RESULT_SL)

    closed_for_wr = tp1 + sl
    wr = tp1 / closed_for_wr * 100.0 if closed_for_wr else 50.0

    avg_move = sum(safe_float(r.move_percent) for r in records) / total
    avg_mfe = sum(safe_float(r.mfe_percent) for r in records) / total
    avg_mae = sum(safe_float(r.mae_percent) for r in records) / total
    avg_hold = sum(safe_float(r.holding_seconds) for r in records) / total
    avg_pnl = sum(safe_float(r.realized_pnl_percent) for r in records) / total

    successes = [record_success(r) for r in records]
    outcome_success_rate = sum(1 for ok in successes if ok) / total * 100.0

    timing_scores = [record_timing_score(r) for r in records]
    timing = sum(timing_scores) / total if timing_scores else 50.0

    early_successes = [
        r for r, ok, t in zip(records, successes, timing_scores)
        if ok
        and t >= 55.0
        and max(safe_float(r.mfe_percent), safe_float(r.move_percent)) >= max(0.05, safe_float(r.atr_percent) * 0.50)
    ]
    early_success_rate = len(early_successes) / total * 100.0

    similarities = [condition_similarity(condition_key, r.condition_key) for r in records]
    fuzzy_match = sum(similarities) / total if similarities else 0.0

    pattern_records = [r for r in records if safe_float(r.pattern_match_score) > 0 or r.pattern_id]

    # Root fix for "early/start patterns = 0":
    # Older ghost/real outcome paths did not pass pattern_summary, so stored
    # pattern_match_score stayed 0 even when the trade was actually an early
    # successful movement. Treat good fast/clean outcomes as Pattern Start
    # learning samples so predictor/AI can learn before the explicit pattern
    # database is mature.
    if not pattern_records:
        pattern_records = [
            r for r, ok, t in zip(records, successes, timing_scores)
            if ok
            and t >= 55.0
            and max(safe_float(r.mfe_percent), safe_float(r.move_percent)) >= max(0.05, safe_float(r.atr_percent) * 0.55)
            and abs(safe_float(r.mae_percent)) <= max(safe_float(r.mfe_percent) * 1.35, safe_float(r.atr_percent) * 1.15, 0.05)
        ]

    pattern_count = len(pattern_records)
    pattern_successes = [record_success(r) for r in pattern_records]
    pattern_win_rate = sum(1 for ok in pattern_successes if ok) / pattern_count * 100.0 if pattern_count else 0.0

    raw_avg_pattern_score = sum(safe_float(r.pattern_match_score) for r in pattern_records) / pattern_count if pattern_count else 0.0
    inferred_pattern_score = 0.0
    if pattern_count:
        inferred_pattern_score = clamp(
            fuzzy_match * 0.34
            + outcome_success_rate * 0.26
            + timing * 0.24
            + pattern_win_rate * 0.16
        )
    avg_pattern_score = max(raw_avg_pattern_score, inferred_pattern_score)

    pattern_confidence = clamp(
        fuzzy_match * 0.30
        + outcome_success_rate * 0.25
        + timing * 0.25
        + min(100.0, total * 7.0) * 0.10
        + avg_pattern_score * 0.10
    )

    notes: List[str] = []
    min_samples = int(getattr(SETTINGS.pattern, "min_repeats_for_importance", 3))
    if total < max(3, min_samples):
        confidence_hint = "LOW_DATA"
        notes.append("LOW_SAMPLE_COUNT")
    elif outcome_success_rate >= 62 and timing >= 58:
        confidence_hint = "GOOD_HISTORY"
        notes.append("CONDITION_WORKED")
    elif outcome_success_rate <= 40 and total >= 5:
        confidence_hint = "WEAK_HISTORY"
        notes.append("CONDITION_WEAK")
    else:
        confidence_hint = "MIXED_HISTORY"

    if early_success_rate >= 35 and total >= 3:
        notes.append("EARLY_MOVE_PATTERN_WORKED")
    if timing <= 42 and total >= 5:
        notes.append("TIMING_WEAK_OR_LATE")
    if sl >= 3 and wr <= 45:
        notes.append("REPEATED_SL_CONDITION")
    if avg_mae > avg_mfe:
        notes.append("PULLBACK_RISK_HIGH")

    if sl >= 3 and wr <= 45:
        risk_label = "RISKY_CONDITION"
    elif outcome_success_rate >= 62 and avg_mfe > abs(avg_mae) and timing >= 55:
        risk_label = "FAVORABLE_CONDITION"
    else:
        risk_label = "NEUTRAL_CONDITION"

    best_pattern = max(
        pattern_records,
        key=lambda r: max(safe_float(r.pattern_match_score), record_timing_score(r)),
        default=None,
    )
    matched_pattern_id = (best_pattern.pattern_id or best_pattern.learning_id) if best_pattern else ""

    expected_move = max(0.0, avg_mfe * 0.88 if avg_mfe > 0 else avg_move)
    expected_pullback = max(0.0, abs(avg_mae))
    best_entry_zone = infer_best_entry_zone(direction, expected_pullback, expected_move)
    pattern_features = build_pattern_features(pattern_records or records)

    return LearningSummary(
        strategy_level=DEFAULT_STRATEGY_LEVEL,
        condition_key=condition_key,
        coin=coin,
        direction=direction,
        timeframe=timeframe,
        sample_count=total,
        real_samples=real,
        ghost_samples=ghost,
        tp1_count=tp1,
        tp2_count=tp2,
        ai_exit_count=ai_exit,
        sl_count=sl,
        win_rate=clamp(wr),
        similar_win_rate=clamp(wr),
        avg_move_percent=safe_float(avg_move),
        expected_move_percent=safe_float(expected_move),
        expected_pullback_percent=safe_float(expected_pullback),
        best_entry_zone=best_entry_zone,
        avg_mfe_percent=safe_float(avg_mfe),
        avg_mae_percent=safe_float(avg_mae),
        avg_holding_seconds=safe_float(avg_hold),
        avg_realized_pnl_percent=safe_float(avg_pnl),
        outcome_success_rate=clamp(outcome_success_rate),
        timing_score=clamp(timing),
        early_success_rate=clamp(early_success_rate),
        fuzzy_match_score=clamp(fuzzy_match),
        pattern_match_score=clamp(avg_pattern_score),
        pattern_confidence=clamp(pattern_confidence),
        pattern_count=pattern_count,
        pattern_win_rate=clamp(pattern_win_rate),
        matched_pattern_id=matched_pattern_id,
        pattern_features=pattern_features,
        risk_label=risk_label,
        confidence_hint=confidence_hint,
        notes=tuple(dict.fromkeys(notes)),
    )


def build_coin_behavior(summary: LearningSummary) -> CoinBehaviorRecord:
    best: List[str] = []
    weak: List[str] = []

    if summary.outcome_success_rate >= 62 and summary.timing_score >= 58:
        best.append(summary.condition_key)
    if summary.sl_count >= 3 or summary.outcome_success_rate <= 40:
        weak.append(summary.condition_key)

    return CoinBehaviorRecord(
        behavior_id=f"beh_{uuid4().hex}",
        strategy_level=summary.strategy_level,
        coin=summary.coin,
        direction=summary.direction,
        timeframe=summary.timeframe,
        condition_key=summary.condition_key,
        sample_count=summary.sample_count,
        real_samples=summary.real_samples,
        ghost_samples=summary.ghost_samples,
        tp1_count=summary.tp1_count,
        tp2_count=summary.tp2_count,
        ai_exit_count=summary.ai_exit_count,
        sl_count=summary.sl_count,
        win_rate=summary.win_rate,
        pattern_count=summary.pattern_count,
        pattern_win_rate=summary.pattern_win_rate,
        avg_mfe_percent=summary.avg_mfe_percent,
        avg_mae_percent=summary.avg_mae_percent,
        last_updated=now_ts(),
        best_conditions=tuple(best),
        weak_conditions=tuple(weak),
    )


class CoinLearningEngine:
    def __init__(self, records: Optional[Iterable[Any]] = None):
        if records is None:
            records = load_persisted_learning_records()
        self.memory = CoinLearningMemory(records=records)
        self.record_builder = LearningRecordBuilder()

    def learn_outcome(
        self,
        source_type: str,
        candidate: AnalysisCandidate,
        result: str,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        realized_pnl: float = 0.0,
        realized_pnl_percent: float = 0.0,
        mfe_percent: float = 0.0,
        mae_percent: float = 0.0,
        holding_seconds: int = 0,
        pattern_summary: Optional[Any] = None,
        meta: Optional[JsonDict] = None,
        persist: bool = True,
        **kwargs: Any,
    ) -> LearningRecord:
        record = self.record_builder.build(
            source_type=source_type,
            candidate=candidate,
            result=result,
            entry_price=entry_price,
            exit_price=exit_price,
            realized_pnl=realized_pnl,
            realized_pnl_percent=realized_pnl_percent,
            mfe_percent=mfe_percent,
            mae_percent=mae_percent,
            holding_seconds=holding_seconds,
            pattern_summary=pattern_summary,
            meta=meta,
            **kwargs,
        )

        self.memory.add(record)

        if persist and append_bounded is not None:
            append_bounded(
                "learning",
                record.learning_id,
                record.to_dict(),
                max_items=MAX_LEARNING_RECORDS,
                sort_key="timestamp",
            )

            summary = self.memory.summarize(record.condition_key, record.coin, record.direction, record.timeframe)
            behavior = build_coin_behavior(summary)
            if save_coin_behavior is not None:
                save_coin_behavior(f"{record.coin}|{record.direction}|{record.condition_key}", behavior.to_dict())

        return record

    def summarize_candidate(self, candidate: AnalysisCandidate, **_: Any) -> LearningSummary:
        return self.memory.summarize_for_candidate(candidate)


def load_persisted_learning_records() -> List[Any]:
    if store is None:
        return []
    try:
        return list(store().section("learning").values())
    except Exception as exc:
        if save_error is not None:
            try:
                save_error("coin_learning_load", str(exc), {})
            except Exception:
                pass
        return []


_default_engine: Optional[CoinLearningEngine] = None


def engine(records: Optional[Iterable[Any]] = None) -> CoinLearningEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = CoinLearningEngine(records=records)
    elif records is not None:
        existing = list(_default_engine.memory.records)
        _default_engine = CoinLearningEngine(records=[*existing, *list(records)])
    return _default_engine


def learn_outcome(
    source_type: str,
    candidate: AnalysisCandidate,
    result: str,
    entry_price: float = 0.0,
    exit_price: float = 0.0,
    realized_pnl: float = 0.0,
    realized_pnl_percent: float = 0.0,
    mfe_percent: float = 0.0,
    mae_percent: float = 0.0,
    holding_seconds: int = 0,
    pattern_summary: Optional[Any] = None,
    meta: Optional[JsonDict] = None,
    persist: bool = True,
    **kwargs: Any,
) -> LearningRecord:
    return engine().learn_outcome(
        source_type=source_type,
        candidate=candidate,
        result=result,
        entry_price=entry_price,
        exit_price=exit_price,
        realized_pnl=realized_pnl,
        realized_pnl_percent=realized_pnl_percent,
        mfe_percent=mfe_percent,
        mae_percent=mae_percent,
        holding_seconds=holding_seconds,
        pattern_summary=pattern_summary,
        meta=meta,
        persist=persist,
        **kwargs,
    )


def summarize_candidate_learning(candidate: AnalysisCandidate, **kwargs: Any) -> LearningSummary:
    return engine().summarize_candidate(candidate, **kwargs)


def coin_learning_summary_for_ai(candidate: AnalysisCandidate, **kwargs: Any) -> JsonDict:
    return summarize_candidate_learning(candidate, **kwargs).to_dict()


def coin_learning_summary_for_confidence(candidate: AnalysisCandidate, **kwargs: Any) -> JsonDict:
    # compatibility name while old files are being rewritten
    return coin_learning_summary_for_ai(candidate, **kwargs)
