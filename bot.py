# -*- coding: utf-8 -*-
"""
Crypto AI Telegram Bot - Full Command Version

این نسخه برای معماری فعلی ربات نوشته شده:
- سیگنال‌ها خودکار Track می‌شوند؛ دستور دستی «زیر نظر» حذف شده.
- دستورهای ترید/سرمایه/لوریج/حجم پوزیشن اضافه شده.
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
    from paper_trader import (
        open_paper_position,
        format_paper_stats,
        format_open_positions,
        reset_paper_trades,
    )
except Exception:
    open_paper_position = None
    format_paper_stats = None
    format_open_positions = None
    reset_paper_trades = None


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
    from ghost_signals import format_ghost_report
except Exception:
    format_ghost_report = None

try:
    from slot_manager import format_slot_report
except Exception:
    format_slot_report = None


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
USER_PENDING_ACTION: Dict[int, str] = {}
AUTO_SIGNAL_COOLDOWN_SECONDS = int(AUTO_SIGNAL_COOLDOWN_MINUTES) * 60

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
TRADE_SETTINGS_FILE = os.path.join(DATA_DIR, "trade_settings.json")
BOT_SETTINGS_FILE = os.path.join(DATA_DIR, "bot_settings.json")


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
# Bot / AI settings helpers
# ============================================================

DEFAULT_BOT_SETTINGS = {
    "trading_enabled": True,
    "ai_enabled": True,
    "learning_enabled": True,
    "daily_report_enabled": False,
    "mode": "normal",
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


def format_bot_settings_status() -> str:
    st = load_bot_settings()
    return (
        "⚙️ وضعیت تنظیمات ربات\n\n"
        f"ترید: {'✅ فعال' if st.get('trading_enabled') else '❌ غیرفعال'}\n"
        f"اتوسیگنال: {'✅ فعال' if st.get('auto_signal_enabled') else '❌ غیرفعال'}\n"
        f"AI: {'✅ روشن' if st.get('ai_enabled') else '❌ خاموش'}\n"
        f"یادگیری: {'✅ روشن' if st.get('learning_enabled') else '❌ خاموش'}\n"
        f"گزارش روزانه: {'✅ روشن' if st.get('daily_report_enabled') else '❌ خاموش'}\n"
        f"حالت: {'محافظه‌کار' if st.get('mode') == 'conservative' else 'عادی'}"
    )


async def set_bot_setting_command(update: Update, key: str, value: Any, message: str) -> None:
    st = load_bot_settings()
    st[key] = value
    save_bot_settings(st)
    await update.message.reply_text(f"✅ {message}\n\n{format_bot_settings_status()}")


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


def normalize_symbol_text(text: str) -> Optional[str]:
    raw_text = str(text or "").strip()
    t = raw_text.lower()

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

    for key, symbol in PERSIAN_SYMBOLS.items():
        if key in cleaned:
            return symbol

    raw = cleaned.upper().replace(" ", "")
    if raw.endswith("USDT") and len(raw) >= 6:
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
    "capital_usd": 1000.0,
    "trade_margin_usd": 20.0,
    "leverage": 5.0,
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


def format_trade_status() -> str:
    s = load_trade_settings()
    position_size = calc_position_size(s)
    capital = safe_num(s.get("capital_usd"), 0.0)
    margin = safe_num(s.get("trade_margin_usd"), 0.0)
    leverage = safe_num(s.get("leverage"), 1.0)
    max_positions = int(s.get("max_positions", 5) or 5)
    risk_pct = round((margin / capital) * 100, 2) if capital > 0 else 0

    return (
        "💰 وضعیت ترید\n\n"
        f"سرمایه ترید: {capital}$\n"
        f"مبلغ هر ترید: {margin}$\n"
        f"لوریج: {leverage}x\n"
        f"حجم پوزیشن تقریبی: {position_size}$\n"
        f"ریسک هر ترید نسبت به سرمایه: {risk_pct}٪\n"
        f"حداکثر پوزیشن همزمان: {max_positions}\n\n"
        "دستورها:\n"
        "سرمایه ترید 1000\n"
        "ترید دلار / ترید دلار 20\n"
        "ترید لوریج / ترید لوریج 5\n"
        "حداکثر پوزیشن / حداکثر پوزیشن 10\n"
        "حجم پوزیشن\n"
        "ترید فعال / ترید غیرفعال\n"
        "ریست ترید"
    )


async def trade_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(format_trade_status())


async def set_capital_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    value = extract_first_number(update.message.text)
    if value is None:
        USER_PENDING_ACTION[get_user_id(update)] = "set_capital"
        await update.message.reply_text("کل سرمایه ترید چند دلار است؟\nعدد بزرگ‌تر از 1 بفرست.")
        return
    if value < 1:
        await update.message.reply_text("سرمایه باید عددی بزرگ‌تر از 1 دلار باشد.")
        return
    s = load_trade_settings()
    s["capital_usd"] = float(value)
    save_trade_settings(s)
    await update.message.reply_text(f"✅ سرمایه ترید روی {value}$ تنظیم شد.\n\n{format_trade_status()}")


async def set_trade_margin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    value = extract_first_number(update.message.text)
    if value is None:
        USER_PENDING_ACTION[get_user_id(update)] = "set_trade_margin"
        await update.message.reply_text("مبلغ هر پوزیشن چند دلار باشد؟\nعدد بین 1 تا 1,000,000 بفرست.")
        return
    if value < 1 or value > 1_000_000:
        await update.message.reply_text("عدد باید بین 1 تا 1,000,000 دلار باشد.")
        return
    s = load_trade_settings()
    s["trade_margin_usd"] = float(value)
    save_trade_settings(s)
    await update.message.reply_text(f"✅ مبلغ هر پوزیشن روی {value}$ تنظیم شد.\n\n{format_trade_status()}")


async def set_leverage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    value = extract_first_number(update.message.text)
    if value is None:
        USER_PENDING_ACTION[get_user_id(update)] = "set_leverage"
        await update.message.reply_text("لوریج چند باشد؟\nعدد بین 1 تا 50 بفرست.")
        return
    if value < 1 or value > 50:
        await update.message.reply_text("لوریج باید بین 1 تا 50 باشد.")
        return
    value = float(value)
    s = load_trade_settings()
    s["leverage"] = value
    save_trade_settings(s)
    await update.message.reply_text(f"✅ لوریج روی {value}x تنظیم شد.\n\n{format_trade_status()}")


async def set_max_positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    value = extract_first_number(update.message.text)
    if value is None:
        USER_PENDING_ACTION[get_user_id(update)] = "set_max_positions"
        await update.message.reply_text("حداکثر چند پوزیشن همزمان باشد؟\nعدد بین 1 تا 50 بفرست.")
        return
    if value < 1 or value > 50:
        await update.message.reply_text("حداکثر پوزیشن باید بین 1 تا 50 باشد.")
        return
    st = load_trade_settings()
    st["max_positions"] = int(value)
    save_trade_settings(st)
    await update.message.reply_text(f"✅ حداکثر پوزیشن همزمان روی {int(value)} تنظیم شد.\n\n{format_trade_status()}")


async def position_size_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = load_trade_settings()
    await update.message.reply_text(
        "📌 حجم پوزیشن\n\n"
        f"مبلغ ترید: {s.get('trade_margin_usd')}$\n"
        f"لوریج: {s.get('leverage')}x\n"
        f"حجم پوزیشن تقریبی: {calc_position_size(s)}$"
    )


async def reset_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    save_trade_settings(DEFAULT_TRADE_SETTINGS.copy())
    await update.message.reply_text("✅ تنظیمات ترید ریست شد.\n\n" + format_trade_status())


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




def format_commands_help() -> str:
    return (
        "📚 دستورات ربات\n\n"
        "📊 تحلیل و سیگنال:\n"
        "تحلیل بیتکوین\n"
        "سیگنال سولانا\n"
        "BTC / ETH / SOL\n"
        "بهترین سیگنال\n"
        "بررسی / بررسی بازار\n\n"
        "💰 ترید و مدیریت سرمایه:\n"
        "وضعیت ترید / وضعیت / ترید\n"
        "سرمایه ترید / سرمایه ترید 1000\n"
        "ترید دلار / ترید دلار 20\n"
        "ترید لوریج / ترید لوریج 5\n"
        "حداکثر پوزیشن / حداکثر پوزیشن 10\n"
        "حجم پوزیشن\n"
        "ریست ترید\n"
        "ترید فعال / ترید غیرفعال\n"
        "اتوسیگنال فعال / اتوسیگنال غیرفعال\n\n"
        "📈 آمار و پوزیشن‌ها:\n"
        "آمار / آمار 7 روز / آمار کل\n"
        "آمار ارزها\n"
        "بهترین ارزها / بدترین ارزها\n"
        "پوزیشن‌ها\n"
        "سیگنال‌های فعال\n"
        "حذف آمار\n\n"
        "🧠 هوش مصنوعی:\n"
        "هوش مصنوعی / AI / وضعیت AI\n"
        "آمار هوشمند\n"
        "حافظه ربات\n"
        "رفتار کوین BTC\n"
        "ریسک کوین‌ها\n"
        "بهترین کوین‌ها\n"
        "بدترین کوین‌ها\n"
        "سیگنال‌های مخفی\n"
        "اسلات‌ها\n\n"
        "⚙️ تنظیمات AI و ربات:\n"
        "تنظیمات ربات\n"
        "AI روشن / AI خاموش\n"
        "یادگیری روشن / یادگیری خاموش\n"
        "گزارش روزانه روشن / گزارش روزانه خاموش\n"
        "حالت محافظه‌کار / حالت عادی\n"
    )

# ============================================================
# Core commands
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    await send_long_text(update, format_commands_help())


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

    if format_paper_stats:
        try:
            parts.append(format_paper_stats())
        except Exception:
            pass

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

    try:
        if reset_paper_trades:
            reset_paper_trades()
            done.append("Paper Trade")
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

    if not format_open_positions:
        await update.message.reply_text("ماژول Paper Trade فعال نیست.")
        return

    try:
        await send_long_text(update, format_open_positions())
    except Exception as e:
        await update.message.reply_text(str(e)[:250])


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

        if open_paper_position:
            try:
                open_paper_position(meta, telegram_message_id=message_id, chat_id=chat_id)
            except Exception as e:
                logger.error(f"open_paper_position error: {e}")

    except Exception as e:
        logger.error(f"register_sent_signal error: {e}")


# ============================================================
# Auto Signal Loop
# ============================================================

def auto_signal_key(signal: Dict[str, Any]) -> str:
    return f"{signal.get('symbol')}_{signal.get('direction')}"


def can_send_auto_signal(signal: Dict[str, Any]) -> bool:
    try:
        if signal.get("status") != "ACTIVE":
            return False
        if not signal.get("entry_confirmed", False):
            return False
        if int(signal.get("score", 0) or 0) < int(AUTO_DIRECT_SCORE_MIN):
            return False

        key = auto_signal_key(signal)
        now = int(time.time())
        last = int(LAST_AUTO_SIGNAL_TIME.get(key, 0))
        return now - last >= AUTO_SIGNAL_COOLDOWN_SECONDS
    except Exception:
        return False


def mark_auto_signal_sent(signal: Dict[str, Any]) -> None:
    LAST_AUTO_SIGNAL_TIME[auto_signal_key(signal)] = int(time.time())


async def auto_signal_loop(app: Application) -> None:
    if not OWNER_ID:
        logger.info("Auto signal disabled or OWNER_ID missing")
        return

    await asyncio.sleep(10)

    while True:
        try:
            bot_settings = load_bot_settings()
            if not bot_settings.get("auto_signal_enabled", True) or not bot_settings.get("trading_enabled", True):
                await asyncio.sleep(max(60, int(AUTO_SCAN_INTERVAL_MINUTES) * 60))
                continue
            result = scan_for_auto_signals(max_results=3, allow_ghost=True)
            signals = result.get("signals", [])

            for signal in signals:
                if not can_send_auto_signal(signal):
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
    uid = get_user_id(update)

    # Follow-up answers for trade settings
    if uid in USER_PENDING_ACTION:
        action = USER_PENDING_ACTION.pop(uid)
        value = extract_first_number(text)

        if action == "set_trade_margin":
            if value is None or value < 1 or value > 1_000_000:
                await update.message.reply_text("عدد باید بین 1 تا 1,000,000 دلار باشد. دوباره دستور «ترید دلار» را بفرست.")
                return
            s = load_trade_settings()
            s["trade_margin_usd"] = float(value)
            save_trade_settings(s)
            await update.message.reply_text(f"✅ مبلغ هر پوزیشن روی {value}$ تنظیم شد.\n\n{format_trade_status()}")
            return

        if action == "set_leverage":
            if value is None or value < 1 or value > 50:
                await update.message.reply_text("لوریج باید بین 1 تا 50 باشد. دوباره دستور «ترید لوریج» را بفرست.")
                return
            s = load_trade_settings()
            s["leverage"] = float(value)
            save_trade_settings(s)
            await update.message.reply_text(f"✅ لوریج روی {value}x تنظیم شد.\n\n{format_trade_status()}")
            return

        if action == "set_capital":
            if value is None or value < 1 or value > 1_000_000_000:
                await update.message.reply_text("سرمایه باید عددی بزرگ‌تر از 1 دلار باشد. دوباره دستور «سرمایه ترید» را بفرست.")
                return
            s = load_trade_settings()
            s["capital_usd"] = float(value)
            save_trade_settings(s)
            await update.message.reply_text(f"✅ سرمایه ترید روی {value}$ تنظیم شد.\n\n{format_trade_status()}")
            return

        if action == "set_max_positions":
            if value is None or value < 1 or value > 50:
                await update.message.reply_text("حداکثر پوزیشن باید بین 1 تا 50 باشد. دوباره دستور «حداکثر پوزیشن» را بفرست.")
                return
            s = load_trade_settings()
            s["max_positions"] = int(value)
            save_trade_settings(s)
            await update.message.reply_text(f"✅ حداکثر پوزیشن همزمان روی {int(value)} تنظیم شد.\n\n{format_trade_status()}")
            return

    # Removed manual tracking commands
    if low in ["زیر نظر", "زیرنظر", "زیر نظر بگیر", "نظر"]:
        await update.message.reply_text("نیازی به دستور زیر نظر نیست؛ همه سیگنال‌های معتبر خودکار Track می‌شوند.")
        return

    if low in ["دستورات", "راهنما", "help", "/help"]:
        await send_long_text(update, format_commands_help())
        return

    # Bot / AI / trading switches
    if low in ["وضعیت ربات", "تنظیمات ربات", "تنظیمات", "تنظیمات ai", "تنظیمات هوش مصنوعی"]:
        await update.message.reply_text(format_bot_settings_status())
        return

    if low in ["ترید فعال", "فعال ترید"]:
        await set_bot_setting_command(update, "trading_enabled", True, "ترید فعال شد.")
        return

    if low in ["ترید غیرفعال", "ترید غیر فعال", "غیرفعال ترید", "غیر فعال ترید"]:
        await set_bot_setting_command(update, "trading_enabled", False, "ترید غیرفعال شد.")
        return

    if low in ["اتوسیگنال فعال", "اتو سیگنال فعال", "سیگنال خودکار فعال"]:
        await set_bot_setting_command(update, "auto_signal_enabled", True, "اتوسیگنال فعال شد.")
        return

    if low in ["اتوسیگنال غیرفعال", "اتوسیگنال غیر فعال", "اتو سیگنال غیرفعال", "اتو سیگنال غیر فعال", "سیگنال خودکار غیرفعال"]:
        await set_bot_setting_command(update, "auto_signal_enabled", False, "اتوسیگنال غیرفعال شد.")
        return

    if low in ["ai روشن", "هوش مصنوعی روشن"]:
        await set_bot_setting_command(update, "ai_enabled", True, "AI روشن شد.")
        return

    if low in ["ai خاموش", "هوش مصنوعی خاموش"]:
        await set_bot_setting_command(update, "ai_enabled", False, "AI خاموش شد.")
        return

    if low in ["یادگیری روشن", "learning on"]:
        await set_bot_setting_command(update, "learning_enabled", True, "یادگیری روشن شد.")
        return

    if low in ["یادگیری خاموش", "learning off"]:
        await set_bot_setting_command(update, "learning_enabled", False, "یادگیری خاموش شد.")
        return

    if low in ["گزارش روزانه روشن", "گزارش روزانه فعال"]:
        await set_bot_setting_command(update, "daily_report_enabled", True, "گزارش روزانه روشن شد.")
        return

    if low in ["گزارش روزانه خاموش", "گزارش روزانه غیرفعال", "گزارش روزانه غیر فعال"]:
        await set_bot_setting_command(update, "daily_report_enabled", False, "گزارش روزانه خاموش شد.")
        return

    if low in ["حالت محافظه‌کار", "حالت محافظه کار", "محافظه‌کار", "محافظه کار"]:
        await set_bot_setting_command(update, "mode", "conservative", "حالت محافظه‌کار فعال شد.")
        return

    if low in ["حالت عادی", "عادی"]:
        await set_bot_setting_command(update, "mode", "normal", "حالت عادی فعال شد.")
        return

    # Trade commands
    if low in ["وضعیت ترید", "وضعیت", "ترید"]:
        await trade_status_command(update, context)
        return

    if low.startswith("سرمایه ترید"):
        await set_capital_command(update, context)
        return

    if low.startswith("ترید دلار"):
        await set_trade_margin_command(update, context)
        return

    if low.startswith("ترید لوریج") or low.startswith("لوریج ترید") or low.startswith("لوریج دلار") or low.startswith("دلار لوریج") or low.startswith("لوریج"):
        await set_leverage_command(update, context)
        return

    if low.startswith("حداکثر پوزیشن") or low.startswith("حد اکثر پوزیشن") or low.startswith("حداکثر معاملات") or low.startswith("حداکثر معامله"):
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

    if low in ["حافظه ربات", "حافظه ai", "یادگیری", "learning", "آمار هوشمند", "امار هوشمند"]:
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
        "وضعیت ترید\n"
        "ترید دلار\n"
        "ترید لوریج\n"
        "حداکثر پوزیشن\n"
        "هوش مصنوعی"
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
    app.add_handler(CommandHandler("commands", help_command))
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
