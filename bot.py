# -*- coding: utf-8 -*-
import os
import telebot
import threading
import time

from config import BOT_TOKEN, AUTO_SCAN_INTERVAL_MINUTES, TRACKER_CHECK_INTERVAL_SECONDS, AUTO_TRACK_AUTO_SIGNALS
from coins_fa import COINS_FA
from analysis import analyze_symbol
from scanner import get_best_signals, SCAN_SYMBOLS, should_send_auto_signal
from market_scanner import get_market_status_text
from users import is_user_allowed, is_owner, add_user, remove_user, list_users
from diagnostics import format_error_report, log_exception
from signal_tracker import (
    add_signal_to_tracking,
    check_active_signals,
    get_stats_report,
    parse_days_from_text,
    parse_profit_calc_text,
    parse_days_from_report_text,
    get_profit_for_signal_text,
    get_profit_simulation_report,
    reset_stats,
    can_add_automatic_signal,
    get_symbol_stats_report,
)

# Paper Trade / Trade commands
try:
    from paper_trader import (
        set_trade_enabled,
        emergency_stop,
        set_trade_margin,
        set_leverage,
        set_max_open_positions,
        reset_trade_stats,
        set_trade_balance,
        format_trade_status,
        format_open_positions,
        format_trade_stats,
        format_trade_settings,
        format_empty_slots,
    )
    PAPER_TRADER_AVAILABLE = True
    PAPER_TRADER_IMPORT_ERROR = None
except Exception as e:
    PAPER_TRADER_AVAILABLE = False
    PAPER_TRADER_IMPORT_ERROR = str(e)

    def _paper_unavailable(*args, **kwargs):
        return (
            "❌ بخش Paper Trade لود نشد.\n"
            "فایل‌های paper_trader.py و auto_trade_config.py را چک کن.\n"
            f"جزئیات: {PAPER_TRADER_IMPORT_ERROR}"
        )

    def set_trade_enabled(enabled):
        return _paper_unavailable()

    def emergency_stop():
        return _paper_unavailable()

    def format_trade_status():
        return _paper_unavailable()

    def format_open_positions():
        return _paper_unavailable()

    def format_trade_stats():
        return _paper_unavailable()

    def format_trade_settings():
        return _paper_unavailable()

    def format_empty_slots():
        return _paper_unavailable()

    def set_trade_margin(value):
        return False, _paper_unavailable()

    def set_leverage(value):
        return False, _paper_unavailable()

    def set_max_open_positions(value):
        return False, _paper_unavailable()

    def reset_trade_stats():
        return _paper_unavailable()

    def set_trade_balance(value):
        return False, _paper_unavailable()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN تنظیم نشده است. اول روی VPS دستور export BOT_TOKEN را بزن.")

bot = telebot.TeleBot(BOT_TOKEN)

# \u062d\u0627\u0641\u0638\u0647 \u0645\u0648\u0642\u062a \u0628\u0631\u0627\u06cc \u0627\u062a\u0635\u0627\u0644 \u067e\u06cc\u0627\u0645 \u0633\u06cc\u06af\u0646\u0627\u0644 \u0628\u0647 \u062f\u0633\u062a\u0648\u0631 \xab\u0632\u06cc\u0631 \u0646\u0638\u0631\xbb
MESSAGE_RESULTS = {}

TRACK_COMMANDS = ["\u0632\u06cc\u0631 \u0646\u0638\u0631", "\u0632\u06cc\u0631\u0646\u0638\u0631", "\u0632\u06cc\u0631 \u0646\u0638\u0631 \u0628\u06af\u06cc\u0631", "\u0646\u0638\u0631"]

# حافظه موقت برای دستورات دو مرحله‌ای ترید مثل «ترید دلار» و «ترید لوریج»
TRADE_WAITING_ACTION = {}


def safe(value, default="\u0646\u0627\u0645\u0634\u062e\u0635"):
    if value is None:
        return default
    return value


def remember_signal_result(sent_message, result):
    try:
        if result and result.get("direction") != "NO TRADE":
            key = (int(sent_message.chat.id), int(sent_message.message_id))
            MESSAGE_RESULTS[key] = result
    except Exception as e:
        print("REMEMBER SIGNAL ERROR:", str(e))


def get_replied_signal_result(message):
    if not message.reply_to_message:
        return None

    key = (
        int(message.reply_to_message.chat.id),
        int(message.reply_to_message.message_id)
    )

    return MESSAGE_RESULTS.get(key)


def is_track_command(text):
    clean = text.strip().lower()
    return clean in TRACK_COMMANDS


def is_stats_command(text):
    clean = text.strip()
    return clean == "\u0622\u0645\u0627\u0631" or clean.startswith("\u0622\u0645\u0627\u0631 ")


def is_reset_stats_command(text):
    clean = text.strip().lower()
    return clean in ["حذف آمار", "ریست آمار", "پاک کردن آمار", "صفر کردن آمار", "پاکسازی آمار"]


def is_market_status_command(text):
    clean = text.strip().lower()
    return clean in [
        "وضعیت بازار",
        "وضعیت ارزها",
        "محاسبه وضعیت بازار",
        "بررسی",
    ]


def is_symbol_stats_command(text):
    clean = _normalize_trade_text(text)
    commands = [
        "آمار کلی ارزها", "امار کلی ارزها",
        "آمار ارزها", "امار ارزها",
        "بهترین ارزها",
        "بدترین ارزها", "ضعیف ترین ارزها", "ضعیف‌ترین ارزها",
    ]
    if clean in commands:
        return True
    for prefix in commands:
        if clean.startswith(prefix + " "):
            return True
    return False


def get_symbol_stats_mode(text):
    clean = _normalize_trade_text(text)
    if clean.startswith("بهترین ارزها"):
        return "best"
    if clean.startswith("بدترین ارزها") or clean.startswith("ضعیف ترین ارزها") or clean.startswith("ضعیف‌ترین ارزها"):
        return "worst"
    return "all"



def _normalize_trade_text(text):
    return " ".join((text or "").strip().lower().replace("ي", "ی").replace("ك", "ک").split())


def _reply_trade_unavailable(message):
    bot.reply_to(message, format_trade_status())


def handle_trade_waiting_input(message, text):
    """مرحله دوم دستورهای ترید مثل ترید دلار / ترید لوریج."""
    user_id = int(message.from_user.id)

    if user_id not in TRADE_WAITING_ACTION:
        return False

    action = TRADE_WAITING_ACTION.pop(user_id)

    if not is_owner(user_id):
        bot.reply_to(message, "⛔ فقط مالک ربات می‌تواند تنظیمات ترید را تغییر دهد.")
        return True

    try:
        value = _normalize_trade_text(text)

        if action == "trade_margin":
            ok, reply = set_trade_margin(value)
            bot.reply_to(message, reply)
            return True

        if action == "leverage":
            ok, reply = set_leverage(value)
            bot.reply_to(message, reply)
            return True

        if action == "max_positions":
            ok, reply = set_max_open_positions(value)
            bot.reply_to(message, reply)
            return True

        bot.reply_to(message, "❌ دستور تنظیم نامشخص بود.")
        return True

    except Exception as e:
        bot.reply_to(message, f"❌ خطا در ثبت تنظیم:\n{e}")
        return True


def handle_trade_command(message, text):
    """دستورات Paper Trade مثل ربات فیوچرز اصلی، قبل از تشخیص ارز اجرا می‌شود."""
    clean = _normalize_trade_text(text)
    user_id = int(message.from_user.id)

    # وضعیت/داشبورد عمومی
    if clean in ["ترید", "وضعیت ترید", "داشبورد", "داشبورد ترید"]:
        bot.reply_to(message, format_trade_status())
        return True

    if clean in ["آمار ترید", "امار ترید", "آمار معاملات", "امار معاملات"]:
        bot.reply_to(message, format_trade_stats())
        return True

    if clean in [
        "پوزیشن ها", "پوزیشن‌ها", "پوزیشنهای باز", "پوزیشن های باز",
        "پوزیشن فعال", "پوزیشن‌های فعال", "پوزیشنهای فعال",
        "معاملات فعال", "معامله فعال", "تریدهای فعال", "ترید های فعال"
    ]:
        bot.reply_to(message, format_open_positions())
        return True

    if clean in ["تنظیمات ترید", "تنظیم ترید", "ستینگ ترید"]:
        bot.reply_to(message, format_trade_settings())
        return True

    if clean in ["اسلات خالی", "اسلات ترید", "ظرفیت ترید"]:
        bot.reply_to(message, format_empty_slots())
        return True

    # دستورهای مالک
    owner_commands = [
        "ترید فعال", "فعال کردن ترید", "ترید روشن", "روشن کردن ترید",
        "ترید غیرفعال", "ترید غیر فعال", "غیرفعال کردن ترید", "ترید خاموش", "خاموش کردن ترید",
        "توقف اضطراری", "استاپ ترید", "توقف ترید",
        "ترید دلار", "دلار ترید", "حجم ترید", "مارجین ترید",
        "ترید لوریج", "لوریج ترید", "اهرم ترید",
        "حداکثر پوزیشن", "حد اکثر پوزیشن", "حداکثر معاملات",
        "ریست ترید", "حذف آمار ترید", "حذف امار ترید", "ریست آمار ترید", "ریست امار ترید",
    ]

    if clean in owner_commands and not is_owner(user_id):
        bot.reply_to(message, "⛔ فقط مالک ربات می‌تواند دستورهای ترید را اجرا کند.")
        return True

    if clean in ["ترید فعال", "فعال کردن ترید", "ترید روشن", "روشن کردن ترید"]:
        bot.reply_to(message, set_trade_enabled(True))
        return True

    if clean in ["ترید غیرفعال", "ترید غیر فعال", "غیرفعال کردن ترید", "ترید خاموش", "خاموش کردن ترید"]:
        bot.reply_to(message, set_trade_enabled(False))
        return True

    if clean in ["توقف اضطراری", "استاپ ترید", "توقف ترید"]:
        bot.reply_to(message, emergency_stop())
        return True

    if clean in ["ریست ترید", "حذف آمار ترید", "حذف امار ترید", "ریست آمار ترید", "ریست امار ترید"]:
        bot.reply_to(message, reset_trade_stats())
        return True

    if clean.startswith("سرمایه ترید "):
        if not is_owner(user_id):
            bot.reply_to(message, "⛔ فقط مالک ربات می‌تواند سرمایه ترید را تغییر دهد.")
            return True
        try:
            value = clean.split()[-1]
            ok, reply = set_trade_balance(value)
            bot.reply_to(message, reply)
        except Exception:
            bot.reply_to(message, "فرمت درست: سرمایه ترید 50")
        return True

    if clean in ["ترید دلار", "دلار ترید", "حجم ترید", "مارجین ترید"]:
        TRADE_WAITING_ACTION[user_id] = "trade_margin"
        bot.reply_to(message, "مقدار دلاری هر پوزیشن را وارد کن:\nمثلاً: 5")
        return True

    if clean in ["ترید لوریج", "لوریج ترید", "اهرم ترید"]:
        TRADE_WAITING_ACTION[user_id] = "leverage"
        bot.reply_to(message, "لوریج معاملات بعدی را وارد کن:\nمثلاً: 10")
        return True

    if clean in ["حداکثر پوزیشن", "حد اکثر پوزیشن", "حداکثر معاملات"]:
        TRADE_WAITING_ACTION[user_id] = "max_positions"
        bot.reply_to(message, "حداکثر تعداد پوزیشن همزمان را وارد کن:\nمثلاً: 5")
        return True

    # فرمت‌های یک‌خطی هم پشتیبانی شوند: مارجین 5 / لوریج 10 / حداکثر پوزیشن 5
    if clean.startswith("مارجین ") or clean.startswith("دلار "):
        if not is_owner(user_id):
            bot.reply_to(message, "⛔ فقط مالک ربات می‌تواند مارجین را تغییر دهد.")
            return True
        try:
            value = clean.split()[-1]
            ok, reply = set_trade_margin(value)
            bot.reply_to(message, reply)
        except Exception:
            bot.reply_to(message, "فرمت درست: مارجین 5")
        return True

    if clean.startswith("لوریج ") or clean.startswith("اهرم "):
        if not is_owner(user_id):
            bot.reply_to(message, "⛔ فقط مالک ربات می‌تواند لوریج را تغییر دهد.")
            return True
        try:
            value = clean.split()[-1]
            ok, reply = set_leverage(value)
            bot.reply_to(message, reply)
        except Exception:
            bot.reply_to(message, "فرمت درست: لوریج 10")
        return True

    if clean.startswith("حداکثر پوزیشن ") or clean.startswith("حد اکثر پوزیشن "):
        if not is_owner(user_id):
            bot.reply_to(message, "⛔ فقط مالک ربات می‌تواند حداکثر پوزیشن را تغییر دهد.")
            return True
        try:
            value = clean.split()[-1]
            ok, reply = set_max_open_positions(value)
            bot.reply_to(message, reply)
        except Exception:
            bot.reply_to(message, "فرمت درست: حداکثر پوزیشن 5")
        return True

    return False

def find_symbol(text):
    text = text.lower().strip()

    for name, symbol in COINS_FA.items():
        if name.lower() in text:
            return symbol

    text = text.replace("\u062a\u062d\u0644\u06cc\u0644", "").replace("\u0633\u06cc\u06af\u0646\u0627\u0644", "").strip().upper()

    if text.endswith("USDT"):
        return text

    return None


def fa_direction(direction):
    return {
        "LONG": "\U0001f7e2 \u0644\u0627\u0646\u06af",
        "SHORT": "\U0001f534 \u0634\u0648\u0631\u062a",
        "NO TRADE": "\u26aa \u0641\u0639\u0644\u0627\u064b \u0648\u0631\u0648\u062f \u0645\u0646\u0627\u0633\u0628 \u0646\u06cc\u0633\u062a"
    }.get(direction, direction)


def fa_general(value):
    data = {
        "bullish": "\u0635\u0639\u0648\u062f\u06cc",
        "bearish": "\u0646\u0632\u0648\u0644\u06cc",
        "neutral": "\u062e\u0646\u062b\u06cc",
        "range": "\u0631\u0646\u062c",
        "weak": "\u0636\u0639\u06cc\u0641",
        "none": "\u0646\u062f\u0627\u0631\u062f",
        "unknown": "\u0646\u0627\u0645\u0634\u062e\u0635",
        "ok": "\u062a\u0623\u06cc\u06cc\u062f \u0634\u062f\u0647",

        "uptrend": "\u0635\u0639\u0648\u062f\u06cc",
        "downtrend": "\u0646\u0632\u0648\u0644\u06cc",
        "sideways": "\u062e\u0646\u062b\u06cc",

        "bullish_structure": "\u0633\u0627\u062e\u062a\u0627\u0631 \u0635\u0639\u0648\u062f\u06cc",
        "bearish_structure": "\u0633\u0627\u062e\u062a\u0627\u0631 \u0646\u0632\u0648\u0644\u06cc",
        "range_structure": "\u0631\u0646\u062c / \u0628\u062f\u0648\u0646 \u0631\u0648\u0646\u062f \u0648\u0627\u0636\u062d",

        "bullish_breakout": "\u0628\u0631\u06cc\u06a9\u200c\u0627\u0648\u062a \u0635\u0639\u0648\u062f\u06cc",
        "bearish_breakout": "\u0628\u0631\u06cc\u06a9\u200c\u0627\u0648\u062a \u0646\u0632\u0648\u0644\u06cc",
        "fake_bullish_breakout": "\u0641\u06cc\u06a9 \u0628\u0631\u06cc\u06a9\u200c\u0627\u0648\u062a \u0635\u0639\u0648\u062f\u06cc",
        "fake_bearish_breakout": "\u0641\u06cc\u06a9 \u0628\u0631\u06cc\u06a9\u200c\u0627\u0648\u062a \u0646\u0632\u0648\u0644\u06cc",
        "no_breakout": "\u0628\u062f\u0648\u0646 \u0628\u0631\u06cc\u06a9\u200c\u0627\u0648\u062a",

        "bullish_engulfing": "\u0627\u0646\u06af\u0627\u0644\u0641 \u0635\u0639\u0648\u062f\u06cc",
        "bearish_engulfing": "\u0627\u0646\u06af\u0627\u0644\u0641 \u0646\u0632\u0648\u0644\u06cc",
        "bullish_pinbar": "\u067e\u06cc\u0646\u200c\u0628\u0627\u0631 \u0635\u0639\u0648\u062f\u06cc",
        "bearish_pinbar": "\u067e\u06cc\u0646\u200c\u0628\u0627\u0631 \u0646\u0632\u0648\u0644\u06cc",
        "bullish_strong": "\u06a9\u0646\u062f\u0644 \u0635\u0639\u0648\u062f\u06cc \u0642\u0648\u06cc",
        "bearish_strong": "\u06a9\u0646\u062f\u0644 \u0646\u0632\u0648\u0644\u06cc \u0642\u0648\u06cc",

        "bullish_liquidity_grab": "\u062c\u0645\u0639\u200c\u0622\u0648\u0631\u06cc \u0646\u0642\u062f\u06cc\u0646\u06af\u06cc \u0635\u0639\u0648\u062f\u06cc",
        "bearish_liquidity_grab": "\u062c\u0645\u0639\u200c\u0622\u0648\u0631\u06cc \u0646\u0642\u062f\u06cc\u0646\u06af\u06cc \u0646\u0632\u0648\u0644\u06cc",
        "bullish_stop_hunt": "\u0627\u0633\u062a\u0627\u067e\u200c\u0647\u0627\u0646\u062a \u0635\u0639\u0648\u062f\u06cc",
        "bearish_stop_hunt": "\u0627\u0633\u062a\u0627\u067e\u200c\u0647\u0627\u0646\u062a \u0646\u0632\u0648\u0644\u06cc",

        "bullish_fvg": "\u0646\u0627\u062d\u06cc\u0647 \u062e\u0627\u0644\u06cc \u0646\u0642\u062f\u06cc\u0646\u06af\u06cc \u0635\u0639\u0648\u062f\u06cc",
        "bearish_fvg": "\u0646\u0627\u062d\u06cc\u0647 \u062e\u0627\u0644\u06cc \u0646\u0642\u062f\u06cc\u0646\u06af\u06cc \u0646\u0632\u0648\u0644\u06cc",

        "bullish_order_block": "\u0627\u0648\u0631\u062f\u0631 \u0628\u0644\u0627\u06a9 \u0635\u0639\u0648\u062f\u06cc",
        "bearish_order_block": "\u0627\u0648\u0631\u062f\u0631 \u0628\u0644\u0627\u06a9 \u0646\u0632\u0648\u0644\u06cc",

        "bullish_rsi_divergence": "\u0648\u0627\u06af\u0631\u0627\u06cc\u06cc \u0645\u062b\u0628\u062a RSI",
        "bearish_rsi_divergence": "\u0648\u0627\u06af\u0631\u0627\u06cc\u06cc \u0645\u0646\u0641\u06cc RSI",
        "bullish_macd_divergence": "\u0648\u0627\u06af\u0631\u0627\u06cc\u06cc \u0645\u062b\u0628\u062a MACD",
        "bearish_macd_divergence": "\u0648\u0627\u06af\u0631\u0627\u06cc\u06cc \u0645\u0646\u0641\u06cc MACD",

        "bullish_exhaustion": "\u062e\u0633\u062a\u06af\u06cc \u0631\u0648\u0646\u062f \u0635\u0639\u0648\u062f\u06cc",
        "bearish_exhaustion": "\u062e\u0633\u062a\u06af\u06cc \u0631\u0648\u0646\u062f \u0646\u0632\u0648\u0644\u06cc",

        "above_vwap": "\u0628\u0627\u0644\u0627\u06cc \u0645\u06cc\u0627\u0646\u06af\u06cc\u0646 \u062d\u062c\u0645\u06cc \u0642\u06cc\u0645\u062a",
        "below_vwap": "\u067e\u0627\u06cc\u06cc\u0646 \u0645\u06cc\u0627\u0646\u06af\u06cc\u0646 \u062d\u062c\u0645\u06cc \u0642\u06cc\u0645\u062a",
        "near_vwap": "\u0646\u0632\u062f\u06cc\u06a9 \u0645\u06cc\u0627\u0646\u06af\u06cc\u0646 \u062d\u062c\u0645\u06cc \u0642\u06cc\u0645\u062a",

        "above_poc": "\u0628\u0627\u0644\u0627\u06cc \u0646\u0627\u062d\u06cc\u0647 \u062d\u062c\u0645\u06cc \u0627\u0635\u0644\u06cc",
        "below_poc": "\u067e\u0627\u06cc\u06cc\u0646 \u0646\u0627\u062d\u06cc\u0647 \u062d\u062c\u0645\u06cc \u0627\u0635\u0644\u06cc",
        "near_poc": "\u0646\u0632\u062f\u06cc\u06a9 \u0646\u0627\u062d\u06cc\u0647 \u062d\u062c\u0645\u06cc \u0627\u0635\u0644\u06cc",
    }
    return data.get(value, value)


def format_optional_line(label, value, suffix=""):
    if value is None or value == "" or value == "نامشخص":
        return ""
    return f"{label}: {value}{suffix}\\n"


def format_context_lines(result):
    lines = ""
    lines += format_optional_line("روند کلی بازار", fa_general(result.get("market_regime")))
    lines += format_optional_line("وضعیت آلت‌سیزن", fa_general(result.get("altseason_status")))
    lines += format_optional_line("Fear & Greed", result.get("fear_greed_value"))
    lines += format_optional_line("حجم/Vol", result.get("volume_status"))
    lines += format_optional_line("Order Block", result.get("order_block"))
    return lines.strip() or "نامشخص"


def build_trade_levels(result):
    if result.get("stop_loss") is None:
        return f"""
\u0628\u0631\u0627\u06cc \u0627\u06cc\u0646 \u0648\u0636\u0639\u06cc\u062a\u060c \u0648\u0631\u0648\u062f \u067e\u06cc\u0634\u0646\u0647\u0627\u062f \u0646\u0645\u06cc\u200c\u0634\u0648\u062f.

\u0633\u0637\u0648\u062d \u0627\u062d\u062a\u0645\u0627\u0644\u06cc \u0641\u0642\u0637 \u0628\u0631\u0627\u06cc \u0628\u0631\u0631\u0633\u06cc:
\u062d\u062f \u0636\u0631\u0631 \u0627\u062d\u062a\u0645\u0627\u0644\u06cc:
{safe(result.get('candidate_stop_loss'))}

\u062d\u062f \u0633\u0648\u062f 1 \u0627\u062d\u062a\u0645\u0627\u0644\u06cc:
{safe(result.get('candidate_tp1'))}

\u062d\u062f \u0633\u0648\u062f 2 \u0627\u062d\u062a\u0645\u0627\u0644\u06cc:
{safe(result.get('candidate_tp2'))}
"""

    return f"""
\u0648\u0631\u0648\u062f \u062a\u0642\u0631\u06cc\u0628\u06cc:
{result['price']}

\u062d\u062f \u0636\u0631\u0631:
{result['stop_loss']}

\u062d\u062f \u0633\u0648\u062f 1:
{result['tp1']}

\u062d\u062f \u0633\u0648\u062f 2:
{result['tp2']}
"""


def build_analysis_text(result):
    reasons = result.get("reasons", [])[:8]
    reasons_text = "\n".join([f"✅ {r}" for r in reasons]) if reasons else "ندارد"
    trend_map = result.get("trends") or {}
    trends_text = " | ".join([f"{tf}: {fa_general(v)}" for tf, v in trend_map.items()]) if isinstance(trend_map, dict) else "نامشخص"

    return f"""
📊 تحلیل تکنیکال کلاسیک {result.get('symbol')}

وضعیت ورود: {'✅ فعال' if result.get('entry_confirmed') else '⛔ بدون ورود'}
قیمت فعلی: {safe(result.get('price'))}

جهت نهایی: {fa_direction(result.get('direction'))}
امتیاز: {safe(result.get('score'))}
تاییدیه‌ها: {safe(result.get('confirmations'))}
ریسک: {safe(result.get('risk_level'))}
ریسک به ریوارد: {safe(result.get('risk_reward'))}

📌 شاخص‌های اصلی:
RSI 15M: {safe(result.get('rsi'))}
MACD: {safe(result.get('macd'))}
MACD Signal: {safe(result.get('macd_signal'))}
MACD Histogram: {safe(result.get('macd_hist'))}
ADX 15M: {safe(result.get('adx'))}
VWAP: {fa_general(result.get('vwap_status'))}

📈 روند تایم‌فریم‌ها:
{trends_text}

🧠 شرایط کمکی:
{format_context_lines(result)}

🛡 TP/SL هوشمند:
حمایت: {safe(result.get('support'))}
مقاومت: {safe(result.get('resistance'))}
تایم‌فریم سطوح: {safe(result.get('sr_timeframe'), 'نامشخص')}

🎯 سطوح معامله:
{build_trade_levels(result)}

⏱ تایم‌فریم: {safe(result.get('signal_timeframe'))}
⏰ اعتبار: {safe(result.get('validity'))}

دلایل اصلی:
{reasons_text}

⚠️ مدیریت ریسک فراموش نشود.
"""


def send_analysis(message, symbol):
    bot.reply_to(message, f"\u23f3 \u062f\u0631 \u062d\u0627\u0644 \u062a\u062d\u0644\u06cc\u0644 {symbol} ...")

    try:
        result = analyze_symbol(symbol)
    except Exception as e:
        print("ANALYSIS ERROR:", str(e))
        bot.reply_to(message, f"\u274c \u062e\u0637\u0627 \u062f\u0631 \u062a\u062d\u0644\u06cc\u0644 {symbol}\n\n\u0639\u0644\u062a \u062e\u0637\u0627:\n{e}")
        return

    sent = bot.reply_to(message, build_analysis_text(result))
    remember_signal_result(sent, result)


def send_best_signals(message, very_safe_only=False):
    if very_safe_only:
        bot.reply_to(message, "\u23f3 \u062f\u0631 \u062d\u0627\u0644 \u0627\u0633\u06a9\u0646 \u0628\u0627\u0632\u0627\u0631 \u0628\u0631\u0627\u06cc \u0633\u06cc\u06af\u0646\u0627\u0644\u200c\u0647\u0627\u06cc \u062e\u06cc\u0644\u06cc \u0627\u0645\u0646...")
    else:
        bot.reply_to(message, "\u23f3 \u062f\u0631 \u062d\u0627\u0644 \u0627\u0633\u06a9\u0646 \u0628\u0627\u0632\u0627\u0631...")

    try:
        results = get_best_signals(limit=5, very_safe_only=very_safe_only)
    except Exception as e:
        print("BEST SIGNAL ERROR:", str(e))
        bot.reply_to(message, f"\u274c \u062e\u0637\u0627 \u062f\u0631 \u0627\u0633\u06a9\u0646 \u0628\u0627\u0632\u0627\u0631:\n{e}")
        return

    if not results:
        if very_safe_only:
            bot.reply_to(message, "\u0641\u0639\u0644\u0627\u064b \u0633\u06cc\u06af\u0646\u0627\u0644 \u062e\u06cc\u0644\u06cc \u0627\u0645\u0646 \u0645\u0646\u0627\u0633\u0628\u06cc \u067e\u06cc\u062f\u0627 \u0646\u0634\u062f.")
        else:
            bot.reply_to(message, "\u0641\u0639\u0644\u0627\u064b \u0633\u06cc\u06af\u0646\u0627\u0644 \u0645\u0646\u0627\u0633\u0628\u06cc \u067e\u06cc\u062f\u0627 \u0646\u0634\u062f.")
        return

    msg = "\U0001f3c6 \u0628\u0647\u062a\u0631\u06cc\u0646 \u0633\u06cc\u06af\u0646\u0627\u0644\u200c\u0647\u0627\u06cc \u062e\u06cc\u0644\u06cc \u0627\u0645\u0646:\n\n" if very_safe_only else "\U0001f3c6 \u0628\u0647\u062a\u0631\u06cc\u0646 \u0633\u06cc\u06af\u0646\u0627\u0644\u200c\u0647\u0627\u06cc \u0627\u0644\u0627\u0646:\n\n"
    medals = ["\U0001f947", "\U0001f948", "\U0001f949", "4\ufe0f\u20e3", "5\ufe0f\u20e3"]

    for i, r in enumerate(results):
        direction_fa = "\u0644\u0627\u0646\u06af" if r["direction"] == "LONG" else "\u0634\u0648\u0631\u062a"

        msg += f"""
{medals[i]} {r['symbol']}
جهت: {direction_fa}
حالت ورود: {safe(r.get('entry_mode'))}
تازگی حرکت: {safe(r.get('freshness'))}
تاییدیه‌های ورود: {safe(r.get('predictive_confirmations'))}
ریسک: {safe(r.get('risk_level'))}
ریسک به ریوارد: {safe(r.get('risk_reward'))}
اعتبار: {r['validity']}
تایم‌فریم: {r['signal_timeframe']}
قیمت: {r['price']}
قدرت 2کندلی: خرید {safe(r.get('power2_buy'))}٪ / فروش {safe(r.get('power2_sell'))}٪
قدرت 3کندلی: خرید {safe(r.get('power3_buy'))}٪ / فروش {safe(r.get('power3_sell'))}٪
ADX: {safe(r.get('adx'))}
اسپرد: {safe(r.get('spread_percent'))}٪
نرخ فاندینگ: {safe(r.get('funding_rate'))}٪
حالت خیلی امن: {"بله ✅" if r.get("very_safe") else "خیر"}
"""

    bot.reply_to(message, msg)


def send_auto_signal_to_all_users(result):
    direction_fa = "لانگ" if result.get("direction") == "LONG" else "شورت"
    text = f"""
🚨 سیگنال خودکار تکنیکال کلاسیک

وضعیت: ✅ ورود فعال
ارز: {result.get('symbol')}
جهت: {direction_fa}
امتیاز: {result.get('score')}
تاییدیه‌ها: {result.get('confirmations')}
ریسک: {result.get('risk_level')}
ریسک به ریوارد: {result.get('risk_reward')}

قیمت/ورود: {result.get('entry') or result.get('price')}
حد ضرر هوشمند: {result.get('stop_loss')}
حد سود 1 هوشمند: {result.get('tp1')}
حد سود 2 هوشمند: {result.get('tp2')}

RSI 15M: {result.get('rsi')}
MACD Hist: {result.get('macd_hist')}
ADX 15M: {result.get('adx')}
VWAP: {fa_general(result.get('vwap_status'))}

حمایت: {result.get('support')}
مقاومت: {result.get('resistance')}

⚠️ مدیریت ریسک فراموش نشود.
"""
    for user_id in list_users():
        try:
            can_add, reason = can_add_automatic_signal(user_id, result.get("symbol"))
            if not can_add:
                print("AUTO SIGNAL SKIPPED:", result.get("symbol"), reason)
                continue
            sent = bot.send_message(user_id, text)
            remember_signal_result(sent, result)
            if AUTO_TRACK_AUTO_SIGNALS:
                ok, msg = add_signal_to_tracking(user_id=user_id, chat_id=user_id, message_id=sent.message_id, result=result)
                if not ok:
                    print("AUTO TRACK SKIPPED:", result.get("symbol"), msg)
        except Exception as e:
            log_exception("ارسال سیگنال خودکار", e, "bot.py", "send_auto_signal_to_all_users", result.get("symbol"))


def auto_signal_loop():
    time.sleep(60)

    while True:
        for symbol in SCAN_SYMBOLS:
            try:
                result = analyze_symbol(symbol)

                if should_send_auto_signal(result):
                    send_auto_signal_to_all_users(result)

            except Exception as e:
                msg = str(e)
                quiet_errors = [
                    "does not have market symbol",
                    "Too Many Requests",
                    "429 Client Error",
                    "BTC dominance not found",
                    "Unauthorized for url",
                    "داده کافی",
                ]
                if not any(x in msg for x in quiet_errors):
                    print("AUTO SIGNAL ERROR:", symbol, msg)
                continue

        time.sleep(AUTO_SCAN_INTERVAL_MINUTES * 60)


def signal_tracking_loop():
    time.sleep(30)

    while True:
        try:
            messages = check_active_signals()

            for item in messages:
                try:
                    bot.send_message(
                        item["chat_id"],
                        item["message"],
                        reply_to_message_id=item.get("reply_to_message_id")
                    )
                except Exception as e:
                    print("SEND TRACK RESULT REPLY ERROR:", str(e))
                    try:
                        bot.send_message(item["chat_id"], item["message"])
                    except Exception as e2:
                        print("SEND TRACK RESULT FALLBACK ERROR:", str(e2))

        except Exception as e:
            print("SIGNAL TRACKING LOOP ERROR:", str(e))

        time.sleep(TRACKER_CHECK_INTERVAL_SECONDS)


@bot.message_handler(commands=["start"])
def start(message):
    if not is_user_allowed(message.from_user.id):
        bot.reply_to(message, "\u26d4 \u0634\u0645\u0627 \u0645\u062c\u0627\u0632 \u0628\u0647 \u0627\u0633\u062a\u0641\u0627\u062f\u0647 \u0627\u0632 \u0627\u06cc\u0646 \u0631\u0628\u0627\u062a \u0646\u06cc\u0633\u062a\u06cc\u062f.")
        return

    bot.reply_to(message, """
\u0633\u0644\u0627\u0645 \U0001f44b

\u0631\u0628\u0627\u062a \u062f\u0633\u062a\u06cc\u0627\u0631 \u0641\u06cc\u0648\u0686\u0631\u0632 \u06a9\u0631\u06cc\u067e\u062a\u0648 \u0641\u0639\u0627\u0644 \u0627\u0633\u062a.

\u0645\u062b\u0627\u0644:
\u0628\u06cc\u062a\u06a9\u0648\u06cc\u0646
\u0627\u062a\u0631\u06cc\u0648\u0645
\u062a\u062d\u0644\u06cc\u0644 \u062f\u0648\u062c
\u0633\u06cc\u06af\u0646\u0627\u0644 \u0633\u0648\u0644\u0627\u0646\u0627
\u0628\u0647\u062a\u0631\u06cc\u0646 \u0633\u06cc\u06af\u0646\u0627\u0644 \u0627\u0644\u0627\u0646
\u0633\u06cc\u06af\u0646\u0627\u0644 \u062e\u06cc\u0644\u06cc \u0627\u0645\u0646

\u0632\u06cc\u0631 \u0646\u0638\u0631 \u06af\u0631\u0641\u062a\u0646 \u0633\u06cc\u06af\u0646\u0627\u0644:
\u0631\u0648\u06cc \u067e\u06cc\u0627\u0645 \u062a\u062d\u0644\u06cc\u0644 \u06cc\u0627 \u0633\u06cc\u06af\u0646\u0627\u0644 \u062e\u0648\u062f\u06a9\u0627\u0631 \u0631\u06cc\u067e\u0644\u0627\u06cc \u06a9\u0646 \u0648 \u0628\u0646\u0648\u06cc\u0633:
\u0632\u06cc\u0631 \u0646\u0638\u0631

\u0622\u0645\u0627\u0631:
\u0622\u0645\u0627\u0631
\u0622\u0645\u0627\u0631 3 \u0631\u0648\u0632
\u0622\u0645\u0627\u0631 7 \u0631\u0648\u0632
\u0622\u0645\u0627\u0631 14 \u0631\u0648\u0632
\u0622\u0645\u0627\u0631 30 \u0631\u0648\u0632
\u0622\u0645\u0627\u0631 \u06a9\u0644

\u062f\u0633\u062a\u0648\u0631\u0627\u062a \u0627\u062f\u0645\u06cc\u0646:
/adduser 123456789
/removeuser 123456789
/listusers
""")


@bot.message_handler(commands=["adduser"])
def add_user_command(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "\u26d4 \u0641\u0642\u0637 \u0645\u0627\u0644\u06a9 \u0631\u0628\u0627\u062a \u0645\u06cc\u200c\u062a\u0648\u0627\u0646\u062f \u06a9\u0627\u0631\u0628\u0631 \u0627\u0636\u0627\u0641\u0647 \u06a9\u0646\u062f.")
        return

    try:
        user_id = int(message.text.split()[1])
        add_user(user_id)
        bot.reply_to(message, f"\u2705 \u06a9\u0627\u0631\u0628\u0631 {user_id} \u0627\u0636\u0627\u0641\u0647 \u0634\u062f.")
    except Exception:
        bot.reply_to(message, "\u0641\u0631\u0645\u062a \u062f\u0631\u0633\u062a:\n/adduser 123456789")


@bot.message_handler(commands=["removeuser"])
def remove_user_command(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "\u26d4 \u0641\u0642\u0637 \u0645\u0627\u0644\u06a9 \u0631\u0628\u0627\u062a \u0645\u06cc\u200c\u062a\u0648\u0627\u0646\u062f \u06a9\u0627\u0631\u0628\u0631 \u062d\u0630\u0641 \u06a9\u0646\u062f.")
        return

    try:
        user_id = int(message.text.split()[1])
        ok = remove_user(user_id)

        if ok:
            bot.reply_to(message, f"\u2705 \u06a9\u0627\u0631\u0628\u0631 {user_id} \u062d\u0630\u0641 \u0634\u062f.")
        else:
            bot.reply_to(message, "\u274c \u0645\u0627\u0644\u06a9 \u0627\u0635\u0644\u06cc \u0642\u0627\u0628\u0644 \u062d\u0630\u0641 \u0646\u06cc\u0633\u062a \u06cc\u0627 \u06a9\u0627\u0631\u0628\u0631 \u0648\u062c\u0648\u062f \u0646\u062f\u0627\u0631\u062f.")
    except Exception:
        bot.reply_to(message, "\u0641\u0631\u0645\u062a \u062f\u0631\u0633\u062a:\n/removeuser 123456789")


@bot.message_handler(commands=["listusers"])
def list_users_command(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "\u26d4 \u0641\u0642\u0637 \u0645\u0627\u0644\u06a9 \u0631\u0628\u0627\u062a \u0645\u06cc\u200c\u062a\u0648\u0627\u0646\u062f \u0644\u06cc\u0633\u062a \u06a9\u0627\u0631\u0628\u0631\u0627\u0646 \u0631\u0627 \u0628\u0628\u06cc\u0646\u062f.")
        return

    users = list_users()
    users_text = "\n".join([str(u) for u in users])
    bot.reply_to(message, f"\U0001f465 \u06a9\u0627\u0631\u0628\u0631\u0627\u0646 \u0645\u062c\u0627\u0632:\n{users_text}")


@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if not is_user_allowed(message.from_user.id):
        bot.reply_to(message, "\u26d4 \u0634\u0645\u0627 \u0645\u062c\u0627\u0632 \u0628\u0647 \u0627\u0633\u062a\u0641\u0627\u062f\u0647 \u0627\u0632 \u0627\u06cc\u0646 \u0631\u0628\u0627\u062a \u0646\u06cc\u0633\u062a\u06cc\u062f.")
        return

    if not message.text:
        return

    text = message.text.strip()

    # دستورات ترید باید قبل از تشخیص ارز اجرا شوند تا به تحلیل ارز نروند.
    if handle_trade_waiting_input(message, text):
        return

    if handle_trade_command(message, text):
        return

    if is_market_status_command(text):
        bot.reply_to(message, "⏳ در حال محاسبه وضعیت بازار...")
        try:
            report = get_market_status_text()
            bot.reply_to(message, report)
        except Exception as e:
            print("MARKET STATUS ERROR:", str(e))
            bot.reply_to(message, f"❌ خطا در محاسبه وضعیت بازار:\n{e}")
        return
    if is_reset_stats_command(text):
        if not is_owner(message.from_user.id):
            bot.reply_to(message, "⛔ فقط مالک ربات می‌تواند آمار را پاک کند.")
            return
        if reset_stats():
            bot.reply_to(message, "✅ آمار سیگنال‌ها صفر شد.\nسیگنال‌های فعال زیرنظر حذف نشدند.")
        else:
            bot.reply_to(message, "❌ خطا در پاک کردن آمار.")
        return


    profit_calc = parse_profit_calc_text(text)
    if profit_calc:
        margin, leverage = profit_calc

        reply_text = None
        if message.reply_to_message and message.reply_to_message.text:
            reply_text = message.reply_to_message.text

        single_report = get_profit_for_signal_text(reply_text, margin, leverage)

        if single_report:
            bot.reply_to(message, single_report)
            return

        days = parse_days_from_report_text(reply_text) if reply_text else 7
        report = get_profit_simulation_report(margin, leverage, days)
        bot.reply_to(message, report)
        return

    if is_track_command(text):
        result = get_replied_signal_result(message)

        if not result:
            bot.reply_to(
                message,
                "\u274c \u0628\u0631\u0627\u06cc \u0632\u06cc\u0631 \u0646\u0638\u0631 \u06af\u0631\u0641\u062a\u0646\u060c \u0628\u0627\u06cc\u062f \u0631\u0648\u06cc \u067e\u06cc\u0627\u0645 \u062a\u062d\u0644\u06cc\u0644 \u06cc\u0627 \u0633\u06cc\u06af\u0646\u0627\u0644 \u062e\u0648\u062f\u06a9\u0627\u0631 \u0631\u06cc\u067e\u0644\u0627\u06cc \u0628\u0632\u0646\u06cc.\n"
                "\u0627\u06af\u0631 \u0631\u0628\u0627\u062a \u0631\u06cc\u200c\u0627\u0633\u062a\u0627\u0631\u062a \u0634\u062f\u0647 \u0628\u0627\u0634\u062f\u060c \u062f\u0648\u0628\u0627\u0631\u0647 \u0647\u0645\u0627\u0646 \u0627\u0631\u0632 \u0631\u0627 \u062a\u062d\u0644\u06cc\u0644 \u0628\u06af\u06cc\u0631 \u0648 \u0628\u0639\u062f \u0631\u06cc\u067e\u0644\u0627\u06cc \u06a9\u0646."
            )
            return

        ok, msg = add_signal_to_tracking(
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            message_id=message.reply_to_message.message_id,
            result=result
        )

        bot.reply_to(message, msg)
        return

    if is_symbol_stats_command(text):
        days = parse_days_from_text(text)
        mode = get_symbol_stats_mode(text)
        report = get_symbol_stats_report(days, mode=mode)
        bot.reply_to(message, report)
        return

    if is_stats_command(text):
        days = parse_days_from_text(text)
        report = get_stats_report(days)
        bot.reply_to(message, report)
        return

    if "\u062e\u06cc\u0644\u06cc \u0627\u0645\u0646" in text or "very safe" in text.lower():
        send_best_signals(message, very_safe_only=True)
        return

    if "\u0628\u0647\u062a\u0631\u06cc\u0646 \u0633\u06cc\u06af\u0646\u0627\u0644" in text or "\u0628\u0647\u062a\u0631\u06cc\u0646 \u0641\u0631\u0635\u062a" in text:
        send_best_signals(message)
        return

    symbol = find_symbol(text)

    if not symbol:
        bot.reply_to(message, "\u0627\u0631\u0632 \u0631\u0648 \u0645\u062a\u0648\u062c\u0647 \u0646\u0634\u062f\u0645. \u0645\u062b\u0644\u0627 \u0628\u0646\u0648\u06cc\u0633: \u0628\u06cc\u062a\u06a9\u0648\u06cc\u0646 \u06cc\u0627 \u0627\u062a\u0631\u06cc\u0648\u0645")
        return

    send_analysis(message, symbol)


if os.getenv("AUTO_SIGNAL_ENABLED", "1") == "1":
    threading.Thread(target=auto_signal_loop, daemon=True).start()

threading.Thread(target=signal_tracking_loop, daemon=True).start()

print("Bot is running...")
bot.infinity_polling(timeout=60, long_polling_timeout=50)
