# -*- coding: utf-8 -*-

import os


# ============================================================
# Telegram
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)

ALLOWED_USER_IDS = [
    int(x.strip())
    for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
]

if OWNER_ID and OWNER_ID not in ALLOWED_USER_IDS:
    ALLOWED_USER_IDS.append(OWNER_ID)


# ============================================================
# Auto Signal
# ============================================================

AUTO_SIGNAL_ENABLED = os.getenv(
    "AUTO_SIGNAL_ENABLED",
    "true",
).lower() == "true"

AUTO_SCAN_INTERVAL_MINUTES = int(
    os.getenv("AUTO_SCAN_INTERVAL_MINUTES", "5")
)

AUTO_DIRECT_SCORE_MIN = int(
    os.getenv("AUTO_DIRECT_SCORE_MIN", "82")
)


# ============================================================
# Analysis Thresholds
# ============================================================

MIN_DIRECT_SCORE = int(
    os.getenv("MIN_DIRECT_SCORE", "82")
)

MIN_MANUAL_CONFIRMATIONS = int(
    os.getenv("MIN_MANUAL_CONFIRMATIONS", "4")
)

MIN_ADX_FOR_TREND = float(
    os.getenv("MIN_ADX_FOR_TREND", "20")
)


# ============================================================
# Slot / Position Limits
# ============================================================

MAX_ACTIVE_POSITIONS = int(
    os.getenv("MAX_ACTIVE_POSITIONS", "5")
)

MAX_POSITIONS_PER_SYMBOL = int(
    os.getenv("MAX_POSITIONS_PER_SYMBOL", "1")
)


# ============================================================
# Tracker
# ============================================================

TRACKER_CHECK_INTERVAL_SECONDS = int(
    os.getenv("TRACKER_CHECK_INTERVAL_SECONDS", "20")
)


# ============================================================
# Paper Trading
# ============================================================

PAPER_TRADING_ENABLED = os.getenv(
    "PAPER_TRADING_ENABLED",
    "true",
).lower() == "true"


# ============================================================
# AI / Learning
# ============================================================

AI_ENABLED = os.getenv(
    "AI_ENABLED",
    "true",
).lower() == "true"

AI_LEARNING_ENABLED = os.getenv(
    "AI_LEARNING_ENABLED",
    "true",
).lower() == "true"

GHOST_LEARNING_ENABLED = os.getenv(
    "GHOST_LEARNING_ENABLED",
    "true",
).lower() == "true"

MAX_GHOST_SIGNALS = int(
    os.getenv("MAX_GHOST_SIGNALS", "500")
)


# ============================================================
# Coin Risk
# ============================================================

DAILY_SL_STRICTNESS_START = int(
    os.getenv("DAILY_SL_STRICTNESS_START", "3")
)

MAX_DAILY_STRICTNESS_LEVEL = int(
    os.getenv("MAX_DAILY_STRICTNESS_LEVEL", "5")
)


# ============================================================
# Scan Symbols
# ============================================================

SCAN_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "TONUSDT",
    "TRXUSDT",
    "DOTUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "UNIUSDT",
    "APTUSDT",
    "ARBUSDT",
    "OPUSDT",
    "NEARUSDT",
    "FILUSDT",
    "INJUSDT",
    "ATOMUSDT",
    "SUIUSDT",
    "SEIUSDT",
    "ETCUSDT",
    "AAVEUSDT",
    "ICPUSDT",
    "TIAUSDT",
    "ORDIUSDT",
    "WIFUSDT",
    "PEPEUSDT",
    "SHIBUSDT",
    "FLOKIUSDT",
    "BONKUSDT",
]
