"""
position_manager.py
Level 4 / 1H Smart Scalp Bot

Position record manager.

Architecture lock:
- Owns high-level position record operations on positions.json.
- Uses state_store.py for actual JSON IO.
- Does not call exchange APIs, AI, market data, position monitor, or Telegram.
- real_trade_manager.py remains the owner of REAL exchange-side open/close actions.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from constants import (
    MODE_GHOST,
    MODE_REAL,
    OPEN_POSITION_STATES,
    POSITION_ACTIVE_GHOST,
    POSITION_ACTIVE_REAL,
    POSITION_CLOSED,
    POSITION_CLOSING,
    POSITION_FAILED,
    POSITION_PARTIAL_TP1,
    POSITION_PENDING_REAL_CONFIRM,
    STATUS_FAILED,
    STATUS_OK,
    SYSTEM_VERSION,
)
from models import RecordResult, TradePosition, from_dict, to_dict
from state_store import load_json, save_json_atomic, log_error
from utils import normalize_direction, normalize_symbol, safe_float, safe_str, utc_now_iso


POSITION_MANAGER_VERSION: str = SYSTEM_VERSION
POSITIONS_KEY: str = "positions"


# =============================================================================
# Internal helpers
# =============================================================================

def _empty_positions_payload() -> dict[str, Any]:
    return {
        "system_version": SYSTEM_VERSION,
        "positions": [],
        "updated_at": utc_now_iso(),
    }


def _load_payload() -> dict[str, Any]:
    data = load_json(POSITIONS_KEY, default=_empty_positions_payload())
    if not isinstance(data, dict):
        return _empty_positions_payload()
    if not isinstance(data.get("positions"), list):
        data["positions"] = []
    data.setdefault("system_version", SYSTEM_VERSION)
    return data


def _save_payload(payload: Mapping[str, Any]) -> bool:
    data = dict(payload)
    data.setdefault("system_version", SYSTEM_VERSION)
    data["updated_at"] = utc_now_iso()
    return save_json_atomic(POSITIONS_KEY, data)


def _position_from_any(position: Any) -> TradePosition:
    if isinstance(position, TradePosition):
        return position
    if isinstance(position, dict):
        return from_dict(TradePosition, position)
    raise TypeError("position must be TradePosition or dict")


def _normalize_record(record: Any) -> dict[str, Any]:
    pos = _position_from_any(record)
    return to_dict(pos)


def _matches_symbol_direction(record: Mapping[str, Any], symbol: Any, direction: Any = "") -> bool:
    symbol_ok = normalize_symbol(record.get("symbol")) == normalize_symbol(symbol)
    if not symbol_ok:
        return False
    normalized_direction = normalize_direction(direction)
    if normalized_direction:
        return normalize_direction(record.get("direction")) == normalized_direction
    return True


def _is_open_status(status: Any) -> bool:
    return safe_str(status).upper() in set(OPEN_POSITION_STATES)


# =============================================================================
# Read operations
# =============================================================================

def load_positions() -> list[TradePosition]:
    """Load all positions as TradePosition objects."""
    payload = _load_payload()
    positions: list[TradePosition] = []
    for item in payload.get("positions", []):
        try:
            positions.append(_position_from_any(item))
        except Exception as exc:
            log_error(
                module="position_manager",
                function="load_positions",
                error=exc,
                context={"bad_record": item if isinstance(item, dict) else str(item)},
            )
    return positions


def load_position_dicts() -> list[dict[str, Any]]:
    """Load all positions as normalized dictionaries."""
    return [to_dict(position) for position in load_positions()]


def get_position(position_id: str) -> Optional[TradePosition]:
    """Return one position by id."""
    pid = safe_str(position_id)
    for position in load_positions():
        if position.position_id == pid:
            return position
    return None


def get_open_positions(*, mode: str = "", symbol: str = "", direction: str = "") -> list[TradePosition]:
    """Return open positions, optionally filtered by mode/symbol/direction."""
    mode_norm = safe_str(mode).upper()
    symbol_norm = normalize_symbol(symbol) if symbol else ""
    direction_norm = normalize_direction(direction) if direction else ""

    result: list[TradePosition] = []
    for position in load_positions():
        if not _is_open_status(position.status):
            continue
        if mode_norm and position.mode != mode_norm:
            continue
        if symbol_norm and position.symbol != symbol_norm:
            continue
        if direction_norm and position.direction != direction_norm:
            continue
        result.append(position)
    return result


def get_closed_positions(*, symbol: str = "", direction: str = "") -> list[TradePosition]:
    """Return closed/failed positions, optionally filtered."""
    symbol_norm = normalize_symbol(symbol) if symbol else ""
    direction_norm = normalize_direction(direction) if direction else ""
    result: list[TradePosition] = []

    for position in load_positions():
        if _is_open_status(position.status):
            continue
        if symbol_norm and position.symbol != symbol_norm:
            continue
        if direction_norm and position.direction != direction_norm:
            continue
        result.append(position)
    return result


def has_open_position(symbol: str, direction: str = "", *, mode: str = "") -> bool:
    """Return True if there is an open position for symbol/direction."""
    return bool(get_open_positions(mode=mode, symbol=symbol, direction=direction))


def count_open_positions(*, mode: str = "") -> int:
    """Count open positions, optionally by mode."""
    return len(get_open_positions(mode=mode))


def count_open_real_positions() -> int:
    return count_open_positions(mode=MODE_REAL)


def count_open_ghost_positions() -> int:
    return count_open_positions(mode=MODE_GHOST)


def find_duplicate_position(symbol: str, direction: str, *, mode: str = "") -> Optional[TradePosition]:
    """Find an open duplicate symbol+direction position."""
    matches = get_open_positions(mode=mode, symbol=symbol, direction=direction)
    return matches[0] if matches else None


# =============================================================================
# Write operations
# =============================================================================

def save_positions(positions: list[TradePosition | dict[str, Any]]) -> bool:
    """Replace the full positions list with normalized records."""
    payload = _empty_positions_payload()
    payload["positions"] = [_normalize_record(position) for position in positions]
    return _save_payload(payload)


def upsert_position(position: TradePosition | dict[str, Any]) -> RecordResult:
    """Insert or update one position by position_id."""
    try:
        pos = _position_from_any(position)
        payload = _load_payload()
        records = payload.get("positions", [])

        updated = False
        new_records: list[dict[str, Any]] = []
        for item in records:
            if safe_str(item.get("position_id")) == pos.position_id:
                new_records.append(to_dict(pos))
                updated = True
            else:
                new_records.append(item)

        if not updated:
            new_records.append(to_dict(pos))

        payload["positions"] = new_records
        ok = _save_payload(payload)
        return RecordResult(
            status=STATUS_OK if ok else STATUS_FAILED,
            recorded=ok,
            record_id=pos.position_id,
            message="position_updated" if updated and ok else "position_created" if ok else "position_save_failed",
            metadata={"updated": updated, "symbol": pos.symbol, "direction": pos.direction, "mode": pos.mode},
        )

    except Exception as exc:
        log_error(module="position_manager", function="upsert_position", error=exc)
        return RecordResult(status=STATUS_FAILED, recorded=False, message="position_upsert_failed", error=str(exc))


def add_position(position: TradePosition | dict[str, Any], *, reject_duplicate: bool = True) -> RecordResult:
    """Add a new position. Optionally reject open duplicate symbol+direction."""
    try:
        pos = _position_from_any(position)

        if reject_duplicate and has_open_position(pos.symbol, pos.direction):
            return RecordResult(
                status=STATUS_FAILED,
                recorded=False,
                record_id=pos.position_id,
                message="duplicate_open_position",
                error="open position already exists for symbol/direction",
                metadata={"symbol": pos.symbol, "direction": pos.direction},
            )

        existing = get_position(pos.position_id)
        if existing is not None:
            return RecordResult(
                status=STATUS_FAILED,
                recorded=False,
                record_id=pos.position_id,
                message="position_id_exists",
                error="position_id already exists",
            )

        return upsert_position(pos)

    except Exception as exc:
        log_error(module="position_manager", function="add_position", error=exc)
        return RecordResult(status=STATUS_FAILED, recorded=False, message="position_add_failed", error=str(exc))


def update_position(position_id: str, updates: Mapping[str, Any]) -> RecordResult:
    """Patch one position by id."""
    pid = safe_str(position_id)
    if not pid:
        return RecordResult(status=STATUS_FAILED, recorded=False, message="missing_position_id")

    payload = _load_payload()
    records = payload.get("positions", [])

    found = False
    updated_record: Optional[dict[str, Any]] = None
    new_records: list[dict[str, Any]] = []

    for item in records:
        if safe_str(item.get("position_id")) == pid:
            merged = dict(item)
            merged.update(dict(updates))
            merged["updated_at"] = utc_now_iso()
            normalized = _normalize_record(merged)
            new_records.append(normalized)
            updated_record = normalized
            found = True
        else:
            new_records.append(item)

    if not found:
        return RecordResult(status=STATUS_FAILED, recorded=False, record_id=pid, message="position_not_found")

    payload["positions"] = new_records
    ok = _save_payload(payload)
    return RecordResult(
        status=STATUS_OK if ok else STATUS_FAILED,
        recorded=ok,
        record_id=pid,
        message="position_patched" if ok else "position_patch_failed",
        metadata={"position": updated_record or {}},
    )


def update_position_with(position_id: str, updater: Callable[[TradePosition], TradePosition]) -> RecordResult:
    """Load one position, apply updater, then save it."""
    position = get_position(position_id)
    if position is None:
        return RecordResult(status=STATUS_FAILED, recorded=False, record_id=position_id, message="position_not_found")

    try:
        updated = updater(position)
        if updated is None:
            updated = position
        return upsert_position(updated)
    except Exception as exc:
        log_error(module="position_manager", function="update_position_with", error=exc, context={"position_id": position_id})
        return RecordResult(status=STATUS_FAILED, recorded=False, record_id=position_id, message="position_update_failed", error=str(exc))


def remove_position(position_id: str) -> RecordResult:
    """Remove one position record. Use sparingly; normally close_position_record is preferred."""
    pid = safe_str(position_id)
    payload = _load_payload()
    records = payload.get("positions", [])
    new_records = [item for item in records if safe_str(item.get("position_id")) != pid]

    if len(new_records) == len(records):
        return RecordResult(status=STATUS_FAILED, recorded=False, record_id=pid, message="position_not_found")

    payload["positions"] = new_records
    ok = _save_payload(payload)
    return RecordResult(
        status=STATUS_OK if ok else STATUS_FAILED,
        recorded=ok,
        record_id=pid,
        message="position_removed" if ok else "position_remove_failed",
    )


# =============================================================================
# State transitions
# =============================================================================

def mark_real_confirmed(position_id: str, *, entry: Any = None, quantity: Any = None, exchange_order_id: str = "") -> RecordResult:
    """Mark pending REAL as exchange-confirmed active REAL."""
    updates: dict[str, Any] = {
        "status": POSITION_ACTIVE_REAL,
        "mode": MODE_REAL,
    }
    if entry is not None:
        updates["entry"] = safe_float(entry, 0.0) or 0.0
        updates["current_price"] = updates["entry"]
    if quantity is not None:
        updates["quantity"] = safe_float(quantity, 0.0) or 0.0
    if exchange_order_id:
        updates["exchange_order_id"] = safe_str(exchange_order_id)
    return update_position(position_id, updates)


def mark_real_failed(position_id: str, reason: str = "") -> RecordResult:
    """Mark a pending/open REAL position as failed."""
    return update_position(
        position_id,
        {
            "status": POSITION_FAILED,
            "monitor_metadata": {"failure_reason": reason, "failed_at": utc_now_iso()},
        },
    )


def mark_closing(position_id: str, reason: str = "") -> RecordResult:
    """Mark position as closing while waiting for close confirmation."""
    return update_position(
        position_id,
        {
            "status": POSITION_CLOSING,
            "monitor_metadata": {"closing_reason": reason, "closing_at": utc_now_iso()},
        },
    )


def mark_tp1_partial(
    position_id: str,
    *,
    closed_quantity: Any,
    runner_quantity: Any,
    protected_sl: Any = None,
) -> RecordResult:
    """Mark TP1 partial close and runner state."""
    updates = {
        "status": POSITION_PARTIAL_TP1,
        "tp1_hit": True,
        "tp1_profit_locked": True,
        "closed_quantity": safe_float(closed_quantity, 0.0) or 0.0,
        "runner_quantity": safe_float(runner_quantity, 0.0) or 0.0,
    }
    if protected_sl is not None:
        updates["protected_sl"] = safe_float(protected_sl, None)
    return update_position(position_id, updates)


def mark_tp2_hit(position_id: str) -> RecordResult:
    """Mark TP2 hit."""
    return update_position(position_id, {"tp2_hit": True})


def mark_sl_hit(position_id: str) -> RecordResult:
    """Mark SL hit."""
    return update_position(position_id, {"sl_hit": True})


def mark_ai_exit_done(position_id: str) -> RecordResult:
    """Mark AI exit completed."""
    return update_position(position_id, {"ai_exit_done": True})


def close_position_record(
    position_id: str,
    *,
    close_price: Any = None,
    pnl_usdt: Any = None,
    pnl_confirmed: bool = False,
    close_reason: str = "",
) -> RecordResult:
    """
    Mark a position record as closed.

    This does NOT send exchange close order. real_trade_manager/position_monitor
    must confirm real close before calling this for REAL positions.
    """
    updates: dict[str, Any] = {
        "status": POSITION_CLOSED,
        "monitor_metadata": {
            "close_reason": close_reason,
            "closed_at": utc_now_iso(),
            "pnl_usdt": safe_float(pnl_usdt, None),
            "pnl_confirmed": bool(pnl_confirmed),
        },
    }
    if close_price is not None:
        updates["current_price"] = safe_float(close_price, 0.0) or 0.0

    return update_position(position_id, updates)


def update_price_extremes(position_id: str, current_price: Any) -> RecordResult:
    """Update current/highest/lowest price for a position."""
    price = safe_float(current_price, None)
    if price is None or price <= 0:
        return RecordResult(
            status=STATUS_FAILED,
            recorded=False,
            record_id=position_id,
            message="invalid_current_price",
        )

    def _updater(position: TradePosition) -> TradePosition:
        position.current_price = price
        if position.highest_price <= 0 or price > position.highest_price:
            position.highest_price = price
        if position.lowest_price <= 0 or price < position.lowest_price:
            position.lowest_price = price
        position.updated_at = utc_now_iso()
        return position

    return update_position_with(position_id, _updater)


# =============================================================================
# Recovery helpers
# =============================================================================

def get_recoverable_positions() -> list[TradePosition]:
    """Return open positions that should be resumed after restart."""
    return get_open_positions()


def get_pending_real_confirm_positions() -> list[TradePosition]:
    """Return REAL positions waiting for exchange confirmation."""
    return [p for p in get_open_positions(mode=MODE_REAL) if p.status == POSITION_PENDING_REAL_CONFIRM]


def get_active_monitor_positions() -> list[TradePosition]:
    """Return positions position_monitor should check."""
    return [
        p
        for p in get_open_positions()
        if p.status in {
            POSITION_ACTIVE_REAL,
            POSITION_ACTIVE_GHOST,
            POSITION_PARTIAL_TP1,
            POSITION_PENDING_REAL_CONFIRM,
            POSITION_CLOSING,
        }
    ]


# =============================================================================
# Validation / summaries
# =============================================================================

def validate_position_record(position: TradePosition | dict[str, Any]) -> dict[str, Any]:
    """Lightweight validation for one position record."""
    try:
        pos = _position_from_any(position)
        errors: list[str] = []

        if not pos.position_id:
            errors.append("missing_position_id")
        if not pos.symbol:
            errors.append("missing_symbol")
        if pos.direction not in {"LONG", "SHORT"}:
            errors.append("invalid_direction")
        if pos.mode not in {MODE_REAL, MODE_GHOST}:
            errors.append("invalid_mode")
        if pos.entry <= 0:
            errors.append("invalid_entry")
        if pos.tp1 <= 0:
            errors.append("invalid_tp1")
        if pos.sl <= 0:
            errors.append("invalid_sl")
        if not pos.status:
            errors.append("missing_status")

        return {
            "valid": not errors,
            "errors": errors,
            "position_id": pos.position_id,
            "symbol": pos.symbol,
            "direction": pos.direction,
            "mode": pos.mode,
            "status": pos.status,
        }

    except Exception as exc:
        return {
            "valid": False,
            "errors": [str(exc)],
            "position_id": "",
        }


def validate_positions_file_light() -> dict[str, Any]:
    """Lightweight validation for startup recovery/preflight."""
    positions = load_positions()
    validations = [validate_position_record(p) for p in positions]
    invalid = [v for v in validations if not v["valid"]]

    return {
        "status": STATUS_OK if not invalid else STATUS_FAILED,
        "system_version": SYSTEM_VERSION,
        "total": len(positions),
        "open": len(get_open_positions()),
        "real_open": count_open_real_positions(),
        "ghost_open": count_open_ghost_positions(),
        "invalid_count": len(invalid),
        "invalid": invalid,
        "checked_at": utc_now_iso(),
    }


def get_positions_summary() -> dict[str, Any]:
    """Return lightweight positions summary for bot/telegram_ui."""
    positions = load_positions()
    open_positions = [p for p in positions if p.status in OPEN_POSITION_STATES]

    return {
        "system_version": SYSTEM_VERSION,
        "total": len(positions),
        "open": len(open_positions),
        "closed": len([p for p in positions if p.status == POSITION_CLOSED]),
        "failed": len([p for p in positions if p.status == POSITION_FAILED]),
        "real_open": len([p for p in open_positions if p.mode == MODE_REAL]),
        "ghost_open": len([p for p in open_positions if p.mode == MODE_GHOST]),
        "by_symbol": _summary_by_symbol(open_positions),
        "updated_at": utc_now_iso(),
    }


def _summary_by_symbol(positions: list[TradePosition]) -> dict[str, int]:
    result: dict[str, int] = {}
    for position in positions:
        result[position.symbol] = result.get(position.symbol, 0) + 1
    return result


__all__ = [
    "POSITION_MANAGER_VERSION",
    "POSITIONS_KEY",
    "load_positions",
    "load_position_dicts",
    "get_position",
    "get_open_positions",
    "get_closed_positions",
    "has_open_position",
    "count_open_positions",
    "count_open_real_positions",
    "count_open_ghost_positions",
    "find_duplicate_position",
    "save_positions",
    "upsert_position",
    "add_position",
    "update_position",
    "update_position_with",
    "remove_position",
    "mark_real_confirmed",
    "mark_real_failed",
    "mark_closing",
    "mark_tp1_partial",
    "mark_tp2_hit",
    "mark_sl_hit",
    "mark_ai_exit_done",
    "close_position_record",
    "update_price_extremes",
    "get_recoverable_positions",
    "get_pending_real_confirm_positions",
    "get_active_monitor_positions",
    "validate_position_record",
    "validate_positions_file_light",
    "get_positions_summary",
]
