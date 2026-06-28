from __future__ import annotations

import os


def owner_id() -> str:
    return str(os.getenv("OWNER_ID") or os.getenv("TELEGRAM_CHAT_ID") or "")


def is_owner(user_id: int | str) -> bool:
    return str(user_id) == owner_id()
