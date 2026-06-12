# -*- coding: utf-8 -*-
import os


def get_env_str(name, default=None):
    value = os.getenv(name)
    if value is None:
        return default
    value = str(value).strip()
    return value if value else default


def get_env_int(name, default):
    try:
        return int(get_env_str(name, default))
    except Exception:
        return int(default)


def get_env_float(name, default):
    try:
        return float(get_env_str(name, default))
    except Exception:
        return float(default)


def get_env_bool(name, default=True):
    value = get_env_str(name)
    if value is None:
        return bool(default)
    return value.lower() in ["1", "true", "yes", "on", "enable", "enabled"]


BOT_TOKEN = get_env_str("BOT_TOKEN")
OWNER_ID = get_env_int("OWNER_ID", 1055122209)
ALLOWED_USERS = [OWNER_ID]

# Auto signal: standard technical direct signals only
AUTO_SIGNAL_ENABLED = get_env_bool("AUTO_SIGNAL_ENABLED", True)
AUTO_SIGNAL_SCORE = get_env_int("AUTO_SIGNAL_SCORE", 75)
AUTO_SIGNAL_COOLDOWN_MINUTES = get_env_int("AUTO_SIGNAL_COOLDOWN_MINUTES", 120)
AUTO_SCAN_INTERVAL_MINUTES = get_env_int("AUTO_SCAN_INTERVAL_MINUTES", 3)
AUTO_SCAN_MAX_SYMBOLS = get_env_int("AUTO_SCAN_MAX_SYMBOLS", 70)
AUTO_TRACK_AUTO_SIGNALS = get_env_bool("AUTO_TRACK_AUTO_SIGNALS", True)

# Tracker
TRACKER_CHECK_INTERVAL_SECONDS = get_env_int("TRACKER_CHECK_INTERVAL_SECONDS", 30)

# Technical engine tuning: balanced, not too dry and not too loose
MIN_DIRECT_SCORE = get_env_int("MIN_DIRECT_SCORE", 70)
MIN_AUTO_CONFIRMATIONS = get_env_int("MIN_AUTO_CONFIRMATIONS", 4)
MIN_MANUAL_CONFIRMATIONS = get_env_int("MIN_MANUAL_CONFIRMATIONS", 3)
MIN_ADX_FOR_TREND = get_env_float("MIN_ADX_FOR_TREND", 14)

# Risk display
MAX_LEVERAGE_SUGGESTION = get_env_int("MAX_LEVERAGE_SUGGESTION", 5)
RISK_PER_TRADE_PERCENT = get_env_int("RISK_PER_TRADE_PERCENT", 1)

# Market data cache used by market_sentiment.py if present
MARKET_SENTIMENT_CACHE_SECONDS = get_env_int("MARKET_SENTIMENT_CACHE_SECONDS", 1800)
