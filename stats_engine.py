"""
stats_engine.py
Level 4 / 1H Smart Scalp Bot

Statistics aggregation engine.

Architecture lock:
- Builds lightweight statistics only.
- Reads from learning_memory.py and position_manager.py public APIs.
- Does not make AI decisions, place orders, fetch market data, write JSON state,
  monitor positions, or build Telegram text.
- Toobit and real_trade_manager are intentionally not imported here.
- Allowed project imports:
  constants.py, utils.py, models.py, learning_memory.py, position_manager.py only.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Mapping, Optional

from constants import (
    EVENT_AI_EXIT,
    EVENT_MANUAL_CLOSE,
    EVENT_SL,
    EVENT_TP1,
    EVENT_TP2,
    MODE_GHOST,
    MODE_REAL,
    POSITION_ACTIVE_GHOST,
    POSITION_ACTIVE_REAL,
    POSITION_CLOSED,
    POSITION_PARTIAL_TP1,
    POSITION_PENDING_REAL_CONFIRM,
    STATUS_FAILED,
    STATUS_OK,
    STRATEGY_LEVEL,
    SYSTEM_VERSION,
)
from learning_memory import (
    get_learning_records,
    get_learning_summary,
    get_coin_stats as learning_get_coin_stats,
    win_rate,
)
from position_manager import get_closed_positions, get_open_positions, get_positions_summary, load_positions
from utils import normalize_direction, normalize_symbol, safe_float, safe_int, safe_str, utc_now_iso


STATS_ENGINE_VERSION: str = SYSTEM_VERSION
DEFAULT_STATS_LIMIT: int = 20000


# =============================================================================
# Basic helpers
# =============================================================================

def _empty_counter() -> dict[str, Any]:
    return {
        "total": 0,
        "wins": 0,
        "losses": 0,
        "exits": 0,
        "tp1": 0,
        "tp2": 0,
        "sl": 0,
        "ai_exit": 0,
        "manual_close": 0,
        "real": 0,
        "ghost": 0,
        "pnl_usdt": 0.0,
        "confirmed_pnl_usdt": 0.0,
        "unconfirmed_pnl_usdt": 0.0,
        "mfe_sum": 0.0,
        "mae_sum": 0.0,
    }


def _finalize_counter(counter: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(counter)
    wins = safe_int(data.get("wins"), 0) or 0
    losses = safe_int(data.get("losses"), 0) or 0
    total = safe_int(data.get("total"), 0) or 0
    tp2 = safe_int(data.get("tp2"), 0) or 0
    exits = safe_int(data.get("exits"), 0) or 0

    data["win_rate"] = win_rate({"wins": wins, "losses": losses})
    data["tp2_rate"] = (tp2 / total * 100.0) if total > 0 else 0.0
    data["exit_rate"] = (exits / total * 100.0) if total > 0 else 0.0
    data["avg_mfe_pct"] = ((safe_float(data.get("mfe_sum"), 0.0) or 0.0) / total) if total > 0 else 0.0
    data["avg_mae_pct"] = ((safe_float(data.get("mae_sum"), 0.0) or 0.0) / total) if total > 0 else 0.0
    data["updated_at"] = utc_now_iso()
    return data


def _record_timestamp(record: Mapping[str, Any]) -> Optional[datetime]:
    raw = (
        record.get("created_at")
        or record.get("closed_at")
        or record.get("updated_at")
        or record.get("timestamp")
    )
    text = safe_str(raw)
    if not text:
        return None
    try:
        text = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _record_level(record: Mapping[str, Any]) -> int:
    level = safe_int(record.get("level"), None)
    if level is not None:
        return level
    meta = record.get("metadata")
    if isinstance(meta, Mapping):
        return safe_int(meta.get("level"), STRATEGY_LEVEL) or STRATEGY_LEVEL
    return STRATEGY_LEVEL


def _record_mode(record: Mapping[str, Any]) -> str:
    mode = safe_str(record.get("mode")).upper()
    if mode:
        return mode
    meta = record.get("metadata")
    if isinstance(meta, Mapping):
        return safe_str(meta.get("mode")).upper()
    return ""


def _record_symbol(record: Mapping[str, Any]) -> str:
    return normalize_symbol(record.get("symbol"))


def _record_direction(record: Mapping[str, Any]) -> str:
    return normalize_direction(record.get("direction"))


def _record_event(record: Mapping[str, Any]) -> str:
    return safe_str(record.get("event")).upper()


def _record_result(record: Mapping[str, Any]) -> str:
    return safe_str(record.get("result")).upper()


def _record_pnl_confirmed(record: Mapping[str, Any]) -> bool:
    if "pnl_confirmed" in record:
        return bool(record.get("pnl_confirmed"))
    meta = record.get("metadata")
    if isinstance(meta, Mapping):
        return bool(meta.get("pnl_confirmed", False))
    return False


def _add_record(counter: dict[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    event = _record_event(record)
    result = _record_result(record)
    mode = _record_mode(record)
    pnl = safe_float(record.get("pnl_usdt"), 0.0) or 0.0
    mfe = safe_float(record.get("mfe_pct"), 0.0) or 0.0
    mae = safe_float(record.get("mae_pct"), 0.0) or 0.0

    counter["total"] = safe_int(counter.get("total"), 0) + 1

    if result == "WIN":
        counter["wins"] = safe_int(counter.get("wins"), 0) + 1
    elif result == "LOSS":
        counter["losses"] = safe_int(counter.get("losses"), 0) + 1
    elif result == "EXIT":
        counter["exits"] = safe_int(counter.get("exits"), 0) + 1

    if event == EVENT_TP1:
        counter["tp1"] = safe_int(counter.get("tp1"), 0) + 1
    elif event == EVENT_TP2:
        counter["tp2"] = safe_int(counter.get("tp2"), 0) + 1
    elif event == EVENT_SL:
        counter["sl"] = safe_int(counter.get("sl"), 0) + 1
    elif event == EVENT_AI_EXIT:
        counter["ai_exit"] = safe_int(counter.get("ai_exit"), 0) + 1
    elif event == EVENT_MANUAL_CLOSE:
        counter["manual_close"] = safe_int(counter.get("manual_close"), 0) + 1

    if mode == MODE_REAL:
        counter["real"] = safe_int(counter.get("real"), 0) + 1
    elif mode == MODE_GHOST:
        counter["ghost"] = safe_int(counter.get("ghost"), 0) + 1

    counter["pnl_usdt"] = (safe_float(counter.get("pnl_usdt"), 0.0) or 0.0) + pnl
    if _record_pnl_confirmed(record):
        counter["confirmed_pnl_usdt"] = (safe_float(counter.get("confirmed_pnl_usdt"), 0.0) or 0.0) + pnl
    else:
        counter["unconfirmed_pnl_usdt"] = (safe_float(counter.get("unconfirmed_pnl_usdt"), 0.0) or 0.0) + pnl

    counter["mfe_sum"] = (safe_float(counter.get("mfe_sum"), 0.0) or 0.0) + mfe
    counter["mae_sum"] = (safe_float(counter.get("mae_sum"), 0.0) or 0.0) + mae
    return counter


def _filter_records(
    records: list[dict[str, Any]],
    *,
    mode: str = "",
    symbol: str = "",
    direction: str = "",
    level: Optional[int] = None,
    since: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    mode_norm = safe_str(mode).upper()
    symbol_norm = normalize_symbol(symbol) if symbol else ""
    direction_norm = normalize_direction(direction) if direction else ""

    result: list[dict[str, Any]] = []
    for record in records:
        if mode_norm and _record_mode(record) != mode_norm:
            continue
        if symbol_norm and _record_symbol(record) != symbol_norm:
            continue
        if direction_norm and _record_direction(record) != direction_norm:
            continue
        if level is not None and _record_level(record) != level:
            continue
        if since is not None:
            ts = _record_timestamp(record)
            if ts is None or ts < since:
                continue
        result.append(record)
    return result


def _counter_from_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    counter = _empty_counter()
    for record in records:
        _add_record(counter, record)
    return _finalize_counter(counter)


def _all_learning_records(limit: int = DEFAULT_STATS_LIMIT) -> list[dict[str, Any]]:
    return get_learning_records(limit=limit)


# =============================================================================
# Public stats
# =============================================================================

def get_global_stats(limit: int = DEFAULT_STATS_LIMIT) -> dict[str, Any]:
    records = _all_learning_records(limit)
    data = _counter_from_records(records)
    data["scope"] = "GLOBAL"
    return data


def get_real_stats(limit: int = DEFAULT_STATS_LIMIT) -> dict[str, Any]:
    records = _filter_records(_all_learning_records(limit), mode=MODE_REAL)
    data = _counter_from_records(records)
    data["scope"] = MODE_REAL
    return data


def get_ghost_stats(limit: int = DEFAULT_STATS_LIMIT) -> dict[str, Any]:
    records = _filter_records(_all_learning_records(limit), mode=MODE_GHOST)
    data = _counter_from_records(records)
    data["scope"] = MODE_GHOST
    return data


def get_level_stats(level: int = STRATEGY_LEVEL, limit: int = DEFAULT_STATS_LIMIT) -> dict[str, Any]:
    lvl = safe_int(level, STRATEGY_LEVEL) or STRATEGY_LEVEL
    records = _filter_records(_all_learning_records(limit), level=lvl)
    data = _counter_from_records(records)
    data["scope"] = "LEVEL"
    data["level"] = lvl
    return data


def get_coin_stats(symbol: str, direction: str = "", level: int = STRATEGY_LEVEL, limit: int = DEFAULT_STATS_LIMIT) -> dict[str, Any]:
    symbol_norm = normalize_symbol(symbol)
    direction_norm = normalize_direction(direction) if direction else ""
    lvl = safe_int(level, STRATEGY_LEVEL) or STRATEGY_LEVEL

    records = _filter_records(
        _all_learning_records(limit),
        symbol=symbol_norm,
        direction=direction_norm,
        level=lvl,
    )
    data = _counter_from_records(records)

    if direction_norm:
        bucket = learning_get_coin_stats(symbol_norm, direction_norm, lvl)
        if data["total"] == 0 and safe_int(bucket.get("total"), 0) > 0:
            data.update(_finalize_counter(bucket))

    data["scope"] = "COIN"
    data["symbol"] = symbol_norm
    data["direction"] = direction_norm
    data["level"] = lvl
    return data


def get_coin_rankings(limit: int = DEFAULT_STATS_LIMIT, *, min_total: int = 1, level: Optional[int] = None) -> list[dict[str, Any]]:
    records = _all_learning_records(limit)
    buckets: dict[str, dict[str, Any]] = {}

    for record in records:
        if level is not None and _record_level(record) != level:
            continue
        symbol = _record_symbol(record)
        direction = _record_direction(record)
        lvl = _record_level(record)
        if not symbol:
            continue
        key = f"{symbol}:{direction}:{lvl}"
        bucket = buckets.setdefault(key, {**_empty_counter(), "symbol": symbol, "direction": direction, "level": lvl})
        _add_record(bucket, record)

    ranked = [_finalize_counter(bucket) for bucket in buckets.values() if safe_int(bucket.get("total"), 0) >= min_total]
    ranked.sort(key=lambda x: (safe_float(x.get("win_rate"), 0.0) or 0.0, safe_int(x.get("total"), 0) or 0), reverse=True)
    return ranked


def get_ai_exit_stats(limit: int = DEFAULT_STATS_LIMIT) -> dict[str, Any]:
    records = [r for r in _all_learning_records(limit) if _record_event(r) == EVENT_AI_EXIT]
    data = _counter_from_records(records)

    profit_count = 0
    loss_count = 0
    flat_count = 0
    for record in records:
        pnl = safe_float(record.get("pnl_usdt"), 0.0) or 0.0
        if pnl > 0:
            profit_count += 1
        elif pnl < 0:
            loss_count += 1
        else:
            flat_count += 1

    data.update(
        {
            "scope": "AI_EXIT",
            "ai_exit_count": len(records),
            "ai_exit_profit_count": profit_count,
            "ai_exit_loss_count": loss_count,
            "ai_exit_flat_count": flat_count,
            "ai_exit_profit_rate": (profit_count / len(records) * 100.0) if records else 0.0,
        }
    )
    return data


def get_daily_stats(days: int = 1, limit: int = DEFAULT_STATS_LIMIT) -> dict[str, Any]:
    d = max(1, safe_int(days, 1) or 1)
    since = datetime.now(timezone.utc) - timedelta(days=d)
    records = _filter_records(_all_learning_records(limit), since=since)
    data = _counter_from_records(records)
    data["scope"] = "DAILY"
    data["days"] = d
    data["since"] = since.isoformat(timespec="seconds")
    return data


def get_position_stats() -> dict[str, Any]:
    summary = get_positions_summary()
    open_positions = get_open_positions()
    closed_positions = get_closed_positions()

    by_status: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    for position in load_positions():
        status = safe_str(position.status).upper()
        mode = safe_str(position.mode).upper()
        by_status[status] = by_status.get(status, 0) + 1
        by_mode[mode] = by_mode.get(mode, 0) + 1

    return {
        "system_version": SYSTEM_VERSION,
        "scope": "POSITIONS",
        "summary": summary,
        "open_total": len(open_positions),
        "closed_total": len(closed_positions),
        "pending_real_confirm": by_status.get(POSITION_PENDING_REAL_CONFIRM, 0),
        "active_real": by_status.get(POSITION_ACTIVE_REAL, 0),
        "active_ghost": by_status.get(POSITION_ACTIVE_GHOST, 0),
        "partial_tp1": by_status.get(POSITION_PARTIAL_TP1, 0),
        "closed": by_status.get(POSITION_CLOSED, 0),
        "by_status": by_status,
        "by_mode": by_mode,
        "updated_at": utc_now_iso(),
    }


def build_stats_snapshot(limit: int = DEFAULT_STATS_LIMIT) -> dict[str, Any]:
    return {
        "system_version": SYSTEM_VERSION,
        "stats_engine_version": STATS_ENGINE_VERSION,
        "created_at": utc_now_iso(),
        "learning_summary": get_learning_summary(),
        "global": get_global_stats(limit),
        "real": get_real_stats(limit),
        "ghost": get_ghost_stats(limit),
        "level": get_level_stats(STRATEGY_LEVEL, limit),
        "ai_exit": get_ai_exit_stats(limit),
        "positions": get_position_stats(),
        "daily": {
            "today": get_daily_stats(1, limit),
            "last_7_days": get_daily_stats(7, limit),
            "last_30_days": get_daily_stats(30, limit),
        },
    }


def validate_stats_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    errors: list[str] = []

    if safe_str(snapshot.get("system_version")) != SYSTEM_VERSION:
        errors.append("INVALID_SYSTEM_VERSION")

    for key in ["global", "real", "ghost", "level", "ai_exit", "positions", "daily"]:
        if key not in snapshot:
            errors.append(f"MISSING_{key.upper()}")

    for section in ["global", "real", "ghost", "level"]:
        value = snapshot.get(section)
        if isinstance(value, Mapping):
            wr = safe_float(value.get("win_rate"), None)
            if wr is None or not (0.0 <= wr <= 100.0):
                errors.append(f"INVALID_{section.upper()}_WIN_RATE")
        else:
            errors.append(f"INVALID_{section.upper()}_SECTION")

    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "checked_at": utc_now_iso(),
    }


__all__ = [
    "STATS_ENGINE_VERSION",
    "DEFAULT_STATS_LIMIT",
    "get_global_stats",
    "get_real_stats",
    "get_ghost_stats",
    "get_level_stats",
    "get_coin_stats",
    "get_coin_rankings",
    "get_ai_exit_stats",
    "get_daily_stats",
    "get_position_stats",
    "build_stats_snapshot",
    "validate_stats_snapshot",
]
