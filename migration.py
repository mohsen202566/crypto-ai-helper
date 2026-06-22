from __future__ import annotations

"""
33 - migration.py

Data migration layer for the locked Movement Hunter architecture.

Responsibilities:
- Safely initialize data store sections.
- Backup current data before migration.
- Migrate old bot data into new architecture-compatible sections when possible.
- Preserve learning and trading history unless explicit reset is requested.
- Reset only specific sections when requested.
- Validate schema-like required fields.
- Produce migration reports for VPS deployment.

Strictly forbidden:
- No trading.
- No Toobit calls.
- No AI decisions.
- No Telegram sending.
- No Paper mode.
- No Setup flow.
"""

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from uuid import uuid4
import json
import shutil
import time

from data_store import store
from logger import info, warning, error


JsonDict = Dict[str, Any]

MIGRATION_OK = "OK"
MIGRATION_WARN = "WARN"
MIGRATION_FAIL = "FAIL"


REQUIRED_SECTIONS = (
    "runtime_settings",
    "allowed_users",
    "positions",
    "ghosts",
    "stats",
    "learning",
    "coin_behavior",
    "movement_memory",
    "meta_learning",
    "errors",
    "health",
    "system",
)


@dataclass(frozen=True)
class MigrationStep:
    name: str
    status: str
    message: str
    details: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class MigrationReport:
    migration_id: str
    timestamp: int
    status: str
    steps: Tuple[MigrationStep, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)

    def short_text(self) -> str:
        ok = sum(1 for s in self.steps if s.status == MIGRATION_OK)
        warn = sum(1 for s in self.steps if s.status == MIGRATION_WARN)
        fail = sum(1 for s in self.steps if s.status == MIGRATION_FAIL)
        icon = "✅" if fail == 0 else "❌"
        return f"{icon} Migration {self.status}\nOK:{ok} WARN:{warn} FAIL:{fail}"


def now_ts() -> int:
    return int(time.time())


def _step(name: str, status: str, message: str, details: Optional[JsonDict] = None) -> MigrationStep:
    return MigrationStep(name=name, status=status, message=message, details=details or {})


class DataBackupManager:
    """Creates safe JSON backups before migration."""

    def __init__(self, data_dir: str = "data", backup_dir: str = "data/backups"):
        self.data_dir = Path(data_dir)
        self.backup_dir = Path(backup_dir)

    def backup(self) -> MigrationStep:
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            target = self.backup_dir / f"migration_backup_{stamp}.json"

            data = {}
            try:
                data = store().data
            except Exception:
                data = {}

            target.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            return _step("backup", MIGRATION_OK, "backup created", {"path": str(target)})
        except Exception as exc:
            return _step("backup", MIGRATION_FAIL, str(exc))


class SectionInitializer:
    """Ensures required sections exist."""

    def run(self) -> MigrationStep:
        try:
            s = store()
            created: List[str] = []
            for section in REQUIRED_SECTIONS:
                if section not in s.data or not isinstance(s.data.get(section), dict):
                    s.data[section] = {}
                    created.append(section)
            s.save()
            return _step("sections", MIGRATION_OK, "sections initialized", {"created": created})
        except Exception as exc:
            return _step("sections", MIGRATION_FAIL, str(exc))


class RuntimeSettingsMigrator:
    """Initializes runtime settings without overwriting existing values."""

    def run(self) -> MigrationStep:
        try:
            section = store().section("runtime_settings")
            defaults = {
                "real_trading_enabled": False,
                "auto_signal_enabled": True,
                "scan_interval_seconds": 240,
                "last_scan_ts": 0,
                "migration_version": 33,
            }
            added = []
            for k, v in defaults.items():
                if k not in section:
                    section[k] = v
                    added.append(k)
            store().save()
            return _step("runtime_settings", MIGRATION_OK, "runtime settings migrated", {"added": added})
        except Exception as exc:
            return _step("runtime_settings", MIGRATION_FAIL, str(exc))


class LegacyDataMigrator:
    """
    Attempts lightweight migration from common old section names.

    It does not delete old sections.
    """

    LEGACY_MAP = {
        "active_signals": "positions",
        "tracked_signals": "positions",
        "ghost_signals": "ghosts",
        "signal_stats": "stats",
        "ai_memory": "learning",
        "coin_learning": "learning",
    }

    def run(self) -> MigrationStep:
        try:
            s = store()
            copied: Dict[str, int] = {}

            for old, new in self.LEGACY_MAP.items():
                old_section = s.data.get(old)
                if not isinstance(old_section, dict):
                    continue
                new_section = s.section(new)
                count = 0
                for key, value in old_section.items():
                    new_key = key
                    if new_key in new_section:
                        new_key = f"legacy_{old}_{key}"
                    new_section[new_key] = value
                    count += 1
                if count:
                    copied[f"{old}->{new}"] = count

            s.save()
            status = MIGRATION_WARN if copied else MIGRATION_OK
            msg = "legacy data copied" if copied else "no legacy data found"
            return _step("legacy", status, msg, copied)
        except Exception as exc:
            return _step("legacy", MIGRATION_FAIL, str(exc))


class DataValidator:
    """Validates high-level data store shape."""

    def run(self) -> MigrationStep:
        try:
            data = store().data
            bad = []
            for section in REQUIRED_SECTIONS:
                if section not in data:
                    bad.append(f"missing:{section}")
                elif not isinstance(data[section], dict):
                    bad.append(f"not_dict:{section}")

            if bad:
                return _step("validate", MIGRATION_FAIL, "data validation failed", {"bad": bad})

            return _step("validate", MIGRATION_OK, "data validation ok")
        except Exception as exc:
            return _step("validate", MIGRATION_FAIL, str(exc))


class ResetManager:
    """Selective reset helper. Never called automatically."""

    RESETTABLE = {
        "stats",
        "errors",
        "health",
        "ghosts",
        "positions",
        "learning",
        "coin_behavior",
        "movement_memory",
        "meta_learning",
    }

    def reset(self, section: str) -> MigrationStep:
        section = str(section or "").strip()
        if section not in self.RESETTABLE:
            return _step("reset", MIGRATION_FAIL, "section not resettable", {"section": section})

        try:
            s = store()
            count = len(s.section(section))
            s.data[section] = {}
            s.save()
            return _step("reset", MIGRATION_OK, "section reset", {"section": section, "removed": count})
        except Exception as exc:
            return _step("reset", MIGRATION_FAIL, str(exc), {"section": section})


class MigrationRunner:
    """Runs full migration sequence."""

    def __init__(self):
        self.backup = DataBackupManager()
        self.sections = SectionInitializer()
        self.runtime = RuntimeSettingsMigrator()
        self.legacy = LegacyDataMigrator()
        self.validator = DataValidator()

    def run(self) -> MigrationReport:
        steps: List[MigrationStep] = []
        steps.append(self.backup.backup())
        steps.append(self.sections.run())
        steps.append(self.runtime.run())
        steps.append(self.legacy.run())
        steps.append(self.validator.run())

        fail = any(s.status == MIGRATION_FAIL for s in steps)
        warn = any(s.status == MIGRATION_WARN for s in steps)
        status = MIGRATION_FAIL if fail else MIGRATION_WARN if warn else MIGRATION_OK

        report = MigrationReport(
            migration_id=f"mig_{uuid4().hex}",
            timestamp=now_ts(),
            status=status,
            steps=tuple(steps),
        )

        try:
            store().section("system")["last_migration"] = report.to_dict()
            store().save()
        except Exception:
            pass

        if status == MIGRATION_OK:
            info("migration", "migration ok", report.to_dict())
        elif status == MIGRATION_WARN:
            warning("migration", "migration warning", report.to_dict())
        else:
            error("migration", "migration failed", report.to_dict())

        return report


def run_migration() -> MigrationReport:
    return MigrationRunner().run()


def reset_section(section: str) -> MigrationStep:
    return ResetManager().reset(section)


def migration_text() -> str:
    return run_migration().short_text()
