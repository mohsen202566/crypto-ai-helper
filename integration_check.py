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
    "candidate_selector",
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
        "CANDIDATE_SELECTOR_VERSION",
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



# =============================================================================
# Hunter architecture contract checks
# =============================================================================

HUNTER_REQUIRED_FIELDS: list[str] = [
    "start_score",
    "start_active",
    "start_signal_count",
    "start_reasons",
    "chase_risk_score",
    "chase_active",
    "move_age_score",
    "late_risk_score",
    "fresh_momentum_score",
    "exhaustion_score",
    "start_pressure_score",
    "structure_start_active",
    "momentum_start_active",
    "liquidity_start_active",
    "fresh_context_active",
    "early_start_synergy",
]

HUNTER_SOURCE_CONTRACTS: dict[str, list[str]] = {
    # Upstream engines must expose fresh-start / anti-chase signals.
    "structure_engine": [
        "move_start_zone",
        "move_already_extended",
        "room_to_target",
        "atr_expansion_start",
        "micro_structure_shift",
    ],
    "momentum_engine": [
        "momentum_start_active",
        "start_pressure_score",
        "fresh_momentum_score",
        "move_age_score",
        "exhaustion_score",
    ],
    "timing_engine": [
        "late_risk_score",
        "move_age_score",
        "fresh_momentum_score",
        "wait_for_better_entry",
        "timing_start_active",
    ],
    "reversal_engine": [
        "reversal_probability",
        "exhaustion_probability",
        "continuation_probability",
        "early_start_synergy",
    ],
    "liquidity_engine": [
        "trap_risk_score",
        "fake_break_risk",
        "liquidity_start_active",
        "start_liquidity_score",
    ],
    "market_context": [
        "fresh_context_active",
        "fresh_context_score",
        "context_score",
    ],
    # Final decision and downstream routing must preserve the same fields.
    "ai_brain": [
        "start_evidence_profile",
        "REAL_BLOCK_NO_START_EVIDENCE",
        "REAL_BLOCK_CHASE_RISK",
        "REAL_BLOCK_MOVE_TOO_OLD",
        "SCORE_PENALTY_CHASE_RISK",
        "learning_features",
        *HUNTER_REQUIRED_FIELDS,
    ],
    "candidate_selector": [
        "select_best_real_candidates",
        "selector_selected_for_real",
        "selector_rank_score",
        "start_quality",
        "chase_safety",
        "start_score",
        "chase_risk_score",
        "move_age_score",
    ],
    "signal_manager": [
        "record_signal",
        "start_score",
        "chase_risk_score",
        "move_age_score",
        "start_signal_count",
    ],
    "bot": [
        "select_best_real_candidates",
        "_selector_real_guard_blocks",
        "selector_selected_for_real",
        "_save_response_message_links",
        "_save_signal_message_link",
        "reply_to_message_id",
        "signal_message_id",
    ],
    "position_monitor": [
        "reply_to_message_id",
        "signal_message_id",
        "MonitorEvent",
    ],
}


def _read_module_source(module_name: str, *, base_dir: Path | None = None) -> str:
    path = _module_file(module_name, base_dir)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(errors="ignore")
    except Exception:
        return ""


def check_hunter_source_contracts(*, base_dir: Path | None = None) -> dict[str, Any]:
    """Check that the full hunter pipeline still exposes the locked fields.

    This is intentionally source/static based: it must stay safe on VPS and must
    not fetch candles, call Toobit, place orders, or mutate trading state.
    """
    errors: list[str] = []
    checked: dict[str, list[str]] = {}

    for module_name, required_terms in HUNTER_SOURCE_CONTRACTS.items():
        source = _read_module_source(module_name, base_dir=base_dir)
        if not source:
            errors.append(f"{module_name}:missing_or_unreadable_source")
            continue
        missing = [term for term in required_terms if term not in source]
        checked[module_name] = list(required_terms)
        if missing:
            errors.append(f"{module_name}:missing_hunter_terms:{missing}")

    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "checked": checked,
        "checked_at": utc_now_iso(),
    }


def check_hunter_required_symbols() -> dict[str, Any]:
    """Check runtime symbols needed by the hunter chain after imports succeed."""
    required: dict[str, list[str]] = {
        "ai_brain": [
            "start_evidence_profile",
            "adjusted_score_for_entry_quality",
            "build_ai_decision",
            "validate_ai_decision",
        ],
        "candidate_selector": [
            "select_best_real_candidates",
            "summarize_selection",
            "validate_selection",
        ],
        "signal_manager": [
            "record_signal",
        ],
        "bot": [
            "scan_market_with_provider",
            "maybe_execute_real_decision",
            "persist_signal_lifecycle",
        ],
        "position_monitor": [
            "monitor_positions_once",
        ],
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
                errors.append(f"{module_name}:missing_hunter_symbol:{symbol}")

    # attach_message_id is not strictly required for compile, but without it the
    # signal record may not store Telegram message_id. Position-level reply still
    # works through bot.py, so report it as a warning rather than hard failure.
    warnings: list[str] = []
    try:
        signal_manager = importlib.import_module("signal_manager")
        if not hasattr(signal_manager, "attach_message_id"):
            warnings.append("signal_manager:missing_optional_attach_message_id")
    except Exception as exc:
        errors.append(f"signal_manager:import_error_for_optional_attach:{exc}")

    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "checked_at": utc_now_iso(),
    }


def check_hunter_metadata_flow(*, base_dir: Path | None = None) -> dict[str, Any]:
    """Verify start/anti-chase metadata is preserved AI -> selector -> signal/bot."""
    errors: list[str] = []
    flow_modules = ["ai_brain", "candidate_selector", "signal_manager", "bot"]
    field_presence: dict[str, list[str]] = {}

    for module_name in flow_modules:
        source = _read_module_source(module_name, base_dir=base_dir)
        if not source:
            errors.append(f"{module_name}:missing_or_unreadable_source")
            continue
        present = [field for field in HUNTER_REQUIRED_FIELDS if field in source]
        field_presence[module_name] = present
        required_min = 5 if module_name in {"signal_manager", "bot"} else 10
        if len(present) < required_min:
            missing = [field for field in HUNTER_REQUIRED_FIELDS if field not in present]
            errors.append(f"{module_name}:hunter_metadata_flow_weak:missing={missing}")

    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "field_presence": field_presence,
        "checked_at": utc_now_iso(),
    }


def check_reply_chain_contract(*, base_dir: Path | None = None) -> dict[str, Any]:
    """Check Signal -> message_id -> Position -> monitor reply contract."""
    errors: list[str] = []

    bot_source = _read_module_source("bot", base_dir=base_dir)
    monitor_source = _read_module_source("position_monitor", base_dir=base_dir)
    signal_source = _read_module_source("signal_manager", base_dir=base_dir)

    if not bot_source:
        errors.append("bot:missing_or_unreadable_source")
    else:
        for term in ["_save_response_message_links", "_save_signal_message_link", "signal_message_id", "execution.get(\"position_id\")", "reply_to_message_id"]:
            if term not in bot_source:
                errors.append(f"bot:reply_chain_missing:{term}")

    if not monitor_source:
        errors.append("position_monitor:missing_or_unreadable_source")
    else:
        for term in ["reply_to_message_id", "signal_message_id", "MonitorEvent"]:
            if term not in monitor_source:
                errors.append(f"position_monitor:reply_chain_missing:{term}")

    # signal_manager attach is optional because bot.py also stores on Position.
    warnings: list[str] = []
    if signal_source and "attach_message_id" not in signal_source:
        warnings.append("signal_manager:attach_message_id_not_found_position_reply_still_required")

    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
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
        "candidate_selector": ["select_best_real_candidates", "validate_selection"],
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
    hunter_source_result = check_hunter_source_contracts()
    hunter_symbols_result = check_hunter_required_symbols()
    hunter_flow_result = check_hunter_metadata_flow()
    reply_chain_result = check_reply_chain_contract()
    wiring_result = check_bot_wiring()
    optional_result = check_optional_final_modules()

    sections = {
        "compile": compile_result,
        "imports": import_result,
        "versions": version_result,
        "required_symbols": symbol_result,
        "hunter_source_contracts": hunter_source_result,
        "hunter_required_symbols": hunter_symbols_result,
        "hunter_metadata_flow": hunter_flow_result,
        "reply_chain_contract": reply_chain_result,
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
            warnings = section.get("warnings", [])
            if warnings:
                lines.append(f"  warnings: {warnings}")

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
    "check_hunter_source_contracts",
    "check_hunter_required_symbols",
    "check_hunter_metadata_flow",
    "check_reply_chain_contract",
    "check_bot_wiring",
    "check_optional_final_modules",
    "run_integration_check",
    "format_integration_report",
    "main",
]
