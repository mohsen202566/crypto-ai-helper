"""
learning_memory.py
Level 4 / 1H Smart Scalp Bot

Lightweight learning memory layer.

Architecture lock:
- Owns learning record creation and lightweight stats updates.
- Uses state_store.py for actual JSON IO.
- Does not make final AI decisions, place orders, monitor positions, fetch market data,
  or build Telegram text.
- Allowed project imports:
  constants.py, utils.py, models.py, state_store.py only.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from constants import (
    MODE_GHOST,
    MODE_REAL,
    STATUS_FAILED,
    STATUS_OK,
    SYSTEM_VERSION,
)
from models import LearningRecord, RecordResult, TradeOutcome, TradePosition, from_dict, to_dict
from state_store import append_record, load_json, save_json_atomic, log_error
from utils import (
    make_event_id,
    market_symbol_key,
    normalize_direction,
    normalize_symbol,
    safe_float,
    safe_int,
    safe_str,
    utc_now_iso,
)


LEARNING_MEMORY_VERSION: str = SYSTEM_VERSION
LEARNING_KEY: str = "learning_memory"
GHOST_KEY: str = "ghost_records"
REAL_KEY: str = "real_records"


# =============================================================================
# Defaults / IO
# =============================================================================

def default_learning_payload() -> dict[str, Any]:
    return {
        "system_version": SYSTEM_VERSION,
        "records": [],
        "coin_stats": {},
        "condition_stats": {},
        "updated_at": utc_now_iso(),
    }


def _load_learning_payload() -> dict[str, Any]:
    data = load_json(LEARNING_KEY, default=default_learning_payload())
    if not isinstance(data, dict):
        data = default_learning_payload()
    if not isinstance(data.get("records"), list):
        data["records"] = []
    if not isinstance(data.get("coin_stats"), dict):
        data["coin_stats"] = {}
    if not isinstance(data.get("condition_stats"), dict):
        data["condition_stats"] = {}
    data.setdefault("system_version", SYSTEM_VERSION)
    return data


def _save_learning_payload(payload: Mapping[str, Any]) -> bool:
    data = dict(payload)
    data.setdefault("system_version", SYSTEM_VERSION)
    data["updated_at"] = utc_now_iso()
    return save_json_atomic(LEARNING_KEY, data)


# =============================================================================
# Record builders
# =============================================================================

def outcome_result_label(event: str) -> str:
    """Normalize outcome event to learning result label."""
    ev = safe_str(event).upper()
    if ev in {"TP1", "TP2", "AI_EXIT_PROFIT", "MANUAL_PROFIT"}:
        return "WIN"
    if ev in {"SL", "STOP_LOSS"}:
        return "LOSS"
    if ev in {"AI_EXIT", "MANUAL_CLOSE"}:
        return "EXIT"
    return ev or "UNKNOWN"


def condition_bucket(value: Any, step: float = 5.0, default: str = "UNKNOWN") -> str:
    """Bucket numeric values to reduce overfitting."""
    v = safe_float(value, None)
    if v is None:
        return default
    if step <= 0:
        step = 5.0
    bucket = round(v / step) * step
    return f"{bucket:.0f}"


def build_condition_key(
    *,
    symbol: str,
    direction: str,
    level: int,
    indicators: Optional[Mapping[str, Any]] = None,
    market_context: Optional[Mapping[str, Any]] = None,
) -> str:
    """
    Build condition key.

    Important: learning is conditional, not broad. This avoids globally marking
    DOGE LONG/SHORT as good/bad without context.
    """
    ind = dict(indicators or {})
    ctx = dict(market_context or {})
    parts = [
        market_symbol_key(symbol, direction, level),
        f"rsi:{condition_bucket(ind.get('rsi'))}",
        f"adx:{condition_bucket(ind.get('adx'))}",
        f"atr:{condition_bucket(ind.get('atr_pct'), step=0.25)}",
        f"macd:{condition_bucket(ind.get('macd_hist'), step=0.0005)}",
        f"power:{condition_bucket(ind.get('buy_power'))}-{condition_bucket(ind.get('sell_power'))}",
        f"market:{safe_str(ctx.get('market_mode'), 'UNKNOWN').upper()}",
        f"btc:{safe_str(ctx.get('btc_bias'), 'UNKNOWN').upper()}",
    ]
    return "|".join(parts)


def learning_record_from_outcome(
    outcome: TradeOutcome,
    *,
    indicators: Optional[Mapping[str, Any]] = None,
    structure: Optional[Mapping[str, Any]] = None,
    momentum: Optional[Mapping[str, Any]] = None,
    liquidity: Optional[Mapping[str, Any]] = None,
    market_context: Optional[Mapping[str, Any]] = None,
    tp_sl: Optional[Mapping[str, Any]] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> LearningRecord:
    """Create LearningRecord from closed/partial outcome."""
    return LearningRecord(
        record_id=make_event_id("learning"),
        symbol=outcome.symbol,
        direction=outcome.direction,
        level=outcome.level,
        event=outcome.event,
        result=outcome_result_label(outcome.event),
        indicators=dict(indicators or {}),
        structure=dict(structure or {}),
        momentum=dict(momentum or {}),
        liquidity=dict(liquidity or {}),
        market_context=dict(market_context or {}),
        tp_sl=dict(tp_sl or {}),
        mfe_pct=outcome.mfe_pct,
        mae_pct=outcome.mae_pct,
        pnl_usdt=outcome.pnl_usdt,
        metadata={
            **dict(metadata or {}),
            "mode": outcome.mode,
            "position_id": outcome.position_id,
            "pnl_confirmed": outcome.pnl_confirmed,
            "condition_key": build_condition_key(
                symbol=outcome.symbol,
                direction=outcome.direction,
                level=outcome.level,
                indicators=indicators,
                market_context=market_context,
            ),
        },
    )


def learning_record_from_position(
    position: TradePosition,
    *,
    event: str,
    result: str,
    indicators: Optional[Mapping[str, Any]] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> LearningRecord:
    """Create a lightweight record from position state."""
    return LearningRecord(
        record_id=make_event_id("learning"),
        symbol=position.symbol,
        direction=position.direction,
        level=position.level,
        event=event,
        result=result,
        indicators=dict(indicators or {}),
        tp_sl={
            "entry": position.entry,
            "tp1": position.tp1,
            "tp2": position.tp2,
            "sl": position.sl,
        },
        metadata={
            **dict(metadata or {}),
            "mode": position.mode,
            "position_id": position.position_id,
            "status": position.status,
            "condition_key": build_condition_key(
                symbol=position.symbol,
                direction=position.direction,
                level=position.level,
                indicators=indicators,
            ),
        },
    )


# =============================================================================
# Stats update
# =============================================================================

def _empty_stats() -> dict[str, Any]:
    return {
        "total": 0,
        "wins": 0,
        "losses": 0,
        "exits": 0,
        "tp1": 0,
        "tp2": 0,
        "sl": 0,
        "real": 0,
        "ghost": 0,
        "pnl_usdt": 0.0,
        "mfe_sum": 0.0,
        "mae_sum": 0.0,
        "updated_at": utc_now_iso(),
    }


def _update_stats_bucket(bucket: dict[str, Any], record: LearningRecord) -> dict[str, Any]:
    result = safe_str(record.result).upper()
    event = safe_str(record.event).upper()
    mode = safe_str(record.metadata.get("mode")).upper()

    bucket["total"] = safe_int(bucket.get("total"), 0) + 1

    if result == "WIN":
        bucket["wins"] = safe_int(bucket.get("wins"), 0) + 1
    elif result == "LOSS":
        bucket["losses"] = safe_int(bucket.get("losses"), 0) + 1
    elif result == "EXIT":
        bucket["exits"] = safe_int(bucket.get("exits"), 0) + 1

    if event == "TP1":
        bucket["tp1"] = safe_int(bucket.get("tp1"), 0) + 1
    elif event == "TP2":
        bucket["tp2"] = safe_int(bucket.get("tp2"), 0) + 1
    elif event in {"SL", "STOP_LOSS"}:
        bucket["sl"] = safe_int(bucket.get("sl"), 0) + 1

    if mode == MODE_REAL:
        bucket["real"] = safe_int(bucket.get("real"), 0) + 1
    elif mode == MODE_GHOST:
        bucket["ghost"] = safe_int(bucket.get("ghost"), 0) + 1

    bucket["pnl_usdt"] = (safe_float(bucket.get("pnl_usdt"), 0.0) or 0.0) + (safe_float(record.pnl_usdt, 0.0) or 0.0)
    bucket["mfe_sum"] = (safe_float(bucket.get("mfe_sum"), 0.0) or 0.0) + (safe_float(record.mfe_pct, 0.0) or 0.0)
    bucket["mae_sum"] = (safe_float(bucket.get("mae_sum"), 0.0) or 0.0) + (safe_float(record.mae_pct, 0.0) or 0.0)
    bucket["updated_at"] = utc_now_iso()
    return bucket


def update_learning_stats(payload: dict[str, Any], record: LearningRecord) -> dict[str, Any]:
    """Update coin and condition stats with one record."""
    coin_key = market_symbol_key(record.symbol, record.direction, record.level)
    condition_key = safe_str(record.metadata.get("condition_key"), coin_key)

    coin_stats = payload.setdefault("coin_stats", {})
    condition_stats = payload.setdefault("condition_stats", {})

    coin_bucket = dict(coin_stats.get(coin_key, _empty_stats()))
    condition_bucket_data = dict(condition_stats.get(condition_key, _empty_stats()))

    coin_stats[coin_key] = _update_stats_bucket(coin_bucket, record)
    condition_stats[condition_key] = _update_stats_bucket(condition_bucket_data, record)
    payload["coin_stats"] = coin_stats
    payload["condition_stats"] = condition_stats
    return payload


# =============================================================================
# Record persistence
# =============================================================================

def record_learning_sample(record: LearningRecord | Mapping[str, Any], *, max_records: int = 20000) -> RecordResult:
    """Record one learning sample and update stats."""
    try:
        rec = record if isinstance(record, LearningRecord) else from_dict(LearningRecord, dict(record))
        payload = _load_learning_payload()

        records = payload.get("records", [])
        records.append(to_dict(rec))
        if max_records > 0 and len(records) > max_records:
            records = records[-max_records:]

        payload["records"] = records
        payload = update_learning_stats(payload, rec)
        ok = _save_learning_payload(payload)

        return RecordResult(
            status=STATUS_OK if ok else STATUS_FAILED,
            recorded=ok,
            record_id=rec.record_id,
            message="learning_recorded" if ok else "learning_record_failed",
            metadata={
                "symbol": rec.symbol,
                "direction": rec.direction,
                "level": rec.level,
                "result": rec.result,
            },
        )

    except Exception as exc:
        log_error(module="learning_memory", function="record_learning_sample", error=exc)
        return RecordResult(status=STATUS_FAILED, recorded=False, message="learning_record_exception", error=str(exc))


def record_outcome(
    outcome: TradeOutcome | Mapping[str, Any],
    *,
    indicators: Optional[Mapping[str, Any]] = None,
    structure: Optional[Mapping[str, Any]] = None,
    momentum: Optional[Mapping[str, Any]] = None,
    liquidity: Optional[Mapping[str, Any]] = None,
    market_context: Optional[Mapping[str, Any]] = None,
    tp_sl: Optional[Mapping[str, Any]] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> RecordResult:
    """Record outcome into learning memory and REAL/GHOST archive."""
    try:
        out = outcome if isinstance(outcome, TradeOutcome) else from_dict(TradeOutcome, dict(outcome))
        rec = learning_record_from_outcome(
            out,
            indicators=indicators,
            structure=structure,
            momentum=momentum,
            liquidity=liquidity,
            market_context=market_context,
            tp_sl=tp_sl,
            metadata=metadata,
        )

        learning_result = record_learning_sample(rec)

        archive_key = REAL_KEY if out.mode == MODE_REAL else GHOST_KEY
        archive_ok = append_record(archive_key, to_dict(out), list_key="records", max_records=20000)

        return RecordResult(
            status=STATUS_OK if learning_result.recorded and archive_ok else STATUS_FAILED,
            recorded=learning_result.recorded and archive_ok,
            record_id=rec.record_id,
            message="outcome_recorded" if learning_result.recorded and archive_ok else "outcome_record_failed",
            metadata={
                "learning_recorded": learning_result.recorded,
                "archive_recorded": archive_ok,
                "archive": archive_key,
            },
        )

    except Exception as exc:
        log_error(module="learning_memory", function="record_outcome", error=exc)
        return RecordResult(status=STATUS_FAILED, recorded=False, message="outcome_record_exception", error=str(exc))


def record_ghost_outcome(outcome: TradeOutcome | Mapping[str, Any], **kwargs: Any) -> RecordResult:
    """Force outcome mode to GHOST and record."""
    out = outcome if isinstance(outcome, TradeOutcome) else from_dict(TradeOutcome, dict(outcome))
    out.mode = MODE_GHOST
    return record_outcome(out, **kwargs)


def record_real_outcome(outcome: TradeOutcome | Mapping[str, Any], **kwargs: Any) -> RecordResult:
    """Force outcome mode to REAL and record."""
    out = outcome if isinstance(outcome, TradeOutcome) else from_dict(TradeOutcome, dict(outcome))
    out.mode = MODE_REAL
    return record_outcome(out, **kwargs)


# =============================================================================
# Query helpers
# =============================================================================

def get_learning_records(limit: int = 100, *, symbol: str = "", direction: str = "", level: Optional[int] = None) -> list[dict[str, Any]]:
    """Return recent learning records filtered by optional fields."""
    payload = _load_learning_payload()
    records = [r for r in payload.get("records", []) if isinstance(r, dict)]

    symbol_norm = normalize_symbol(symbol) if symbol else ""
    direction_norm = normalize_direction(direction) if direction else ""

    filtered: list[dict[str, Any]] = []
    for record in records:
        if symbol_norm and normalize_symbol(record.get("symbol")) != symbol_norm:
            continue
        if direction_norm and normalize_direction(record.get("direction")) != direction_norm:
            continue
        if level is not None and safe_int(record.get("level"), 0) != level:
            continue
        filtered.append(record)

    max_items = max(1, safe_int(limit, 100) or 100)
    return filtered[-max_items:]


def get_coin_stats(symbol: str, direction: str = "", level: int = 4) -> dict[str, Any]:
    """Return conditional coin+direction+level stats."""
    payload = _load_learning_payload()
    stats = payload.get("coin_stats", {})
    key = market_symbol_key(symbol, direction, level)
    return dict(stats.get(key, _empty_stats()))


def get_condition_stats(condition_key: str) -> dict[str, Any]:
    """Return stats for one condition key."""
    payload = _load_learning_payload()
    stats = payload.get("condition_stats", {})
    return dict(stats.get(safe_str(condition_key), _empty_stats()))


def win_rate(stats: Mapping[str, Any]) -> float:
    """Win rate based on TP1/wins vs losses; TP2 does not affect win rate separately."""
    wins = safe_int(stats.get("wins"), 0) or 0
    losses = safe_int(stats.get("losses"), 0) or 0
    total = wins + losses
    if total <= 0:
        return 0.0
    return (wins / total) * 100.0


def tp2_rate(stats: Mapping[str, Any]) -> float:
    """TP2 hit rate as separate statistic."""
    tp2 = safe_int(stats.get("tp2"), 0) or 0
    total = safe_int(stats.get("total"), 0) or 0
    if total <= 0:
        return 0.0
    return (tp2 / total) * 100.0


def get_learning_summary() -> dict[str, Any]:
    """Return lightweight learning summary for status/telegram_ui."""
    payload = _load_learning_payload()
    records = payload.get("records", [])
    coin_stats = payload.get("coin_stats", {})

    total = len(records)
    wins = sum(safe_int(v.get("wins"), 0) or 0 for v in coin_stats.values() if isinstance(v, dict))
    losses = sum(safe_int(v.get("losses"), 0) or 0 for v in coin_stats.values() if isinstance(v, dict))
    tp2 = sum(safe_int(v.get("tp2"), 0) or 0 for v in coin_stats.values() if isinstance(v, dict))

    return {
        "system_version": SYSTEM_VERSION,
        "total_records": total,
        "coin_buckets": len(coin_stats),
        "condition_buckets": len(payload.get("condition_stats", {})),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate({"wins": wins, "losses": losses}),
        "tp2": tp2,
        "updated_at": payload.get("updated_at"),
    }



def reset_learning_memory(*, reset_archives: bool = True, reset_stats: bool = True) -> RecordResult:
    """
    Clear learning/statistical records without touching open positions or strategy settings.

    This backs the Telegram command "حذف آمار" and is intentionally real, not display-only.
    """
    try:
        ok_learning = _save_learning_payload(default_learning_payload())
        ok_ghost = True
        ok_real = True
        ok_stats = True
        if reset_archives:
            ok_ghost = save_json_atomic(GHOST_KEY, {"system_version": SYSTEM_VERSION, "records": [], "updated_at": utc_now_iso()})
            ok_real = save_json_atomic(REAL_KEY, {"system_version": SYSTEM_VERSION, "records": [], "updated_at": utc_now_iso()})
        if reset_stats:
            ok_stats = save_json_atomic("stats", {"system_version": SYSTEM_VERSION, "events": [], "summary": {}, "updated_at": utc_now_iso()})
        ok = bool(ok_learning and ok_ghost and ok_real and ok_stats)
        return RecordResult(
            status=STATUS_OK if ok else STATUS_FAILED,
            recorded=ok,
            message="learning_memory_reset" if ok else "learning_memory_reset_failed",
            metadata={
                "learning": ok_learning,
                "ghost_records": ok_ghost,
                "real_records": ok_real,
                "stats": ok_stats,
                "open_positions_preserved": True,
            },
        )
    except Exception as exc:
        log_error(module="learning_memory", function="reset_learning_memory", error=exc)
        return RecordResult(status=STATUS_FAILED, recorded=False, message="learning_memory_reset_exception", error=str(exc))


def validate_learning_memory_light() -> dict[str, Any]:
    """Lightweight startup validation."""
    payload = _load_learning_payload()
    errors: list[str] = []

    if payload.get("system_version") != SYSTEM_VERSION:
        errors.append("invalid_system_version")
    if not isinstance(payload.get("records"), list):
        errors.append("records_not_list")
    if not isinstance(payload.get("coin_stats"), dict):
        errors.append("coin_stats_not_dict")
    if not isinstance(payload.get("condition_stats"), dict):
        errors.append("condition_stats_not_dict")

    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "summary": get_learning_summary(),
        "checked_at": utc_now_iso(),
    }


__all__ = [
    "LEARNING_MEMORY_VERSION",
    "LEARNING_KEY",
    "GHOST_KEY",
    "REAL_KEY",
    "default_learning_payload",
    "outcome_result_label",
    "condition_bucket",
    "build_condition_key",
    "learning_record_from_outcome",
    "learning_record_from_position",
    "update_learning_stats",
    "record_learning_sample",
    "record_outcome",
    "record_ghost_outcome",
    "record_real_outcome",
    "get_learning_records",
    "get_coin_stats",
    "get_condition_stats",
    "win_rate",
    "tp2_rate",
    "get_learning_summary",
    "reset_learning_memory",
    "validate_learning_memory_light",
]
