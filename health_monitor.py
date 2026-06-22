from __future__ import annotations

"""
32 - health_monitor.py

Runtime health monitor for the locked Movement Hunter architecture.

Responsibilities:
- Check bot/runtime health without trading.
- Check data directory, log directory, data store, config, scheduler, Toobit connectivity,
  market data freshness, open-position sync status, and recent errors.
- Produce short Persian and structured health reports for bot.py commands.
- Never interrupt trading flow because a health check fails.

Strictly forbidden:
- No trade execution.
- No AI decision.
- No Telegram sending.
- No Paper mode.
- No Setup flow.
"""

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from pathlib import Path
import os
import time
import math

from logger import info, warning, error
from data_store import store
from config import SETTINGS


JsonDict = Dict[str, Any]

HEALTH_OK = "OK"
HEALTH_WARN = "WARN"
HEALTH_FAIL = "FAIL"


@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: str
    message: str
    details: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class HealthReport:
    status: str
    timestamp: int
    ok_count: int
    warn_count: int
    fail_count: int
    checks: Tuple[HealthCheck, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)

    def short_text(self) -> str:
        icon = "✅" if self.status == HEALTH_OK else "⚠️" if self.status == HEALTH_WARN else "❌"
        lines = [
            f"{icon} وضعیت سلامت ربات",
            f"OK: {self.ok_count} | WARN: {self.warn_count} | FAIL: {self.fail_count}",
        ]
        bad = [c for c in self.checks if c.status != HEALTH_OK][:6]
        for check in bad:
            lines.append(f"{check.status}: {check.name} - {check.message}")
        return "\n".join(lines)


def now_ts() -> int:
    return int(time.time())


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def _check(name: str, status: str, message: str, details: Optional[JsonDict] = None) -> HealthCheck:
    return HealthCheck(name=name, status=status, message=message, details=details or {})


class HealthMonitor:
    """Runs lightweight runtime health checks."""

    def run_all(self, client: Optional[Any] = None, scheduler_status: Optional[JsonDict] = None) -> HealthReport:
        checks: List[HealthCheck] = []

        checks.extend(self.check_directories())
        checks.append(self.check_data_store())
        checks.append(self.check_config())
        checks.append(self.check_runtime_settings())
        checks.append(self.check_recent_errors())
        checks.append(self.check_positions_state())

        if scheduler_status is not None:
            checks.append(self.check_scheduler(scheduler_status))

        if client is not None:
            checks.append(self.check_toobit_client(client))

        fail = sum(1 for c in checks if c.status == HEALTH_FAIL)
        warn_count = sum(1 for c in checks if c.status == HEALTH_WARN)
        ok_count = sum(1 for c in checks if c.status == HEALTH_OK)

        status = HEALTH_FAIL if fail else HEALTH_WARN if warn_count else HEALTH_OK

        report = HealthReport(
            status=status,
            timestamp=now_ts(),
            ok_count=ok_count,
            warn_count=warn_count,
            fail_count=fail,
            checks=tuple(checks),
        )

        if status == HEALTH_OK:
            info("health_monitor", "health ok", report.to_dict())
        elif status == HEALTH_WARN:
            warning("health_monitor", "health warnings", report.to_dict())
        else:
            error("health_monitor", "health failed", report.to_dict())

        return report

    def check_directories(self) -> List[HealthCheck]:
        checks: List[HealthCheck] = []
        for dirname in ("data", "logs", "data/backups"):
            path = Path(dirname)
            try:
                path.mkdir(parents=True, exist_ok=True)
                test = path / ".health_write_test"
                test.write_text("ok", encoding="utf-8")
                test.unlink(missing_ok=True)
                checks.append(_check(f"dir.{dirname}", HEALTH_OK, "writable"))
            except Exception as exc:
                checks.append(_check(f"dir.{dirname}", HEALTH_FAIL, str(exc)))
        return checks

    def check_data_store(self) -> HealthCheck:
        try:
            s = store()
            section = s.section("health")
            section["last_health_check"] = now_ts()
            s.save()
            return _check("data_store", HEALTH_OK, "read/write ok")
        except Exception as exc:
            return _check("data_store", HEALTH_FAIL, str(exc))

    def check_config(self) -> HealthCheck:
        try:
            margin = safe_float(getattr(SETTINGS.trading, "margin_usdt", 0.0))
            leverage = int(getattr(SETTINGS.trading, "leverage", 0))
            max_positions = int(getattr(SETTINGS.trading, "max_positions", 0))
            if margin <= 0 or leverage <= 0 or max_positions <= 0:
                return _check(
                    "config",
                    HEALTH_FAIL,
                    "invalid trading settings",
                    {"margin": margin, "leverage": leverage, "max_positions": max_positions},
                )
            return _check("config", HEALTH_OK, "settings ok", {"margin": margin, "leverage": leverage, "max_positions": max_positions})
        except Exception as exc:
            return _check("config", HEALTH_FAIL, str(exc))

    def check_runtime_settings(self) -> HealthCheck:
        try:
            runtime = store().section("runtime_settings")
            return _check(
                "runtime_settings",
                HEALTH_OK,
                "runtime settings readable",
                {
                    "real_trading_enabled": bool(runtime.get("real_trading_enabled", False)),
                    "auto_signal_enabled": bool(runtime.get("auto_signal_enabled", True)),
                    "scan_interval_seconds": runtime.get("scan_interval_seconds", None),
                },
            )
        except Exception as exc:
            return _check("runtime_settings", HEALTH_WARN, str(exc))

    def check_recent_errors(self) -> HealthCheck:
        try:
            errors = store().section("errors")
            recent = []
            threshold = now_ts() - 3600
            for item in errors.values():
                if isinstance(item, dict) and int(item.get("timestamp", 0) or 0) >= threshold:
                    recent.append(item)
            if len(recent) >= 10:
                return _check("recent_errors", HEALTH_WARN, "many recent errors", {"count": len(recent)})
            return _check("recent_errors", HEALTH_OK, "recent errors acceptable", {"count": len(recent)})
        except Exception as exc:
            return _check("recent_errors", HEALTH_WARN, str(exc))

    def check_positions_state(self) -> HealthCheck:
        try:
            positions = store().section("positions")
            pending = 0
            open_count = 0
            stale_pending = 0
            threshold = now_ts() - 120

            for item in positions.values():
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", "")).upper()
                if status == "PENDING_REAL_CONFIRM":
                    pending += 1
                    created = int(item.get("created_at", 0) or 0)
                    if created and created < threshold:
                        stale_pending += 1
                elif status in {"OPEN", "CONFIRMED"}:
                    open_count += 1

            if stale_pending:
                return _check("positions", HEALTH_WARN, "stale pending positions found", {"pending": pending, "stale_pending": stale_pending, "open": open_count})
            return _check("positions", HEALTH_OK, "position state ok", {"pending": pending, "open": open_count})
        except Exception as exc:
            return _check("positions", HEALTH_WARN, str(exc))

    def check_scheduler(self, status: JsonDict) -> HealthCheck:
        try:
            jobs = status.get("jobs", {})
            failed = [name for name, job in jobs.items() if str(job.get("status", "")).upper() == "FAILED"]
            if failed:
                return _check("scheduler", HEALTH_WARN, "some jobs failed", {"failed": failed})
            return _check("scheduler", HEALTH_OK, "scheduler ok", {"jobs": list(jobs.keys())})
        except Exception as exc:
            return _check("scheduler", HEALTH_WARN, str(exc))

    def check_toobit_client(self, client: Any) -> HealthCheck:
        try:
            # Lightweight call; no private order or account action.
            price = 0.0
            try:
                price = safe_float(client.get_latest_price("BTCUSDT"))
            except Exception:
                price = 0.0
            if price > 0:
                return _check("toobit_public", HEALTH_OK, "public price ok", {"BTCUSDT": price})
            return _check("toobit_public", HEALTH_WARN, "could not read BTCUSDT price")
        except Exception as exc:
            return _check("toobit_public", HEALTH_WARN, str(exc))


_default_monitor: Optional[HealthMonitor] = None


def monitor() -> HealthMonitor:
    global _default_monitor
    if _default_monitor is None:
        _default_monitor = HealthMonitor()
    return _default_monitor


def run_health_checks(client: Optional[Any] = None, scheduler_status: Optional[JsonDict] = None) -> HealthReport:
    return monitor().run_all(client=client, scheduler_status=scheduler_status)


def health_text(client: Optional[Any] = None, scheduler_status: Optional[JsonDict] = None) -> str:
    return run_health_checks(client=client, scheduler_status=scheduler_status).short_text()
