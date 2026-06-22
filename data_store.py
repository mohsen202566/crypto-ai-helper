from __future__ import annotations

"""
02 - data_store.py

Production-ready persistence layer for the locked Movement Hunter crypto futures bot.

Responsibilities:
- JSON persistence only.
- Atomic writes.
- Backup / recovery.
- Thread-safe section updates.
- Store and retrieve bot state objects:
  signals, positions, ghosts, learning, movement_memory, stats, meta_learning,
  coin_behavior, errors, settings.

Strictly forbidden in this file:
- No AI decision logic.
- No technical analysis.
- No Toobit API calls.
- No Telegram handlers.
- No trade execution.
- No Paper mode.
- No Setup flow.

Architecture lock:
- REAL / GHOST / REJECT only.
- Ghost and Real learning share persistent storage.
- Movement Memory is a first-class persistent section.
- Raw exchange errors can be stored for debugging and preventing repeated Toobit bugs.
"""

import json
import shutil
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from uuid import uuid4

from config import SETTINGS


JsonDict = Dict[str, Any]
JsonValue = Union[None, bool, int, float, str, List[Any], Dict[str, Any]]

STORE_SCHEMA_VERSION = 1


DEFAULT_STORE: JsonDict = {
    "schema_version": STORE_SCHEMA_VERSION,
    "created_at": "",
    "updated_at": "",
    "runtime": {
        "trade_enabled": False,
        "ai_enabled": True,
        "learning_enabled": True,
        "emergency_stop": False,
        "emergency_reason": "",
        "last_scan_at": "",
        "last_backup_at": "",
    },
    "runtime_settings": {
        "real_trading_enabled": False,
        "auto_signal_enabled": True,
        "scan_interval_seconds": 240,
        "last_scan_ts": 0,
    },
    "allowed_users": {},
    "health": {},
    "system": {},
    "signals": {},
    "positions": {},
    "ghosts": {},
    "learning": {},
    "movement_memory": {},
    "coin_behavior": {},
    "meta_learning": {},
    "stats": {},
    "errors": {},
    "settings": {},
}


class DataStoreError(RuntimeError):
    """Raised for persistence layer failures."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str = "id") -> str:
    return f"{prefix}_{uuid4().hex}"


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    return str(value)


def _deepcopy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=_json_default, ensure_ascii=False))


def _safe_dict(value: Any) -> JsonDict:
    if value is None:
        return {}
    if is_dataclass(value):
        return _deepcopy_json(asdict(value))
    if hasattr(value, "to_dict") and callable(value.to_dict):
        data = value.to_dict()
        return _deepcopy_json(data if isinstance(data, dict) else {"value": data})
    if isinstance(value, dict):
        return _deepcopy_json(value)
    return {"value": _deepcopy_json(value)}


def _merge_defaults(data: Any) -> JsonDict:
    if not isinstance(data, dict):
        data = {}
    merged = _deepcopy_json(DEFAULT_STORE)
    for key, value in data.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key].update(value)
        else:
            merged[key] = value
    merged["schema_version"] = STORE_SCHEMA_VERSION
    if not merged.get("created_at"):
        merged["created_at"] = utc_now_iso()
    merged["updated_at"] = utc_now_iso()
    return merged


def _read_json_file(path: Path) -> JsonDict:
    if not path.exists():
        return _merge_defaults({})
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return _merge_defaults({})
        return _merge_defaults(json.loads(raw))
    except json.JSONDecodeError as exc:
        raise DataStoreError(f"invalid_json:{path}:{exc}") from exc
    except Exception as exc:
        raise DataStoreError(f"read_failed:{path}:{exc}") from exc


def _atomic_write_json(path: Path, data: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.write("\n")
            f.flush()
        tmp_path.replace(path)
    except Exception as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise DataStoreError(f"write_failed:{path}:{exc}") from exc


class DataStore:
    """
    Thread-safe JSON store.

    Canonical sections:
    - signals: AI decisions/signals by decision_id.
    - positions: real Toobit positions by position_id.
    - ghosts: ghost decisions by ghost_id.
    - learning: unified Real/Ghost learning records by learning_id.
    - movement_memory: pre-pump/pre-dump memory by movement_id.
    - coin_behavior: learned coin+direction+condition summaries.
    - meta_learning: self-audit/module weights.
    - stats: aggregated reports.
    - errors: raw errors and exchange responses.
    - runtime/settings: bot runtime flags.
    """

    VALID_SECTIONS = set(DEFAULT_STORE.keys()) - {"schema_version", "created_at", "updated_at"}

    def __init__(self, path: Optional[Path] = None, backups_dir: Optional[Path] = None, atomic_writes: Optional[bool] = None):
        self.path = Path(path or (SETTINGS.storage.data_dir / "store.json"))
        self.backups_dir = Path(backups_dir or SETTINGS.storage.backups_dir)
        self.atomic_writes = SETTINGS.storage.atomic_writes if atomic_writes is None else bool(atomic_writes)
        self._lock = threading.RLock()
        self._state: JsonDict = _merge_defaults({})
        self.load()

    def load(self) -> JsonDict:
        with self._lock:
            try:
                self._state = _read_json_file(self.path)
            except DataStoreError:
                recovered = self.recover_latest_backup()
                if recovered is None:
                    raise
                self._state = recovered
                self.save()
            return self.snapshot()

    def save(self) -> None:
        with self._lock:
            self._state["updated_at"] = utc_now_iso()
            if self.atomic_writes:
                _atomic_write_json(self.path, self._state)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default) + "\n",
                    encoding="utf-8",
                )

    def snapshot(self) -> JsonDict:
        with self._lock:
            return _deepcopy_json(self._state)

    def section(self, name: str) -> JsonDict:
        self._validate_section(name)
        with self._lock:
            section = self._state.setdefault(name, {})
            if not isinstance(section, dict):
                section = {}
                self._state[name] = section
            return _deepcopy_json(section)


    def section_ref(self, name: str) -> JsonDict:
        """
        Internal mutable section reference.

        Use only when the caller immediately saves after mutation.
        This avoids expensive deepcopy for high-frequency runtime/stat/health writes.
        """
        self._validate_section(name)
        with self._lock:
            section = self._state.setdefault(name, {})
            if not isinstance(section, dict):
                section = {}
                self._state[name] = section
            return section

    def append_bounded(
        self,
        section: str,
        item_id: str,
        item: Any,
        max_items: int = 20000,
        sort_key: str = "created_at",
        save: bool = True,
    ) -> JsonDict:
        data = self.upsert(section, item_id, item, save=False)
        if max_items and max_items > 0:
            self.prune_section(section, max_items=max_items, sort_key=sort_key, save=False)
        if save:
            self.save()
        return data


    def replace_section(self, name: str, value: Dict[str, Any], save: bool = True) -> None:
        self._validate_section(name)
        with self._lock:
            self._state[name] = _safe_dict(value)
            if save:
                self.save()

    def update_section(self, name: str, mutator: Callable[[JsonDict], Any], save: bool = True) -> Any:
        self._validate_section(name)
        with self._lock:
            section = self._state.setdefault(name, {})
            if not isinstance(section, dict):
                section = {}
                self._state[name] = section
            result = mutator(section)
            if save:
                self.save()
            return result

    @contextmanager
    def transaction(self):
        """
        Transaction context.

        Mutations are saved once at the end. If an exception occurs, in-memory
        state is rolled back to the pre-transaction snapshot.
        """
        with self._lock:
            before = self.snapshot()
            try:
                yield self._state
                self.save()
            except Exception:
                self._state = before
                raise

    def upsert(self, section: str, item_id: str, item: Any, save: bool = True) -> JsonDict:
        self._validate_section(section)
        if not item_id:
            raise DataStoreError("item_id_required")
        data = _safe_dict(item)
        data.setdefault("id", item_id)
        data.setdefault("created_at", utc_now_iso())
        data["updated_at"] = utc_now_iso()

        with self._lock:
            target = self._state.setdefault(section, {})
            if not isinstance(target, dict):
                target = {}
                self._state[section] = target

            existing = target.get(item_id)
            if isinstance(existing, dict):
                merged = dict(existing)
                merged.update(data)
                data = merged
                data["updated_at"] = utc_now_iso()

            target[item_id] = data
            if save:
                self.save()
        return _deepcopy_json(data)

    def get(self, section: str, item_id: str, default: Any = None) -> Any:
        self._validate_section(section)
        with self._lock:
            return _deepcopy_json(self._state.get(section, {}).get(item_id, default))

    def delete(self, section: str, item_id: str, save: bool = True) -> bool:
        self._validate_section(section)
        with self._lock:
            target = self._state.setdefault(section, {})
            if not isinstance(target, dict) or item_id not in target:
                return False
            del target[item_id]
            if save:
                self.save()
            return True

    def list_items(self, section: str, predicate: Optional[Callable[[JsonDict], bool]] = None) -> List[JsonDict]:
        self._validate_section(section)
        with self._lock:
            target = self._state.setdefault(section, {})
            if not isinstance(target, dict):
                return []
            items = [_deepcopy_json(v) for v in target.values() if isinstance(v, dict)]
        if predicate is None:
            return items
        return [item for item in items if predicate(item)]

    # ------------------------------------------------------------------
    # Signals / decisions
    # ------------------------------------------------------------------

    def save_signal(self, decision_id: str, signal: Any) -> JsonDict:
        data = _safe_dict(signal)
        data.setdefault("decision_id", decision_id)
        data.setdefault("status", "NEW")
        data.setdefault("decision_type", data.get("type", ""))
        return self.upsert("signals", decision_id, data)

    def update_signal_status(self, decision_id: str, status: str, extra: Optional[Dict[str, Any]] = None) -> JsonDict:
        existing = self.get("signals", decision_id, {})
        if not isinstance(existing, dict):
            existing = {}
        existing["status"] = status
        existing["updated_at"] = utc_now_iso()
        if extra:
            existing.update(_safe_dict(extra))
        return self.upsert("signals", decision_id, existing)

    # ------------------------------------------------------------------
    # Real positions
    # ------------------------------------------------------------------

    def save_position(self, position_id: str, position: Any) -> JsonDict:
        data = _safe_dict(position)
        data.setdefault("position_id", position_id)
        data.setdefault("status", "OPEN")
        data.setdefault("events", [])
        return self.upsert("positions", position_id, data)

    def add_position_event(self, position_id: str, event_type: str, price: float = 0.0, pnl: float = 0.0, note: str = "", extra: Optional[Dict[str, Any]] = None) -> JsonDict:
        position = self.get("positions", position_id, {})
        if not isinstance(position, dict) or not position:
            raise DataStoreError(f"position_not_found:{position_id}")

        event = {
            "event_id": new_id("evt"),
            "event_type": event_type,
            "timestamp": utc_now_iso(),
            "price": price,
            "pnl": pnl,
            "note": note,
        }
        if extra:
            event.update(_safe_dict(extra))

        events = position.setdefault("events", [])
        if not isinstance(events, list):
            events = []
            position["events"] = events
        events.append(event)
        position["updated_at"] = utc_now_iso()

        normalized = str(event_type).upper()
        if normalized == "TP1":
            position["tp1_hit"] = True
            position["status"] = "TP1"
        elif normalized == "TP2":
            position["tp2_hit"] = True
            position["status"] = "TP2"
            position["close_time"] = event["timestamp"]
        elif normalized == "AI_EXIT":
            position["ai_exit"] = True
            position["status"] = "AI_EXIT"
            position["close_time"] = event["timestamp"]
        elif normalized == "SL":
            position["status"] = "SL"
            position["close_time"] = event["timestamp"]
        elif normalized == "CLOSED":
            position["close_time"] = event["timestamp"]
            position["status"] = position.get("status") or "CLOSED"

        return self.upsert("positions", position_id, position)

    def open_positions(self) -> List[JsonDict]:
        closed = {"TP2", "AI_EXIT", "SL", "CLOSED"}
        return self.list_items("positions", lambda p: str(p.get("status", "")).upper() not in closed)

    # ------------------------------------------------------------------
    # Ghosts
    # ------------------------------------------------------------------

    def save_ghost(self, ghost_id: str, ghost: Any) -> JsonDict:
        data = _safe_dict(ghost)
        data.setdefault("ghost_id", ghost_id)
        data.setdefault("status", "OPEN")
        data.setdefault("events", [])
        return self.upsert("ghosts", ghost_id, data)

    def update_ghost_result(self, ghost_id: str, result: str, extra: Optional[Dict[str, Any]] = None) -> JsonDict:
        ghost = self.get("ghosts", ghost_id, {})
        if not isinstance(ghost, dict) or not ghost:
            raise DataStoreError(f"ghost_not_found:{ghost_id}")
        ghost["result"] = result
        ghost["status"] = "CLOSED"
        ghost["closed_at"] = utc_now_iso()
        if extra:
            ghost.update(_safe_dict(extra))
        return self.upsert("ghosts", ghost_id, ghost)

    def open_ghosts(self) -> List[JsonDict]:
        return self.list_items("ghosts", lambda g: str(g.get("status", "")).upper() == "OPEN")

    # ------------------------------------------------------------------
    # Learning and memory
    # ------------------------------------------------------------------

    def save_learning_record(self, learning_id: str, record: Any) -> JsonDict:
        data = _safe_dict(record)
        data.setdefault("learning_id", learning_id)
        data.setdefault("source_type", "UNKNOWN")
        data.setdefault("created_at", utc_now_iso())
        return self.upsert("learning", learning_id, data)

    def save_movement_memory(self, movement_id: str, record: Any) -> JsonDict:
        data = _safe_dict(record)
        data.setdefault("movement_id", movement_id)
        data.setdefault("created_at", utc_now_iso())
        return self.upsert("movement_memory", movement_id, data)

    def save_coin_behavior(self, key: str, record: Any) -> JsonDict:
        return self.upsert("coin_behavior", key, record)

    def save_meta_learning(self, module_name: str, record: Any) -> JsonDict:
        data = _safe_dict(record)
        data.setdefault("module_name", module_name)
        data.setdefault("last_updated", utc_now_iso())
        return self.upsert("meta_learning", module_name, data)

    # ------------------------------------------------------------------
    # Stats / runtime / errors
    # ------------------------------------------------------------------

    def save_stats(self, key: str, stats: Any) -> JsonDict:
        return self.upsert("stats", key, stats)

    def save_stat_event(self, stat_id: str, event: Any) -> JsonDict:
        data = _safe_dict(event)
        data.setdefault("stat_id", stat_id)
        data.setdefault("created_at", utc_now_iso())
        return self.upsert("stats", stat_id, data)

    def set_runtime_flag(self, name: str, value: Any, save: bool = True) -> None:
        with self._lock:
            runtime = self._state.setdefault("runtime", {})
            runtime[name] = _deepcopy_json(value)
            runtime["updated_at"] = utc_now_iso()
            if save:
                self.save()

    def get_runtime_flag(self, name: str, default: Any = None) -> Any:
        with self._lock:
            runtime = self._state.setdefault("runtime", {})
            return _deepcopy_json(runtime.get(name, default))

    def save_error(self, source: str, error: Any, context: Optional[Dict[str, Any]] = None) -> JsonDict:
        error_id = new_id("err")
        payload = {
            "error_id": error_id,
            "source": source,
            "error": _deepcopy_json(error),
            "context": _safe_dict(context or {}),
            "timestamp": utc_now_iso(),
        }
        return self.upsert("errors", error_id, payload)

    # ------------------------------------------------------------------
    # Backup / recovery / pruning
    # ------------------------------------------------------------------

    def backup(self, reason: str = "manual") -> Path:
        with self._lock:
            self.backups_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            safe_reason = "".join(ch for ch in reason if ch.isalnum() or ch in {"_", "-"})[:40] or "manual"
            backup_path = self.backups_dir / f"store_{stamp}_{safe_reason}.json"
            self.save()
            shutil.copy2(self.path, backup_path)
            runtime = self._state.setdefault("runtime", {})
            runtime["last_backup_at"] = utc_now_iso()
            self.save()
            return backup_path

    def recover_latest_backup(self) -> Optional[JsonDict]:
        if not self.backups_dir.exists():
            return None
        candidates = sorted(self.backups_dir.glob("store_*.json"), reverse=True)
        for candidate in candidates:
            try:
                return _read_json_file(candidate)
            except Exception:
                continue
        return None

    def export_section(self, section: str, path: Path) -> Path:
        data = self.section(section)
        _atomic_write_json(path, data)
        return path

    def prune_section(self, section: str, max_items: int, sort_key: str = "created_at", save: bool = True) -> int:
        self._validate_section(section)
        if max_items <= 0:
            raise DataStoreError("max_items_must_be_positive")

        with self._lock:
            target = self._state.setdefault(section, {})
            if not isinstance(target, dict) or len(target) <= max_items:
                return 0

            items: List[Tuple[str, Dict[str, Any]]] = [
                (k, v) for k, v in target.items() if isinstance(v, dict)
            ]
            items.sort(key=lambda kv: str(kv[1].get(sort_key, "")), reverse=True)
            keep = {k for k, _ in items[:max_items]}
            removed = 0
            for key in list(target.keys()):
                if key not in keep:
                    del target[key]
                    removed += 1
            if save:
                self.save()
            return removed

    def _validate_section(self, name: str) -> None:
        if name not in self.VALID_SECTIONS:
            raise DataStoreError(f"invalid_section:{name}")


_default_store: Optional[DataStore] = None
_default_lock = threading.RLock()


def store() -> DataStore:
    global _default_store
    with _default_lock:
        if _default_store is None:
            _default_store = DataStore()
        return _default_store


def load_store() -> JsonDict:
    return store().load()


def save_store() -> None:
    store().save()


def snapshot() -> JsonDict:
    return store().snapshot()


def save_signal(decision_id: str, signal: Any) -> JsonDict:
    return store().save_signal(decision_id, signal)


def save_position(position_id: str, position: Any) -> JsonDict:
    return store().save_position(position_id, position)


def save_ghost(ghost_id: str, ghost: Any) -> JsonDict:
    return store().save_ghost(ghost_id, ghost)


def save_learning_record(learning_id: str, record: Any) -> JsonDict:
    return store().save_learning_record(learning_id, record)


def save_movement_memory(movement_id: str, record: Any) -> JsonDict:
    return store().save_movement_memory(movement_id, record)


def save_error(source: str, error: Any, context: Optional[Dict[str, Any]] = None) -> JsonDict:
    return store().save_error(source, error, context)


def save_stat_event(stat_id: str, event: Any) -> JsonDict:
    return store().save_stat_event(stat_id, event)


def save_coin_behavior(key: str, record: Any) -> JsonDict:
    return store().save_coin_behavior(key, record)


def save_meta_learning(module_name: str, record: Any) -> JsonDict:
    return store().save_meta_learning(module_name, record)


def append_bounded(section: str, item_id: str, item: Any, max_items: int = 20000, sort_key: str = "created_at") -> JsonDict:
    return store().append_bounded(section, item_id, item, max_items=max_items, sort_key=sort_key)


def prune_section(section: str, max_items: int, sort_key: str = "created_at") -> int:
    return store().prune_section(section, max_items=max_items, sort_key=sort_key)


def update_section(name: str, mutator: Callable[[JsonDict], Any], save: bool = True) -> Any:
    return store().update_section(name, mutator, save=save)
