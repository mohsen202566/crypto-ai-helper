# -*- coding: utf-8 -*-
import os

def env_bool(name, default="0"):
    return str(os.getenv(name, default)).strip().lower() in ["1", "true", "yes", "on"]

BOT_TOKEN = os.getenv("BOT_TOKEN")

# بهتر است OWNER_ID را هم روی VPS با export ست کنی؛ اگر ست نشد مقدار قبلی استفاده می‌شود.
OWNER_ID = int(os.getenv("OWNER_ID", "1055122209"))

ALLOWED_USERS = [
    OWNER_ID
]

AUTO_SIGNAL_SCORE = int(os.getenv("AUTO_SIGNAL_SCORE", "85"))

# برای جلوگیری از فشار به API و خطای پشت سر هم، اسکن خودکار متعادل‌تر شد.
AUTO_SCAN_INTERVAL_MINUTES = int(os.getenv("AUTO_SCAN_INTERVAL_MINUTES", "15"))
AUTO_SIGNAL_COOLDOWN_MINUTES = int(os.getenv("AUTO_SIGNAL_COOLDOWN_MINUTES", "120"))
AUTO_SIGNAL_ENABLED = env_bool("AUTO_SIGNAL_ENABLED", "1")
AUTO_SCAN_MAX_SYMBOLS = int(os.getenv("AUTO_SCAN_MAX_SYMBOLS", "35"))

DEFAULT_TIMEFRAMES = {
    "main_trend": "4h",
    "mid_trend": "1h",
    "structure": "30m",
    "entry_1": "15m",
    "entry_2": "5m"
}

MAX_LEVERAGE_SUGGESTION = 5
RISK_PER_TRADE_PERCENT = 1

TRACKER_CHECK_INTERVAL_SECONDS = int(os.getenv("TRACKER_CHECK_INTERVAL_SECONDS", "60"))
MARKET_SENTIMENT_CACHE_SECONDS = int(os.getenv("MARKET_SENTIMENT_CACHE_SECONDS", "1800"))
