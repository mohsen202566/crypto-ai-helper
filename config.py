from __future__ import annotations

"""
Central configuration for the AI Movement Hunter bot.

Rules:
- This file must stay dependency-light.
- Do not import bot.py or high-level modules here.
- Runtime/user-editable settings are stored in data/trade_state.json,
  not hardcoded here.
"""

import os
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Dict, Any, List


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = DATA_DIR / "backups"
LOG_DIR = DATA_DIR / "logs"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_present(name: str) -> bool:
    return name in os.environ and os.getenv(name, "").strip() != ""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "on", "enable", "enabled"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except Exception:
        return default


BOT_TOKEN = _env("BOT_TOKEN")
OWNER_ID = _env_int("OWNER_ID", 0)

TOOBIT_API_KEY = _env("TOOBIT_API_KEY")
TOOBIT_API_SECRET = _env("TOOBIT_API_SECRET")
TOOBIT_BASE_URL = _env("TOOBIT_BASE_URL", "https://api.toobit.com")

# Defaults. Runtime settings can override these through trade_state, but
# environment values must never be silently ignored after restart.
#
# Root fix:
# - REAL_TRADING_ENABLED is the master safety flag.
# - TRADE_MODE controls execution mode when provided.
# - If TRADE_MODE is missing, mode is derived from REAL_TRADING_ENABLED.
# - Existing data/trade_state.json must not permanently trap the bot in PAPER
#   after systemd/env changes; real_trade_manager syncs these values on startup.
def _resolve_real_trading_enabled() -> bool:
    if _env_present("REAL_TRADING_ENABLED"):
        return _env_bool("REAL_TRADING_ENABLED", False)
    if _env_present("TRADE_MODE"):
        return _env("TRADE_MODE", "PAPER").upper() == "REAL"
    # This bot's production architecture is real-trade-first.
    # Keep systemd safety explicit: set REAL_TRADING_ENABLED=false for PAPER-only.
    return True


DEFAULT_REAL_TRADING_ENABLED = _resolve_real_trading_enabled()


def _resolve_trade_mode() -> str:
    mode = _env("TRADE_MODE", "").upper()
    if mode in {"PAPER", "REAL"}:
        return mode
    return "REAL" if DEFAULT_REAL_TRADING_ENABLED else "PAPER"


DEFAULT_TRADE_MODE = _resolve_trade_mode()
DEFAULT_AI_ENABLED = _env_bool("AI_ENABLED", True)
DEFAULT_LEARNING_ENABLED = _env_bool("LEARNING_ENABLED", True)
DEFAULT_DAILY_REPORT_ENABLED = _env_bool("DAILY_REPORT_ENABLED", True)

DEFAULT_INITIAL_CAPITAL = _env_float("INITIAL_CAPITAL", 50.0)
DEFAULT_POSITION_SIZE_USD = _env_float("POSITION_SIZE_USD", 5.0)
DEFAULT_LEVERAGE = _env_int("LEVERAGE", 15)
DEFAULT_MAX_POSITIONS = _env_int("MAX_POSITIONS", 10)

DEFAULT_DAILY_LOSS_LOCK_AMOUNT = _env_float("DAILY_LOSS_LOCK_AMOUNT", 5.0)
DEFAULT_DAILY_LOCK_HOURS = _env_float("DAILY_LOCK_HOURS", 1.0)

AUTO_SIGNAL_ENABLED = _env_bool("AUTO_SIGNAL_ENABLED", True)
AUTO_SCAN_INTERVAL_SECONDS = _env_int("AUTO_SCAN_INTERVAL_SECONDS", 180)
AUTO_SCAN_MAX_SYMBOLS_PER_CYCLE = _env_int("AUTO_SCAN_MAX_SYMBOLS_PER_CYCLE", 40)

REAL_CONFIRM_TIMEOUT_SECONDS = _env_int("REAL_CONFIRM_TIMEOUT_SECONDS", 70)
REAL_CLOSED_PNL_WAIT_SECONDS = _env_int("REAL_CLOSED_PNL_WAIT_SECONDS", 70)

COMMAND_CACHE_TTL_SECONDS = _env_int("COMMAND_CACHE_TTL_SECONDS", 20)
MARKET_CACHE_TTL_SECONDS = _env_int("MARKET_CACHE_TTL_SECONDS", 60)

ISOLATED_MARGIN_ONLY = True

CORE_DATA_FILES = {
    "ai_memory": DATA_DIR / "ai_memory.json",
    "ai_weights": DATA_DIR / "ai_weights.json",
    "ghost_signals": DATA_DIR / "ghost_signals.json",
    "active_signals": DATA_DIR / "active_signals.json",
    "trade_state": DATA_DIR / "trade_state.json",
    "stats": DATA_DIR / "stats.json",
    "coin_learning": DATA_DIR / "coin_learning.json",
    "coin_risk": DATA_DIR / "coin_risk.json",
    "coin_rotation": DATA_DIR / "coin_rotation.json",
    "sr_learning": DATA_DIR / "sr_learning.json",
    "market_cache": DATA_DIR / "market_cache.json",
    "diagnostics": LOG_DIR / "diagnostics.log",
}

DEFAULT_SYMBOLS: List[str] = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TRXUSDT",
    "DOTUSDT", "MATICUSDT", "LTCUSDT", "BCHUSDT", "ATOMUSDT",
    "NEARUSDT", "INJUSDT", "APTUSDT", "ARBUSDT", "OPUSDT",
    "SUIUSDT", "FILUSDT", "ETCUSDT", "UNIUSDT", "AAVEUSDT",
    "PEPEUSDT", "SHIBUSDT", "WIFUSDT", "TIAUSDT", "SEIUSDT",
]


@dataclass
class RuntimeDefaults:
    trade_mode: str = DEFAULT_TRADE_MODE
    real_trading_enabled: bool = DEFAULT_REAL_TRADING_ENABLED
    ai_enabled: bool = DEFAULT_AI_ENABLED
    learning_enabled: bool = DEFAULT_LEARNING_ENABLED
    daily_report_enabled: bool = DEFAULT_DAILY_REPORT_ENABLED
    initial_capital: float = DEFAULT_INITIAL_CAPITAL
    protected_balance: float = DEFAULT_INITIAL_CAPITAL
    balance: float = DEFAULT_INITIAL_CAPITAL
    position_size_usd: float = DEFAULT_POSITION_SIZE_USD
    leverage: int = DEFAULT_LEVERAGE
    max_positions: int = DEFAULT_MAX_POSITIONS
    emergency_stop: bool = False
    daily_loss_lock_enabled: bool = True
    daily_loss_lock_amount: float = DEFAULT_DAILY_LOSS_LOCK_AMOUNT
    daily_lock_hours: float = DEFAULT_DAILY_LOCK_HOURS
    conservative_mode: bool = False
    auto_signal_enabled: bool = AUTO_SIGNAL_ENABLED
    isolated_margin_only: bool = ISOLATED_MARGIN_ONLY


def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def runtime_defaults_dict() -> Dict[str, Any]:
    return asdict(RuntimeDefaults())


def validate_static_config() -> Dict[str, Any]:
    """Return config health without raising. Used by diagnostics/validate_source."""
    ensure_directories()
    issues = []
    if not BOT_TOKEN:
        issues.append("BOT_TOKEN is missing; Telegram bot cannot start.")
    if OWNER_ID == 0:
        issues.append("OWNER_ID is missing or invalid; owner-only alerts may not work.")
    if DEFAULT_TRADE_MODE not in {"PAPER", "REAL"}:
        issues.append(f"Invalid TRADE_MODE={DEFAULT_TRADE_MODE}; expected PAPER or REAL.")
    if DEFAULT_TRADE_MODE == "REAL" and not DEFAULT_REAL_TRADING_ENABLED:
        issues.append("TRADE_MODE=REAL but REAL_TRADING_ENABLED=false; real orders are blocked.")
    if DEFAULT_TRADE_MODE == "REAL" and (not TOOBIT_API_KEY or not TOOBIT_API_SECRET):
        issues.append("TRADE_MODE=REAL requires TOOBIT_API_KEY and TOOBIT_API_SECRET.")
    if DEFAULT_LEVERAGE < 1 or DEFAULT_LEVERAGE > 50:
        issues.append("DEFAULT_LEVERAGE must be between 1 and 50.")
    if DEFAULT_MAX_POSITIONS < 1:
        issues.append("DEFAULT_MAX_POSITIONS must be at least 1.")
    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "data_dir": str(DATA_DIR),
        "backup_dir": str(BACKUP_DIR),
        "log_dir": str(LOG_DIR),
        "trade_mode": DEFAULT_TRADE_MODE,
        "real_trading_enabled": DEFAULT_REAL_TRADING_ENABLED,
    }
