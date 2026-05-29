import os

BOT_TOKEN = os.getenv("BOT_TOKEN")

OWNER_ID = 1055122209

ALLOWED_USERS = [
    OWNER_ID
]

AUTO_SIGNAL_SCORE = 85
AUTO_SCAN_INTERVAL_MINUTES = 5
AUTO_SIGNAL_COOLDOWN_MINUTES = 120

DEFAULT_TIMEFRAMES = {
    "main_trend": "4h",
    "mid_trend": "1h",
    "structure": "30m",
    "entry_1": "15m",
    "entry_2": "5m"
}

MAX_LEVERAGE_SUGGESTION = 5
RISK_PER_TRADE_PERCENT = 1
