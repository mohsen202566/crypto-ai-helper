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
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN تنظیم نشده است. اول روی VPS دستور export BOT_TOKEN را بزن.")

bot = telebot.TeleBot(BOT_TOKEN)

# \u062d\u0627\u0641\u0638\u0647 \u0645\u0648\u0642\u062a \u0628\u0631\u0627\u06cc \u0627\u062a\u0635\u0627\u0644 \u067e\u06cc\u0627\u0645 \u0633\u06cc\u06af\u0646\u0627\u0644 \u0628\u0647 \u062f\u0633\u062a\u0648\u0631 \xab\u0632\u06cc\u0631 \u0646\u0638\u0631\xbb
MESSAGE_RESULTS = {}

TRACK_COMMANDS = ["\u0632\u06cc\u0631 \u0646\u0638\u0631", "\u0632\u06cc\u0631\u0646\u0638\u0631", "\u0632\u06cc\u0631 \u0646\u0638\u0631 \u0628\u06af\u06cc\u0631", "\u0646\u0638\u0631"]


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
    reasons = result.get("reasons", [])[:10]
    reasons_text = chr(10).join([f"✅ {r}" for r in reasons])
    trade_levels = build_trade_levels(result)

    return f"""
📊 تحلیل فیوچرز {result['symbol']}

وضعیت ورود: {"✅ فعال" if result.get("entry_confirmed") else "👀 منتظر فعال‌سازی" if result.get("setup_waiting_activation") else "غیرفعال"}
قیمت فعلی: {result['price']}

جهت نهایی: {fa_direction(result['direction'])}
حالت ورود: {safe(result.get('entry_mode'))}
تازگی حرکت: {safe(result.get('freshness'))}
تعداد تاییدیه ورود: {safe(result.get('predictive_confirmations'))}
ریسک: {safe(result.get('risk_level'))}
ریسک به ریوارد: {safe(result.get('risk_reward'))}

قدرت خرید 2 کندلی: {safe(result.get('power2_buy'))}٪
قدرت فروش 2 کندلی: {safe(result.get('power2_sell'))}٪
قدرت خرید 3 کندلی: {safe(result.get('power3_buy'))}٪
قدرت فروش 3 کندلی: {safe(result.get('power3_sell'))}٪
شتاب قدرت: {safe(result.get('power_acceleration'))}

RSI: {result['rsi']}
MACD: {result['macd']}
ADX: {safe(result.get('adx'))}
VWAP: {fa_general(result.get('vwap_status'))}

حمایت: {result['support']}
مقاومت: {result['resistance']}

🎯 سطوح معامله:
{trade_levels}

⏱ تایم‌فریم: {result['signal_timeframe']}
⏰ اعتبار: {result['validity']}

دلایل اصلی:
{reasons_text}

⚠️ این تحلیل تضمین سود نیست؛ با حد ضرر، حجم کم و مدیریت ریسک وارد شو.
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
    direction_fa = "لانگ" if result["direction"] == "LONG" else "شورت"

    entry_mode = result.get("entry_mode")
    is_setup_waiting = (
        bool(result.get("setup_waiting_activation"))
        or entry_mode == "PREDICTIVE_SETUP"
        or not bool(result.get("entry_confirmed"))
    )

    title = "🚨 سیگنال آماده" if is_setup_waiting else "🚨 سیگنال خودکار"
    entry_status_text = "👀 منتظر فعال‌سازی ورود" if is_setup_waiting else "✅ ورود فعال"

    text = f"""
{title}

وضعیت: {entry_status_text}
ارز: {result['symbol']}
جهت: {direction_fa}
حالت ورود: {safe(result.get('entry_mode'))}
تازگی حرکت: {safe(result.get('freshness'))}
تاییدیه‌های ورود: {safe(result.get('predictive_confirmations'))}
ریسک: {safe(result.get('risk_level'))}
ریسک به ریوارد: {safe(result.get('risk_reward'))}

قیمت: {result['price']}
حد ضرر: {result['stop_loss']}
حد سود 1: {result['tp1']}
حد سود 2: {result['tp2']}

قدرت خرید 2 کندلی: {safe(result.get('power2_buy'))}٪
قدرت فروش 2 کندلی: {safe(result.get('power2_sell'))}٪
قدرت خرید 3 کندلی: {safe(result.get('power3_buy'))}٪
قدرت فروش 3 کندلی: {safe(result.get('power3_sell'))}٪
شتاب قدرت: {safe(result.get('power_acceleration'))}
RSI: {result['rsi']}
ADX: {safe(result.get('adx'))}

⚠️ مدیریت ریسک فراموش نشود.
"""

    for user_id in list_users():
        try:
            can_add, reason = can_add_automatic_signal(user_id, result.get("symbol"))
            if not can_add:
                continue

            sent = bot.send_message(user_id, text)
            remember_signal_result(sent, result)
            if AUTO_TRACK_AUTO_SIGNALS:
                try:
                    add_signal_to_tracking(user_id=user_id, chat_id=user_id, message_id=sent.message_id, result=result)
                except Exception as e:
                    log_exception("ثبت خودکار مانیتورینگ", e, "bot.py", "send_auto_signal_to_all_users", result.get("symbol"))
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
