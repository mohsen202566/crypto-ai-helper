# -*- coding: utf-8 -*-
import telebot
import threading
import time

from config import BOT_TOKEN, AUTO_SCAN_INTERVAL_MINUTES, TRACKER_CHECK_INTERVAL_SECONDS, AUTO_SIGNAL_ENABLED
from coins_fa import COINS_FA
from analysis import analyze_symbol
from scanner import get_best_signals, SCAN_SYMBOLS, should_send_auto_signal
from users import is_user_allowed, is_owner, add_user, remove_user, list_users
from signal_tracker import (
    add_signal_to_tracking,
    check_active_signals,
    get_stats_report,
    parse_days_from_text,
    parse_profit_calc_text,
    parse_days_from_report_text,
    get_profit_for_signal_text,
    get_profit_simulation_report,
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN تنظیم نشده است. اول export BOT_TOKEN را روی VPS ست کن.")

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
    reasons_text = "\n".join([f"\u2705 {r}" for r in result.get("reasons", [])])
    trade_levels = build_trade_levels(result)

    return f"""
\U0001f4ca \u062a\u062d\u0644\u06cc\u0644 \u0641\u06cc\u0648\u0686\u0631\u0632 {result['symbol']}

\u0642\u06cc\u0645\u062a \u0641\u0639\u0644\u06cc:
{result['price']}

\u062c\u0647\u062a \u0646\u0647\u0627\u06cc\u06cc:
{fa_direction(result['direction'])}

\u062c\u0647\u062a \u062e\u0627\u0645 \u062a\u062d\u0644\u06cc\u0644:
{fa_direction(result.get('raw_direction'))}

\u0627\u0645\u062a\u06cc\u0627\u0632 \u0633\u06cc\u06af\u0646\u0627\u0644:
{result['score']}/100

\u0627\u062d\u062a\u0645\u0627\u0644 \u0645\u0648\u0641\u0642\u06cc\u062a \u062a\u0642\u0631\u06cc\u0628\u06cc:
{safe(result.get('win_probability'))}\u066a

\u06af\u0631\u06cc\u062f \u0648\u0631\u0648\u062f:
{safe(result.get('entry_grade'))}

\u0633\u0637\u062d \u0631\u06cc\u0633\u06a9:
{safe(result.get('risk_level'))}

\u0631\u06cc\u0633\u06a9 \u0628\u0647 \u0631\u06cc\u0648\u0627\u0631\u062f:
{safe(result.get('risk_reward'))}

\u0631\u06cc\u0633\u06a9 \u0644\u06cc\u06a9\u0648\u06cc\u06cc\u062f\u06cc\u062a\u06cc:
{safe(result.get('liquidity_risk'))}

\u23f0 \u0627\u0639\u062a\u0628\u0627\u0631 \u0633\u06cc\u06af\u0646\u0627\u0644:
{result['validity']}

\u23f1 \u062a\u0627\u06cc\u0645\u200c\u0641\u0631\u06cc\u0645 \u0645\u0646\u0627\u0633\u0628:
{result['signal_timeframe']}

\u0627\u0645\u062a\u06cc\u0627\u0632 \u0644\u0627\u0646\u06af:
{result['long_score']}

\u0627\u0645\u062a\u06cc\u0627\u0632 \u0634\u0648\u0631\u062a:
{result['short_score']}

\u0642\u062f\u0631\u062a \u062e\u0631\u06cc\u062f:
{result['buy_power']}\u066a

\u0642\u062f\u0631\u062a \u0641\u0631\u0648\u0634:
{result['sell_power']}\u066a

\u0634\u0627\u062e\u0635 RSI:
{result['rsi']}

\u0642\u062f\u0631\u062a \u0631\u0648\u0646\u062f ADX:
{safe(result.get('adx'))}

MACD:
{result['macd']}

\u0647\u06cc\u0633\u062a\u0648\u06af\u0631\u0627\u0645 MACD:
{safe(result.get('macd_hist'))}

\u0645\u06cc\u0627\u0646\u06af\u06cc\u0646 \u062d\u062c\u0645\u06cc \u0642\u06cc\u0645\u062a:
{safe(result.get('vwap'))}

\u0648\u0636\u0639\u06cc\u062a \u0645\u06cc\u0627\u0646\u06af\u06cc\u0646 \u062d\u062c\u0645\u06cc \u0642\u06cc\u0645\u062a:
{fa_general(result.get('vwap_status'))}

POC \u062d\u062c\u0645\u06cc:
{safe(result.get('poc_price'))}

\u0648\u0636\u0639\u06cc\u062a \u062d\u062c\u0645:
{fa_general(result.get('volume_profile_status'))}

\u0646\u0631\u062e \u0641\u0627\u0646\u062f\u06cc\u0646\u06af:
{safe(result.get('funding_rate'))}\u066a

\u062d\u062c\u0645 \u0642\u0631\u0627\u0631\u062f\u0627\u062f\u0647\u0627\u06cc \u0628\u0627\u0632:
{safe(result.get('open_interest'))}

\u0627\u0633\u067e\u0631\u062f:
{safe(result.get('spread_percent'))}\u066a

\u0641\u06cc\u0644\u062a\u0631 \u0628\u06cc\u062a\u06a9\u0648\u06cc\u0646:
{fa_general(result.get('btc_filter'))}

\u06a9\u0646\u062f\u0644 \u062a\u0627\u06cc\u06cc\u062f\u06cc:
{fa_general(result.get('candle_pattern'))}

\u062a\u0627\u06cc\u06cc\u062f \u0686\u0646\u062f \u06a9\u0646\u062f\u0644\u06cc:
{fa_general(result.get('multi_candle'))}

\u062c\u0645\u0639\u200c\u0622\u0648\u0631\u06cc \u0646\u0642\u062f\u06cc\u0646\u06af\u06cc:
{fa_general(result.get('liquidity_grab'))}

\u0627\u0633\u062a\u0627\u067e\u200c\u0647\u0627\u0646\u062a:
{fa_general(result.get('stop_hunt'))}

\u0646\u0627\u062d\u06cc\u0647 \u062e\u0627\u0644\u06cc \u0646\u0642\u062f\u06cc\u0646\u06af\u06cc:
{fa_general(result.get('fvg'))}

\u0627\u0648\u0631\u062f\u0631 \u0628\u0644\u0627\u06a9:
{fa_general(result.get('order_block'))}

\u0648\u0627\u06af\u0631\u0627\u06cc\u06cc RSI:
{fa_general(result.get('rsi_divergence'))}

\u0648\u0627\u06af\u0631\u0627\u06cc\u06cc MACD:
{fa_general(result.get('macd_divergence'))}

\u0641\u06cc\u06a9 \u0628\u0631\u06cc\u06a9\u200c\u0627\u0648\u062a:
{fa_general(result.get('fake_breakout'))}

\u062e\u0633\u062a\u06af\u06cc \u0631\u0648\u0646\u062f:
{fa_general(result.get('trend_exhaustion'))}

\u062d\u0645\u0627\u06cc\u062a:
{result['support']}

\u0645\u0642\u0627\u0648\u0645\u062a:
{result['resistance']}

\u062e\u0637 \u0631\u0648\u0646\u062f:
{fa_general(result['trendline'])}

\u0633\u0627\u062e\u062a\u0627\u0631 \u0628\u0627\u0632\u0627\u0631:
{fa_general(result['market_structure'])}

\u0648\u0636\u0639\u06cc\u062a \u0628\u0631\u06cc\u06a9\u200c\u0627\u0648\u062a:
{fa_general(result['breakout'])}

\u0634\u0627\u062e\u0635 \u062a\u0631\u0633 \u0648 \u0637\u0645\u0639:
{safe(result.get('fear_value'))} - {safe(result.get('fear_text'))}

\u062f\u0627\u0645\u06cc\u0646\u0646\u0633 \u0628\u06cc\u062a\u06a9\u0648\u06cc\u0646:
{safe(result.get('btc_dominance'))}\u066a

\u0648\u0636\u0639\u06cc\u062a \u062f\u0627\u0645\u06cc\u0646\u0646\u0633:
{safe(result.get('dominance_status'))}

\u0648\u0636\u0639\u06cc\u062a \u0622\u0644\u062a\u200c\u0633\u06cc\u0632\u0646:
{safe(result.get('altseason_status'))}

\U0001f3af \u0633\u0637\u0648\u062d \u0645\u0639\u0627\u0645\u0644\u0647:
{trade_levels}

\U0001f9ed \u0646\u0627\u062d\u06cc\u0647 \u0648\u0631\u0648\u062f \u067e\u06cc\u0634\u0646\u0647\u0627\u062f\u06cc:
{safe(result.get('entry_zone_low'))} \u062a\u0627 {safe(result.get('entry_zone_high'))}

\u062a\u0631\u06cc\u06af\u0631 \u0648\u0631\u0648\u062f:
{safe(result.get('entry_trigger'))}

\u062d\u0627\u0644\u062a \u062e\u06cc\u0644\u06cc \u0627\u0645\u0646:
{"\u2705 \u0628\u0644\u0647" if result.get("very_safe") else "\u274c \u0646\u0647"}

\u062f\u0644\u0627\u06cc\u0644 \u062a\u062d\u0644\u06cc\u0644:
{reasons_text}

\u26a0\ufe0f \u0627\u06cc\u0646 \u062a\u062d\u0644\u06cc\u0644 \u062a\u0636\u0645\u06cc\u0646 \u0633\u0648\u062f \u0646\u06cc\u0633\u062a. \u062d\u062a\u0645\u0627\u064b \u0628\u0627 \u062d\u062f \u0636\u0631\u0631\u060c \u062d\u062c\u0645 \u06a9\u0645 \u0648 \u0645\u062f\u06cc\u0631\u06cc\u062a \u0631\u06cc\u0633\u06a9 \u0648\u0627\u0631\u062f \u0634\u0648.
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
\u062c\u0647\u062a: {direction_fa}
\u0627\u0645\u062a\u06cc\u0627\u0632: {r['score']}/100
\u0627\u062d\u062a\u0645\u0627\u0644 \u0645\u0648\u0641\u0642\u06cc\u062a: {safe(r.get('win_probability'))}\u066a
\u06af\u0631\u06cc\u062f: {safe(r.get('entry_grade'))}
\u0631\u06cc\u0633\u06a9: {safe(r.get('risk_level'))}
\u0631\u06cc\u0633\u06a9 \u0628\u0647 \u0631\u06cc\u0648\u0627\u0631\u062f: {safe(r.get('risk_reward'))}
\u0627\u0639\u062a\u0628\u0627\u0631: {r['validity']}
\u062a\u0627\u06cc\u0645\u200c\u0641\u0631\u06cc\u0645: {r['signal_timeframe']}
\u0642\u06cc\u0645\u062a: {r['price']}
ADX: {safe(r.get('adx'))}
\u0627\u0633\u067e\u0631\u062f: {safe(r.get('spread_percent'))}\u066a
\u0646\u0631\u062e \u0641\u0627\u0646\u062f\u06cc\u0646\u06af: {safe(r.get('funding_rate'))}\u066a
\u062d\u0627\u0644\u062a \u062e\u06cc\u0644\u06cc \u0627\u0645\u0646: {"\u0628\u0644\u0647 \u2705" if r.get("very_safe") else "\u062e\u06cc\u0631"}
"""

    bot.reply_to(message, msg)


def send_auto_signal_to_all_users(result):
    direction_fa = "\u0644\u0627\u0646\u06af" if result["direction"] == "LONG" else "\u0634\u0648\u0631\u062a"

    text = f"""
\U0001f6a8 \u0633\u06cc\u06af\u0646\u0627\u0644 \u062e\u0648\u062f\u06a9\u0627\u0631 \u0642\u0648\u06cc

\u0627\u0631\u0632:
{result['symbol']}

\u062c\u0647\u062a:
{direction_fa}

\u0627\u0645\u062a\u06cc\u0627\u0632:
{result['score']}/100

\u0627\u062d\u062a\u0645\u0627\u0644 \u0645\u0648\u0641\u0642\u06cc\u062a:
{safe(result.get('win_probability'))}\u066a

\u06af\u0631\u06cc\u062f:
{safe(result.get('entry_grade'))}

\u0631\u06cc\u0633\u06a9:
{safe(result.get('risk_level'))}

\u0631\u06cc\u0633\u06a9 \u0628\u0647 \u0631\u06cc\u0648\u0627\u0631\u062f:
{safe(result.get('risk_reward'))}

\u0627\u0639\u062a\u0628\u0627\u0631 \u0633\u06cc\u06af\u0646\u0627\u0644:
{result['validity']}

\u062a\u0627\u06cc\u0645\u200c\u0641\u0631\u06cc\u0645 \u0645\u0646\u0627\u0633\u0628:
{result['signal_timeframe']}

\u0642\u06cc\u0645\u062a:
{result['price']}

\u062d\u062f \u0636\u0631\u0631:
{result['stop_loss']}

\u062d\u062f \u0633\u0648\u062f 1:
{result['tp1']}

\u062d\u062f \u0633\u0648\u062f 2:
{result['tp2']}

\u0642\u062f\u0631\u062a \u062e\u0631\u06cc\u062f:
{result['buy_power']}\u066a

\u0642\u062f\u0631\u062a \u0641\u0631\u0648\u0634:
{result['sell_power']}\u066a

ADX:
{safe(result.get('adx'))}

\u0646\u0631\u062e \u0641\u0627\u0646\u062f\u06cc\u0646\u06af:
{safe(result.get('funding_rate'))}\u066a

\u0645\u06cc\u0627\u0646\u06af\u06cc\u0646 \u062d\u062c\u0645\u06cc \u0642\u06cc\u0645\u062a:
{fa_general(result.get('vwap_status'))}

\u0646\u0627\u062d\u06cc\u0647 \u062e\u0627\u0644\u06cc \u0646\u0642\u062f\u06cc\u0646\u06af\u06cc:
{fa_general(result.get('fvg'))}

\u0627\u0648\u0631\u062f\u0631 \u0628\u0644\u0627\u06a9:
{fa_general(result.get('order_block'))}

\u0646\u0627\u062d\u06cc\u0647 \u0648\u0631\u0648\u062f:
{safe(result.get('entry_zone_low'))} \u062a\u0627 {safe(result.get('entry_zone_high'))}

\u062d\u0627\u0644\u062a \u062e\u06cc\u0644\u06cc \u0627\u0645\u0646:
{"\u0628\u0644\u0647 \u2705" if result.get("very_safe") else "\u062e\u06cc\u0631"}

\u26a0\ufe0f \u0645\u062f\u06cc\u0631\u06cc\u062a \u0631\u06cc\u0633\u06a9 \u0641\u0631\u0627\u0645\u0648\u0634 \u0646\u0634\u0648\u062f.
"""

    for user_id in list_users():
        try:
            sent = bot.send_message(user_id, text)
            remember_signal_result(sent, result)
        except Exception as e:
            print("SEND AUTO SIGNAL ERROR:", user_id, str(e))


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
                if "does not have market symbol" not in msg and "429" not in msg and "Too Many Requests" not in msg:
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
                    bot.send_message(item["chat_id"], item["message"])
                except Exception as e:
                    print("SEND TRACK RESULT ERROR:", str(e))

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

    text = message.text.strip()

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


if AUTO_SIGNAL_ENABLED:
    threading.Thread(target=auto_signal_loop, daemon=True).start()
threading.Thread(target=signal_tracking_loop, daemon=True).start()

print("Bot is running...")
bot.infinity_polling()
