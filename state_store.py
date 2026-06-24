"""
state_store.py
Level 4 / 1H Smart Scalp Bot

Safe JSON persistence layer.

Architecture lock:
- This file is the only owner of low-level JSON read/write helpers.
- No AI decision logic, market analysis, order execution, or Telegram message creation here.
- Allowed project imports: constants.py and utils.py only.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from constants import (
    DATA_DIR,
    DATA_FILES,
    ERROR_SEVERITY_ERROR,
    ERROR_SEVERITY_INFO,
    ERROR_SEVERITY_WARNING,
    EVENT_ERROR,
    STATUS_FAILED,
    STATUS_OK,
    SYSTEM_VERSION,
)
from utils import make_event_id, safe_str, utc_now_iso, utc_now_ms


STATE_STORE_VERSION: str = SYSTEM_VERSION


def default_data_for_key(file_key: str) -> Any:
    """Return the initial JSON structure for a known data file key."""
    key = safe_str(file_key)

    defaults = {
        "strategy_state": {
            "system_version": SYSTEM_VERSION,
            "active_level": 4,
            "active_strategy": "LEVEL_4_1H_SMART_SCALP",
            "real_trading_enabled": False,
            "updated_at": utc_now_iso(),
        },
        "positions": {
            "system_version": SYSTEM_VERSION,
            "positions": [],
            "updated_at": utc_now_iso(),
        },
        "signals": {
            "system_version": SYSTEM_VERSION,
            "signals": [],
            "updated_at": utc_now_iso(),
        },
        "learning_memory": {
            "system_version": SYSTEM_VERSION,
            "records": [],
            "coin_stats": {},
            "condition_stats": {},
            "updated_at": utc_now_iso(),
        },
        "ghost_records": {
            "system_version": SYSTEM_VERSION,
            "records": [],
            "updated_at": utc_now_iso(),
        },
        "real_records": {
            "system_version": SYSTEM_VERSION,
            "records": [],
            "updated_at": utc_now_iso(),
        },
        "stats": {
            "system_version": SYSTEM_VERSION,
            "events": [],
            "summary": {},
            "updated_at": utc_now_iso(),
        },
        "errors": {
            "system_version": SYSTEM_VERSION,
            "errors": [],
            "updated_at": utc_now_iso(),
        },
    }

    return defaults.get(
        key,
        {
            "system_version": SYSTEM_VERSION,
            "records": [],
            "updated_at": utc_now_iso(),
        },
    )


def resolve_data_path(file_key_or_path: Any) -> Path:
    """Resolve a DATA_FILES key or raw path to Path."""
    if isinstance(file_key_or_path, Path):
        return file_key_or_path

    key = safe_str(file_key_or_path)
    if key in DATA_FILES:
        return Path(DATA_FILES[key])

    return Path(key)


def get_file_key_for_path(path: Path) -> str:
    """Return DATA_FILES key for a path when known, otherwise stem."""
    p = Path(path).resolve()
    for key, value in DATA_FILES.items():
        try:
            if Path(value).resolve() == p:
                return key
        except OSError:
            continue
    return p.stem


def ensure_data_dir() -> Path:
    """Create data directory if missing and return it."""
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    return Path(DATA_DIR)


def ensure_parent_dir(path: Any) -> Path:
    """Create parent directory for path if missing."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def read_text_safe(path: Any, default: str = "") -> str:
    """Read text safely. Return default on failure."""
    p = Path(path)
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return default


def write_text_atomic(path: Any, text: str) -> bool:
    """
    Atomically write text to path.

    Uses a temporary file in the same directory and os.replace to avoid partial
    writes corrupting JSON files.
    """
    p = ensure_parent_dir(path)
    tmp_path: Optional[Path] = None

    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
        tmp_path = Path(tmp_name)

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(tmp_path, p)
        return True

    except OSError:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def backup_corrupt_file(path: Any) -> Optional[Path]:
    """Move corrupt/unreadable JSON aside with a timestamped .corrupt suffix."""
    p = Path(path)
    if not p.exists():
        return None

    backup = p.with_name(f"{p.name}.corrupt.{utc_now_ms()}")
    try:
        shutil.move(str(p), str(backup))
        return backup
    except OSError:
        return None


def load_json(
    file_key_or_path: Any,
    default: Any = None,
    *,
    create_if_missing: bool = True,
    backup_on_corrupt: bool = True,
) -> Any:
    """
    Load JSON from a known data file key or path.

    If missing and create_if_missing=True, create a default JSON file.
    If corrupt and backup_on_corrupt=True, move corrupt file aside and recreate.
    """
    path = resolve_data_path(file_key_or_path)
    file_key = get_file_key_for_path(path)

    if default is None:
        default = default_data_for_key(file_key)

    if not path.exists():
        if create_if_missing:
            save_json_atomic(path, default)
        return default

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        if backup_on_corrupt:
            backup_corrupt_file(path)
        if create_if_missing:
            save_json_atomic(path, default)
        return default


def save_json_atomic(file_key_or_path: Any, data: Any) -> bool:
    """
    Save JSON atomically.

    This function owns actual JSON writes. Other modules should use this
    instead of writing JSON directly.
    """
    path = resolve_data_path(file_key_or_path)

    try:
        if isinstance(data, dict):
            data = dict(data)
            data.setdefault("system_version", SYSTEM_VERSION)
            data["updated_at"] = utc_now_iso()

        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)
        text += "\n"
        return write_text_atomic(path, text)

    except (TypeError, ValueError, OSError):
        return False


def ensure_json_file(file_key_or_path: Any, default: Any = None) -> bool:
    """Ensure one JSON file exists and is readable."""
    path = resolve_data_path(file_key_or_path)
    file_key = get_file_key_for_path(path)
    if default is None:
        default = default_data_for_key(file_key)

    if not path.exists():
        return save_json_atomic(path, default)

    _ = load_json(path, default=default, create_if_missing=True, backup_on_corrupt=True)
    return path.exists()


def ensure_all_data_files() -> dict[str, bool]:
    """Ensure data directory and all DATA_FILES exist."""
    ensure_data_dir()
    return {key: ensure_json_file(key) for key in DATA_FILES}


def append_record(
    file_key_or_path: Any,
    record: Mapping[str, Any],
    *,
    list_key: str = "records",
    max_records: Optional[int] = None,
) -> bool:
    """
    Append one record to a list inside a JSON file.

    The record receives timestamp/system_version if missing.
    """
    path = resolve_data_path(file_key_or_path)
    file_key = get_file_key_for_path(path)
    data = load_json(path, default=default_data_for_key(file_key))

    if not isinstance(data, dict):
        data = default_data_for_key(file_key)

    items = data.get(list_key)
    if not isinstance(items, list):
        items = []

    new_record = dict(record)
    new_record.setdefault("id", make_event_id(list_key))
    new_record.setdefault("system_version", SYSTEM_VERSION)
    new_record.setdefault("created_at", utc_now_iso())

    items.append(new_record)

    if max_records is not None and max_records > 0 and len(items) > max_records:
        items = items[-max_records:]

    data[list_key] = items
    return save_json_atomic(path, data)


def update_json(
    file_key_or_path: Any,
    updater: Callable[[Any], Any],
    *,
    default: Any = None,
) -> bool:
    """
    Load JSON, pass it to updater, then save returned value atomically.

    updater may mutate and return the same object, or return a new one.
    """
    path = resolve_data_path(file_key_or_path)
    file_key = get_file_key_for_path(path)
    if default is None:
        default = default_data_for_key(file_key)

    data = load_json(path, default=default)
    try:
        updated = updater(data)
        if updated is None:
            updated = data
        return save_json_atomic(path, updated)
    except Exception as exc:
        log_error(
            module="state_store",
            function="update_json",
            error=exc,
            severity=ERROR_SEVERITY_ERROR,
            context={"path": str(path)},
        )
        return False


def set_key(file_key_or_path: Any, key: str, value: Any) -> bool:
    """Set one top-level key in a JSON dict."""
    def _updater(data: Any) -> Any:
        if not isinstance(data, dict):
            data = {}
        data[key] = value
        return data

    return update_json(file_key_or_path, _updater)


def get_key(file_key_or_path: Any, key: str, default: Any = None) -> Any:
    """Read one top-level key from a JSON dict."""
    data = load_json(file_key_or_path)
    if isinstance(data, dict):
        return data.get(key, default)
    return default


def make_error_record(
    *,
    module: str,
    function: str,
    error: Any,
    severity: str = ERROR_SEVERITY_ERROR,
    context: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Create a standard lightweight error record."""
    if isinstance(error, BaseException):
        error_type = type(error).__name__
        error_message = str(error)
        short_trace = "".join(traceback.format_exception_only(type(error), error)).strip()
    else:
        error_type = "Message"
        error_message = safe_str(error)
        short_trace = error_message

    return {
        "id": make_event_id(EVENT_ERROR),
        "event": EVENT_ERROR,
        "system_version": SYSTEM_VERSION,
        "severity": severity,
        "module": safe_str(module, "unknown"),
        "function": safe_str(function, "unknown"),
        "error_type": error_type,
        "message": error_message,
        "short_trace": short_trace,
        "context": dict(context or {}),
        "created_at": utc_now_iso(),
    }


def log_error(
    *,
    module: str,
    function: str,
    error: Any,
    severity: str = ERROR_SEVERITY_ERROR,
    context: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Append an error record to errors.json."""
    record = make_error_record(
        module=module,
        function=function,
        error=error,
        severity=severity,
        context=context,
    )
    return append_record("errors", record, list_key="errors", max_records=1000)


def log_info(module: str, function: str, message: str, context: Optional[Mapping[str, Any]] = None) -> bool:
    """Append an informational diagnostic event to errors.json."""
    return log_error(
        module=module,
        function=function,
        error=message,
        severity=ERROR_SEVERITY_INFO,
        context=context,
    )


def log_warning(module: str, function: str, message: str, context: Optional[Mapping[str, Any]] = None) -> bool:
    """Append a warning diagnostic event to errors.json."""
    return log_error(
        module=module,
        function=function,
        error=message,
        severity=ERROR_SEVERITY_WARNING,
        context=context,
    )


def safe_execute(
    func: Callable[..., Any],
    *args: Any,
    module: str = "unknown",
    function: Optional[str] = None,
    default: Any = None,
    severity: str = ERROR_SEVERITY_ERROR,
    context: Optional[Mapping[str, Any]] = None,
    **kwargs: Any,
) -> Any:
    """
    Execute a callable and log any exception.

    Use for non-critical safe wrappers. Do not hide errors where the caller must
    explicitly handle failure.
    """
    fn_name = function or getattr(func, "__name__", "unknown")
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        log_error(
            module=module,
            function=fn_name,
            error=exc,
            severity=severity,
            context=context,
        )
        return default


def validate_json_readable(file_key_or_path: Any) -> bool:
    """Return True if a JSON file can be loaded into a valid object."""
    path = resolve_data_path(file_key_or_path)
    try:
        if not path.exists():
            return False
        with path.open("r", encoding="utf-8") as handle:
            json.load(handle)
        return True
    except Exception:
        return False


def validate_system_version_in_data(data: Any) -> bool:
    """Return True if dict has matching system_version, or no version yet."""
    if not isinstance(data, dict):
        return False
    version = data.get("system_version")
    return version in (None, "", SYSTEM_VERSION)


def check_data_files_light() -> dict[str, Any]:
    """
    Lightweight data-file check for startup preflight.

    Does not call market APIs, does not scan symbols, and does not run analysis.
    """
    ensure_data_dir()
    files: dict[str, Any] = {}

    for key, path in DATA_FILES.items():
        p = Path(path)
        ok = ensure_json_file(key)
        data = load_json(key)
        files[key] = {
            "path": str(p),
            "exists": p.exists(),
            "readable": ok,
            "version_ok": validate_system_version_in_data(data),
        }

    return {
        "status": STATUS_OK,
        "system_version": SYSTEM_VERSION,
        "files": files,
        "checked_at": utc_now_iso(),
    }


def make_result(status: str, message: str = "", **extra: Any) -> dict[str, Any]:
    """Small standard result dict for storage-layer functions."""
    result = {
        "status": status,
        "message": message,
        "system_version": SYSTEM_VERSION,
        "created_at": utc_now_iso(),
    }
    result.update(extra)
    return result


def failed_result(message: str, **extra: Any) -> dict[str, Any]:
    return make_result(STATUS_FAILED, message, **extra)


def ok_result(message: str = "", **extra: Any) -> dict[str, Any]:
    return make_result(STATUS_OK, message, **extra)


__all__ = [
    "STATE_STORE_VERSION",
    "default_data_for_key",
    "resolve_data_path",
    "get_file_key_for_path",
    "ensure_data_dir",
    "ensure_parent_dir",
    "read_text_safe",
    "write_text_atomic",
    "backup_corrupt_file",
    "load_json",
    "save_json_atomic",
    "ensure_json_file",
    "ensure_all_data_files",
    "append_record",
    "update_json",
    "set_key",
    "get_key",
    "make_error_record",
    "log_error",
    "log_info",
    "log_warning",
    "safe_execute",
    "validate_json_readable",
    "validate_system_version_in_data",
    "check_data_files_light",
    "make_result",
    "failed_result",
    "ok_result",
]
