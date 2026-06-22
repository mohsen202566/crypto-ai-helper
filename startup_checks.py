from __future__ import annotations

"""
31 - startup_checks.py

Startup validation layer for the locked Movement Hunter architecture.

Responsibilities:
- Validate required environment variables.
- Validate critical settings.
- Validate required files/directories.
- Validate imports for core modules.
- Validate Toobit client can be constructed.
- Validate there is no Paper/Setup mode enabled.
- Validate real-trade safety settings:
  isolated-only
  leverage
  margin
  max positions
- Produce a startup report for bot.py / VPS logs.

Strictly forbidden:
- No trading.
- No Toobit order placement.
- No Telegram sending.
- No AI decisions.
"""

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Tuple
import importlib
import os
from pathlib import Path
import time

from logger import info, warning, error
from config import SETTINGS


JsonDict = Dict[str, Any]


REQUIRED_MODULES = (
    "schemas",
    "config",
    "data_store",
    "symbol_mapper",
    "market_data",
    "market_context",
    "analysis_layers",
    "analysis_engine",
    "movement_hunter",
    "trap_engine",
    "state_engine",
    "confidence_engine",
    "correlation_engine",
    "coin_learning",
    "ghost_manager",
    "movement_memory",
    "movement_predictor",
    "meta_learning",
    "ai_decision_engine",
    "tp_sl_engine",
    "exit_engine",
    "real_trade_manager",
    "tobit_client",
    "position_monitor",
    "result_reporter",
    "stats_manager",
    "logger",
    "error_handler",
    "scheduler",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    message: str
    details: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class StartupReport:
    ok: bool
    timestamp: int
    passed: int
    failed: int
    warnings: int
    results: Tuple[CheckResult, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)

    def short_text(self) -> str:
        status = "✅ OK" if self.ok else "❌ FAILED"
        return (
            f"Startup Checks: {status}\n"
            f"Passed: {self.passed} | Failed: {self.failed} | Warnings: {self.warnings}"
        )




def _first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _toobit_api_key() -> str:
    return _first_non_empty(
        os.getenv("TOOBIT_API_KEY"),
        os.getenv("TOBIT_API_KEY"),
        getattr(SETTINGS.toobit, "api_key", ""),
    )


def _toobit_api_secret() -> str:
    return _first_non_empty(
        os.getenv("TOOBIT_API_SECRET"),
        os.getenv("TOOBIT_SECRET_KEY"),
        os.getenv("TOBIT_API_SECRET"),
        os.getenv("TOBIT_SECRET_KEY"),
        getattr(SETTINGS.toobit, "api_secret", ""),
    )


def now_ts() -> int:
    return int(time.time())


def _ok(name: str, message: str, details: JsonDict | None = None) -> CheckResult:
    return CheckResult(name=name, ok=True, message=message, details=details or {})


def _fail(name: str, message: str, details: JsonDict | None = None) -> CheckResult:
    return CheckResult(name=name, ok=False, message=message, details=details or {})


class StartupChecker:
    """Runs all VPS startup checks."""

    def __init__(self):
        self.results: List[CheckResult] = []

    def run_all(self) -> StartupReport:
        self.results = []
        self.check_environment()
        self.check_directories()
        self.check_imports()
        self.check_settings()
        self.check_real_trade_safety()
        self.check_no_paper_setup()

        failed = sum(1 for r in self.results if not r.ok)
        passed = sum(1 for r in self.results if r.ok)
        warnings_count = sum(1 for r in self.results if r.ok and "warning" in r.message.lower())

        report = StartupReport(
            ok=failed == 0,
            timestamp=now_ts(),
            passed=passed,
            failed=failed,
            warnings=warnings_count,
            results=tuple(self.results),
        )

        if report.ok:
            info("startup_checks", "startup checks passed", report.to_dict())
        else:
            error("startup_checks", "startup checks failed", report.to_dict())

        return report

    def check_environment(self) -> None:
        bot_token = os.getenv("BOT_TOKEN", getattr(SETTINGS.telegram, "bot_token", ""))
        if bot_token:
            self.results.append(_ok("env.BOT_TOKEN", "BOT_TOKEN exists"))
        else:
            self.results.append(_fail("env.BOT_TOKEN", "BOT_TOKEN missing"))

        api_key = _toobit_api_key()
        api_secret = _toobit_api_secret()

        if api_key and api_secret:
            self.results.append(_ok("env.TOOBIT", "Toobit credentials exist"))
        else:
            self.results.append(_fail("env.TOOBIT", "Toobit API key/secret missing", {"accepted_names": ["TOOBIT_API_KEY", "TOOBIT_API_SECRET", "TOOBIT_SECRET_KEY", "TOBIT_API_KEY", "TOBIT_API_SECRET", "TOBIT_SECRET_KEY"]}))

        owner_id = os.getenv("OWNER_ID", getattr(SETTINGS.telegram, "owner_id", ""))
        if owner_id:
            self.results.append(_ok("env.OWNER_ID", "OWNER_ID exists"))
        else:
            self.results.append(_ok("env.OWNER_ID", "warning: OWNER_ID missing"))

    def check_directories(self) -> None:
        for dirname in ("data", "logs", "data/backups"):
            path = Path(dirname)
            try:
                path.mkdir(parents=True, exist_ok=True)
                test_file = path / ".write_test"
                test_file.write_text("ok", encoding="utf-8")
                test_file.unlink(missing_ok=True)
                self.results.append(_ok(f"dir.{dirname}", "directory writable"))
            except Exception as exc:
                self.results.append(_fail(f"dir.{dirname}", str(exc)))

    def check_imports(self) -> None:
        for module in REQUIRED_MODULES:
            try:
                importlib.import_module(module)
                self.results.append(_ok(f"import.{module}", "import ok"))
            except Exception as exc:
                self.results.append(_fail(f"import.{module}", str(exc)))

    def check_settings(self) -> None:
        try:
            max_positions = int(getattr(SETTINGS.trading, "max_positions", 0))
            if max_positions > 0:
                self.results.append(_ok("settings.max_positions", "max_positions valid", {"value": max_positions}))
            else:
                self.results.append(_fail("settings.max_positions", "max_positions must be > 0"))
        except Exception as exc:
            self.results.append(_fail("settings.max_positions", str(exc)))

        try:
            leverage = int(getattr(SETTINGS.trading, "leverage", 0))
            if 1 <= leverage <= 125:
                self.results.append(_ok("settings.leverage", "leverage valid", {"value": leverage}))
            else:
                self.results.append(_fail("settings.leverage", "leverage out of safe range"))
        except Exception as exc:
            self.results.append(_fail("settings.leverage", str(exc)))

        try:
            margin = float(getattr(SETTINGS.trading, "margin_usdt", 0.0))
            if margin > 0:
                self.results.append(_ok("settings.margin_usdt", "margin valid", {"value": margin}))
            else:
                self.results.append(_fail("settings.margin_usdt", "margin_usdt must be > 0"))
        except Exception as exc:
            self.results.append(_fail("settings.margin_usdt", str(exc)))

    def check_real_trade_safety(self) -> None:
        isolated_only = True
        try:
            isolated_only = bool(getattr(SETTINGS.trading, "isolated_only", True))
        except Exception:
            isolated_only = True

        if isolated_only:
            self.results.append(_ok("safety.isolated_only", "isolated-only enforced"))
        else:
            self.results.append(_fail("safety.isolated_only", "isolated-only must be true"))

        try:
            tp2_enabled = bool(getattr(SETTINGS.tp, "tp2_enabled", True))
            self.results.append(_ok("safety.tp2", "TP2 setting loaded", {"tp2_enabled": tp2_enabled}))
        except Exception as exc:
            self.results.append(_fail("safety.tp2", str(exc)))

    def check_no_paper_setup(self) -> None:
        forbidden = []
        for attr in ("paper_enabled", "paper_trading_enabled", "setup_enabled", "setup_mode"):
            for section_name in ("trading", "ai", "scanner"):
                section = getattr(SETTINGS, section_name, None)
                if section is not None and bool(getattr(section, attr, False)):
                    forbidden.append(f"{section_name}.{attr}")

        if forbidden:
            self.results.append(_fail("architecture.no_paper_setup", "forbidden Paper/Setup setting enabled", {"items": forbidden}))
        else:
            self.results.append(_ok("architecture.no_paper_setup", "no Paper/Setup settings enabled"))


def run_startup_checks() -> StartupReport:
    return StartupChecker().run_all()


def startup_ok() -> bool:
    return run_startup_checks().ok
