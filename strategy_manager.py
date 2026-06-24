"""
strategy_manager.py
Level 4 / 1H Smart Scalp Bot

Strategy state manager.

Architecture lock:
- Owns active strategy/level state and trade on/off flag.
- Blocks all non-Level-4 new signals while Level 4 is active.
- Does not analyze market, run AI, place orders, monitor positions, or build Telegram text.
- Allowed project imports: constants.py, state_store.py, models.py, utils.py only.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from constants import (
    DATA_FILES,
    CMD_SET_LEVEL_4,
    MODE_GHOST,
    MODE_REAL,
    STATUS_FAILED,
    STATUS_OK,
    STRATEGY_CODE,
    STRATEGY_LEVEL,
    SYSTEM_VERSION,
    TRADE_CONFIG,
)
from models import RecordResult, to_dict
from state_store import load_json, save_json_atomic, log_error, log_info
from utils import safe_bool, safe_float, safe_int, safe_str, utc_now_iso


STRATEGY_MANAGER_VERSION: str = SYSTEM_VERSION
STRATEGY_STATE_KEY: str = "strategy_state"
MIN_TRADE_MARGIN_USDT: float = 1.0
MAX_TRADE_MARGIN_USDT: float = 1000.0


def _clamp_margin_usdt(value: Any, default: float = 1.0) -> float:
    """Clamp user-configurable trade margin to the locked 1-1000 USDT range."""
    margin = safe_float(value, default)
    if margin is None:
        margin = default
    return max(MIN_TRADE_MARGIN_USDT, min(MAX_TRADE_MARGIN_USDT, float(margin)))


# =============================================================================
# Default state
# =============================================================================

def default_strategy_state() -> dict[str, Any]:
    """Return default strategy state for this Level 4 architecture."""
    return {
        "system_version": SYSTEM_VERSION,
        "active_level": STRATEGY_LEVEL,
        "active_strategy": STRATEGY_CODE,
        "real_trading_enabled": bool(TRADE_CONFIG.get("real_trading_default_enabled", False)),
        # margin_usdt is kept for backward compatibility.
        # min/max margin are the new AI position-sizing bounds; when equal, behavior is fixed-size.
        "margin_usdt": float(TRADE_CONFIG.get("default_margin_usdt", 7.0)),
        "min_margin_usdt": float(TRADE_CONFIG.get("default_min_margin_usdt", TRADE_CONFIG.get("default_margin_usdt", 7.0))),
        "max_margin_usdt": float(TRADE_CONFIG.get("default_max_margin_usdt", TRADE_CONFIG.get("default_margin_usdt", 7.0))),
        "leverage": int(TRADE_CONFIG.get("default_leverage", 10)),
        "max_concurrent_real_positions": int(TRADE_CONFIG.get("max_concurrent_real_positions", 3)),
        "max_concurrent_total_positions": int(TRADE_CONFIG.get("max_concurrent_total_positions", 6)),
        "updated_at": utc_now_iso(),
    }


def normalize_strategy_state(state: Any) -> dict[str, Any]:
    """Normalize old/missing strategy state into the Level 4 contract."""
    if not isinstance(state, dict):
        state = {}

    defaults = default_strategy_state()
    normalized = dict(defaults)
    normalized.update(state)

    normalized["system_version"] = safe_str(normalized.get("system_version"), SYSTEM_VERSION)
    normalized["active_level"] = safe_int(normalized.get("active_level"), STRATEGY_LEVEL) or STRATEGY_LEVEL
    normalized["active_strategy"] = safe_str(normalized.get("active_strategy"), STRATEGY_CODE) or STRATEGY_CODE
    normalized["real_trading_enabled"] = safe_bool(normalized.get("real_trading_enabled"), False)

    # Normalize margin settings. Old states only have margin_usdt, so default min=max=margin
    # to preserve the previous fixed-margin behavior until the new commands are used.
    legacy_margin = _clamp_margin_usdt(normalized.get("margin_usdt"), defaults["margin_usdt"])
    min_margin = _clamp_margin_usdt(normalized.get("min_margin_usdt", legacy_margin), legacy_margin)
    max_margin = _clamp_margin_usdt(normalized.get("max_margin_usdt", legacy_margin), legacy_margin)

    if min_margin > max_margin:
        # Self-heal corrupted/old state without crashing the bot.
        max_margin = min_margin

    active_margin = _clamp_margin_usdt(normalized.get("margin_usdt", min_margin), min_margin)
    active_margin = max(min_margin, min(max_margin, active_margin))

    normalized["min_margin_usdt"] = min_margin
    normalized["max_margin_usdt"] = max_margin
    normalized["margin_usdt"] = active_margin

    leverage = safe_int(normalized.get("leverage"), defaults["leverage"]) or defaults["leverage"]
    min_lev = safe_int(TRADE_CONFIG.get("min_leverage"), 1) or 1
    max_lev = safe_int(TRADE_CONFIG.get("max_leverage"), 20) or 20
    normalized["leverage"] = max(min_lev, min(max_lev, leverage))

    normalized["max_concurrent_real_positions"] = max(
        0,
        safe_int(
            normalized.get("max_concurrent_real_positions"),
            defaults["max_concurrent_real_positions"],
        ) or defaults["max_concurrent_real_positions"],
    )
    normalized["max_concurrent_total_positions"] = max(
        0,
        safe_int(
            normalized.get("max_concurrent_total_positions"),
            defaults["max_concurrent_total_positions"],
        ) or defaults["max_concurrent_total_positions"],
    )

    normalized["updated_at"] = safe_str(normalized.get("updated_at"), utc_now_iso())
    return normalized


# =============================================================================
# State IO
# =============================================================================

def load_strategy_state() -> dict[str, Any]:
    """Load and normalize strategy state from data/strategy_state.json."""
    state = load_json(STRATEGY_STATE_KEY, default=default_strategy_state())
    normalized = normalize_strategy_state(state)

    # Save back only if missing/old shape was normalized.
    if state != normalized:
        save_strategy_state(normalized)

    return normalized


def save_strategy_state(state: Mapping[str, Any]) -> bool:
    """Save normalized strategy state."""
    normalized = normalize_strategy_state(dict(state))
    normalized["updated_at"] = utc_now_iso()
    return save_json_atomic(STRATEGY_STATE_KEY, normalized)


def reset_strategy_state() -> RecordResult:
    """Reset strategy state to default Level 4 values."""
    ok = save_strategy_state(default_strategy_state())
    return RecordResult(
        status=STATUS_OK if ok else STATUS_FAILED,
        recorded=ok,
        message="strategy_state_reset" if ok else "strategy_state_reset_failed",
    )


# =============================================================================
# Strategy / level control
# =============================================================================

def is_level4_active(state: Optional[Mapping[str, Any]] = None) -> bool:
    """Return True if Level 4 is active for new scans/signals."""
    if state is None:
        state = load_strategy_state()

    level = safe_int(state.get("active_level"), 0) or 0
    code = safe_str(state.get("active_strategy"))
    return level == STRATEGY_LEVEL and code == STRATEGY_CODE


def set_level4_active() -> RecordResult:
    """
    Activate Level 4 for new opportunities.

    Open positions from any previous level are not deleted or modified here.
    position_monitor keeps managing existing positions according to their stored level.
    """
    state = load_strategy_state()
    state["active_level"] = STRATEGY_LEVEL
    state["active_strategy"] = STRATEGY_CODE
    ok = save_strategy_state(state)

    if ok:
        log_info(
            "strategy_manager",
            "set_level4_active",
            "Level 4 strategy activated.",
            {"active_level": STRATEGY_LEVEL, "active_strategy": STRATEGY_CODE},
        )

    return RecordResult(
        status=STATUS_OK if ok else STATUS_FAILED,
        recorded=ok,
        message="level4_active" if ok else "level4_activation_failed",
        metadata={"active_level": STRATEGY_LEVEL, "active_strategy": STRATEGY_CODE},
    )


def can_scan_new_opportunities(state: Optional[Mapping[str, Any]] = None) -> bool:
    """Return True if this process is allowed to scan for new Level 4 signals."""
    return is_level4_active(state)


def can_create_signal_for_level(level: Any, state: Optional[Mapping[str, Any]] = None) -> bool:
    """
    Enforce single-active-level rule for new signals.

    Only active Level 4 may create new signals in this architecture.
    """
    if state is None:
        state = load_strategy_state()

    requested_level = safe_int(level, 0) or 0
    return requested_level == STRATEGY_LEVEL and is_level4_active(state)


def block_reason_for_level(level: Any, state: Optional[Mapping[str, Any]] = None) -> str:
    """Return a stable reason code when a level is blocked."""
    if can_create_signal_for_level(level, state):
        return ""
    return "LEVEL_NOT_ACTIVE"


# =============================================================================
# Trade on/off control
# =============================================================================

def is_real_trading_enabled(state: Optional[Mapping[str, Any]] = None) -> bool:
    """Return True if REAL trading is enabled in strategy state."""
    if state is None:
        state = load_strategy_state()
    return safe_bool(state.get("real_trading_enabled"), False)


def set_real_trading(enabled: bool) -> RecordResult:
    """Set real trading on/off."""
    state = load_strategy_state()
    state["real_trading_enabled"] = bool(enabled)
    ok = save_strategy_state(state)

    if ok:
        log_info(
            "strategy_manager",
            "set_real_trading",
            "Real trading flag changed.",
            {"enabled": bool(enabled)},
        )

    return RecordResult(
        status=STATUS_OK if ok else STATUS_FAILED,
        recorded=ok,
        message="real_trading_enabled" if enabled and ok else "real_trading_disabled" if ok else "real_trading_update_failed",
        metadata={"real_trading_enabled": bool(enabled)},
    )


def enable_real_trading() -> RecordResult:
    """Enable REAL trading."""
    return set_real_trading(True)


def disable_real_trading() -> RecordResult:
    """Disable REAL trading. New REAL decisions must be downgraded to GHOST by bot/AI flow."""
    return set_real_trading(False)


def execution_mode_for_new_decision(requested_mode: str, state: Optional[Mapping[str, Any]] = None) -> str:
    """
    Return executable mode for a new AI decision.

    If AI requests REAL but trade is OFF, downgrade to GHOST.
    """
    mode = safe_str(requested_mode).upper()
    if mode == MODE_REAL and not is_real_trading_enabled(state):
        return MODE_GHOST
    return mode


# =============================================================================
# Trade config control
# =============================================================================

def get_trade_runtime_config(state: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Return current runtime trade config merged from constants + strategy state."""
    if state is None:
        state = load_strategy_state()

    default_margin = _clamp_margin_usdt(TRADE_CONFIG.get("default_margin_usdt"), 7.0)
    default_leverage = safe_int(TRADE_CONFIG.get("default_leverage"), 10) or 10
    min_margin = _clamp_margin_usdt(state.get("min_margin_usdt"), default_margin)
    max_margin = _clamp_margin_usdt(state.get("max_margin_usdt"), default_margin)
    if min_margin > max_margin:
        max_margin = min_margin
    margin = _clamp_margin_usdt(state.get("margin_usdt"), min_margin)
    margin = max(min_margin, min(max_margin, margin))
    dynamic_position_sizing = max_margin > min_margin

    return {
        "real_trading_enabled": is_real_trading_enabled(state),
        "margin_usdt": margin,
        "min_margin_usdt": min_margin,
        "max_margin_usdt": max_margin,
        "dynamic_position_sizing_enabled": dynamic_position_sizing,
        "position_sizing_mode": "AI_DYNAMIC" if dynamic_position_sizing else "FIXED",
        "leverage": safe_int(state.get("leverage"), default_leverage) or default_leverage,
        "margin_mode": safe_str(TRADE_CONFIG.get("margin_mode"), "ISOLATED"),
        "max_concurrent_real_positions": safe_int(
            state.get("max_concurrent_real_positions"),
            TRADE_CONFIG.get("max_concurrent_real_positions", 3),
        ) or 3,
        "max_concurrent_total_positions": safe_int(
            state.get("max_concurrent_total_positions"),
            TRADE_CONFIG.get("max_concurrent_total_positions", 6),
        ) or 6,
        "require_leverage_verification": safe_bool(TRADE_CONFIG.get("require_leverage_verification"), True),
        "require_position_confirmation": safe_bool(TRADE_CONFIG.get("require_position_confirmation"), True),
    }


def set_margin_usdt(margin_usdt: Any) -> RecordResult:
    """
    Set fixed runtime margin per trade.

    Backward compatibility: the old command "ترید دلار 7" keeps working by
    setting min=max=margin, which disables dynamic AI margin sizing.
    """
    margin_raw = safe_float(margin_usdt, None)
    if margin_raw is None or margin_raw < MIN_TRADE_MARGIN_USDT or margin_raw > MAX_TRADE_MARGIN_USDT:
        return RecordResult(
            status=STATUS_FAILED,
            recorded=False,
            message="invalid_margin",
            error="margin_usdt must be between 1 and 1000",
        )

    margin = _clamp_margin_usdt(margin_raw, MIN_TRADE_MARGIN_USDT)
    state = load_strategy_state()
    state["margin_usdt"] = margin
    state["min_margin_usdt"] = margin
    state["max_margin_usdt"] = margin
    ok = save_strategy_state(state)
    return RecordResult(
        status=STATUS_OK if ok else STATUS_FAILED,
        recorded=ok,
        message="margin_updated" if ok else "margin_update_failed",
        metadata={"margin_usdt": margin, "min_margin_usdt": margin, "max_margin_usdt": margin, "position_sizing_mode": "FIXED"},
    )


def set_min_margin_usdt(min_margin_usdt: Any) -> RecordResult:
    """Set the minimum margin AI is allowed to use per REAL trade, 1-1000 USDT."""
    value_raw = safe_float(min_margin_usdt, None)
    if value_raw is None or value_raw < MIN_TRADE_MARGIN_USDT or value_raw > MAX_TRADE_MARGIN_USDT:
        return RecordResult(
            status=STATUS_FAILED,
            recorded=False,
            message="invalid_min_margin",
            error="min_margin_usdt must be between 1 and 1000",
        )

    state = load_strategy_state()
    value = _clamp_margin_usdt(value_raw, MIN_TRADE_MARGIN_USDT)
    current_max = _clamp_margin_usdt(state.get("max_margin_usdt", state.get("margin_usdt", value)), value)
    if value > current_max:
        return RecordResult(
            status=STATUS_FAILED,
            recorded=False,
            message="invalid_min_margin",
            error="min_margin_usdt cannot be greater than max_margin_usdt",
            metadata={"min_margin_usdt": value, "max_margin_usdt": current_max},
        )

    state["min_margin_usdt"] = value
    state["margin_usdt"] = max(value, min(current_max, _clamp_margin_usdt(state.get("margin_usdt", value), value)))
    ok = save_strategy_state(state)
    return RecordResult(
        status=STATUS_OK if ok else STATUS_FAILED,
        recorded=ok,
        message="min_margin_updated" if ok else "min_margin_update_failed",
        metadata={"min_margin_usdt": value, "max_margin_usdt": current_max, "position_sizing_mode": "AI_DYNAMIC" if current_max > value else "FIXED"},
    )


def set_max_margin_usdt(max_margin_usdt: Any) -> RecordResult:
    """Set the maximum margin AI is allowed to use per REAL trade, 1-1000 USDT."""
    value_raw = safe_float(max_margin_usdt, None)
    if value_raw is None or value_raw < MIN_TRADE_MARGIN_USDT or value_raw > MAX_TRADE_MARGIN_USDT:
        return RecordResult(
            status=STATUS_FAILED,
            recorded=False,
            message="invalid_max_margin",
            error="max_margin_usdt must be between 1 and 1000",
        )

    state = load_strategy_state()
    value = _clamp_margin_usdt(value_raw, MAX_TRADE_MARGIN_USDT)
    current_min = _clamp_margin_usdt(state.get("min_margin_usdt", state.get("margin_usdt", value)), value)
    if value < current_min:
        return RecordResult(
            status=STATUS_FAILED,
            recorded=False,
            message="invalid_max_margin",
            error="max_margin_usdt cannot be less than min_margin_usdt",
            metadata={"min_margin_usdt": current_min, "max_margin_usdt": value},
        )

    state["max_margin_usdt"] = value
    state["margin_usdt"] = max(current_min, min(value, _clamp_margin_usdt(state.get("margin_usdt", current_min), current_min)))
    ok = save_strategy_state(state)
    return RecordResult(
        status=STATUS_OK if ok else STATUS_FAILED,
        recorded=ok,
        message="max_margin_updated" if ok else "max_margin_update_failed",
        metadata={"min_margin_usdt": current_min, "max_margin_usdt": value, "position_sizing_mode": "AI_DYNAMIC" if value > current_min else "FIXED"},
    )


def set_leverage(leverage: Any) -> RecordResult:
    """Set runtime leverage, clamped to configured min/max."""
    lev = safe_int(leverage, None)
    if lev is None:
        return RecordResult(
            status=STATUS_FAILED,
            recorded=False,
            message="invalid_leverage",
            error="leverage must be an integer",
        )

    min_lev = safe_int(TRADE_CONFIG.get("min_leverage"), 1) or 1
    max_lev = safe_int(TRADE_CONFIG.get("max_leverage"), 20) or 20
    lev = max(min_lev, min(max_lev, lev))

    state = load_strategy_state()
    state["leverage"] = lev
    ok = save_strategy_state(state)
    return RecordResult(
        status=STATUS_OK if ok else STATUS_FAILED,
        recorded=ok,
        message="leverage_updated" if ok else "leverage_update_failed",
        metadata={"leverage": lev},
    )


# =============================================================================
# Status helpers for bot / telegram_ui
# =============================================================================

def get_strategy_status() -> dict[str, Any]:
    """Return status payload. telegram_ui.py turns this into Persian text."""
    state = load_strategy_state()
    runtime = get_trade_runtime_config(state)

    return {
        "system_version": SYSTEM_VERSION,
        "active_level": state.get("active_level"),
        "active_strategy": state.get("active_strategy"),
        "level4_active": is_level4_active(state),
        "real_trading_enabled": runtime["real_trading_enabled"],
        "execution_when_trade_off": MODE_GHOST,
        "margin_usdt": runtime["margin_usdt"],
        "min_margin_usdt": runtime["min_margin_usdt"],
        "max_margin_usdt": runtime["max_margin_usdt"],
        "dynamic_position_sizing_enabled": runtime["dynamic_position_sizing_enabled"],
        "position_sizing_mode": runtime["position_sizing_mode"],
        "leverage": runtime["leverage"],
        "max_concurrent_real_positions": runtime["max_concurrent_real_positions"],
        "max_concurrent_total_positions": runtime["max_concurrent_total_positions"],
        "updated_at": state.get("updated_at"),
    }


def list_available_strategies() -> list[dict[str, Any]]:
    """
    Return available strategies.

    For this implementation only Level 4 is enabled for new opportunities.
    Other levels may exist in old code, but are inactive under single-level rule.
    """
    return [
        {
            "level": STRATEGY_LEVEL,
            "code": STRATEGY_CODE,
            "name": "Level 4 / 1H Smart Scalp",
            "active": True,
            "new_signals_allowed": True,
        }
    ]


def handle_strategy_command(text: str) -> Optional[RecordResult]:
    """
    Handle only strategy/trade state-changing commands.

    bot.py may use this helper, but telegram_ui.py still owns message text.
    """
    cmd = safe_str(text)

    if cmd == CMD_SET_LEVEL_4:
        return set_level4_active()

    return None


# =============================================================================
# Startup validation
# =============================================================================

def validate_strategy_state_light() -> dict[str, Any]:
    """
    Lightweight startup check for strategy state.

    Does not touch market APIs, Toobit, AI, or positions.
    """
    try:
        state = load_strategy_state()
        min_margin = safe_float(state.get("min_margin_usdt"), 0.0) or 0.0
        max_margin = safe_float(state.get("max_margin_usdt"), 0.0) or 0.0
        valid = (
            state.get("system_version") == SYSTEM_VERSION
            and safe_int(state.get("active_level"), 0) == STRATEGY_LEVEL
            and safe_str(state.get("active_strategy")) == STRATEGY_CODE
            and MIN_TRADE_MARGIN_USDT <= min_margin <= MAX_TRADE_MARGIN_USDT
            and MIN_TRADE_MARGIN_USDT <= max_margin <= MAX_TRADE_MARGIN_USDT
            and min_margin <= max_margin
        )

        return {
            "status": STATUS_OK if valid else STATUS_FAILED,
            "valid": valid,
            "system_version": SYSTEM_VERSION,
            "level4_active": is_level4_active(state),
            "real_trading_enabled": is_real_trading_enabled(state),
            "checked_at": utc_now_iso(),
        }

    except Exception as exc:
        log_error(
            module="strategy_manager",
            function="validate_strategy_state_light",
            error=exc,
        )
        return {
            "status": STATUS_FAILED,
            "valid": False,
            "system_version": SYSTEM_VERSION,
            "error": str(exc),
            "checked_at": utc_now_iso(),
        }


__all__ = [
    "STRATEGY_MANAGER_VERSION",
    "STRATEGY_STATE_KEY",
    "MIN_TRADE_MARGIN_USDT",
    "MAX_TRADE_MARGIN_USDT",
    "default_strategy_state",
    "normalize_strategy_state",
    "load_strategy_state",
    "save_strategy_state",
    "reset_strategy_state",
    "is_level4_active",
    "set_level4_active",
    "can_scan_new_opportunities",
    "can_create_signal_for_level",
    "block_reason_for_level",
    "is_real_trading_enabled",
    "set_real_trading",
    "enable_real_trading",
    "disable_real_trading",
    "execution_mode_for_new_decision",
    "get_trade_runtime_config",
    "set_margin_usdt",
    "set_min_margin_usdt",
    "set_max_margin_usdt",
    "set_leverage",
    "get_strategy_status",
    "list_available_strategies",
    "handle_strategy_command",
    "validate_strategy_state_light",
]
