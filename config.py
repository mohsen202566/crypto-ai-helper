# -*- coding: utf-8 -*-
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")

# اگر OWNER_ID را روی VPS ست نکنی، مقدار قبلی خودت استفاده می‌شود.
OWNER_ID = int(os.getenv("OWNER_ID", "1055122209"))

ALLOWED_USERS = [OWNER_ID]

# Auto signal
AUTO_SIGNAL_ENABLED = os.getenv("AUTO_SIGNAL_ENABLED", "1") == "1"
AUTO_SIGNAL_SCORE = int(os.getenv("AUTO_SIGNAL_SCORE", "85"))
AUTO_SIGNAL_COOLDOWN_MINUTES = int(os.getenv("AUTO_SIGNAL_COOLDOWN_MINUTES", "120"))
AUTO_SCAN_INTERVAL_MINUTES = int(os.getenv("AUTO_SCAN_INTERVAL_MINUTES", "5"))
AUTO_SCAN_MAX_SYMBOLS = int(os.getenv("AUTO_SCAN_MAX_SYMBOLS", "70"))

# Tracker
TRACKER_CHECK_INTERVAL_SECONDS = int(os.getenv("TRACKER_CHECK_INTERVAL_SECONDS", "60"))

# Market data cache
MARKET_SENTIMENT_CACHE_SECONDS = int(os.getenv("MARKET_SENTIMENT_CACHE_SECONDS", "900"))

# Risk display
MAX_LEVERAGE_SUGGESTION = 5
RISK_PER_TRADE_PERCENT = 1
