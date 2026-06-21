from __future__ import annotations

"""
Safe persistence layer.

Responsibilities:
- atomic JSON writes
- safe JSON reads with defaults
- automatic directory creation
- timestamped backups
- corruption recovery attempts
- lightweight cache helpers

Rules:
- Do not import high-level bot modules here.
- This module can be used by AI memory, ghost, tracker, trade manager, etc.
"""

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, List, Callable

from config import DATA_DIR, BACKUP_DIR, ensure_directories


def now_ts() -> int:
    return int(time.time())


def utc_ms() -> int:
    return int(time.time() * 1000)


def _path(path_or_name: str | Path) -> Path:
    ensure_directories()
    p = Path(path_or_name)
    if p.suffix:
        return p
    return DATA_DIR / f"{p.name}.json"


def json_default(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def load_json(path_or_name: str | Path, default: Any = None) -> Any:
    p = _path(path_or_name)
    if default is None:
        default = {}
    if not p.exists():
        return default
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        # Corrupted file: backup it and return default.
        backup_file(p, suffix="corrupt")
        return default
    except Exception:
        return default


def save_json(path_or_name: str | Path, data: Any, make_backup: bool = False) -> bool:
    p = _path(path_or_name)
    ensure_directories()
    p.parent.mkdir(parents=True, exist_ok=True)

    if make_backup and p.exists():
        backup_file(p)

    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=json_default)
            f.write("\n")
        os.replace(str(tmp), str(p))
        return True
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def backup_file(path_or_name: str | Path, suffix: str = "bak") -> str:
    p = _path(path_or_name)
    ensure_directories()
    if not p.exists():
        return ""
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    backup_name = f"{p.stem}.{ts}.{suffix}{p.suffix}"
    dst = BACKUP_DIR / backup_name
    try:
        shutil.copy2(str(p), str(dst))
        return str(dst)
    except Exception:
        return ""


def append_jsonl(path_or_name: str | Path, row: Dict[str, Any]) -> bool:
    p = _path(path_or_name)
    if p.suffix != ".jsonl":
        p = p.with_suffix(".jsonl")
    ensure_directories()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=json_default) + "\n")
        return True
    except Exception:
        return False


def load_list(path_or_name: str | Path) -> List[Any]:
    data = load_json(path_or_name, default=[])
    return data if isinstance(data, list) else []


def load_dict(path_or_name: str | Path) -> Dict[str, Any]:
    data = load_json(path_or_name, default={})
    return data if isinstance(data, dict) else {}


def update_json(path_or_name: str | Path, updater: Callable[[Any], Any], default: Any = None, make_backup: bool = True) -> Any:
    data = load_json(path_or_name, default=default if default is not None else {})
    new_data = updater(data)
    save_json(path_or_name, new_data, make_backup=make_backup)
    return new_data


def ensure_json_file(path_or_name: str | Path, default: Any) -> Path:
    p = _path(path_or_name)
    if not p.exists():
        save_json(p, default)
    return p


def prune_records(records: List[Dict[str, Any]], max_records: int = 10000) -> List[Dict[str, Any]]:
    if len(records) <= max_records:
        return records
    return records[-max_records:]


def cache_get(cache_name: str, key: str, ttl_seconds: int, default: Any = None) -> Any:
    cache = load_dict(cache_name)
    item = cache.get(key)
    if not isinstance(item, dict):
        return default
    if now_ts() - int(item.get("ts", 0)) > ttl_seconds:
        return default
    return item.get("value", default)


def cache_set(cache_name: str, key: str, value: Any) -> bool:
    cache = load_dict(cache_name)
    cache[key] = {"ts": now_ts(), "value": value}
    return save_json(cache_name, cache)


def initialize_data_files(defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ensure_directories()
    defaults = defaults or {}
    created = []
    for name, default in defaults.items():
        p = ensure_json_file(name, default)
        created.append(str(p))
    return {"ok": True, "created_or_existing": created}
