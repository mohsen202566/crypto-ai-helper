"""
integration_check.py
Level 4 / 1H Smart Scalp Bot

Lightweight integration/self-check runner.

Architecture lock:
- Checks compile/import/version/wiring health.
- Does not place orders, fetch live market data, call Toobit, write trading state,
  or send Telegram messages.
- Safe to run on VPS before restart:
    python integration_check.py
"""

from __future__ import annotations

import importlib
import py_compile
import tempfile
from pathlib import Path
from typing import Any, Mapping

from constants import STATUS_FAILED, STATUS_OK, SYSTEM_VERSION
from utils import safe_str, utc_now_iso


INTEGRATION_CHECK_VERSION: str = SYSTEM_VERSION


CORE_MODULES: list[str] = [
    "constants",
    "utils",
    "state_store",
    "models",
    "strategy_manager",
    "position_manager",
    "signal_manager",
    "market_data",
    "technical_sensors",
    "structure_engine",
    "momentum_engine",
    "liquidity_engine",
    "market_context",
    "reversal_engine",
    "timing_engine",
    "tp_sl_engine",
    "ai_brain",
    "learning_memory",
    "position_monitor",
    "stats_engine",
    "telegram_ui",
    "command_router",
    "bot",
]

OPTIONAL_FINAL_MODULES: list[str] = [
    "tobit_client",
    "real_trade_manager",
]


def _module_file(module_name: str, base_dir: Path | None = None) -> Path:
    root = base_dir or Path(__file__).resolve().parent
    return root / f"{module_name}.py"


def check_compile(modules: list[str] | None = None, *, base_dir: Path | None = None) -> dict[str, Any]:
    names = modules or CORE_MODULES
    errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="l4_compile_") as tmp:
        tmp_path = Path(tmp)
        for name in names:
            path = _module_file(name, base_dir)
            if not path.exists():
                errors.append(f"{name}:missing_file")
                continue
            try:
                py_compile.compile(str(path), cfile=str(tmp_path / f"{name}.pyc"), doraise=True)
            except Exception as exc:
                errors.append(f"{name}:compile_error:{exc}")

    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "checked_at": utc_now_iso(),
    }


def check_imports(modules: list[str] | None = None) -> dict[str, Any]:
    names = modules or CORE_MODULES
    errors: list[str] = []
    imported: list[str] = []

    for name in names:
        try:
            importlib.import_module(name)
            imported.append(name)
        except Exception as exc:
            errors.append(f"{name}:import_error:{exc}")

    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "imported": imported,
        "checked_at": utc_now_iso(),
    }


def check_versions(modules: list[str] | None = None) -> dict[str, Any]:
    names = modules or CORE_MODULES
    errors: list[str] = []
    versions: dict[str, str] = {}

    version_attrs = (
        "SYSTEM_VERSION",
        "UTILS_VERSION",
        "STATE_STORE_VERSION",
        "MODELS_VERSION",
        "STRATEGY_MANAGER_VERSION",
        "POSITION_MANAGER_VERSION",
        "SIGNAL_MANAGER_VERSION",
        "MARKET_DATA_VERSION",
        "TECHNICAL_SENSORS_VERSION",
        "STRUCTURE_ENGINE_VERSION",
        "MOMENTUM_ENGINE_VERSION",
        "LIQUIDITY_ENGINE_VERSION",
        "MARKET_CONTEXT_VERSION",
        "REVERSAL_ENGINE_VERSION",
        "TIMING_ENGINE_VERSION",
        "TP_SL_ENGINE_VERSION",
        "AI_BRAIN_VERSION",
        "LEARNING_MEMORY_VERSION",
        "POSITION_MONITOR_VERSION",
        "STATS_ENGINE_VERSION",
        "TELEGRAM_UI_VERSION",
        "COMMAND_ROUTER_VERSION",
        "BOT_VERSION",
    )

    for name in names:
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            errors.append(f"{name}:cannot_import_for_version:{exc}")
            continue

        found = ""
        for attr in version_attrs:
            if hasattr(module, attr):
                found = safe_str(getattr(module, attr))
                break

        if found:
            versions[name] = found
            if found != SYSTEM_VERSION:
                errors.append(f"{name}:version_mismatch:{found}!={SYSTEM_VERSION}")

    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "versions": versions,
        "checked_at": utc_now_iso(),
    }


def check_required_symbols() -> dict[str, Any]:
    required: dict[str, list[str]] = {
        "models": ["Candle", "MarketSnapshot", "AIDecision", "TradePosition", "TradeOutcome", "TPSLPlan"],
        "position_manager": ["get_open_positions", "load_positions"],
        "learning_memory": ["record_outcome", "get_learning_summary"],
        "stats_engine": ["build_stats_snapshot", "validate_stats_snapshot"],
        "telegram_ui": ["render_ai_decision", "render_stats_snapshot", "render_strategy_status"],
        "command_router": ["parse_command", "validate_route"],
        "bot": ["handle_text_message", "validate_bot_wiring"],
    }

    errors: list[str] = []
    for module_name, names in required.items():
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            errors.append(f"{module_name}:import_error:{exc}")
            continue
        for symbol in names:
            if not hasattr(module, symbol):
                errors.append(f"{module_name}:missing_symbol:{symbol}")

    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "checked_at": utc_now_iso(),
    }


def check_bot_wiring() -> dict[str, Any]:
    errors: list[str] = []
    data: dict[str, Any] = {}

    try:
        bot = importlib.import_module("bot")
        result = bot.validate_bot_wiring()
        data["bot_wiring"] = result
        if not result.get("valid"):
            errors.append(f"bot_wiring_invalid:{result.get('errors')}")
    except Exception as exc:
        errors.append(f"bot_wiring_exception:{exc}")

    try:
        command_router = importlib.import_module("command_router")
        route = command_router.parse_command("آمار", user_id=1, chat_id=2)
        route_validation = command_router.validate_route(route)
        data["route_validation"] = route_validation
        if not route_validation.get("valid"):
            errors.append("route_validation_invalid")
    except Exception as exc:
        errors.append(f"route_validation_exception:{exc}")

    try:
        bot = importlib.import_module("bot")
        response = bot.handle_text_message("راهنما")
        response_validation = bot.validate_bot_response(response)
        data["response_validation"] = response_validation
        if not response_validation.get("valid"):
            errors.append("response_validation_invalid")
    except Exception as exc:
        errors.append(f"response_validation_exception:{exc}")

    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "data": data,
        "checked_at": utc_now_iso(),
    }


def check_optional_final_modules() -> dict[str, Any]:
    present: list[str] = []
    missing: list[str] = []

    for name in OPTIONAL_FINAL_MODULES:
        path = _module_file(name)
        if path.exists():
            present.append(name)
        else:
            missing.append(name)

    return {
        "status": STATUS_OK,
        "valid": True,
        "present": present,
        "missing": missing,
        "note": "tobit_client.py and real_trade_manager.py are intentionally final-stage modules.",
        "checked_at": utc_now_iso(),
    }


def run_integration_check() -> dict[str, Any]:
    compile_result = check_compile()
    import_result = check_imports()
    version_result = check_versions()
    symbol_result = check_required_symbols()
    wiring_result = check_bot_wiring()
    optional_result = check_optional_final_modules()

    sections = {
        "compile": compile_result,
        "imports": import_result,
        "versions": version_result,
        "required_symbols": symbol_result,
        "bot_wiring": wiring_result,
        "optional_final_modules": optional_result,
    }

    errors: list[str] = []
    for name, result in sections.items():
        if name == "optional_final_modules":
            continue
        if not result.get("valid"):
            errors.append(f"{name}:{result.get('errors')}")

    return {
        "system_version": SYSTEM_VERSION,
        "integration_check_version": INTEGRATION_CHECK_VERSION,
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "sections": sections,
        "checked_at": utc_now_iso(),
    }


def format_integration_report(result: Mapping[str, Any]) -> str:
    status = safe_str(result.get("status"))
    lines = [
        "🧪 گزارش Integration Check",
        f"Status: {'OK ✅' if status == STATUS_OK else 'FAILED ❌'}",
        f"Version: {safe_str(result.get('system_version'))}",
        "",
    ]

    sections = result.get("sections", {})
    if isinstance(sections, Mapping):
        for name, section in sections.items():
            if not isinstance(section, Mapping):
                continue
            ok = section.get("valid", False)
            lines.append(f"{name}: {'OK ✅' if ok else 'FAILED ❌'}")
            if not ok:
                lines.append(f"  errors: {section.get('errors')}")

    optional = sections.get("optional_final_modules") if isinstance(sections, Mapping) else {}
    if isinstance(optional, Mapping):
        missing = optional.get("missing", [])
        if missing:
            lines.append("")
            lines.append(f"Final-stage missing modules: {', '.join(missing)}")
            lines.append("این طبیعی است چون Toobit و RealTrade را آخر می‌سازیم.")

    return "\n".join(lines)


def main() -> int:
    result = run_integration_check()
    print(format_integration_report(result))
    return 0 if result.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "INTEGRATION_CHECK_VERSION",
    "CORE_MODULES",
    "OPTIONAL_FINAL_MODULES",
    "check_compile",
    "check_imports",
    "check_versions",
    "check_required_symbols",
    "check_bot_wiring",
    "check_optional_final_modules",
    "run_integration_check",
    "format_integration_report",
    "main",
]
