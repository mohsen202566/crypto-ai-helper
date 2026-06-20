# -*- coding: utf-8 -*-
import os


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on', 'enabled')


def env_int(name, default=0, min_value=None, max_value=None):
    try:
        value = int(float(os.getenv(name, str(default)) or default))
    except Exception:
        value = int(default)
    if min_value is not None:
        value = max(int(min_value), value)
    if max_value is not None:
        value = min(int(max_value), value)
    return value


def env_float(name, default=0.0, min_value=None, max_value=None):
    try:
        value = float(os.getenv(name, str(default)) or default)
    except Exception:
        value = float(default)
    if min_value is not None:
        value = max(float(min_value), value)
    if max_value is not None:
        value = min(float(max_value), value)
    return value


BOT_TOKEN = os.getenv('BOT_TOKEN', '')
OWNER_ID = env_int('OWNER_ID', 0, min_value=0)
ALLOWED_USER_IDS = [int(x.strip()) for x in os.getenv('ALLOWED_USER_IDS', '').split(',') if x.strip().isdigit()]
if OWNER_ID and OWNER_ID not in ALLOWED_USER_IDS:
    ALLOWED_USER_IDS.append(OWNER_ID)

# Auto signal: medium-balanced, not over-strict.
AUTO_SIGNAL_ENABLED = env_bool('AUTO_SIGNAL_ENABLED', True)
AUTO_SCAN_INTERVAL_MINUTES = env_int('AUTO_SCAN_INTERVAL_MINUTES', 3, min_value=1, max_value=1440)
AUTO_DIRECT_SCORE_MIN = env_int('AUTO_DIRECT_SCORE_MIN', 82, min_value=1, max_value=100)
AUTO_SIGNAL_SCORE = AUTO_DIRECT_SCORE_MIN  # backward-compatible alias
AUTO_SIGNAL_COOLDOWN_MINUTES = env_int('AUTO_SIGNAL_COOLDOWN_MINUTES', 30, min_value=0, max_value=1440)

MIN_DIRECT_SCORE = env_int('MIN_DIRECT_SCORE', 82, min_value=1, max_value=100)
MIN_MANUAL_CONFIRMATIONS = env_int('MIN_MANUAL_CONFIRMATIONS', 4, min_value=0, max_value=20)
MIN_ADX_FOR_TREND = env_float('MIN_ADX_FOR_TREND', 20, min_value=0, max_value=100)

# Real trading / Toobit controls. Paper mode stays disabled in Telegram controls.
REAL_TRADING_ENABLED = env_bool('REAL_TRADING_ENABLED', False)
TOBIT_REAL_TRADING_ENABLED = env_bool('TOBIT_REAL_TRADING_ENABLED', REAL_TRADING_ENABLED)
PAPER_TRADING_ENABLED = False

MAX_ACTIVE_POSITIONS = env_int('MAX_ACTIVE_POSITIONS', 10, min_value=1, max_value=100)
MAX_POSITIONS_PER_SYMBOL = env_int('MAX_POSITIONS_PER_SYMBOL', 1, min_value=1, max_value=20)
MAX_LEVERAGE = env_int('MAX_LEVERAGE', 50, min_value=1, max_value=50)
DEFAULT_LEVERAGE = env_int('DEFAULT_LEVERAGE', 15, min_value=1, max_value=MAX_LEVERAGE)
MIN_TRADE_MARGIN_USD = env_float('MIN_TRADE_MARGIN_USD', 1, min_value=0)
MAX_TRADE_MARGIN_USD = env_float('MAX_TRADE_MARGIN_USD', 1000000, min_value=1)
DEFAULT_TRADE_MARGIN_USD = env_float('DEFAULT_TRADE_MARGIN_USD', 5, min_value=MIN_TRADE_MARGIN_USD, max_value=MAX_TRADE_MARGIN_USD)

# Tracker / position sync. 75s matches slot_manager and real_position_sync pending window.
TRACKER_CHECK_INTERVAL_SECONDS = env_int('TRACKER_CHECK_INTERVAL_SECONDS', 20, min_value=2, max_value=300)
PENDING_REAL_CONFIRM_TIMEOUT_SECONDS = env_int('PENDING_REAL_CONFIRM_TIMEOUT_SECONDS', 75, min_value=20, max_value=300)
REAL_POSITION_SYNC_FAST_SECONDS = env_int('REAL_POSITION_SYNC_FAST_SECONDS', 2, min_value=1, max_value=30)
REAL_POSITION_SYNC_SLOW_SECONDS = env_int('REAL_POSITION_SYNC_SLOW_SECONDS', 10, min_value=2, max_value=300)

# AI / learning memory. 20000 aligns with coin_learning, coin_risk, ghost_signals and sr_learning.
AI_ENABLED = env_bool('AI_ENABLED', True)
AI_LEARNING_ENABLED = env_bool('AI_LEARNING_ENABLED', True)
GHOST_LEARNING_ENABLED = env_bool('GHOST_LEARNING_ENABLED', True)
MAX_GHOST_SIGNALS = env_int('MAX_GHOST_SIGNALS', 20000, min_value=1000, max_value=200000)
MAX_SIGNALS_STORED = env_int('MAX_SIGNALS_STORED', 20000, min_value=1000, max_value=200000)
MAX_RECENT_EVENTS = env_int('MAX_RECENT_EVENTS', 20000, min_value=1000, max_value=200000)

# Daily adaptive strictness starts from the 3rd SL per coin+direction.
DAILY_SL_STRICTNESS_START = env_int('DAILY_SL_STRICTNESS_START', 3, min_value=1, max_value=20)
MAX_DAILY_STRICTNESS_LEVEL = env_int('MAX_DAILY_STRICTNESS_LEVEL', 5, min_value=1, max_value=10)

# Daily loss lock defaults: protected-balance logic belongs to real_trade_manager.
DAILY_LOSS_LOCK_ENABLED = env_bool('DAILY_LOSS_LOCK_ENABLED', True)
DAILY_LOSS_LIMIT_USD = env_float('DAILY_LOSS_LIMIT_USD', 5.0, min_value=0)
DAILY_LOCK_HOURS = env_float('DAILY_LOCK_HOURS', 1.0, min_value=0.1, max_value=168)

SCAN_SYMBOLS = [
    'BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','AVAXUSDT',
    'LINKUSDT','TONUSDT','TRXUSDT','DOTUSDT','LTCUSDT','BCHUSDT','UNIUSDT','APTUSDT',
    'ARBUSDT','OPUSDT','NEARUSDT','FILUSDT','INJUSDT','ATOMUSDT','SUIUSDT','SEIUSDT',
    'ETCUSDT','AAVEUSDT','ICPUSDT','TIAUSDT','ORDIUSDT','WIFUSDT','PEPEUSDT','SHIBUSDT',
    'FLOKIUSDT','BONKUSDT','JUPUSDT','FTMUSDT','GALAUSDT','LDOUSDT','RUNEUSDT','MKRUSDT'
]
