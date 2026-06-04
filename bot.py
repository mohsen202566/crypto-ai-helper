# -*- coding: utf-8 -*-
import os
import threading
import time
import telebot

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
    raise RuntimeError("BOT_TOKEN تنظیم نشده است. اول روی VPS دستور export BOT_TOKEN را بزن.")

bot = telebot.TeleBot(BOT_TOKEN)
MESSAGE_RESULTS = {}
TRACK_COMMANDS = ["زیر نظر", "زیرنظر", "زیر نظر بگیر", "نظر"]


def safe(value, default="نامشخص"):
    return default if value is None else value


def remember_signal_result(sent_message, result):
    try:
        if result and result.get("direction") != "NO TRADE":
            MESSAGE_RESULTS[(int(sent_message.chat.id), int(sent_message.message_id))] = result
    except Exception as e:
        print("REMEMBER SIGNAL ERROR:", str(e))


def get_replied_signal_result(message):
    if not message.reply_to_message:
        return None
    key = (int(message.reply_to_message.chat.id), int(message.reply_to_message.message_id))
    return MESSAGE_RESULTS.get(key)


def is_track_command(text):
    return text.strip().lower() in TRACK_COMMANDS


def is_stats_command(text):
    clean = text.strip()
    return clean == "آمار" or clean.startswith("آمار ")


def find_symbol(text):
    text = text.lower().strip()
    for name, symbol in COINS_FA.items():
        if name.lower() in text:
            return symbol
    text = text.replace("تحلیل", "").replace("سیگنال", "").strip().upper()
    if text.endswith("USDT"):
        return text
    return None


def fa_direction(direction):
    return {
        "LONG": "🟢 لانگ",
        "SHORT": "🔴 شورت",
        "NO TRADE": "⚪ فعلاً ورود مناسب نیست"
    }.get(direction, direction)


def fa_general(value):
    data = {
        "bullish": "صعودی", "bearish": "نزولی", "neutral": "خنثی",
        "range": "رنج", "weak": "ضعیف", "none": "ندارد",
        "unknown": "نامشخص", "ok": "تأیید شده",
        "uptrend": "صعودی", "downtrend": "نزولی", "sideways": "خنثی",
        "bullish_structure": "ساختار صعودی",
        "bearish_structure": "ساختار نزولی",
        "range_structure": "رنج / بدون روند واضح",
        "bullish_breakout": "بریک‌اوت صعودی",
        "bearish_breakout": "بریک‌اوت نزولی",
        "fake_bullish_breakout": "فیک بریک‌اوت صعودی",
        "fake_bearish_breakout": "فیک بریک‌اوت نزولی",
        "no_breakout": "بدون بریک‌اوت",
        "bullish_engulfing": "انگالف صعودی",
        "bearish_engulfing": "انگالف نزولی",
        "bullish_pinbar": "پین‌بار صعودی",
        "bearish_pinbar": "پین‌بار نزولی",
        "bullish_strong": "کندل صعودی قوی",
        "bearish_strong": "کندل نزولی قوی",
        "bullish_liquidity_grab": "جمع‌آوری نقدینگی صعودی",
        "bearish_liquidity_grab": "جمع‌آوری نقدینگی نزولی",
        "bullish_stop_hunt": "استاپ‌هانت صعودی",
        "bearish_stop_hunt": "استاپ‌هانت نزولی",
        "bullish_fvg": "FVG صعودی",
        "bearish_fvg": "FVG نزولی",
        "bullish_order_block": "اوردر بلاک صعودی",
        "bearish_order_block": "اوردر بلاک نزولی",
        "bullish_rsi_divergence": "واگرایی مثبت RSI",
        "bearish_rsi_divergence": "واگرایی منفی RSI",
        "bullish_macd_divergence": "واگرایی مثبت MACD",
        "bearish_macd_divergence": "واگرایی منفی MACD",
        "bullish_exhaustion": "خستگی روند صعودی",
        "bearish_exhaustion": "خستگی روند نزولی",
        "above_vwap": "بالای VWAP",
        "below_vwap": "پایین VWAP",
        "near_vwap": "نزدیک VWAP",
        "above_poc": "بالای ناحیه حجمی اصلی",
        "below_poc": "پایین ناحیه حجمی اصلی",
        "near_poc": "نزدیک ناحیه حجمی اصلی",
    }
    return data.get(value, value)


def build_trade_levels(result):
    if result.get("stop_loss") is None:
        return f"""
برای این وضعیت، ورود پیشنهاد نمی‌شود.

سطوح احتمالی فقط برای بررسی:
حد ضرر احتمالی:
{safe(result.get('candidate_stop_loss'))}

حد سود 1 احتمالی:
{safe(result.get('candidate_tp1'))}

حد سود 2 احتمالی:
{safe(result.get('candidate_tp2'))}
"""
    return f"""
ورود تقریبی:
{result['price']}

حد ضرر:
{result['stop_loss']}

حد سود 1:
{result['tp1']}

حد سود 2:
{result['tp2']}
"""


def build_analysis_text(result):
    reasons_text = "\n".join([f"✅ {r}" for r in result.get("reasons", [])])
    trade_levels = build_trade_levels(result)
    return f"""
📊 تحلیل فیوچرز {result['symbol']}

قیمت فعلی:
{result['price']}

جهت نهایی:
{fa_direction(result['direction'])}

جهت خام تحلیل:
{fa_direction(result.get('raw_direction'))}

امتیاز سیگنال:
{result['score']}/100

احتمال موفقیت تقریبی:
{safe(result.get('win_probability'))}٪

گرید ورود:
{safe(result.get('entry_grade'))}

سطح ریسک:
{safe(result.get('risk_level'))}

ریسک به ریوارد:
{safe(result.get('risk_reward'))}

ریسک لیکوییدیتی:
{safe(result.get('liquidity_risk'))}

⏰ اعتبار سیگنال:
{result['validity']}

⏱ تایم‌فریم مناسب:
{result['signal_timeframe']}

امتیاز لانگ:
{result['long_score']}

امتیاز شورت:
{result['short_score']}

قدرت خرید:
{result['buy_power']}٪

قدرت فروش:
{result['sell_power']}٪

RSI:
{result['rsi']}

ADX:
{safe(result.get('adx'))}

MACD:
{result['macd']}

هیستوگرام MACD:
{safe(result.get('macd_hist'))}

VWAP:
{safe(result.get('vwap'))}

وضعیت VWAP:
{fa_general(result.get('vwap_status'))}

POC حجمی:
{safe(result.get('poc_price'))}

وضعیت حجم:
{fa_general(result.get('volume_profile_status'))}

Funding:
{safe(result.get('funding_rate'))}٪

Open Interest:
{safe(result.get('open_interest'))}

Spread:
{safe(result.get('spread_percent'))}٪

BTC Filter:
{fa_general(result.get('btc_filter'))}

کندل تاییدی:
{fa_general(result.get('candle_pattern'))}

تایید چند کندلی:
{fa_general(result.get('multi_candle'))}

Liquidity Grab:
{fa_general(result.get('liquidity_grab'))}

Stop Hunt:
{fa_general(result.get('stop_hunt'))}

FVG:
{fa_general(result.get('fvg'))}

Order Block:
{fa_general(result.get('order_block'))}

واگرایی RSI:
{fa_general(result.get('rsi_divergence'))}

واگرایی MACD:
{fa_general(result.get('macd_divergence'))}

فیک بریک‌اوت:
{fa_general(result.get('fake_breakout'))}

خستگی روند:
{fa_general(result.get('trend_exhaustion'))}

حمایت:
{result['support']}

مقاومت:
{result['resistance']}

خط روند:
{fa_general(result['trendline'])}

ساختار بازار:
{fa_general(result['market_structure'])}

وضعیت بریک‌اوت:
{fa_general(result['breakout'])}

Fear & Greed:
{safe(result.get('fear_value'))} - {safe(result.get('fear_text'))}

BTC Dominance:
{safe(result.get('btc_dominance'))}٪

وضعیت دامیننس:
{safe(result.get('dominance_status'))}

Alt Season:
{safe(result.get('altseason_status'))}

🎯 سطوح معامله:
{trade_levels}

🧭 ناحیه ورود پیشنهادی:
{safe(result.get('entry_zone_low'))} تا {safe(result.get('entry_zone_high'))}

تریگر ورود:
{safe(result.get('entry_trigger'))}

حالت خیلی امن:
{"✅ بله" if result.get("very_safe") else "❌ نه"}

دلایل تحلیل:
{reasons_text}

⚠️ این تحلیل تضمین سود نیست. حتماً با حد ضرر، حجم کم و مدیریت ریسک وارد شو.
"""


def send_analysis(message, symbol):
    bot.reply_to(message, f"⏳ در حال تحلیل {symbol} ...")
    try:
        result = analyze_symbol(symbol)
    except Exception as e:
        print("ANALYSIS ERROR:", str(e))
        bot.reply_to(message, f"❌ خطا در تحلیل {symbol}\n\nعلت خطا:\n{e}")
        return
    sent = bot.reply_to(message, build_analysis_text(result))
    remember_signal_result(sent, result)


def send_best_signals(message, very_safe_only=False):
    bot.reply_to(message, "⏳ در حال اسکن بازار...")
    try:
        results = get_best_signals(limit=5, very_safe_only=very_safe_only)
    except Exception as e:
        print("BEST SIGNAL ERROR:", str(e))
        bot.reply_to(message, f"❌ خطا در اسکن بازار:\n{e}")
        return
    if not results:
        bot.reply_to(message, "فعلاً سیگنال مناسبی پیدا نشد.")
        return

    msg = "🏆 بهترین سیگنال‌های خیلی امن:\n\n" if very_safe_only else "🏆 بهترین سیگنال‌های الان:\n\n"
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for i, r in enumerate(results):
        direction_fa = "لانگ" if r["direction"] == "LONG" else "شورت"
        msg += f"""
{medals[i]} {r['symbol']}
جهت: {direction_fa}
امتیاز: {r['score']}/100
احتمال موفقیت: {safe(r.get('win_probability'))}٪
گرید: {safe(r.get('entry_grade'))}
ریسک: {safe(r.get('risk_level'))}
R/R: {safe(r.get('risk_reward'))}
اعتبار: {r['validity']}
تایم‌فریم: {r['signal_timeframe']}
قیمت: {r['price']}
ADX: {safe(r.get('adx'))}
Spread: {safe(r.get('spread_percent'))}٪
Funding: {safe(r.get('funding_rate'))}٪
Very Safe: {"بله ✅" if r.get("very_safe") else "خیر"}
"""
    bot.reply_to(message, msg)


def send_auto_signal_to_all_users(result):
    direction_fa = "لانگ" if result["direction"] == "LONG" else "شورت"
    text = f"""
🚨 سیگنال خودکار قوی

ارز:
{result['symbol']}

جهت:
{direction_fa}

امتیاز:
{result['score']}/100

احتمال موفقیت:
{safe(result.get('win_probability'))}٪

گرید:
{safe(result.get('entry_grade'))}

ریسک:
{safe(result.get('risk_level'))}

R/R:
{safe(result.get('risk_reward'))}

قیمت:
{result['price']}

حد ضرر:
{result['stop_loss']}

حد سود 1:
{result['tp1']}

حد سود 2:
{result['tp2']}

قدرت خرید:
{result['buy_power']}٪

قدرت فروش:
{result['sell_power']}٪

ADX:
{safe(result.get('adx'))}

FVG:
{fa_general(result.get('fvg'))}

Order Block:
{fa_general(result.get('order_block'))}

Very Safe:
{"بله ✅" if result.get("very_safe") else "خیر"}

⚠️ مدیریت ریسک فراموش نشود.
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
                quiet = ["does not have market symbol", "Too Many Requests", "429", "Unauthorized"]
                if not any(x in msg for x in quiet):
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
        bot.reply_to(message, "⛔ شما مجاز به استفاده از این ربات نیستید.")
        return
    bot.reply_to(message, """
سلام 👋

ربات دستیار فیوچرز کریپتو فعال است.

مثال:
بیتکوین
تحلیل دوج
بهترین سیگنال
سیگنال خیلی امن

زیر نظر گرفتن:
روی پیام تحلیل ریپلای کن و بنویس:
نظر
یا
زیر نظر

آمار:
آمار
آمار 7 روز
آمار کل

محاسبه سود:
روی پیام تحلیل یا آمار ریپلای کن و بنویس:
5 دلار لوریج 10
""")


@bot.message_handler(commands=["adduser"])
def add_user_command(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "⛔ فقط مالک ربات می‌تواند کاربر اضافه کند.")
        return
    try:
        user_id = int(message.text.split()[1])
        add_user(user_id)
        bot.reply_to(message, f"✅ کاربر {user_id} اضافه شد.")
    except Exception:
        bot.reply_to(message, "فرمت درست:\n/adduser 123456789")


@bot.message_handler(commands=["removeuser"])
def remove_user_command(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "⛔ فقط مالک ربات می‌تواند کاربر حذف کند.")
        return
    try:
        user_id = int(message.text.split()[1])
        ok = remove_user(user_id)
        bot.reply_to(message, f"✅ کاربر {user_id} حذف شد." if ok else "❌ مالک اصلی قابل حذف نیست یا کاربر وجود ندارد.")
    except Exception:
        bot.reply_to(message, "فرمت درست:\n/removeuser 123456789")


@bot.message_handler(commands=["listusers"])
def list_users_command(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "⛔ فقط مالک ربات می‌تواند لیست کاربران را ببیند.")
        return
    bot.reply_to(message, "👥 کاربران مجاز:\n" + "\n".join([str(u) for u in list_users()]))


@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if not is_user_allowed(message.from_user.id):
        bot.reply_to(message, "⛔ شما مجاز به استفاده از این ربات نیستید.")
        return
    if not message.text:
        return

    text = message.text.strip()

    profit_calc = parse_profit_calc_text(text)
    if profit_calc:
        margin, leverage = profit_calc
        reply_text = message.reply_to_message.text if message.reply_to_message and message.reply_to_message.text else None
        single_report = get_profit_for_signal_text(reply_text, margin, leverage)
        if single_report:
            bot.reply_to(message, single_report)
            return
        days = parse_days_from_report_text(reply_text) if reply_text else 7
        bot.reply_to(message, get_profit_simulation_report(margin, leverage, days))
        return

    if is_track_command(text):
        result = get_replied_signal_result(message)
        if not result:
            bot.reply_to(message, "❌ برای زیر نظر گرفتن، باید روی پیام تحلیل یا سیگنال خودکار ریپلای بزنی.")
            return
        ok, msg = add_signal_to_tracking(message.from_user.id, message.chat.id, message.reply_to_message.message_id, result)
        bot.reply_to(message, msg)
        return

    if is_stats_command(text):
        days = parse_days_from_text(text)
        bot.reply_to(message, get_stats_report(days))
        return

    if "خیلی امن" in text or "very safe" in text.lower():
        send_best_signals(message, very_safe_only=True)
        return

    if "بهترین سیگنال" in text or "بهترین فرصت" in text:
        send_best_signals(message)
        return

    symbol = find_symbol(text)
    if not symbol:
        bot.reply_to(message, "ارز رو متوجه نشدم. مثلا بنویس: بیتکوین یا اتریوم")
        return
    send_analysis(message, symbol)


if AUTO_SIGNAL_ENABLED:
    threading.Thread(target=auto_signal_loop, daemon=True).start()
threading.Thread(target=signal_tracking_loop, daemon=True).start()

print("Bot is running...")
bot.infinity_polling(timeout=60, long_polling_timeout=50)
