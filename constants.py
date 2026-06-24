"""
constants.py - Level 4 / 1H Smart Scalp shared constants.

This file is intentionally dependency-free inside the project.
It must not import any project module. Keep it as the single source of truth
for names, modes, events, file paths, commands, and static configuration.
"""

from __future__ import annotations

from pathlib import Path

# =============================================================================
# Version Contract
# =============================================================================

SYSTEM_VERSION = "L4_V1"
ARCHITECTURE_NAME = "Level 4 / 1H Smart Scalp"
STRATEGY_LEVEL = 4
STRATEGY_CODE = "LEVEL_4_1H_SMART_SCALP"

# =============================================================================
# Project Paths
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"

DATA_FILES = {
    "strategy_state": DATA_DIR / "strategy_state.json",
    "positions": DATA_DIR / "positions.json",
    "signals": DATA_DIR / "signals.json",
    "learning_memory": DATA_DIR / "learning_memory.json",
    "ghost_records": DATA_DIR / "ghost_records.json",
    "real_records": DATA_DIR / "real_records.json",
    "stats": DATA_DIR / "stats.json",
    "errors": DATA_DIR / "errors.json",
}

LEGACY_FILES_TO_PRESERVE = (
    "user.py",
    "user.json",
    "start_bot.sh",
)

# =============================================================================
# Timeframes / Scan Policy
# =============================================================================

PRIMARY_TIMEFRAME = "1H"
ENTRY_HELPER_TIMEFRAME = "15m"
CONTEXT_TIMEFRAMES = ("4H", "1D")
ALL_REQUIRED_TIMEFRAMES = (PRIMARY_TIMEFRAME, ENTRY_HELPER_TIMEFRAME, *CONTEXT_TIMEFRAMES)

LEVEL_4_HOLD_MINUTES_MIN = 45
LEVEL_4_HOLD_MINUTES_MAX = 90

SCAN_INTERVAL_SECONDS = 60
MARKET_CONTEXT_REFRESH_SECONDS = 180
LEARNING_CACHE_TTL_SECONDS = 60

# =============================================================================
# Symbols
# =============================================================================

# Level 4 uses a curated liquid universe. BTC/ETH are disabled by default for
# small-capital/min-order safety and can be enabled later through config.
LEVEL_4_SYMBOLS = (
    "DOGEUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "INJUSDT",
    "PEPEUSDT",
    "WIFUSDT",
    "BONKUSDT",
)

CONTEXT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
)

DISABLED_BY_DEFAULT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
)

TOOBIT_SPECIAL_SYMBOL_MAP = {
    "PEPEUSDT": "1000PEPEUSDT",
    "BONKUSDT": "1000BONKUSDT",
    "SHIBUSDT": "1000SHIBUSDT",
    "FLOKIUSDT": "1000FLOKIUSDT",
}

# =============================================================================
# Directions / Modes / States
# =============================================================================

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
VALID_DIRECTIONS = (DIRECTION_LONG, DIRECTION_SHORT)

MODE_REAL = "REAL"
MODE_GHOST = "GHOST"
MODE_REJECT = "REJECT"
VALID_DECISION_MODES = (MODE_REAL, MODE_GHOST, MODE_REJECT)

POSITION_PENDING_REAL_CONFIRM = "PENDING_REAL_CONFIRM"
POSITION_ACTIVE_REAL = "ACTIVE_REAL"
POSITION_ACTIVE_GHOST = "ACTIVE_GHOST"
POSITION_PARTIAL_TP1 = "PARTIAL_TP1"
POSITION_CLOSING = "CLOSING"
POSITION_CLOSED = "CLOSED"
POSITION_FAILED = "FAILED"

OPEN_POSITION_STATES = (
    POSITION_PENDING_REAL_CONFIRM,
    POSITION_ACTIVE_REAL,
    POSITION_ACTIVE_GHOST,
    POSITION_PARTIAL_TP1,
    POSITION_CLOSING,
)

# =============================================================================
# Event System Contract
# =============================================================================

EVENT_SIGNAL_CREATED = "SIGNAL_CREATED"
EVENT_REAL_OPEN_REQUESTED = "REAL_OPEN_REQUESTED"
EVENT_REAL_OPEN_CONFIRMED = "REAL_OPEN_CONFIRMED"
EVENT_REAL_OPEN_FAILED = "REAL_OPEN_FAILED"
EVENT_GHOST_OPENED = "GHOST_OPENED"
EVENT_REJECTED = "REJECTED"
EVENT_TP1 = "TP1"
EVENT_TP2 = "TP2"
EVENT_SL = "SL"
EVENT_AI_EXIT = "AI_EXIT"
EVENT_MANUAL_CLOSE = "MANUAL_CLOSE"
EVENT_CLOSE_REQUESTED = "CLOSE_REQUESTED"
EVENT_CLOSE_CONFIRMED = "CLOSE_CONFIRMED"
EVENT_ERROR = "ERROR"
EVENT_RECOVERY = "RECOVERY"

TRADE_RESULT_EVENTS = (
    EVENT_TP1,
    EVENT_TP2,
    EVENT_SL,
    EVENT_AI_EXIT,
    EVENT_MANUAL_CLOSE,
)

# =============================================================================
# Trade / Risk Config
# =============================================================================

TRADE_CONFIG = {
    "real_trading_default_enabled": False,
    "margin_mode": "ISOLATED",
    "default_margin_usdt": 7.0,
    "default_leverage": 10,
    "min_leverage": 1,
    "max_leverage": 20,
    "max_concurrent_real_positions": 3,
    "max_concurrent_total_positions": 6,
    "block_duplicate_symbol_direction": True,
    "block_cross_margin": True,
    "require_leverage_verification": True,
    "require_position_confirmation": True,
    "real_confirm_timeout_seconds": 70,
    "real_confirm_fast_poll_seconds": 2,
    "real_confirm_slow_poll_seconds": 5,
    "close_confirm_attempts": 5,
    "close_confirm_sleep_seconds": 2,
}

FEE_CONFIG = {
    "estimated_round_trip_fee_rate": 0.0012,
    "minimum_net_profit_usdt": 0.10,
    "reject_if_tp1_net_profit_below_minimum": True,
}

# =============================================================================
# TP / SL Config
# =============================================================================

TP_SL_CONFIG = {
    "tp1_close_ratio": 0.75,
    "runner_ratio_after_tp1": 0.25,
    "base_rr_min": 1.20,
    "base_rr_target": 1.50,
    "base_rr_max": 2.20,
    "min_tp_distance_pct": 0.0025,
    "min_sl_distance_pct": 0.0020,
    "max_sl_distance_pct": 0.0120,
    "avoid_exact_sr_buffer_pct": 0.0010,
    "protect_sl_after_tp1": True,
    "move_sl_to_profit_after_tp1": True,
    "tp2_optional": True,
}

AI_EXIT_CONFIG = {
    "enabled": True,
    "pre_tp1_min_progress_to_tp1": 0.70,
    "allow_emergency_exit_before_progress": True,
    "require_weakness_confirmation": True,
    "confirmation_count_required": 2,
    "confirmation_window_seconds": 70,
    "after_tp1_protect_profit_first": True,
}

# =============================================================================
# AI Decision Thresholds
# =============================================================================

AI_THRESHOLDS = {
    "real_min_score": 78,
    "ghost_min_score": 62,
    "reject_below_score": 62,
    "high_confidence_score": 85,
    "min_structure_score": 55,
    "min_momentum_score": 55,
    "max_trap_risk_for_real": 42,
    "max_market_risk_for_real": 65,
    "learning_boost_cap": 8,
    "learning_penalty_cap": -10,
}

AI_WEIGHTS = {
    "structure": 0.24,
    "momentum": 0.24,
    "liquidity": 0.16,
    "market_context": 0.16,
    "tp_sl_quality": 0.10,
    "learning": 0.10,
}

# =============================================================================
# Market Context
# =============================================================================

MARKET_MODE_BULLISH = "BULLISH"
MARKET_MODE_BEARISH = "BEARISH"
MARKET_MODE_NEUTRAL = "NEUTRAL"
MARKET_MODE_CHOPPY = "CHOPPY"
MARKET_MODE_UNKNOWN = "UNKNOWN"
VALID_MARKET_MODES = (
    MARKET_MODE_BULLISH,
    MARKET_MODE_BEARISH,
    MARKET_MODE_NEUTRAL,
    MARKET_MODE_CHOPPY,
    MARKET_MODE_UNKNOWN,
)

# =============================================================================
# Preflight / Diagnostics
# =============================================================================

PREFLIGHT_CONFIG = {
    "enabled": True,
    "run_once_on_startup": True,
    "heavy_preflight_allowed": False,
    "create_data_dir": True,
    "create_missing_json_files": True,
    "check_json_readable": True,
    "check_system_version": True,
    "check_strategy_state": True,
    "check_positions_recoverable": True,
    "check_toobit_env_only_if_real_enabled": True,
    "market_data_light_ping": True,
    "disable_real_on_exchange_unavailable": True,
    "allow_ghost_when_real_unavailable": True,
}

ERROR_SEVERITY_INFO = "INFO"
ERROR_SEVERITY_WARNING = "WARNING"
ERROR_SEVERITY_ERROR = "ERROR"
ERROR_SEVERITY_CRITICAL = "CRITICAL"

# =============================================================================
# External API / Environment Names
# =============================================================================

OKX_BASE_URL = "https://www.okx.com"
OKX_CANDLE_LIMIT_DEFAULT = 200
OKX_TIMEOUT_SECONDS = 10

TOOBIT_BASE_URL_ENV = "TOBIT_BASE_URL"
TOOBIT_API_KEY_ENV = "TOBIT_API_KEY"
TOOBIT_SECRET_KEY_ENV = "TOBIT_SECRET_KEY"
TOOBIT_REQUEST_TIMEOUT_SECONDS = 10

TELEGRAM_BOT_TOKEN_ENV = "BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV = "CHAT_ID"

# =============================================================================
# Telegram Commands
# =============================================================================

CMD_SET_LEVEL_4 = "استراتژی لول 4"
CMD_STRATEGY = "استراتژی"
CMD_STRATEGY_STATUS = "وضعیت استراتژی"
CMD_STRATEGY_LIST = "لیست استراتژی"

CMD_TRADE_ON = "ترید فعال"
CMD_TRADE_OFF = "ترید خاموش"
CMD_TRADE_STATUS = "وضعیت ترید"

CMD_STATS = "آمار"
CMD_RESET_STATS = "حذف آمار"
CMD_POSITION_STATUS = "وضعیت پوزیشن"
CMD_POSITIONS = "پوزیشن ها"

CMD_LEVERAGE_DOLLAR = "لوریج دلار"
CMD_TRADE_DOLLAR = "ترید دلار"
CMD_POSITION_SIZE = "حجم پوزیشن"
CMD_TRADE_CAPITAL = "سرمایه ترید"
CMD_RESET_TRADE = "ریست ترید"
CMD_ANALYZE_PREFIX = "بررسی"

TELEGRAM_COMMANDS = (
    CMD_SET_LEVEL_4,
    CMD_STRATEGY,
    CMD_STRATEGY_STATUS,
    CMD_STRATEGY_LIST,
    CMD_TRADE_ON,
    CMD_TRADE_OFF,
    CMD_TRADE_STATUS,
    CMD_STATS,
    CMD_RESET_STATS,
    CMD_POSITION_STATUS,
    CMD_POSITIONS,
    CMD_LEVERAGE_DOLLAR,
    CMD_TRADE_DOLLAR,
    CMD_POSITION_SIZE,
    CMD_TRADE_CAPITAL,
    CMD_RESET_TRADE,
)

# =============================================================================
# Function Output Status Names
# =============================================================================

STATUS_OK = "OK"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"
STATUS_BLOCKED = "BLOCKED"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_RECOVERED = "RECOVERED"

# =============================================================================
# Restart Recovery
# =============================================================================

RECOVERY_CONFIG = {
    "resume_open_positions_on_startup": True,
    "recover_pending_real_confirm": True,
    "pending_real_confirm_max_age_seconds": 90,
    "continue_ghost_monitoring_after_restart": True,
    "do_not_delete_unknown_positions": True,
}

# =============================================================================
# Ownership Locks - documentation constants used by diagnostics/tests
# =============================================================================

OWNER_POSITION_WRITES = "real_trade_manager.py"
OWNER_JSON_IO = "state_store.py"
OWNER_TELEGRAM_MESSAGES = "telegram_ui.py"
OWNER_AI_DECISIONS = "ai_brain.py"
OWNER_EXCHANGE_API = "tobit_client.py"
OWNER_MARKET_DATA = "market_data.py"
