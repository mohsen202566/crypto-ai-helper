# -*- coding: utf-8 -*-
import telebot
import threading
import time

from config import BOT_TOKEN, AUTO_SCAN_INTERVAL_MINUTES
from coins_fa import COINS_FA
from analysis import analyze_symbol
from scanner import get_best_signals, SCAN_SYMBOLS, should_send_auto_signal
from users import is_user_allowed, is_owner, add_user, remove_user, list_users
from signal_tracker import (
    add_signal_to_tracking,
    check_active_signals,
    get_stats_report,
    parse_days_from_text,
)

bot = telebot.TeleBot(BOT_TOKEN)

# حافظه موقت برای اتصال پیام سیگنال به دستور «زیر نظر»
# اگر ربات ری‌استارت شود، فقط سیگنال‌های قبلاً ثبت‌شده در active_signals.json باقی می‌مانند.
MESSAGE_RESULTS = {}

TRACK_COMMANDS = ["زیر نظر", "زیرنظر", "زیر نظر بگیر", "نظر"]


def safe(value, default="نامشخص"):
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
        "bullish": "صعودی",
        "bearish": "نزولی",
        "neutral": "خنثی",
        "range": "رنج",
        "weak": "ضعیف",
        "none": "ندارد",
        "unknown": "نامشخص",
        "ok": "تأیید شده",

        "uptrend": "صعودی",
        "downtrend": "نزولی",
        "sideways": "خنثی",

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

        "bullish_order_block": "Order Block صعودی",
        "bearish_order_block": "Order Block نزولی",

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

ADX قدرت روند:
{safe(result.get('adx'))}

MACD:
{result['macd']}

MACD Histogram:
{safe(result.get('macd_hist'))}

VWAP:
{safe(result.get('vwap'))}

وضعیت VWAP:
{fa_general(result.get('vwap_status'))}

POC حجمی:
{safe(result.get('poc_price'))}

وضعیت حجم:
{fa_general(result.get('volume_profile_status'))}

Funding Rate:
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

RSI Divergence:
{fa_general(result.get('rsi_divergence'))}

MACD Divergence:
{fa_general(result.get('macd_divergence'))}

Fake Breakout:
{fa_general(result.get('fake_breakout'))}

Trend Exhaustion:
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


def send_best_signals(message):
    bot.reply_to(message, "⏳ در حال اسکن بازار...")

    try:
        results = get_best_signals(limit=5)
    except Exception as e:
        print("BEST SIGNAL ERROR:", str(e))
        bot.reply_to(message, f"❌ خطا در اسکن بازار:\n{e}")
        return

    if not results:
        bot.reply_to(message, "فعلاً سیگنال مناسبی پیدا نشد.")
        return

    msg = "🏆 بهترین سیگنال‌های الان:\n\n"
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

اعتبار سیگنال:
{result['validity']}

تایم‌فریم مناسب:
{result['signal_timeframe']}

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

Funding:
{safe(result.get('funding_rate'))}٪

VWAP:
{fa_general(result.get('vwap_status'))}

FVG:
{fa_general(result.get('fvg'))}

Order Block:
{fa_general(result.get('order_block'))}

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
                print("AUTO SIGNAL ERROR:", symbol, str(e))
                continue

        time.sleep(AUTO_SCAN_INTERVAL_MINUTES * 60)


def signal_tracking_loop():
    # هر 60 ثانیه سیگنال‌های زیر نظر را برای TP1 یا SL بررسی می‌کند.
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

        time.sleep(60)


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
اتریوم
تحلیل دوج
سیگنال سولانا
بهترین سیگنال الان

زیر نظر گرفتن سیگنال:
روی پیام تحلیل یا سیگنال خودکار ریپلای کن و بنویس:
زیر نظر
یا
زیر نظر بگیر
یا
نظر

آمار:
آمار
آمار 3 روز
آمار 7 روز
آمار 14 روز
آمار 30 روز
آمار کل

دستورات ادمین:
/adduser 123456789
/removeuser 123456789
/listusers
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

        if ok:
            bot.reply_to(message, f"✅ کاربر {user_id} حذف شد.")
        else:
            bot.reply_to(message, "❌ مالک اصلی قابل حذف نیست یا کاربر وجود ندارد.")
    except Exception:
        bot.reply_to(message, "فرمت درست:\n/removeuser 123456789")


@bot.message_handler(commands=["listusers"])
def list_users_command(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "⛔ فقط مالک ربات می‌تواند لیست کاربران را ببیند.")
        return

    users = list_users()
    users_text = "\n".join([str(u) for u in users])
    bot.reply_to(message, f"👥 کاربران مجاز:\n{users_text}")


@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if not is_user_allowed(message.from_user.id):
        bot.reply_to(message, "⛔ شما مجاز به استفاده از این ربات نیستید.")
        return

    text = message.text.strip()

    if is_track_command(text):
        result = get_replied_signal_result(message)

        if not result:
            bot.reply_to(
                message,
                "❌ برای زیر نظر گرفتن، باید روی پیام تحلیل یا سیگنال خودکار ریپلای بزنی.\n"
                "اگر ربات ری‌استارت شده باشد، دوباره همان ارز را تحلیل بگیر و بعد ریپلای کن."
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

    if "بهترین سیگنال" in text or "بهترین فرصت" in text:
        send_best_signals(message)
        return

    symbol = find_symbol(text)

    if not symbol:
        bot.reply_to(message, "ارز رو متوجه نشدم. مثلا بنویس: بیتکوین یا اتریوم")
        return

    send_analysis(message, symbol)


threading.Thread(target=auto_signal_loop, daemon=True).start()
threading.Thread(target=signal_tracking_loop, daemon=True).start()

print("Bot is running...")
bot.infinity_polling()
