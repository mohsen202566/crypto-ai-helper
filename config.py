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


def get_env_bool(name, default=True):
    value = get_env_str(name)
    if value is None:
        return bool(default)
    return value.lower() in ["1", "true", "yes", "on", "enable", "enabled"]


BOT_TOKEN = get_env_str("BOT_TOKEN")

# اگر OWNER_ID را روی VPS ست نکنی، مقدار قبلی استفاده می‌شود.
OWNER_ID = get_env_int("OWNER_ID", 1055122209)

ALLOWED_USERS = [OWNER_ID]

# Auto signal
AUTO_SIGNAL_ENABLED = get_env_bool("AUTO_SIGNAL_ENABLED", True)
AUTO_SIGNAL_SCORE = get_env_int("AUTO_SIGNAL_SCORE", 80)
AUTO_SIGNAL_COOLDOWN_MINUTES = get_env_int("AUTO_SIGNAL_COOLDOWN_MINUTES", 120)
AUTO_SCAN_INTERVAL_MINUTES = get_env_int("AUTO_SCAN_INTERVAL_MINUTES", 5)

# برای جلوگیری از فشار روی VPS/API، بیشتر از این مقدار اسکن نمی‌شود.
AUTO_SCAN_MAX_SYMBOLS = get_env_int("AUTO_SCAN_MAX_SYMBOLS", 70)

# حداقل امتیاز برای ورود به لیست اسکن. اگر جداگانه ست نشود همان AUTO_SIGNAL_SCORE است.
AUTO_SCAN_MIN_SCORE = get_env_int("AUTO_SCAN_MIN_SCORE", AUTO_SIGNAL_SCORE)

# Tracker
TRACKER_CHECK_INTERVAL_SECONDS = get_env_int("TRACKER_CHECK_INTERVAL_SECONDS", 60)

# Market data cache
# برای جلوگیری از CoinGecko 429، کمتر از 15 دقیقه نگذار.
MARKET_SENTIMENT_CACHE_SECONDS = get_env_int("MARKET_SENTIMENT_CACHE_SECONDS", 1800)

# Risk display
MAX_LEVERAGE_SUGGESTION = get_env_int("MAX_LEVERAGE_SUGGESTION", 5)
RISK_PER_TRADE_PERCENT = get_env_int("RISK_PER_TRADE_PERCENT", 1)

# Technical quality filters - مقادیر نرم برای حفظ تعداد سیگنال
TECHNICAL_QUALITY_LATE_ENTRY_ATR = get_env_int("TECHNICAL_QUALITY_LATE_ENTRY_ATR", 165) / 100
TECHNICAL_QUALITY_MIN_TP_SPACE_ATR = get_env_int("TECHNICAL_QUALITY_MIN_TP_SPACE_ATR", 75) / 100
