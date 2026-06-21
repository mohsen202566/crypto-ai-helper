from __future__ import annotations

"""
Slot Manager.

Responsibilities:
- Track open/pending slots for Paper/Real/Ghost-aware flow.
- Prevent real slot from being released too early.
- Hold PENDING_REAL_CONFIRM for 60-70 seconds unless real position is confirmed/failed.
- Pick best candidate for free slot via AI/rotation priority.
- Track correlation exposure state.

This module does not call Toobit and does not send Telegram messages.
real_position_sync / real_trade_manager will update slot states.
"""

import time
import uuid
from typing import Any, Dict, List, Optional

from config import CORE_DATA_FILES, DEFAULT_MAX_POSITIONS, REAL_CONFIRM_TIMEOUT_SECONDS
from data_store import load_dict, save_json
from diagnostics import safe
import coin_rotation


SLOT_FILE = CORE_DATA_FILES.get("active_signals")


STATUS_OPEN = "OPEN"
STATUS_PENDING_REAL_CONFIRM = "PENDING_REAL_CONFIRM"
STATUS_CLOSING = "CLOSING"
STATUS_CLOSED = "CLOSED"
STATUS_FAILED = "FAILED"


def _ts() -> int:
    return int(time.time())


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _empty_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "created_at": _ts(),
        "updated_at": _ts(),
        "max_positions": DEFAULT_MAX_POSITIONS,
        "slots": {},
        "history": [],
        "settings": {
            "pending_confirm_timeout": REAL_CONFIRM_TIMEOUT_SECONDS,
            "release_failed_after": REAL_CONFIRM_TIMEOUT_SECONDS,
        },
    }


@safe(default={})
def load_slots() -> Dict[str, Any]:
    st = load_dict(SLOT_FILE)
    if not st:
        st = _empty_state()
        save_json(SLOT_FILE, st)
    for k, v in _empty_state().items():
        st.setdefault(k, v)
    st.setdefault("settings", {}).setdefault("pending_confirm_timeout", REAL_CONFIRM_TIMEOUT_SECONDS)
    return st


@safe(default=False)
def save_slots(st: Dict[str, Any], make_backup: bool = False) -> bool:
    st["updated_at"] = _ts()
    if isinstance(st.get("history"), list):
        st["history"] = st["history"][-1000:]
    return save_json(SLOT_FILE, st, make_backup=make_backup)


@safe(default=True)
def set_max_positions(n: int) -> bool:
    st = load_slots()
    n = max(1, int(n))
    st["max_positions"] = n
    save_slots(st)
    return True


@safe(default={})
def slot_state() -> Dict[str, Any]:
    cleanup_expired_pending()
    st = load_slots()
    slots = st.get("slots", {})
    active = [s for s in slots.values() if s.get("status") in {STATUS_OPEN, STATUS_PENDING_REAL_CONFIRM, STATUS_CLOSING}]
    pending = [s for s in active if s.get("status") == STATUS_PENDING_REAL_CONFIRM]
    open_pos = [s for s in active if s.get("status") == STATUS_OPEN]
    max_pos = int(st.get("max_positions", DEFAULT_MAX_POSITIONS))
    used = len(active)
    free = max(0, max_pos - used)
    return {
        "max_positions": max_pos,
        "used_slots": used,
        "free_slots": free,
        "open_count": len(open_pos),
        "pending_count": len(pending),
        "open_positions": open_pos,
        "pending_positions": pending,
        "can_open": free > 0,
        "updated_at": st.get("updated_at"),
    }


@safe(default="")
def reserve_slot(
    symbol: str,
    direction: str,
    mode: str = "PAPER",
    status: str = STATUS_OPEN,
    ai_record_id: str = "",
    signal_id: str = "",
    telegram_message_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Reserve a slot before/after order request.
    For real orders use status=PENDING_REAL_CONFIRM first.
    """
    st = load_slots()
    state = slot_state()
    if state.get("free_slots", 0) <= 0:
        return ""

    sid = signal_id or f"slot_{int(time.time())}_{str(symbol).upper()}_{uuid.uuid4().hex[:8]}"
    rec = {
        "slot_id": sid,
        "signal_id": signal_id or sid,
        "ai_record_id": ai_record_id,
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "mode": str(mode).upper(),
        "status": status,
        "created_at": _ts(),
        "updated_at": _ts(),
        "pending_since": _ts() if status == STATUS_PENDING_REAL_CONFIRM else 0,
        "telegram_message_id": telegram_message_id,
        "metadata": metadata or {},
    }
    st["slots"][sid] = rec
    _history(st, "RESERVE", rec)
    save_slots(st)
    return sid


@safe(default=False)
def confirm_slot(slot_id: str, exchange_position_id: str = "", metadata: Optional[Dict[str, Any]] = None) -> bool:
    st = load_slots()
    rec = st.get("slots", {}).get(slot_id)
    if not rec:
        return False
    rec["status"] = STATUS_OPEN
    rec["exchange_position_id"] = exchange_position_id
    rec["confirmed_at"] = _ts()
    rec["updated_at"] = _ts()
    if metadata:
        rec.setdefault("metadata", {}).update(metadata)
    st["slots"][slot_id] = rec
    _history(st, "CONFIRM", rec)
    save_slots(st)
    return True


@safe(default=False)
def mark_closing(slot_id: str, reason: str = "") -> bool:
    st = load_slots()
    rec = st.get("slots", {}).get(slot_id)
    if not rec:
        return False
    rec["status"] = STATUS_CLOSING
    rec["closing_since"] = _ts()
    rec["closing_reason"] = reason
    rec["updated_at"] = _ts()
    st["slots"][slot_id] = rec
    _history(st, "CLOSING", rec)
    save_slots(st)
    return True


@safe(default=False)
def release_slot(slot_id: str, reason: str = "", result: str = "") -> bool:
    st = load_slots()
    rec = st.get("slots", {}).pop(slot_id, None)
    if not rec:
        return False
    rec["status"] = STATUS_CLOSED if result else STATUS_FAILED if reason else STATUS_CLOSED
    rec["released_at"] = _ts()
    rec["release_reason"] = reason
    rec["result"] = result
    rec["updated_at"] = _ts()
    _history(st, "RELEASE", rec)
    save_slots(st, make_backup=True)
    return True


@safe(default=0)
def cleanup_expired_pending() -> int:
    """
    Release only pending slots that exceeded timeout and were not confirmed.
    This prevents the old bug: slot becomes free immediately after order send.
    """
    st = load_slots()
    timeout = int(st.get("settings", {}).get("pending_confirm_timeout", REAL_CONFIRM_TIMEOUT_SECONDS))
    now = _ts()
    to_release = []
    for sid, rec in list(st.get("slots", {}).items()):
        if rec.get("status") != STATUS_PENDING_REAL_CONFIRM:
            continue
        pending_since = int(rec.get("pending_since", rec.get("created_at", now)) or now)
        if now - pending_since >= timeout:
            to_release.append(sid)

    for sid in to_release:
        rec = st["slots"].pop(sid)
        rec["status"] = STATUS_FAILED
        rec["released_at"] = now
        rec["release_reason"] = "pending_confirm_timeout"
        rec["updated_at"] = now
        _history(st, "PENDING_TIMEOUT_RELEASE", rec)

    if to_release:
        save_slots(st, make_backup=True)
    return len(to_release)


def _history(st: Dict[str, Any], event: str, rec: Dict[str, Any]) -> None:
    st.setdefault("history", []).append({
        "ts": _ts(),
        "event": event,
        "slot_id": rec.get("slot_id"),
        "symbol": rec.get("symbol"),
        "direction": rec.get("direction"),
        "mode": rec.get("mode"),
        "status": rec.get("status"),
        "reason": rec.get("release_reason") or rec.get("closing_reason") or "",
    })
    st["history"] = st["history"][-1000:]


@safe(default=[])
def open_slots() -> List[Dict[str, Any]]:
    st = load_slots()
    return sorted(st.get("slots", {}).values(), key=lambda r: r.get("created_at", 0), reverse=True)


@safe(default=[])
def pending_slots() -> List[Dict[str, Any]]:
    return [s for s in open_slots() if s.get("status") == STATUS_PENDING_REAL_CONFIRM]


@safe(default={})
def find_slot_by_signal(signal_id: str) -> Dict[str, Any]:
    for s in open_slots():
        if s.get("signal_id") == signal_id or s.get("ai_record_id") == signal_id:
            return s
    return {}


@safe(default=[])
def choose_for_free_slots(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Choose best candidates for currently free slots.
    Candidate fields: symbol, direction, priority/confidence.
    """
    state = slot_state()
    free = int(state.get("free_slots", 0))
    if free <= 0:
        return []
    ranked = coin_rotation.rank_candidates(
        candidates,
        open_positions=state.get("open_positions", []),
        limit=free,
    )
    return ranked


@safe(default="")
def summary_fa() -> str:
    s = slot_state()
    return (
        "📌 اسلات‌ها\n"
        f"باز/درگیر: {s.get('used_slots',0)}/{s.get('max_positions',0)}\n"
        f"باز: {s.get('open_count',0)} | در انتظار تایید: {s.get('pending_count',0)}\n"
        f"خالی: {s.get('free_slots',0)}"
    )


@safe(default=True)
def initialize() -> bool:
    st = load_slots()
    save_slots(st)
    return True
