# -*- coding: utf-8 -*-
"""Persistent JSON storage helpers for the crypto futures bot.

This module is intentionally small and dependency-free because almost every AI
memory layer depends on it.  Design goals:

- Store all relative JSON files under BOT_DATA_DIR (default: data/).
- Avoid accidental data/data/... paths when old modules pass "data/file.json".
- Save atomically with a temporary file + os.replace().
- Keep timestamped backups before overwriting existing files.
- Recover from the latest valid backup if the main JSON file is corrupted.
- Never delete learned data during code updates.
"""

import json
import os
import shutil
import time
from typing import Any, Optional


DATA_DIR = os.getenv("BOT_DATA_DIR", "data")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
MAX_BACKUPS_PER_FILE = int(os.getenv("BOT_DATA_MAX_BACKUPS_PER_FILE", "20") or "20")


def _ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)


_ensure_dirs()


def _path(name: str) -> str:
    """Return the on-disk path for a JSON data file.

    Compatibility note:
    Older files sometimes pass "data/real_trade_state.json" while newer files
    pass "real_trade_state.json".  If BOT_DATA_DIR is "data", blindly joining
    would create "data/data/real_trade_state.json".  This helper prevents that.
    """
    raw = str(name or "").strip()
    if not raw:
        raw = "data.json"

    if os.path.isabs(raw):
        return raw

    norm_raw = os.path.normpath(raw)
    norm_data = os.path.normpath(DATA_DIR)

    if norm_raw == norm_data or norm_raw.startswith(norm_data + os.sep):
        return norm_raw

    return os.path.join(DATA_DIR, norm_raw)


def _backup_glob_prefix(path: str) -> str:
    return os.path.basename(path) + "."


def _list_backups_for(path: str) -> list[str]:
    _ensure_dirs()
    prefix = _backup_glob_prefix(path)
    try:
        rows = [
            os.path.join(BACKUP_DIR, fn)
            for fn in os.listdir(BACKUP_DIR)
            if fn.startswith(prefix) and fn.endswith(".bak")
        ]
    except Exception:
        return []
    rows.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    return rows


def _load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _latest_valid_backup(path: str) -> Optional[Any]:
    for backup_path in _list_backups_for(path):
        try:
            return _load_json_file(backup_path)
        except Exception:
            continue
    return None


def _cleanup_old_backups(path: str) -> None:
    if MAX_BACKUPS_PER_FILE <= 0:
        return
    backups = _list_backups_for(path)
    for old in backups[MAX_BACKUPS_PER_FILE:]:
        try:
            os.remove(old)
        except Exception:
            pass


def load_json(name: str, default: Any = None) -> Any:
    """Load JSON safely.

    If the main file is missing or unreadable, returns default.  If the main
    file is corrupted but a valid backup exists, returns the newest valid backup
    instead of silently losing the AI memory.
    """
    path = _path(name)
    if default is None:
        default = {}

    try:
        if not os.path.exists(path):
            return default
        return _load_json_file(path)
    except Exception:
        recovered = _latest_valid_backup(path)
        if recovered is not None:
            return recovered
        return default


def save_json(name: str, data: Any, backup: bool = True) -> bool:
    """Atomically save JSON with optional backup.

    Returns True on success.  Exceptions during the final write are not hidden;
    that is intentional so the caller/logs can reveal disk permission or space
    problems instead of pretending the AI memory was saved.
    """
    path = _path(name)
    _ensure_dirs()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    if backup and os.path.exists(path):
        try:
            base = os.path.basename(path)
            backup_name = f"{base}.{int(time.time())}.bak"
            backup_path = os.path.join(BACKUP_DIR, backup_name)
            shutil.copy2(path, backup_path)
            _cleanup_old_backups(path)
        except Exception:
            # Backup failure should not stop the main atomic save.
            pass

    tmp = f"{path}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass

    os.replace(tmp, path)
    return True


# Backward-compatible aliases some modules may import.
read_json = load_json
write_json = save_json
