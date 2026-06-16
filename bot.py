# -*- coding: utf-8 -*-
"""
Crypto AI Telegram Bot - Real Trade Command Version

این نسخه برای معماری فعلی ربات نوشته شده:
- سیگنال‌ها خودکار Track می‌شوند؛ دستور دستی «زیر نظر» حذف شده.
- دستورهای ترید واقعی/سرمایه/لوریج/حجم پوزیشن اضافه شده.
- آمار، حذف آمار، بررسی بازار، بهترین سیگنال، وضعیت AI، Ghost/Slot/Coin reports حفظ شده.
"""

import os
import re
import json
import time
import asyncio
import logging
from typing import Dict, List, Any, Optional, Callable

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from analysis import analyze_symbol
from scanner import scan_for_auto_signals, get_top_signals, scan_market_overview


# ============================================================
# Optional project imports
# ============================================================

try:
    from signal_tracker import (
        add_signal_to_tracking,
        check_active_signals,
        format_active_signals,
        format_signal_stats,
        reset_signal_stats,
        parse_days_from_text,
        get_symbol_stats_report,
    )
except Exception:
    add_signal_to_tracking = None
    check_active_signals = None
    format_active_signals = None
    format_signal_stats = None
    reset_signal_stats = None

    def parse_days_from_text(text: str) -> int:
        m = re.search(r"\d+", text or "")
        if m:
            return int(m.group(0))
        if text and "کل" in text:
            return 3650
        return 7

    get_symbol_stats_report = None


try:
    from ai_memory import format_ai_status
except Exception:
    format_ai_status = None

try:
    from coin_learning import (
        format_learning_summary,
        format_coin_behavior,
        format_smart_stats,
    )
except Exception:
    format_learning_summary = None
    format_coin_behavior = None
    format_smart_stats = None

try:
    from coin_rotation import format_rotation_report
except Exception:
    format_rotation_report = None

try:
    from ghost_signals import format_ghost_report, create_ghost_signal
except Exception:
    format_ghost_report = None
    create_ghost_signal = None

try:
    from slot_manager import format_slot_report
except Exception:
    format_slot_report = None

try:
    from real_trade_manager import (
        get_real_trade_status_text,
        get_toobit_balance_text,
        set_real_initial_capital,
        set_real_position_size,
        set_real_leverage,
        set_real_max_positions,
        set_real_daily_loss_limit,
        set_real_lock_duration_hours,
        enable_real_trading,
        disable_real_trading,
        activate_real_emergency_stop,
        reset_real_trade_state,
        open_real_position_from_signal,
        is_real_trade_ready,
        close_real_position_by_symbol,
        close_all_real_positions,
        sync_real_positions_text,
    )
except Exception:
    get_real_trade_status_text = None
    get_toobit_balance_text = None
    set_real_initial_capital = None
    set_real_position_size = None
    set_real_leverage = None
    set_real_max_positions = None
    set_real_daily_loss_limit = None
    set_real_lock_duration_hours = None
    enable_real_trading = None
    disable_real_trading = None
    activate_real_emergency_stop = None
    reset_real_trade_state = None
    open_real_position_from_signal = None
    is_real_trade_ready = None
    close_real_position_by_symbol = None
    close_all_real_positions = None
    sync_real_positions_text = None


# ============================================================
# Config
# ============================================================

try:
    from config import (
        BOT_TOKEN,
        OWNER_ID,
        ALLOWED_USER_IDS,
        AUTO_SIGNAL_ENABLED,
        AUTO_SCAN_INTERVAL_MINUTES,
        AUTO_DIRECT_SCORE_MIN,
        AUTO_SIGNAL_COOLDOWN_MINUTES,
    )
except Exception:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)

    allowed_raw = os.getenv("ALLOWED_USER_IDS", "")
    ALLOWED_USER_IDS = [
        int(x.strip())
        for x in allowed_raw.split(",")
        if x.strip().isdigit()
    ]

    AUTO_SIGNAL_ENABLED = os.getenv("AUTO_SIGNAL_ENABLED", "true").lower() == "true"
    AUTO_SCAN_INTERVAL_MINUTES = int(os.getenv("AUTO_SCAN_INTERVAL_MINUTES", "5"))
    AUTO_DIRECT_SCORE_MIN = int(os.getenv("AUTO_DIRECT_SCORE_MIN", "82"))
    AUTO_SIGNAL_COOLDOWN_MINUTES = int(os.getenv("AUTO_SIGNAL_COOLDOWN_MINUTES", "30"))


if OWNER_ID and OWNER_ID not in ALLOWED_USER_IDS:
    ALLOWED_USER_IDS.append(OWNER_ID)


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("crypto-ai-bot")


# ============================================================
# Runtime state
# ============================================================

LAST_AUTO_SIGNAL_TIME: Dict[str, int] = {}
AUTO_SIGNAL_COOLDOWN_SECONDS = int(AUTO_SIGNAL_COOLDOWN_MINUTES) * 60

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
TRADE_SETTINGS_FILE = os.path.join(DATA_DIR, "trade_settings.json")
BOT_SETTINGS_FILE = os.path.join(DATA_DIR, "bot_settings.json")


DEFAULT_BOT_SETTINGS = {
    "trading_enabled": True,
    "auto_signal_enabled": bool(AUTO_SIGNAL_ENABLED),
    "updated_at": None,
}


def load_bot_settings() -> Dict[str, Any]:
    state = load_json(BOT_SETTINGS_FILE, DEFAULT_BOT_SETTINGS.copy())
    merged = DEFAULT_BOT_SETTINGS.copy()
    if isinstance(state, dict):
        merged.update(state)
    return merged


def save_bot_settings(state: Dict[str, Any]) -> Dict[str, Any]:
    state["updated_at"] = int(time.time())
    save_json(BOT_SETTINGS_FILE, state)
    return state


# ============================================================
# JSON helpers
# ============================================================

def load_json(path: str, default: Any) -> Any:
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, type(default)):
            return default
        return data
    except Exception:
        return default


def save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ============================================================
# Access control
# ============================================================

def get_user_id(update: Update) -> int:
    try:
        return int(update.effective_user.id)
    except Exception:
        return 0


def is_allowed(update: Update) -> bool:
    uid = get_user_id(update)
    if not OWNER_ID:
        return True
    return uid == OWNER_ID or uid in ALLOWED_USER_IDS


async def reject_unauthorized(update: Update) -> None:
    if update.message:
        await update.message.reply_text("⛔️ شما اجازه استفاده از این ربات را ندارید.")


# ============================================================
# Persian symbol mapping
# ============================================================

PERSIAN_SYMBOLS = {
    "بیتکوین": "BTCUSDT",
    "بیت کوین": "BTCUSDT",
    "btc": "BTCUSDT",
    "اتریوم": "ETHUSDT",
    "اتر": "ETHUSDT",
    "eth": "ETHUSDT",
    "سولانا": "SOLUSDT",
    "سول": "SOLUSDT",
    "sol": "SOLUSDT",
    "دوج": "DOGEUSDT",
    "دوج کوین": "DOGEUSDT",
    "doge": "DOGEUSDT",
    "ریپل": "XRPUSDT",
    "xrp": "XRPUSDT",
    "کاردانو": "ADAUSDT",
    "ada": "ADAUSDT",
    "آواکس": "AVAXUSDT",
    "avax": "AVAXUSDT",
    "بایننس": "BNBUSDT",
    "bnb": "BNBUSDT",
    "تون": "TONUSDT",
    "ton": "TONUSDT",
    "لینک": "LINKUSDT",
    "link": "LINKUSDT",
    "اپتوس": "APTUSDT",
    "apt": "APTUSDT",
    "آربیتروم": "ARBUSDT",
    "arb": "ARBUSDT",
    "پالیگان": "POLUSDT",
    "متیک": "POLUSDT",
    "matic": "POLUSDT",
    "شیبا": "SHIBUSDT",
    "shib": "SHIBUSDT",
    "پپه": "PEPEUSDT",
    "pepe": "PEPEUSDT",
    "فلوکی": "FLOKIUSDT",
    "floki": "FLOKIUSDT",
    "بونک": "BONKUSDT",
    "bonk": "BONKUSDT",
    "سوئی": "SUIUSDT",
    "sui": "SUIUSDT",
    "سی": "SEIUSDT",
    "sei": "SEIUSDT",
    "اینترنت کامپیوتر": "ICPUSDT",
    "icp": "ICPUSDT",
    "فایل کوین": "FILUSDT",
    "fil": "FILUSDT",
    "یونی": "UNIUSDT",
    "uni": "UNIUSDT",
    "آوه": "AAVEUSDT",
    "aave": "AAVEUSDT",
}


COMMAND_ONLY_PHRASES = {
    "ترید", "ترید فعال", "فعال ترید", "وضعیت ترید", "تریدها",
    "آمار ترید", "امار ترید", "ریست ترید", "حجم پوزیشن",
    "حداکثر پوزیشن", "پوزیشن‌ها", "پوزیشن ها", "positions",
    "بهترین سیگنال", "بهترین", "بررسی", "بررسی بازار", "بازار", "وضعیت بازار",
    "آمار", "امار", "حذف آمار", "حذف امار", "ریست آمار",
    "هوش مصنوعی", "ai", "وضعیت ai", "وضعیت هوش مصنوعی",
    "حافظه ربات", "حافظه ai", "یادگیری", "learning",
    "ریسک کوین‌ها", "ریسک کوین ها", "چرخش کوین",
    "سیگنال‌های مخفی", "سیگنال های مخفی", "ghost", "ghost signals",
    "اسلات‌ها", "اسلات ها", "slots", "slot",
}

COMMAND_WORDS = {
    "ترید", "فعال", "وضعیت", "آمار", "امار", "ریست", "حذف", "حجم", "پوزیشن",
    "پوزیشن‌ها", "پوزیشنها", "حداکثر", "بهترین", "بدترین", "سیگنال", "سیگنال‌ها",
    "سیگنالهای", "بررسی", "بازار", "ربات", "هوش", "مصنوعی", "حافظه", "یادگیری",
    "ریسک", "چرخش", "اسلات", "اسلات‌ها", "مخفی", "دلار", "لوریج",
}

def _normalize_command_text(text: str) -> str:
    return (
        str(text or "")
        .lower()
        .replace("ي", "ی")
        .replace("ك", "ک")
        .replace("‌", " ")
        .strip()
    )

def is_command_only_text(text: str) -> bool:
    t = _normalize_command_text(text)
    compact = t.replace(" ", "")
    if t in COMMAND_ONLY_PHRASES or compact in {x.replace(" ", "") for x in COMMAND_ONLY_PHRASES}:
        return True
    words = [w for w in re.split(r"\s+", t) if w]
    return bool(words) and all(w in COMMAND_WORDS or w.isdigit() for w in words)

def normalize_symbol_text(text: str) -> Optional[str]:
    raw_text = str(text or "").strip()
    t = _normalize_command_text(raw_text)

    # Safety gate: pure bot/trade/stat commands must never become fake symbols like USDT.
    if is_command_only_text(t):
        return None

    cleaned = (
        t.replace("تحلیل", "")
        .replace("سیگنال", "")
        .replace("بررسی", "")
        .replace("خرید", "")
        .replace("فروش", "")
        .replace("لانگ", "")
        .replace("شورت", "")
        .replace("/", "")
        .replace("-", "")
        .strip()
    )

    # After removing analysis words, check again; empty/command text is not a symbol.
    if not cleaned or is_command_only_text(cleaned):
        return None

    for key, symbol in PERSIAN_SYMBOLS.items():
        if key in cleaned:
            return symbol

    raw = cleaned.upper().replace(" ", "")
    blocked_raw = {"USDT", "تریدفعال", "وضعیتترید", "آمارترید", "امارترید", "حجمپوزیشن"}
    if raw in blocked_raw:
        return None
    if raw.endswith("USDT") and len(raw) >= 6 and raw != "USDT":
        return raw
    if raw.isascii() and raw.isalpha() and 2 <= len(raw) <= 10:
        return raw + "USDT"
    return None


# ============================================================
# Text helpers
# ============================================================

def fa_direction(direction: str) -> str:
    if direction == "LONG":
        return "لانگ"
    if direction == "SHORT":
        return "شورت"
    return "بدون سیگنال"


def safe_num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def extract_first_number(text: str) -> Optional[float]:
    nums = re.findall(r"\d+(?:\.\d+)?", text or "")
    if not nums:
        return None
    try:
        return float(nums[0])
    except Exception:
        return None


async def send_long_text(update: Update, text: str, max_len: int = 3900) -> None:
    if not update.message:
        return
    if len(text) <= max_len:
        await update.message.reply_text(text)
        return
    chunks = []
    current = []
    current_len = 0
    for line in text.splitlines():
        if current_len + len(line) + 1 > max_len:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line) + 1
        else:
            current.append(line)
            current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    for chunk in chunks[:4]:
        await update.message.reply_text(chunk)
        await asyncio.sleep(0.3)


# ============================================================
# Trade settings commands
# ============================================================

DEFAULT_TRADE_SETTINGS = {
    "capital_usd": 50.0,
    "trade_margin_usd": 5.0,
    "leverage": 10.0,
    "max_positions": 5,
    "updated_at": None,
}


def load_trade_settings() -> Dict[str, Any]:
    state = load_json(TRADE_SETTINGS_FILE, DEFAULT_TRADE_SETTINGS.copy())
    merged = DEFAULT_TRADE_SETTINGS.copy()
    if isinstance(state, dict):
        merged.update(state)
    return merged


def save_trade_settings(state: Dict[str, Any]) -> Dict[str, Any]:
    state["updated_at"] = int(time.time())
    save_json(TRADE_SETTINGS_FILE, state)
    return state


def calc_position_size(settings: Dict[str, Any]) -> float:
    margin = safe_num(settings.get("trade_margin_usd"), 0.0)
    leverage = safe_num(settings.get("leverage"), 1.0)
    return round(margin * leverage, 4)


def _real_status_or_unavailable() -> str:
    if get_real_trade_status_text:
        try:
            return get_real_trade_status_text()
        except Exception as e:
            return f"❌ خطا در دریافت وضعیت ترید واقعی:\n{str(e)[:250]}"
    return "ماژول ترید واقعی توبیت فعال نیست یا فایل real_trade_manager.py پیدا نشد."


def format_trade_status() -> str:
    # Generic trade commands now control REAL / TOBIT trading only.
    return _real_status_or_unavailable()

async def trade_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(format_trade_status())



async def set_capital_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    value = extract_first_number(update.message.text)
    if value is None or value <= 0:
        await update.message.reply_text("مثال درست: سرمایه ترید 1000")
        return
    if not set_real_initial_capital:
        await update.message.reply_text("ماژول ترید واقعی فعال نیست.")
        return
    value = max(1.0, min(float(value), 1_000_000.0))
    await update.message.reply_text(set_real_initial_capital(value) + "\n\n" + format_trade_status())



async def set_trade_margin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    value = extract_first_number(update.message.text)
    if value is None or value <= 0:
        await update.message.reply_text("مثال درست: ترید دلار 20")
        return
    if not set_real_position_size:
        await update.message.reply_text("ماژول ترید واقعی فعال نیست.")
        return
    value = max(1.0, min(float(value), 1_000_000.0))
    await update.message.reply_text(set_real_position_size(value) + "\n\n" + format_trade_status())



async def set_leverage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    value = extract_first_number(update.message.text)
    if value is None or value <= 0:
        await update.message.reply_text("مثال درست: ترید لوریج 5")
        return
    if not set_real_leverage:
        await update.message.reply_text("ماژول ترید واقعی فعال نیست.")
        return
    value = max(1.0, min(float(value), 100.0))
    await update.message.reply_text(set_real_leverage(value) + "\n\n" + format_trade_status())



async def position_size_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(format_trade_status())



async def reset_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if get_user_id(update) != OWNER_ID:
        await reject_unauthorized(update)
        return
    if not reset_real_trade_state:
        await update.message.reply_text("ماژول ترید واقعی فعال نیست.")
        return
    await update.message.reply_text(reset_real_trade_state() + "\n\n" + format_trade_status())



async def trade_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return
    await send_long_text(update, format_trade_status())



async def set_max_positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    value = extract_first_number(update.message.text)
    if value is None or value <= 0:
        await update.message.reply_text("مثال درست: حداکثر پوزیشن 10")
        return
    if not set_real_max_positions:
        await update.message.reply_text("ماژول ترید واقعی فعال نیست.")
        return
    value = max(1, min(int(value), 100))
    await update.message.reply_text(set_real_max_positions(value) + "\n\n" + format_trade_status())



async def set_daily_loss_limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    value = extract_first_number(update.message.text)
    if value is None or value <= 0:
        await update.message.reply_text("مثال درست: حد ضرر روزانه 5")
        return
    if not set_real_daily_loss_limit:
        await update.message.reply_text("ماژول تنظیم حد ضرر روزانه واقعی فعال نیست. فایل real_trade_manager.py را به‌روزرسانی کن.")
        return
    try:
        msg = set_real_daily_loss_limit(float(value))
        await update.message.reply_text(msg + "\n\n" + format_trade_status())
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در تنظیم حد ضرر روزانه:\n{str(e)[:250]}")



async def set_daily_lock_hours_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    value = extract_first_number(update.message.text)
    if value is None or value <= 0:
        await update.message.reply_text("مثال درست: قفل ضرر 1 ساعت")
        return
    if not set_real_lock_duration_hours:
        await update.message.reply_text("ماژول تنظیم زمان قفل ضرر واقعی فعال نیست. فایل real_trade_manager.py را به‌روزرسانی کن.")
        return
    try:
        hours = max(1, min(int(value), 168))
        msg = set_real_lock_duration_hours(hours)
        await update.message.reply_text(msg + "\n\n" + format_trade_status())
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در تنظیم زمان قفل ضرر:\n{str(e)[:250]}")



def real_trade_module_available() -> bool:
    return all([
        get_real_trade_status_text,
        get_toobit_balance_text,
        set_real_initial_capital,
        set_real_position_size,
        set_real_leverage,
        set_real_max_positions,
        set_real_daily_loss_limit,
        set_real_lock_duration_hours,
        enable_real_trading,
        disable_real_trading,
        activate_real_emergency_stop,
        reset_real_trade_state,
        is_real_trade_ready,
        open_real_position_from_signal,
    ])

async def real_trade_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not real_trade_module_available() or not get_real_trade_status_text:
        await update.message.reply_text("ماژول ترید واقعی توبیت فعال نیست یا فایل real_trade_manager.py پیدا نشد.")
        return
    await update.message.reply_text(get_real_trade_status_text())


async def toobit_balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not get_toobit_balance_text:
        await update.message.reply_text("ماژول اتصال توبیت فعال نیست یا فایل tobit_client.py پیدا نشد.")
        return
    await send_long_text(update, get_toobit_balance_text())


async def set_real_capital_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not set_real_initial_capital:
        await update.message.reply_text("ماژول ترید واقعی فعال نیست.")
        return
    value = extract_first_number(update.message.text)
    if value is None or value <= 0:
        await update.message.reply_text("مثال درست: سرمایه واقعی 50")
        return
    await update.message.reply_text(set_real_initial_capital(float(value)) + "\n\n" + get_real_trade_status_text())


async def set_real_position_size_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not set_real_position_size:
        await update.message.reply_text("ماژول ترید واقعی فعال نیست.")
        return
    value = extract_first_number(update.message.text)
    if value is None or value <= 0:
        await update.message.reply_text("مثال درست: ترید واقعی دلار 2")
        return
    await update.message.reply_text(set_real_position_size(float(value)) + "\n\n" + get_real_trade_status_text())


async def set_real_leverage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not set_real_leverage:
        await update.message.reply_text("ماژول ترید واقعی فعال نیست.")
        return
    value = extract_first_number(update.message.text)
    if value is None or value <= 0:
        await update.message.reply_text("مثال درست: لوریج واقعی 5")
        return
    value = max(1.0, min(float(value), 100.0))
    await update.message.reply_text(set_real_leverage(value) + "\n\n" + get_real_trade_status_text())


async def set_real_max_positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not set_real_max_positions:
        await update.message.reply_text("ماژول ترید واقعی فعال نیست.")
        return
    value = extract_first_number(update.message.text)
    if value is None or value <= 0:
        await update.message.reply_text("مثال درست: حداکثر پوزیشن واقعی 3")
        return
    await update.message.reply_text(set_real_max_positions(int(value)) + "\n\n" + get_real_trade_status_text())


async def set_real_daily_loss_limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not set_real_daily_loss_limit:
        await update.message.reply_text("ماژول تنظیم حد ضرر روزانه واقعی فعال نیست. فایل real_trade_manager.py را به‌روزرسانی کن.")
        return
    value = extract_first_number(update.message.text)
    if value is None or value <= 0:
        await update.message.reply_text("مثال درست: حد ضرر روزانه واقعی 5")
        return
    try:
        msg = set_real_daily_loss_limit(float(value))
        await update.message.reply_text(msg + "\n\n" + get_real_trade_status_text())
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در تنظیم حد ضرر روزانه واقعی:\n{str(e)[:250]}")


async def set_real_lock_hours_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not set_real_lock_duration_hours:
        await update.message.reply_text("ماژول تنظیم زمان قفل ضرر واقعی فعال نیست. فایل real_trade_manager.py را به‌روزرسانی کن.")
        return
    value = extract_first_number(update.message.text)
    if value is None or value <= 0:
        await update.message.reply_text("مثال درست: قفل ضرر واقعی 1 ساعت")
        return
    try:
        hours = max(1, min(int(value), 168))
        msg = set_real_lock_duration_hours(hours)
        await update.message.reply_text(msg + "\n\n" + get_real_trade_status_text())
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در تنظیم زمان قفل ضرر واقعی:\n{str(e)[:250]}")


async def enable_real_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not enable_real_trading:
        await update.message.reply_text("ماژول ترید واقعی فعال نیست.")
        return
    await update.message.reply_text(enable_real_trading() + "\n\n" + get_real_trade_status_text())


async def disable_real_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not disable_real_trading:
        await update.message.reply_text("ماژول ترید واقعی فعال نیست.")
        return
    await update.message.reply_text(disable_real_trading() + "\n\n" + get_real_trade_status_text())


async def real_emergency_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not activate_real_emergency_stop:
        await update.message.reply_text("ماژول ترید واقعی فعال نیست.")
        return
    await update.message.reply_text(activate_real_emergency_stop() + "\n\n" + get_real_trade_status_text())


async def reset_real_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if get_user_id(update) != OWNER_ID:
        await reject_unauthorized(update)
        return
    if not reset_real_trade_state:
        await update.message.reply_text("ماژول ترید واقعی فعال نیست.")
        return
    await update.message.reply_text(reset_real_trade_state() + "\n\n" + get_real_trade_status_text())


async def sync_real_positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return
    if not sync_real_positions_text:
        await update.message.reply_text("ماژول همگام‌سازی توبیت فعال نیست.")
        return
    try:
        await send_long_text(update, sync_real_positions_text())
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در همگام‌سازی:\n{str(e)[:250]}")


async def close_all_positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if get_user_id(update) != OWNER_ID:
        await reject_unauthorized(update)
        return
    if not close_all_real_positions:
        await update.message.reply_text("ماژول بستن پوزیشن‌های واقعی فعال نیست.")
        return
    try:
        res = close_all_real_positions()
        lines = [f"✅ درخواست بستن همه پوزیشن‌ها ارسال شد. بسته‌شده: {res.get('closed_count', 0)}"]
        for item in (res.get("results") or [])[:10]:
            r = item.get("result") or {}
            ok = "✅" if isinstance(r, dict) and r.get("ok") else "❌"
            lines.append(f"{ok} {item.get('symbol')} {item.get('direction', '')}: {str(r.get('error') or r.get('message') or 'OK')[:120]}")
        lines.append("\n" + format_trade_status())
        await send_long_text(update, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در بستن همه پوزیشن‌ها:\n{str(e)[:250]}")


async def close_symbol_position_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if get_user_id(update) != OWNER_ID:
        await reject_unauthorized(update)
        return
    if not close_real_position_by_symbol:
        await update.message.reply_text("ماژول بستن پوزیشن واقعی فعال نیست.")
        return

    text = update.message.text or ""
    symbol = normalize_symbol_text(text.replace("بستن", "").replace("پوزیشن", ""))
    if not symbol:
        await update.message.reply_text("مثال درست: بستن OPUSDT")
        return
    try:
        res = close_real_position_by_symbol(symbol)
        if res.get("ok"):
            await update.message.reply_text(f"✅ درخواست بستن {symbol} ارسال شد.\n\n" + format_trade_status())
        else:
            await update.message.reply_text(f"❌ بسته نشد: {res.get('error') or res.get('data')}\n\n" + format_trade_status())
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در بستن {symbol}:\n{str(e)[:250]}")


# ============================================================
# Formatting analysis output
# ============================================================

def format_signal_message(result: Dict[str, Any]) -> str:
    if result.get("status") != "ACTIVE":
        return format_manual_analysis(result)

    return (
        "🚨 سیگنال خودکار\n"
        f"نماد: {result.get('symbol')}\n"
        f"جهت: {fa_direction(result.get('direction'))}\n"
        "وضعیت: ✅ ورود فعال\n\n"
        f"ورود: {result.get('entry')}\n"
        f"حد ضرر: {result.get('stop_loss')}\n"
        f"حد سود ۱: {result.get('tp1')}\n"
        f"حد سود ۲: {result.get('tp2')}\n\n"
        f"امتیاز: {result.get('score')}\n"
        f"ریسک: {result.get('risk_level')}\n"
        f"R/R: {result.get('risk_reward')}\n"
        f"اعتبار: {result.get('validity', '15 تا 45 دقیقه')}"
    )


def format_manual_analysis(result: Dict[str, Any]) -> str:
    if result.get("status") != "ACTIVE":
        reasons = "\n".join([f"• {x}" for x in result.get("reasons", [])[:8]]) or "شرایط ورود کامل نیست."
        return (
            f"📊 تحلیل {result.get('symbol')}\n\n"
            "وضعیت: ❌ بدون سیگنال معتبر\n"
            f"امتیاز: {result.get('score', 0)}\n"
            f"لانگ: {result.get('long_score', 0)} | شورت: {result.get('short_score', 0)}\n"
            f"RSI: {result.get('rsi')}\n"
            f"ADX: {result.get('adx')}\n"
            f"VWAP: {result.get('vwap_status')}\n\n"
            f"دلایل:\n{reasons}"
        )

    return (
        f"📊 تحلیل {result.get('symbol')}\n\n"
        "وضعیت: ✅ سیگنال فعال\n"
        f"جهت: {fa_direction(result.get('direction'))}\n\n"
        f"ورود: {result.get('entry')}\n"
        f"حد ضرر: {result.get('stop_loss')}\n"
        f"حد سود ۱: {result.get('tp1')}\n"
        f"حد سود ۲: {result.get('tp2')}\n\n"
        f"امتیاز: {result.get('score')}\n"
        f"لانگ: {result.get('long_score')} | شورت: {result.get('short_score')}\n"
        f"ریسک: {result.get('risk_level')}\n"
        f"R/R: {result.get('risk_reward')}\n\n"
        f"RSI: {result.get('rsi')}\n"
        f"ADX: {result.get('adx')}\n"
        f"VWAP: {result.get('vwap_status')}\n"
        f"روند بازار: {result.get('market_regime')}\n\n"
        f"اعتبار: {result.get('validity', '15 تا 45 دقیقه')}"
    )


def format_top_signals(signals: List[Dict[str, Any]]) -> str:
    if not signals:
        return "فعلاً سیگنال مناسبی پیدا نشد."

    lines = ["🏆 بهترین سیگنال‌های فعلی:"]
    for i, sig in enumerate(signals, 1):
        lines.append(
            f"\n{i}) {sig.get('symbol')} | {fa_direction(sig.get('direction'))}\n"
            f"امتیاز: {sig.get('score')} | ریسک: {sig.get('risk_level')}\n"
            f"ورود: {sig.get('entry')}\n"
            f"SL: {sig.get('stop_loss')} | TP1: {sig.get('tp1')}"
        )
    return "\n".join(lines)


def format_market_overview_text(result: Dict[str, Any]) -> str:
    return (
        "📌 بررسی کلی بازار\n\n"
        f"{result.get('summary')}\n\n"
        f"صعودی: {result.get('bullish_pct')}٪\n"
        f"نزولی: {result.get('bearish_pct')}٪\n"
        f"رنج/نامشخص: {result.get('neutral_pct')}٪\n"
        f"تعداد بررسی‌شده: {result.get('scanned')}"
    )


def attach_signal_metadata(signal: Dict[str, Any], message_id: int, chat_id: int, source: str = "auto_signal") -> Dict[str, Any]:
    s = dict(signal)
    s["telegram_message_id"] = message_id
    s["message_id"] = message_id
    s["chat_id"] = chat_id
    s["user_id"] = OWNER_ID or chat_id
    s["source"] = source
    return s


# ============================================================
# Core commands
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    text = (
        "🤖 Crypto AI Bot\n\n"
        "دستورهای اصلی:\n"
        "تحلیل بیتکوین\n"
        "سیگنال سولانا\n"
        "بهترین سیگنال\n"
        "بررسی / بررسی بازار\n"
        "آمار / آمار 7 روز / آمار کل\n"
        "حذف آمار\n\n"
        "دستورهای ترید واقعی توبیت:\n"
        "ترید / وضعیت ترید\n"
        "بالانس توبیت\n"
        "سرمایه ترید 1000\n"
        "ترید دلار 20\n"
        "ترید لوریج 5\n"
        "حداکثر پوزیشن 10\n"
        "حد ضرر روزانه 5\n"
        "قفل ضرر 1 ساعت\n"
        "ترید فعال\n"
        "ترید خاموش\n"
        "توقف اضطراری\n"
        "همگام سازی پوزیشن ها\n"
        "بستن OPUSDT\n"
        "بستن همه پوزیشن ها\n"
        "ریست ترید\n\n"
        "AI و مدیریت:\n"
        "هوش مصنوعی\n"
        "حافظه ربات\n"
        "ریسک کوین‌ها\n"
        "بهترین کوین‌ها\n"
        "بدترین کوین‌ها\n"
        "سیگنال‌های مخفی\n"
        "اسلات‌ها\n"
        "پوزیشن‌ها\n"
        "سیگنال‌های فعال\n\n"
        "یادداشت: دستورهای ترید مستقیم برای REAL / TOBIT هستند."
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_command(update, context)


async def analyze_request(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str) -> None:
    waiting = await update.message.reply_text("⏳ در حال تحلیل...")
    try:
        result = analyze_symbol(symbol)
        await waiting.edit_text(format_manual_analysis(result))
    except Exception as e:
        await waiting.edit_text(f"❌ خطا در تحلیل:\n{str(e)[:300]}")


async def best_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    waiting = await update.message.reply_text("⏳ در حال بررسی بازار...")
    try:
        signals = get_top_signals(limit=5)
        await waiting.edit_text(format_top_signals(signals))
    except Exception as e:
        await waiting.edit_text(f"❌ خطا:\n{str(e)[:300]}")


async def market_overview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    waiting = await update.message.reply_text("⏳ در حال بررسی بازار...")
    try:
        overview = scan_market_overview()
        await waiting.edit_text(format_market_overview_text(overview))
    except Exception as e:
        await waiting.edit_text(f"❌ خطا:\n{str(e)[:300]}")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    text = update.message.text if update.message else ""
    days = parse_days_from_text(text)
    parts = []

    if format_signal_stats:
        try:
            try:
                parts.append(format_signal_stats(days))
            except TypeError:
                parts.append(format_signal_stats())
        except Exception as e:
            parts.append(f"خطا در آمار سیگنال: {str(e)[:120]}")

    await send_long_text(update, "\n\n".join(parts) if parts else "آماری موجود نیست.")


async def reset_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if get_user_id(update) != OWNER_ID:
        await reject_unauthorized(update)
        return

    done = []
    try:
        if reset_signal_stats:
            reset_signal_stats()
            done.append("آمار سیگنال")
    except Exception:
        pass

    await update.message.reply_text("✅ حذف آمار انجام شد." if done else "ماژول آمار در دسترس نیست.")


async def symbol_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str = "all") -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    if not get_symbol_stats_report:
        await update.message.reply_text("گزارش آمار ارزها در دسترس نیست.")
        return

    days = parse_days_from_text(update.message.text if update.message else "")
    try:
        try:
            text = get_symbol_stats_report(days, mode=mode)
        except TypeError:
            text = get_symbol_stats_report(days)
        await send_long_text(update, text)
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در آمار ارزها:\n{str(e)[:250]}")



async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    if get_real_trade_status_text:
        try:
            await send_long_text(update, get_real_trade_status_text())
            return
        except Exception as e:
            await update.message.reply_text(f"❌ خطا در دریافت پوزیشن‌های توبیت:\n{str(e)[:250]}")
            return

    await update.message.reply_text("ماژول ترید واقعی توبیت فعال نیست.")


async def active_signals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    if not format_active_signals:
        await update.message.reply_text("ماژول Tracker فعال نیست.")
        return

    try:
        await send_long_text(update, format_active_signals())
    except Exception as e:
        await update.message.reply_text(str(e)[:250])


async def ai_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    parts = []
    for fn in [format_ai_status, format_learning_summary, format_rotation_report, format_ghost_report, format_slot_report]:
        if not fn:
            continue
        try:
            parts.append(fn())
        except Exception:
            pass

    await send_long_text(update, "\n\n".join(parts) if parts else "AI Status در دسترس نیست.")


async def learning_memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    parts = []
    if format_learning_summary:
        try:
            parts.append(format_learning_summary())
        except Exception:
            pass
    if format_smart_stats:
        try:
            parts.append(format_smart_stats())
        except Exception:
            pass
    await send_long_text(update, "\n\n".join(parts) if parts else "حافظه ربات در دسترس نیست.")


async def coin_behavior_command(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: Optional[str] = None) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    if not format_coin_behavior:
        await update.message.reply_text("گزارش رفتار کوین در دسترس نیست.")
        return

    symbol = symbol or normalize_symbol_text(update.message.text or "")
    if not symbol:
        await update.message.reply_text("مثال: رفتار بیتکوین")
        return

    try:
        await send_long_text(update, format_coin_behavior(symbol))
    except Exception as e:
        await update.message.reply_text(f"❌ خطا:\n{str(e)[:250]}")


async def rotation_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    if not format_rotation_report:
        await update.message.reply_text("گزارش Coin Rotation در دسترس نیست.")
        return

    try:
        await send_long_text(update, format_rotation_report())
    except Exception as e:
        await update.message.reply_text(f"❌ خطا:\n{str(e)[:250]}")


async def ghost_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    if not format_ghost_report:
        await update.message.reply_text("گزارش سیگنال‌های مخفی در دسترس نیست.")
        return

    try:
        await send_long_text(update, format_ghost_report())
    except Exception as e:
        await update.message.reply_text(f"❌ خطا:\n{str(e)[:250]}")


async def slot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    if not format_slot_report:
        await update.message.reply_text("گزارش Slot Manager در دسترس نیست.")
        return

    try:
        await send_long_text(update, format_slot_report())
    except Exception as e:
        await update.message.reply_text(f"❌ خطا:\n{str(e)[:250]}")


# ============================================================
# Register signal after sending
# ============================================================

async def register_sent_signal(signal: Dict[str, Any], sent_message: Any, source: str = "auto_signal") -> None:
    try:
        chat_id = sent_message.chat_id
        message_id = sent_message.message_id

        meta = attach_signal_metadata(signal, message_id, chat_id, source)

        # In REAL-only mode, an auto signal must not enter tracking/slots
        # unless the real Toobit order is actually accepted first.
        if source == "auto_signal":
            if not open_real_position_from_signal:
                logger.error("REAL order module unavailable; auto signal was not tracked.")
                try:
                    await sent_message.reply_text("❌ سفارش واقعی ثبت نشد: ماژول توبیت در دسترس نیست.")
                except Exception:
                    pass
                return

            try:
                real_result = open_real_position_from_signal(meta)
            except Exception as e:
                logger.error(f"open_real_position_from_signal error: {e}")
                try:
                    await sent_message.reply_text(f"❌ سفارش واقعی ثبت نشد:\n{str(e)[:250]}")
                except Exception:
                    pass
                return

            if real_result.get("ok"):
                logger.info(f"real Toobit position opened: {real_result}")
                meta["real_order"] = real_result
            else:
                reason = real_result.get("error") or real_result.get("message") or real_result.get("data") or "نامشخص"
                logger.warning(f"real Toobit position not opened; signal will not be tracked: {real_result}")
                try:
                    await sent_message.reply_text(f"❌ سفارش واقعی ثبت نشد؛ سیگنال وارد اسلات نشد.\nعلت: {str(reason)[:250]}")
                except Exception:
                    pass
                return

        if add_signal_to_tracking:
            try:
                add_signal_to_tracking(meta)
            except TypeError:
                try:
                    add_signal_to_tracking(
                        user_id=OWNER_ID or chat_id,
                        chat_id=chat_id,
                        message_id=message_id,
                        result=meta,
                    )
                except TypeError:
                    add_signal_to_tracking(OWNER_ID or chat_id, chat_id, message_id, meta)
            except Exception as e:
                logger.error(f"add_signal_to_tracking error: {e}")

    except Exception as e:
        logger.error(f"register_sent_signal error: {e}")


# ============================================================
# Auto Signal Loop
# ============================================================

def auto_signal_key(signal: Dict[str, Any]) -> str:
    return f"{signal.get('symbol')}_{signal.get('direction')}"


def auto_signal_gate(signal: Dict[str, Any]) -> tuple[bool, str, bool]:
    """
    Returns: (can_send_to_telegram, reason, save_as_ghost)

    REAL-only safety rule:
    Auto signals are sent only when REAL trading is fully ready.
    If trading is off, emergency stop is active, daily lock is active,
    Toobit usable balance is not enough, or slots are full, the signal
    must stay silent and be stored as Ghost for learning.
    """
    try:
        st = load_bot_settings()

        if not st.get("trading_enabled", True):
            return False, "BOT_TRADING_DISABLED", True

        if not st.get("auto_signal_enabled", bool(AUTO_SIGNAL_ENABLED)):
            return False, "AUTO_SIGNAL_DISABLED", False

        if signal.get("status") != "ACTIVE":
            return False, "SIGNAL_NOT_ACTIVE", True

        if not signal.get("entry_confirmed", False):
            return False, "ENTRY_NOT_CONFIRMED", True

        if int(signal.get("score", 0) or 0) < int(AUTO_DIRECT_SCORE_MIN):
            return False, "LOW_SCORE", True

        key = auto_signal_key(signal)
        now = int(time.time())
        last = int(LAST_AUTO_SIGNAL_TIME.get(key, 0))
        if now - last < AUTO_SIGNAL_COOLDOWN_SECONDS:
            return False, "COOLDOWN", False

        if not is_real_trade_ready:
            return False, "REAL_TRADE_MODULE_UNAVAILABLE", True

        ready, reason = is_real_trade_ready()
        if not ready:
            return False, f"REAL_NOT_READY: {reason}", True

        return True, "OK", False

    except Exception as e:
        return False, f"GATE_ERROR: {str(e)[:120]}", True


def can_send_auto_signal(signal: Dict[str, Any]) -> bool:
    ok, _reason, _ghost = auto_signal_gate(signal)
    return ok


def save_auto_signal_as_ghost(signal: Dict[str, Any], reason: str) -> None:
    if not create_ghost_signal:
        return

    try:
        create_ghost_signal(
            symbol=signal.get("symbol"),
            direction=signal.get("direction"),
            entry=signal.get("entry"),
            stop_loss=signal.get("stop_loss") or signal.get("sl"),
            tp1=signal.get("tp1"),
            tp2=signal.get("tp2"),
            score=signal.get("score"),
            snapshot=signal.get("snapshot", {}),
            source="auto_signal_gate",
            reason=reason,
        )
        logger.info(f"auto signal saved as ghost: {signal.get('symbol')} {signal.get('direction')} | {reason}")
    except TypeError:
        try:
            create_ghost_signal(
                signal.get("symbol"),
                signal.get("direction"),
                signal.get("entry"),
                signal.get("stop_loss") or signal.get("sl"),
                signal.get("tp1"),
                signal.get("tp2"),
                signal.get("score"),
                signal.get("snapshot", {}),
                "auto_signal_gate",
                reason,
            )
        except Exception as e:
            logger.error(f"save_auto_signal_as_ghost fallback error: {e}")
    except Exception as e:
        logger.error(f"save_auto_signal_as_ghost error: {e}")


def mark_auto_signal_sent(signal: Dict[str, Any]) -> None:
    LAST_AUTO_SIGNAL_TIME[auto_signal_key(signal)] = int(time.time())


async def auto_signal_loop(app: Application) -> None:
    if not AUTO_SIGNAL_ENABLED or not OWNER_ID:
        logger.info("Auto signal disabled or OWNER_ID missing")
        return

    await asyncio.sleep(10)

    while True:
        try:
            result = scan_for_auto_signals(max_results=3, allow_ghost=True)
            signals = result.get("signals", [])

            for signal in signals:
                can_send, reason, should_ghost = auto_signal_gate(signal)
                if not can_send:
                    if should_ghost:
                        save_auto_signal_as_ghost(signal, reason)
                    continue

                sent = await app.bot.send_message(chat_id=OWNER_ID, text=format_signal_message(signal))
                await register_sent_signal(signal, sent, "auto_signal")
                mark_auto_signal_sent(signal)
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"auto_signal_loop error: {e}")

        await asyncio.sleep(max(60, int(AUTO_SCAN_INTERVAL_MINUTES) * 60))


# ============================================================
# Signal Tracker Loop
# ============================================================

async def signal_tracking_loop(app: Application) -> None:
    if not check_active_signals:
        logger.warning("Signal tracker is not available")
        return

    await asyncio.sleep(15)

    while True:
        try:
            events = check_active_signals() or []
            if isinstance(events, dict):
                events = [events]

            for event in events:
                if not isinstance(event, dict):
                    continue

                text = event.get("message") or event.get("text")
                chat_id = event.get("chat_id") or OWNER_ID
                reply_to_message_id = event.get("reply_to_message_id")

                if not text:
                    continue

                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_to_message_id=reply_to_message_id,
                    )
                except Exception:
                    await app.bot.send_message(chat_id=chat_id, text=text)

                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"signal_tracking_loop error: {e}")

        await asyncio.sleep(20)


# ============================================================
# Text handler
# ============================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    low = text.lower().strip()

    # Removed manual tracking commands
    if low in ["زیر نظر", "زیرنظر", "زیر نظر بگیر", "نظر"]:
        await update.message.reply_text("نیازی به دستور زیر نظر نیست؛ همه سیگنال‌های معتبر خودکار Track می‌شوند.")
        return

    # Real Toobit trade commands - must be checked before generic trade commands.
    if low in ["ترید واقعی", "وضعیت ترید واقعی", "ترید توبیت", "وضعیت ترید توبیت"]:
        await real_trade_status_command(update, context)
        return

    if low in ["بالانس توبیت", "موجودی توبیت"]:
        await toobit_balance_command(update, context)
        return

    if low.startswith("سرمایه واقعی") or low.startswith("سرمایه ترید واقعی"):
        await set_real_capital_command(update, context)
        return

    if low.startswith("ترید واقعی دلار") or low.startswith("حجم واقعی") or low.startswith("حجم ترید واقعی"):
        await set_real_position_size_command(update, context)
        return

    if low.startswith("لوریج واقعی") or low.startswith("لوریج ترید واقعی"):
        await set_real_leverage_command(update, context)
        return

    if low.startswith("حداکثر پوزیشن واقعی") or low.startswith("حداکثر پوزیشن توبیت"):
        await set_real_max_positions_command(update, context)
        return

    if low.startswith("حد ضرر روزانه واقعی") or low.startswith("حدضرر روزانه واقعی") or low.startswith("حد ضرر توبیت"):
        await set_real_daily_loss_limit_command(update, context)
        return

    if low.startswith("قفل ضرر واقعی") or low.startswith("قفل ضرر توبیت"):
        await set_real_lock_hours_command(update, context)
        return

    if low in ["ترید واقعی فعال", "فعال ترید واقعی", "فعال سازی ترید واقعی", "فعال‌سازی ترید واقعی"]:
        await enable_real_trade_command(update, context)
        return

    if low in ["ترید واقعی خاموش", "خاموش ترید واقعی", "غیرفعال ترید واقعی", "ترید واقعی غیرفعال"]:
        await disable_real_trade_command(update, context)
        return

    if low in ["توقف اضطراری واقعی", "استاپ اضطراری واقعی", "خاموش اضطراری واقعی"]:
        await real_emergency_stop_command(update, context)
        return

    if low == "ریست ترید واقعی":
        await reset_real_trade_command(update, context)
        return

    # Real risk protection commands.
    if low.startswith("حد ضرر روزانه") or low.startswith("حدضرر روزانه"):
        await set_daily_loss_limit_command(update, context)
        return

    if low.startswith("قفل ضرر"):
        await set_daily_lock_hours_command(update, context)
        return

    # Trade commands - generic names now control REAL / TOBIT only.
    if low in ["ترید", "وضعیت ترید", "تریدها"]:
        await trade_status_command(update, context)
        return

    if low in ["ترید فعال", "فعال ترید", "فعال سازی ترید", "فعال‌سازی ترید"]:
        await enable_real_trade_command(update, context)
        return

    if low in ["ترید خاموش", "خاموش ترید", "غیرفعال ترید", "ترید غیرفعال"]:
        await disable_real_trade_command(update, context)
        return

    if low in ["توقف اضطراری", "استاپ اضطراری", "خاموش اضطراری"]:
        await real_emergency_stop_command(update, context)
        return

    if low in ["همگام سازی", "همگام‌سازی", "همگام سازی پوزیشن ها", "همگام‌سازی پوزیشن‌ها", "sync", "sync positions"]:
        await sync_real_positions_command(update, context)
        return

    if low in ["بستن همه", "بستن همه پوزیشن ها", "بستن همه پوزیشن‌ها", "close all", "close all positions"]:
        await close_all_positions_command(update, context)
        return

    if low.startswith("بستن ") or low.startswith("close "):
        await close_symbol_position_command(update, context)
        return

    if low in ["آمار ترید", "امار ترید"]:
        await trade_stats_command(update, context)
        return

    if low.startswith("سرمایه ترید"):
        await set_capital_command(update, context)
        return

    if low.startswith("ترید دلار"):
        await set_trade_margin_command(update, context)
        return

    if low.startswith("ترید لوریج") or low.startswith("لوریج دلار") or low.startswith("لوریج"):
        await set_leverage_command(update, context)
        return

    if low.startswith("حداکثر پوزیشن"):
        await set_max_positions_command(update, context)
        return

    if low == "حجم پوزیشن":
        await position_size_command(update, context)
        return

    if low == "ریست ترید":
        await reset_trade_command(update, context)
        return

    # Main commands
    if low in ["بهترین سیگنال", "بهترین", "top", "best"]:
        await best_signal_command(update, context)
        return

    if low in ["بررسی", "بررسی بازار", "بازار", "وضعیت بازار"]:
        await market_overview_command(update, context)
        return

    if low in ["حذف آمار", "حذف امار", "ریست آمار", "reset stats"]:
        await reset_stats_command(update, context)
        return

    if low.startswith("آمار ارز") or low.startswith("امار ارز"):
        await symbol_stats_command(update, context, mode="all")
        return

    if low.startswith("بهترین ارز") or low.startswith("بهترین کوین"):
        await symbol_stats_command(update, context, mode="best")
        return

    if low.startswith("بدترین ارز") or low.startswith("بدترین کوین"):
        await symbol_stats_command(update, context, mode="worst")
        return

    if low.startswith("آمار") or low.startswith("امار") or low == "stats":
        await stats_command(update, context)
        return

    if low in ["پوزیشن‌ها", "پوزیشن ها", "positions"]:
        await positions_command(update, context)
        return

    if low in ["سیگنال‌های فعال", "سیگنال های فعال", "active signals"]:
        await active_signals_command(update, context)
        return

    if low in ["هوش مصنوعی", "ai", "وضعیت ai", "وضعیت هوش مصنوعی"]:
        await ai_status_command(update, context)
        return

    if low in ["حافظه ربات", "حافظه ai", "یادگیری", "learning"]:
        await learning_memory_command(update, context)
        return

    if low.startswith("رفتار "):
        await coin_behavior_command(update, context)
        return

    if low in ["ریسک کوین‌ها", "ریسک کوین ها", "بهترین کوین‌ها", "بهترین کوین ها", "بدترین کوین‌ها", "بدترین کوین ها", "چرخش کوین", "coin rotation"]:
        await rotation_command(update, context)
        return

    if low in ["سیگنال‌های مخفی", "سیگنال های مخفی", "ghost", "ghost signals"]:
        await ghost_command(update, context)
        return

    if low in ["اسلات‌ها", "اسلات ها", "slots", "slot"]:
        await slot_command(update, context)
        return

    symbol = normalize_symbol_text(text)
    if symbol:
        await analyze_request(update, context, symbol)
        return

    await update.message.reply_text(
        "متوجه نشدم. مثلا بنویس:\n"
        "تحلیل بیتکوین\n"
        "بهترین سیگنال\n"
        "بررسی\n"
        "وضعیت ترید"
    )


# ============================================================
# Telegram application
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Telegram error", exc_info=context.error)


async def post_init(app: Application) -> None:
    asyncio.create_task(auto_signal_loop(app))
    asyncio.create_task(signal_tracking_loop(app))


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("positions", positions_command))
    app.add_handler(CommandHandler("active", active_signals_command))
    app.add_handler(CommandHandler("ai", ai_status_command))
    app.add_handler(CommandHandler("resetstats", reset_stats_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    return app


def main() -> None:
    app = build_application()
    logger.info("Crypto AI Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)


if __name__ == "__main__":
    main()
