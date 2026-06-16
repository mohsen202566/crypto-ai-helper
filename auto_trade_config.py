# -*- coding: utf-8 -*-
"""
auto_trade_config.py

Central safety limits/defaults for REAL / TOOBIT trading.

Important:
- This file only defines configuration values and helper readers.
- It does NOT control AI, scanner, signal scoring, auto-signal logic, or Telegram handlers.
- Real trading must stay OFF / zero by default until the user configures it from Telegram.
"""

import os


def get_env_str(name, default=None):
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value != "" else default


def get_env_int(name, default):
    value = get_env_str(name)
    if value is None:
        return int(default)
    try:
        return int(float(value))
    except Exception:
        return int(default)


def get_env_float(name, default):
    value = get_env_str(name)
    if value is None:
        return float(default)
    try:
        return float(value)
    except Exception:
        return float(default)


def get_env_bool(name, default=False):
    value = get_env_str(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in ["1", "true", "yes", "on", "enable", "enabled"]


def clamp_float(value, minimum, maximum):
    try:
        v = float(value)
    except Exception:
        v = float(minimum)
    return max(float(minimum), min(float(v), float(maximum)))


def clamp_int(value, minimum, maximum):
    try:
        v = int(float(value))
    except Exception:
        v = int(minimum)
    return max(int(minimum), min(int(v), int(maximum)))


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------
# Keep this aligned with real_trade_manager.py.
# Older modules may still import AUTO_TRADE_STATE_FILE, so keep the name.
AUTO_TRADE_STATE_FILE = get_env_str("AUTO_TRADE_STATE_FILE", "data/real_trade_state.json")


# ---------------------------------------------------------------------------
# Main trade state defaults
# ---------------------------------------------------------------------------
# Real trading must never start automatically.
DEFAULT_TRADE_ENABLED = get_env_bool("DEFAULT_TRADE_ENABLED", False)
DEFAULT_TRADE_MODE = "REAL"


# ---------------------------------------------------------------------------
# Legacy defaults kept only for backward compatibility.
# Telegram trading controls now use REAL / TOBIT values managed by
# real_trade_manager.py.
# ---------------------------------------------------------------------------
DEFAULT_START_BALANCE_USDT = get_env_float("DEFAULT_START_BALANCE_USDT", 50.0)
DEFAULT_TRADE_MARGIN_USDT = get_env_float("DEFAULT_TRADE_MARGIN_USDT", 5.0)
DEFAULT_LEVERAGE = clamp_int(get_env_int("DEFAULT_LEVERAGE", 10), 1, 100)
DEFAULT_MAX_OPEN_POSITIONS = clamp_int(get_env_int("DEFAULT_MAX_OPEN_POSITIONS", 5), 1, 100)

DEFAULT_DAILY_MAX_LOSS_USDT = get_env_float("DEFAULT_DAILY_MAX_LOSS_USDT", 7.0)
DEFAULT_COOLDOWN_AFTER_DAILY_LOSS_HOURS = get_env_int("DEFAULT_COOLDOWN_AFTER_DAILY_LOSS_HOURS", 1)


# ---------------------------------------------------------------------------
# Real Toobit trading safety defaults
# ---------------------------------------------------------------------------
# Initial real values stay zero/off until the user explicitly configures:
#   سرمایه ترید
#   ترید دلار
#   ترید لوریج
#   حداکثر پوزیشن
#
# This prevents the bot from opening a real futures order with hidden defaults.
# ---------------------------------------------------------------------------
DEFAULT_REAL_TRADING_ENABLED = get_env_bool("DEFAULT_REAL_TRADING_ENABLED", False)
DEFAULT_REAL_EMERGENCY_STOP = get_env_bool("DEFAULT_REAL_EMERGENCY_STOP", True)
DEFAULT_REAL_START_BALANCE_USDT = get_env_float("DEFAULT_REAL_START_BALANCE_USDT", 0.0)
DEFAULT_REAL_TRADE_MARGIN_USDT = get_env_float("DEFAULT_REAL_TRADE_MARGIN_USDT", 0.0)
DEFAULT_REAL_LEVERAGE = clamp_int(get_env_int("DEFAULT_REAL_LEVERAGE", 0), 0, 100)
DEFAULT_REAL_MAX_OPEN_POSITIONS = clamp_int(get_env_int("DEFAULT_REAL_MAX_OPEN_POSITIONS", 0), 0, 100)
DEFAULT_REAL_DAILY_MAX_LOSS_USDT = get_env_float("DEFAULT_REAL_DAILY_MAX_LOSS_USDT", 7.0)
DEFAULT_REAL_COOLDOWN_AFTER_DAILY_LOSS_HOURS = get_env_int("DEFAULT_REAL_COOLDOWN_AFTER_DAILY_LOSS_HOURS", 1)


# ---------------------------------------------------------------------------
# User command limits
# ---------------------------------------------------------------------------
# User requirement:
#   ترید دلار: 1 تا 1,000,000
#   ترید لوریج: 1 تا 100 for Toobit
#   حداکثر پوزیشن: 1 تا 100
# ---------------------------------------------------------------------------
MIN_TRADE_MARGIN_USDT = get_env_float("MIN_TRADE_MARGIN_USDT", 1.0)
MAX_TRADE_MARGIN_USDT = get_env_float("MAX_TRADE_MARGIN_USDT", 1000000.0)

MIN_LEVERAGE = get_env_int("MIN_LEVERAGE", 1)
MAX_LEVERAGE = get_env_int("MAX_LEVERAGE", 100)

MIN_MAX_OPEN_POSITIONS = get_env_int("MIN_MAX_OPEN_POSITIONS", 1)
MAX_MAX_OPEN_POSITIONS = get_env_int("MAX_MAX_OPEN_POSITIONS", 100)

MIN_DAILY_MAX_LOSS_USDT = get_env_float("MIN_DAILY_MAX_LOSS_USDT", 1.0)
MAX_DAILY_MAX_LOSS_USDT = get_env_float("MAX_DAILY_MAX_LOSS_USDT", 1000000.0)


# Final hard safety clamps.
# Even if env vars are wrong, Toobit leverage must not exceed 100x.
MIN_LEVERAGE = clamp_int(MIN_LEVERAGE, 1, 100)
MAX_LEVERAGE = clamp_int(MAX_LEVERAGE, MIN_LEVERAGE, 100)

MIN_MAX_OPEN_POSITIONS = clamp_int(MIN_MAX_OPEN_POSITIONS, 1, 100)
MAX_MAX_OPEN_POSITIONS = clamp_int(MAX_MAX_OPEN_POSITIONS, MIN_MAX_OPEN_POSITIONS, 100)

MIN_TRADE_MARGIN_USDT = clamp_float(MIN_TRADE_MARGIN_USDT, 1.0, 1000000.0)
MAX_TRADE_MARGIN_USDT = clamp_float(MAX_TRADE_MARGIN_USDT, MIN_TRADE_MARGIN_USDT, 1000000.0)

MIN_DAILY_MAX_LOSS_USDT = clamp_float(MIN_DAILY_MAX_LOSS_USDT, 0.01, 1000000.0)
MAX_DAILY_MAX_LOSS_USDT = clamp_float(MAX_DAILY_MAX_LOSS_USDT, MIN_DAILY_MAX_LOSS_USDT, 1000000.0)
