"""
signal_manager.py
Level 4 / 1H Smart Scalp Bot

Signal record manager.

Architecture lock:
- Owns high-level signal records in signals.json.
- Uses state_store.py for actual JSON IO.
- Does not run AI, fetch market data, place orders, monitor positions, or build Telegram text.
- Allowed project imports: constants.py, state_store.py, models.py, utils.py only.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from constants import (
    EVENT_GHOST_OPENED,
    EVENT_REAL_OPEN_CONFIRMED,
    EVENT_REAL_OPEN_FAILED,
    EVENT_REAL_OPEN_REQUESTED,
    EVENT_REJECTED,
    EVENT_SIGNAL_CREATED,
    MODE_GHOST,
    MODE_REAL,
    MODE_REJECT,
    STATUS_FAILED,
    STATUS_OK,
    SYSTEM_VERSION,
)
from models import AIDecision, RecordResult, TPSLPlan, from_dict, to_dict
from state_store import load_json, save_json_atomic, append_record, log_error
from utils import (
    make_event_id,
    make_signal_id,
    normalize_direction,
    normalize_symbol,
    safe_float,
    safe_int,
    safe_str,
    utc_now_iso,
)


SIGNAL_MANAGER_VERSION: str = SYSTEM_VERSION
SIGNALS_KEY: str = "signals"


# =============================================================================
# Internal helpers
# =============================================================================

def _empty_signals_payload() -> dict[str, Any]:
    return {
        "system_version": SYSTEM_VERSION,
        "signals": [],
        "updated_at": utc_now_iso(),
    }


def _load_payload() -> dict[str, Any]:
    data = load_json(SIGNALS_KEY, default=_empty_signals_payload())
    if not isinstance(data, dict):
        return _empty_signals_payload()
    if not isinstance(data.get("signals"), list):
        data["signals"] = []
    data.setdefault("system_version", SYSTEM_VERSION)
    return data


def _save_payload(payload: Mapping[str, Any]) -> bool:
    data = dict(payload)
    data.setdefault("system_version", SYSTEM_VERSION)
    data["updated_at"] = utc_now_iso()
    return save_json_atomic(SIGNALS_KEY, data)


def _extract_tp_sl_payload(decision: AIDecision) -> dict[str, Any]:
    if decision.tp_sl is None:
        return {}
    if isinstance(decision.tp_sl, TPSLPlan):
        return to_dict(decision.tp_sl)
    if isinstance(decision.tp_sl, dict):
        return dict(decision.tp_sl)
    return {}


def _normalize_signal_record(record: Mapping[str, Any]) -> dict[str, Any]:
    signal_id = safe_str(record.get("signal_id"))
    symbol = normalize_symbol(record.get("symbol"))
    direction = normalize_direction(record.get("direction"))
    level = safe_int(record.get("level"), 4) or 4

    if not signal_id:
        signal_id = make_signal_id(symbol, direction, level)

    mode = safe_str(record.get("mode")).upper()
    if mode not in {MODE_REAL, MODE_GHOST, MODE_REJECT}:
        mode = MODE_REJECT

    normalized = dict(record)
    normalized.update(
        {
            "system_version": safe_str(record.get("system_version"), SYSTEM_VERSION) or SYSTEM_VERSION,
            "signal_id": signal_id,
            "symbol": symbol,
            "direction": direction,
            "mode": mode,
            "level": level,
            "score": safe_float(record.get("score"), 0.0) or 0.0,
            "confidence": safe_float(record.get("confidence"), 0.0) or 0.0,
            "entry": safe_float(record.get("entry"), 0.0) or 0.0,
            "created_at": safe_str(record.get("created_at"), utc_now_iso()),
            "updated_at": utc_now_iso(),
        }
    )

    normalized.setdefault("events", [])
    normalized.setdefault("reason_codes", [])
    normalized.setdefault("metadata", {})
    normalized.setdefault("status", "CREATED")
    return normalized


def _record_from_decision(decision: AIDecision, *, signal_message_id: Optional[int] = None) -> dict[str, Any]:
    event = EVENT_SIGNAL_CREATED
    if decision.mode == MODE_REAL:
        event = EVENT_REAL_OPEN_REQUESTED
    elif decision.mode == MODE_GHOST:
        event = EVENT_GHOST_OPENED
    elif decision.mode == MODE_REJECT:
        event = EVENT_REJECTED

    record = {
        "system_version": SYSTEM_VERSION,
        "signal_id": decision.signal_id,
        "symbol": decision.symbol,
        "direction": decision.direction,
        "mode": decision.mode,
        "level": decision.level,
        "score": decision.score,
        "confidence": decision.confidence,
        "entry": decision.entry,
        "tp_sl": _extract_tp_sl_payload(decision),
        "reason_codes": list(decision.reason_codes),
        "reject_reason": decision.reject_reason,
        "metadata": dict(decision.metadata),
        "signal_message_id": signal_message_id,
        "status": "CREATED",
        "events": [
            {
                "event_id": make_event_id(event),
                "event": event,
                "created_at": utc_now_iso(),
                "metadata": {},
            }
        ],
        "created_at": decision.created_at,
        "updated_at": utc_now_iso(),
    }
    return _normalize_signal_record(record)


def _find_index(records: list[dict[str, Any]], signal_id: str) -> int:
    sid = safe_str(signal_id)
    for idx, item in enumerate(records):
        if safe_str(item.get("signal_id")) == sid:
            return idx
    return -1


# =============================================================================
# Read operations
# =============================================================================

def load_signals() -> list[dict[str, Any]]:
    """Load all signal records as normalized dictionaries."""
    payload = _load_payload()
    result: list[dict[str, Any]] = []
    for item in payload.get("signals", []):
        if isinstance(item, dict):
            result.append(_normalize_signal_record(item))
    return result


def get_signal(signal_id: str) -> Optional[dict[str, Any]]:
    """Return one signal by id."""
    sid = safe_str(signal_id)
    for signal in load_signals():
        if safe_str(signal.get("signal_id")) == sid:
            return signal
    return None


def get_signals_by_symbol(symbol: str, *, direction: str = "", mode: str = "") -> list[dict[str, Any]]:
    """Return signals filtered by symbol/direction/mode."""
    symbol_norm = normalize_symbol(symbol)
    direction_norm = normalize_direction(direction) if direction else ""
    mode_norm = safe_str(mode).upper() if mode else ""

    result: list[dict[str, Any]] = []
    for signal in load_signals():
        if signal.get("symbol") != symbol_norm:
            continue
        if direction_norm and signal.get("direction") != direction_norm:
            continue
        if mode_norm and signal.get("mode") != mode_norm:
            continue
        result.append(signal)
    return result


def get_recent_signals(limit: int = 20, *, mode: str = "") -> list[dict[str, Any]]:
    """Return most recent signals."""
    max_items = max(1, safe_int(limit, 20) or 20)
    mode_norm = safe_str(mode).upper() if mode else ""

    signals = load_signals()
    if mode_norm:
        signals = [s for s in signals if s.get("mode") == mode_norm]

    return signals[-max_items:]


def has_signal(signal_id: str) -> bool:
    return get_signal(signal_id) is not None


# =============================================================================
# Write operations
# =============================================================================

def save_signals(signals: list[Mapping[str, Any]]) -> bool:
    """Replace the full signals list with normalized records."""
    payload = _empty_signals_payload()
    payload["signals"] = [_normalize_signal_record(item) for item in signals if isinstance(item, Mapping)]
    return _save_payload(payload)


def record_signal(decision: AIDecision | Mapping[str, Any], *, signal_message_id: Optional[int] = None) -> RecordResult:
    """Record a new signal from AIDecision or mapping."""
    try:
        if isinstance(decision, AIDecision):
            record = _record_from_decision(decision, signal_message_id=signal_message_id)
        elif isinstance(decision, Mapping):
            record = _normalize_signal_record(decision)
            if signal_message_id is not None:
                record["signal_message_id"] = signal_message_id
        else:
            return RecordResult(
                status=STATUS_FAILED,
                recorded=False,
                message="invalid_signal_input",
                error="decision must be AIDecision or mapping",
            )

        payload = _load_payload()
        records = payload.get("signals", [])
        idx = _find_index(records, record["signal_id"])
        if idx >= 0:
            return RecordResult(
                status=STATUS_FAILED,
                recorded=False,
                record_id=record["signal_id"],
                message="signal_id_exists",
                error="signal already exists",
            )

        records.append(record)
        payload["signals"] = records
        ok = _save_payload(payload)

        return RecordResult(
            status=STATUS_OK if ok else STATUS_FAILED,
            recorded=ok,
            record_id=record["signal_id"],
            message="signal_recorded" if ok else "signal_record_failed",
            metadata={"symbol": record["symbol"], "direction": record["direction"], "mode": record["mode"]},
        )

    except Exception as exc:
        log_error(module="signal_manager", function="record_signal", error=exc)
        return RecordResult(status=STATUS_FAILED, recorded=False, message="signal_record_exception", error=str(exc))


def upsert_signal(record: Mapping[str, Any]) -> RecordResult:
    """Insert or update a signal record by signal_id."""
    try:
        normalized = _normalize_signal_record(record)
        payload = _load_payload()
        records = payload.get("signals", [])
        idx = _find_index(records, normalized["signal_id"])
        updated = idx >= 0

        if updated:
            records[idx] = normalized
        else:
            records.append(normalized)

        payload["signals"] = records
        ok = _save_payload(payload)
        return RecordResult(
            status=STATUS_OK if ok else STATUS_FAILED,
            recorded=ok,
            record_id=normalized["signal_id"],
            message="signal_updated" if updated and ok else "signal_created" if ok else "signal_save_failed",
            metadata={"updated": updated},
        )
    except Exception as exc:
        log_error(module="signal_manager", function="upsert_signal", error=exc)
        return RecordResult(status=STATUS_FAILED, recorded=False, message="signal_upsert_exception", error=str(exc))


def update_signal(signal_id: str, updates: Mapping[str, Any]) -> RecordResult:
    """Patch one signal."""
    sid = safe_str(signal_id)
    payload = _load_payload()
    records = payload.get("signals", [])
    idx = _find_index(records, sid)

    if idx < 0:
        return RecordResult(status=STATUS_FAILED, recorded=False, record_id=sid, message="signal_not_found")

    merged = dict(records[idx])
    merged.update(dict(updates))
    records[idx] = _normalize_signal_record(merged)
    payload["signals"] = records
    ok = _save_payload(payload)
    return RecordResult(
        status=STATUS_OK if ok else STATUS_FAILED,
        recorded=ok,
        record_id=sid,
        message="signal_updated" if ok else "signal_update_failed",
    )


def attach_message_id(signal_id: str, message_id: Any) -> RecordResult:
    """Attach Telegram signal message id to a signal."""
    mid = safe_int(message_id, None)
    if mid is None:
        return RecordResult(status=STATUS_FAILED, recorded=False, record_id=signal_id, message="invalid_message_id")
    return update_signal(signal_id, {"signal_message_id": mid})


def append_signal_event(
    signal_id: str,
    event: str,
    *,
    metadata: Optional[Mapping[str, Any]] = None,
    status: str = "",
) -> RecordResult:
    """Append an event to a signal history."""
    sid = safe_str(signal_id)
    payload = _load_payload()
    records = payload.get("signals", [])
    idx = _find_index(records, sid)

    if idx < 0:
        return RecordResult(status=STATUS_FAILED, recorded=False, record_id=sid, message="signal_not_found")

    signal = _normalize_signal_record(records[idx])
    events = signal.get("events")
    if not isinstance(events, list):
        events = []

    event_name = safe_str(event).upper()
    events.append(
        {
            "event_id": make_event_id(event_name),
            "event": event_name,
            "created_at": utc_now_iso(),
            "metadata": dict(metadata or {}),
        }
    )
    signal["events"] = events
    signal["last_event"] = event_name
    if status:
        signal["status"] = safe_str(status).upper()
    signal["updated_at"] = utc_now_iso()

    records[idx] = signal
    payload["signals"] = records
    ok = _save_payload(payload)

    return RecordResult(
        status=STATUS_OK if ok else STATUS_FAILED,
        recorded=ok,
        record_id=sid,
        message="signal_event_appended" if ok else "signal_event_append_failed",
        metadata={"event": event_name},
    )


def mark_real_open_requested(signal_id: str, metadata: Optional[Mapping[str, Any]] = None) -> RecordResult:
    return append_signal_event(signal_id, EVENT_REAL_OPEN_REQUESTED, metadata=metadata, status="REAL_OPEN_REQUESTED")


def mark_real_open_confirmed(signal_id: str, metadata: Optional[Mapping[str, Any]] = None) -> RecordResult:
    return append_signal_event(signal_id, EVENT_REAL_OPEN_CONFIRMED, metadata=metadata, status="REAL_OPEN_CONFIRMED")


def mark_real_open_failed(signal_id: str, metadata: Optional[Mapping[str, Any]] = None) -> RecordResult:
    return append_signal_event(signal_id, EVENT_REAL_OPEN_FAILED, metadata=metadata, status="REAL_OPEN_FAILED")


def mark_ghost_opened(signal_id: str, metadata: Optional[Mapping[str, Any]] = None) -> RecordResult:
    return append_signal_event(signal_id, EVENT_GHOST_OPENED, metadata=metadata, status="GHOST_OPENED")


def mark_rejected(signal_id: str, reason: str = "") -> RecordResult:
    return append_signal_event(signal_id, EVENT_REJECTED, metadata={"reason": reason}, status="REJECTED")


def remove_signal(signal_id: str) -> RecordResult:
    """Remove one signal record. Usually only used for cleanup/testing."""
    sid = safe_str(signal_id)
    payload = _load_payload()
    records = payload.get("signals", [])
    new_records = [item for item in records if safe_str(item.get("signal_id")) != sid]

    if len(new_records) == len(records):
        return RecordResult(status=STATUS_FAILED, recorded=False, record_id=sid, message="signal_not_found")

    payload["signals"] = new_records
    ok = _save_payload(payload)
    return RecordResult(
        status=STATUS_OK if ok else STATUS_FAILED,
        recorded=ok,
        record_id=sid,
        message="signal_removed" if ok else "signal_remove_failed",
    )


# =============================================================================
# Validation / summaries
# =============================================================================

def validate_signal_record(signal: Mapping[str, Any]) -> dict[str, Any]:
    """Lightweight validation for one signal record."""
    record = _normalize_signal_record(signal)
    errors: list[str] = []

    if not record.get("signal_id"):
        errors.append("missing_signal_id")
    if not record.get("symbol"):
        errors.append("missing_symbol")
    if record.get("mode") not in {MODE_REAL, MODE_GHOST, MODE_REJECT}:
        errors.append("invalid_mode")
    if record.get("mode") != MODE_REJECT and record.get("direction") not in {"LONG", "SHORT"}:
        errors.append("invalid_direction")
    if record.get("mode") != MODE_REJECT and safe_float(record.get("entry"), 0.0) <= 0:
        errors.append("invalid_entry")

    return {
        "valid": not errors,
        "errors": errors,
        "signal_id": record.get("signal_id"),
        "symbol": record.get("symbol"),
        "direction": record.get("direction"),
        "mode": record.get("mode"),
    }


def validate_signals_file_light() -> dict[str, Any]:
    """Lightweight validation for startup preflight."""
    signals = load_signals()
    validations = [validate_signal_record(s) for s in signals]
    invalid = [v for v in validations if not v["valid"]]

    return {
        "status": STATUS_OK if not invalid else STATUS_FAILED,
        "system_version": SYSTEM_VERSION,
        "total": len(signals),
        "invalid_count": len(invalid),
        "invalid": invalid,
        "checked_at": utc_now_iso(),
    }


def get_signals_summary() -> dict[str, Any]:
    """Return lightweight signal summary."""
    signals = load_signals()
    summary = {
        "system_version": SYSTEM_VERSION,
        "total": len(signals),
        "real": 0,
        "ghost": 0,
        "reject": 0,
        "by_symbol": {},
        "updated_at": utc_now_iso(),
    }

    for signal in signals:
        mode = signal.get("mode")
        symbol = signal.get("symbol")
        if mode == MODE_REAL:
            summary["real"] += 1
        elif mode == MODE_GHOST:
            summary["ghost"] += 1
        elif mode == MODE_REJECT:
            summary["reject"] += 1

        if symbol:
            summary["by_symbol"][symbol] = summary["by_symbol"].get(symbol, 0) + 1

    return summary


__all__ = [
    "SIGNAL_MANAGER_VERSION",
    "SIGNALS_KEY",
    "load_signals",
    "get_signal",
    "get_signals_by_symbol",
    "get_recent_signals",
    "has_signal",
    "save_signals",
    "record_signal",
    "upsert_signal",
    "update_signal",
    "attach_message_id",
    "append_signal_event",
    "mark_real_open_requested",
    "mark_real_open_confirmed",
    "mark_real_open_failed",
    "mark_ghost_opened",
    "mark_rejected",
    "remove_signal",
    "validate_signal_record",
    "validate_signals_file_light",
    "get_signals_summary",
]
