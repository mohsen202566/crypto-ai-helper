# -*- coding: utf-8 -*-
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
        return int(value)
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
    return value.lower() in ["1", "true", "yes", "on", "enable", "enabled"]


AUTO_TRADE_STATE_FILE = get_env_str("AUTO_TRADE_STATE_FILE", "paper_trade_state.json")

DEFAULT_TRADE_ENABLED = get_env_bool("DEFAULT_TRADE_ENABLED", False)
DEFAULT_TRADE_MODE = get_env_str("DEFAULT_TRADE_MODE", "PAPER")

DEFAULT_START_BALANCE_USDT = get_env_float("DEFAULT_START_BALANCE_USDT", 50.0)
DEFAULT_TRADE_MARGIN_USDT = get_env_float("DEFAULT_TRADE_MARGIN_USDT", 5.0)
DEFAULT_LEVERAGE = get_env_int("DEFAULT_LEVERAGE", 10)
DEFAULT_MAX_OPEN_POSITIONS = get_env_int("DEFAULT_MAX_OPEN_POSITIONS", 5)

DEFAULT_DAILY_MAX_LOSS_USDT = get_env_float("DEFAULT_DAILY_MAX_LOSS_USDT", 7.0)
DEFAULT_COOLDOWN_AFTER_DAILY_LOSS_HOURS = get_env_int("DEFAULT_COOLDOWN_AFTER_DAILY_LOSS_HOURS", 12)

MIN_TRADE_MARGIN_USDT = get_env_float("MIN_TRADE_MARGIN_USDT", 1.0)
MAX_TRADE_MARGIN_USDT = get_env_float("MAX_TRADE_MARGIN_USDT", 50.0)

MIN_LEVERAGE = get_env_int("MIN_LEVERAGE", 1)
MAX_LEVERAGE = get_env_int("MAX_LEVERAGE", 20)

MIN_MAX_OPEN_POSITIONS = get_env_int("MIN_MAX_OPEN_POSITIONS", 1)
MAX_MAX_OPEN_POSITIONS = get_env_int("MAX_MAX_OPEN_POSITIONS", 20)
