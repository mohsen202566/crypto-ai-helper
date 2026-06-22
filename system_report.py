from __future__ import annotations

"""
34 - system_report.py

System report layer for the locked Movement Hunter architecture.

Responsibilities:
- Build one concise system report from:
  startup checks
  health monitor
  scheduler status
  runtime settings
  stats
  ghost stats
  meta-learning status
  open positions
  recent errors
- Provide Persian text for bot.py commands:
  گزارش سیستم
  وضعیت سیستم
- Provide structured dict for debugging/VPS audit.

Strictly forbidden:
- No trading.
- No Toobit order placement.
- No AI decision.
- No Telegram sending.
- No Paper mode.
- No Setup flow.
"""

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional, Tuple
import os
import platform
import time

from config import SETTINGS
from data_store import store
from health_monitor import run_health_checks, HealthReport
from startup_checks import run_startup_checks, StartupReport
from scheduler import scheduler_status
from stats_manager import stats_report
from ghost_manager import ghost_stats
from meta_learning import get_meta_learning_summary
from logger import info, warning, error


JsonDict = Dict[str, Any]


@dataclass(frozen=True)
class SystemReport:
    timestamp: int
    hostname: str
    python_version: str
    bot_mode: str
    real_trading_enabled: bool
    auto_signal_enabled: bool
    scan_interval_seconds: int
    startup_ok: bool
    health_status: str
    open_positions: int
    pending_positions: int
    recent_errors: int
    stats_text: str
    ghost_summary: JsonDict
    meta_summary: JsonDict
    health: JsonDict = field(default_factory=dict)
    startup: JsonDict = field(default_factory=dict)
    scheduler: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)

    def short_text(self) -> str:
        trade = "روشن ✅" if self.real_trading_enabled else "خاموش ❌"
        auto = "روشن ✅" if self.auto_signal_enabled else "خاموش ❌"
        start = "OK ✅" if self.startup_ok else "FAIL ❌"

        lines = [
            "🧾 گزارش سیستم ربات",
            f"Startup: {start}",
            f"Health: {self.health_status}",
            f"ترید واقعی: {trade}",
            f"سیگنال خودکار: {auto}",
            f"اسکن: {self.scan_interval_seconds} ثانیه",
            f"پوزیشن باز: {self.open_positions} | Pending: {self.pending_positions}",
            f"خطاهای اخیر: {self.recent_errors}",
            "",
            self.stats_text,
        ]
        return "\n".join(lines)


def now_ts() -> int:
    return int(time.time())


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _runtime() -> Dict[str, Any]:
    try:
        return store().section("runtime_settings")
    except Exception:
        return {}


def _count_positions() -> tuple[int, int]:
    try:
        positions = store().section("positions")
        open_count = 0
        pending = 0
        for item in positions.values():
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", "")).upper()
            if status in {"OPEN", "CONFIRMED"}:
                open_count += 1
            elif status == "PENDING_REAL_CONFIRM":
                pending += 1
        return open_count, pending
    except Exception:
        return 0, 0


def _recent_error_count(seconds: int = 3600) -> int:
    try:
        errors = store().section("errors")
        since = now_ts() - seconds
        count = 0
        for item in errors.values():
            if isinstance(item, dict) and safe_int(item.get("timestamp", 0)) >= since:
                count += 1
        return count
    except Exception:
        return 0


class SystemReportBuilder:
    """Builds structured and text system reports."""

    def build(self, client: Optional[Any] = None) -> SystemReport:
        runtime = _runtime()

        try:
            startup = run_startup_checks()
        except Exception as exc:
            startup = StartupReport(
                ok=False,
                timestamp=now_ts(),
                passed=0,
                failed=1,
                warnings=0,
                results=(),
            )

        try:
            sched = scheduler_status()
        except Exception:
            sched = {}

        try:
            health = run_health_checks(client=client, scheduler_status=sched)
        except Exception:
            health = HealthReport(
                status="FAIL",
                timestamp=now_ts(),
                ok_count=0,
                warn_count=0,
                fail_count=1,
                checks=(),
            )

        open_positions, pending_positions = _count_positions()

        try:
            stats_txt = stats_report(days=None)
        except Exception as exc:
            stats_txt = f"آمار در دسترس نیست: {exc}"

        try:
            ghost = ghost_stats().to_dict()
        except Exception:
            ghost = {}

        try:
            meta = get_meta_learning_summary().to_dict()
        except Exception:
            meta = {}

        report = SystemReport(
            timestamp=now_ts(),
            hostname=platform.node(),
            python_version=platform.python_version(),
            bot_mode="MOVEMENT_HUNTER_REAL_GHOST_REJECT",
            real_trading_enabled=bool(runtime.get("real_trading_enabled", getattr(SETTINGS.trading, "real_trading_enabled", False))),
            auto_signal_enabled=bool(runtime.get("auto_signal_enabled", True)),
            scan_interval_seconds=safe_int(runtime.get("scan_interval_seconds", getattr(SETTINGS.scanner, "scan_interval_seconds", 240)), 240),
            startup_ok=bool(startup.ok),
            health_status=str(health.status),
            open_positions=open_positions,
            pending_positions=pending_positions,
            recent_errors=_recent_error_count(),
            stats_text=stats_txt,
            ghost_summary=ghost,
            meta_summary=meta,
            health=health.to_dict(),
            startup=startup.to_dict(),
            scheduler=sched,
        )

        try:
            store().section("system")["last_system_report"] = report.to_dict()
            store().save()
        except Exception:
            pass

        if report.startup_ok and report.health_status == "OK":
            info("system_report", "system report ok", report.to_dict())
        elif report.health_status == "WARN":
            warning("system_report", "system report warning", report.to_dict())
        else:
            error("system_report", "system report failed", report.to_dict())

        return report


_default_builder: Optional[SystemReportBuilder] = None


def builder() -> SystemReportBuilder:
    global _default_builder
    if _default_builder is None:
        _default_builder = SystemReportBuilder()
    return _default_builder


def build_system_report(client: Optional[Any] = None) -> SystemReport:
    return builder().build(client=client)


def system_report_text(client: Optional[Any] = None) -> str:
    return build_system_report(client=client).short_text()
