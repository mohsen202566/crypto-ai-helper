from __future__ import annotations

"""
User access control.

Responsibilities:
- Owner-only access by default.
- Allowed users list.
- Commands: /id, /adduser, /removeuser, /listusers.
"""

import time
from typing import Any, Dict, List

from config import OWNER_ID, CORE_DATA_FILES
from data_store import load_dict, save_json
from diagnostics import safe


USERS_FILE = "users"


def _ts() -> int:
    return int(time.time())


def _empty_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "owner_id": OWNER_ID,
        "allowed_users": [],
        "updated_at": _ts(),
    }


@safe(default={})
def load_users() -> Dict[str, Any]:
    st = load_dict(USERS_FILE)
    if not st:
        st = _empty_state()
        save_json(USERS_FILE, st)
    for k, v in _empty_state().items():
        st.setdefault(k, v)
    if OWNER_ID and not st.get("owner_id"):
        st["owner_id"] = OWNER_ID
    return st


@safe(default=False)
def save_users(st: Dict[str, Any]) -> bool:
    st["updated_at"] = _ts()
    return save_json(USERS_FILE, st, make_backup=True)


@safe(default=False)
def is_owner(user_id: int) -> bool:
    st = load_users()
    return int(user_id or 0) == int(st.get("owner_id") or OWNER_ID or 0)


@safe(default=False)
def is_allowed(user_id: int) -> bool:
    st = load_users()
    uid = int(user_id or 0)
    if uid == int(st.get("owner_id") or OWNER_ID or 0):
        return True
    return uid in [int(x) for x in st.get("allowed_users", [])]


@safe(default=True)
def add_user(user_id: int) -> bool:
    st = load_users()
    uid = int(user_id)
    users = set(int(x) for x in st.get("allowed_users", []))
    users.add(uid)
    st["allowed_users"] = sorted(users)
    save_users(st)
    return True


@safe(default=True)
def remove_user(user_id: int) -> bool:
    st = load_users()
    uid = int(user_id)
    st["allowed_users"] = [int(x) for x in st.get("allowed_users", []) if int(x) != uid]
    save_users(st)
    return True


@safe(default=[])
def list_users() -> List[int]:
    return [int(x) for x in load_users().get("allowed_users", [])]


@safe(default="")
def list_users_fa() -> str:
    st = load_users()
    allowed = list_users()
    lines = [f"👤 مالک: {st.get('owner_id') or OWNER_ID or 'تنظیم نشده'}"]
    if allowed:
        lines.append("کاربران مجاز: " + "، ".join(str(x) for x in allowed))
    else:
        lines.append("کاربر مجاز اضافه نشده.")
    return "\n".join(lines)


@safe(default=True)
def initialize() -> bool:
    st = load_users()
    save_users(st)
    return True
