"""
users.py
Level 4 / 1H Smart Scalp Bot

User access control without config.py.

Architecture lock:
- Owner-only access by default.
- Reads OWNER_ID from environment.
- Stores allowed users through state_store.py.
- Does not import config.py, data_store.py, or diagnostics.py.
"""

from __future__ import annotations

import os
from typing import Any

from constants import STATUS_FAILED, STATUS_OK, SYSTEM_VERSION
from models import RecordResult
from state_store import load_json, save_json_atomic, log_error
from utils import safe_int, safe_str, utc_now_iso


USERS_VERSION: str = SYSTEM_VERSION
USERS_KEY: str = "users"


def get_owner_id(default: int = 0) -> int:
    return safe_int(os.getenv("OWNER_ID") or os.getenv("TELEGRAM_OWNER_ID"), default) or default


def default_users_state() -> dict[str, Any]:
    return {"system_version": SYSTEM_VERSION, "owner_id": get_owner_id(0), "allowed_users": [], "updated_at": utc_now_iso()}


def normalize_users_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        state = {}
    data = default_users_state()
    data.update(state)
    data["system_version"] = safe_str(data.get("system_version"), SYSTEM_VERSION)
    data["owner_id"] = safe_int(data.get("owner_id"), 0) or get_owner_id(0)
    allowed: list[int] = []
    for item in data.get("allowed_users", []):
        uid = safe_int(item, None)
        if uid is not None and uid > 0 and uid not in allowed:
            allowed.append(uid)
    data["allowed_users"] = sorted(allowed)
    data["updated_at"] = safe_str(data.get("updated_at"), utc_now_iso())
    return data


def load_users() -> dict[str, Any]:
    state = load_json(USERS_KEY, default=default_users_state())
    normalized = normalize_users_state(state)
    if state != normalized:
        save_users(normalized)
    return normalized


def save_users(state: dict[str, Any]) -> bool:
    data = normalize_users_state(state)
    data["updated_at"] = utc_now_iso()
    return save_json_atomic(USERS_KEY, data)


def is_owner(user_id: Any) -> bool:
    uid = safe_int(user_id, 0) or 0
    owner = safe_int(load_users().get("owner_id"), 0) or get_owner_id(0)
    return owner > 0 and uid == owner


def is_allowed(user_id: Any) -> bool:
    uid = safe_int(user_id, 0) or 0
    if uid <= 0:
        return False
    if is_owner(uid):
        return True
    return uid in [safe_int(x, 0) for x in load_users().get("allowed_users", [])]


def add_user(user_id: Any) -> RecordResult:
    try:
        uid = safe_int(user_id, 0) or 0
        if uid <= 0:
            return RecordResult(status=STATUS_FAILED, recorded=False, message="invalid_user_id", error="user_id must be positive")
        state = load_users()
        users = set(safe_int(x, 0) or 0 for x in state.get("allowed_users", []))
        users.add(uid)
        users.discard(0)
        state["allowed_users"] = sorted(users)
        ok = save_users(state)
        return RecordResult(status=STATUS_OK if ok else STATUS_FAILED, recorded=ok, message="user_added" if ok else "user_add_failed", metadata={"user_id": uid})
    except Exception as exc:
        log_error("users", "add_user", exc)
        return RecordResult(status=STATUS_FAILED, recorded=False, message="user_add_exception", error=str(exc))


def remove_user(user_id: Any) -> RecordResult:
    try:
        uid = safe_int(user_id, 0) or 0
        state = load_users()
        state["allowed_users"] = [safe_int(x, 0) for x in state.get("allowed_users", []) if safe_int(x, 0) != uid]
        ok = save_users(state)
        return RecordResult(status=STATUS_OK if ok else STATUS_FAILED, recorded=ok, message="user_removed" if ok else "user_remove_failed", metadata={"user_id": uid})
    except Exception as exc:
        log_error("users", "remove_user", exc)
        return RecordResult(status=STATUS_FAILED, recorded=False, message="user_remove_exception", error=str(exc))


def list_users() -> list[int]:
    return [safe_int(x, 0) or 0 for x in load_users().get("allowed_users", []) if safe_int(x, 0)]


def list_users_fa() -> str:
    state = load_users()
    users = list_users()
    lines = [f"👤 مالک: {state.get('owner_id') or 'تنظیم نشده'}"]
    lines.append("کاربران مجاز: " + "، ".join(str(x) for x in users) if users else "کاربر مجاز اضافه نشده.")
    return "\n".join(lines)


def initialize() -> bool:
    return save_users(load_users())


def validate_users_light() -> dict[str, Any]:
    state = load_users()
    errors: list[str] = []
    if state.get("system_version") != SYSTEM_VERSION:
        errors.append("INVALID_SYSTEM_VERSION")
    if not isinstance(state.get("allowed_users"), list):
        errors.append("ALLOWED_USERS_NOT_LIST")
    return {"status": STATUS_OK if not errors else STATUS_FAILED, "valid": not errors, "errors": errors, "checked_at": utc_now_iso()}


__all__ = [
    "USERS_VERSION", "USERS_KEY", "get_owner_id", "default_users_state", "normalize_users_state",
    "load_users", "save_users", "is_owner", "is_allowed", "add_user", "remove_user", "list_users",
    "list_users_fa", "initialize", "validate_users_light",
]
