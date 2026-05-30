import telebot
import threading
import time

from config import BOT_TOKEN, OWNER_ID, AUTO_SCAN_INTERVAL_MINUTES
from coins_fa import COINS_FA
from analysis import analyze_symbol
from scanner import get_best_signals, SCAN_SYMBOLS, should_send_auto_signal
from users import (
    is_user_allowed,
    is_owner,
    add_user,
    remove_user,
    list_users
)

bot = telebot.TeleBot(BOT_TOKEN)


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
    if direction == "LONG":
        return "🟢 لانگ"
    if direction == "SHORT":
        return "🔴 شورت"
    return "⚪ فعلاً ورود مناسب نیست"


def send_analysis(message, symbol):
    bot.reply_to(message, f"⏳ در حال تحلیل {symbol} ...")

    try:
        result = analyze_symbol(symbol)
    except Exception:
        bot.reply_to(message, f"❌ خطا در تحلیل {symbol}. بعداً دوباره امتحان کن.")
        return

    reasons_text = "\n".join([f"✅ {r}" for r in result["reasons"]])

    if result["stop_loss"] is None:
        trade_levels = "برای این وضعیت، ورود پیشنهاد نمی‌شود."
    else:
        trade_levels = f"""
ورود تقریبی:
{result['price']}

حد ضرر:
{result['stop_loss']}

حد سود 1:
{result['tp1']}

حد سود 2:
{result['tp2']}
"""

    bot.reply_to(message, f"""
📊 تحلیل فیوچرز {result['symbol']}

قیمت فعلی:
{result['price']}

جهت پیشنهادی:
{fa_direction(result['direction'])}

امتیاز سیگنال:
{result['score']}/100

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

MACD:
{result['macd']}

حمایت:
{result['support']}

مقاومت:
{result['resistance']}

خط روند:
{result['trendline']}

ساختار بازار:
{result['market_structure']}

وضعیت بریک‌اوت:
{result['breakout']}

Fear & Greed:
{result['fear_value']} - {result['fear_text']}

BTC Dominance:
{result['btc_dominance']}٪

وضعیت دامیننس:
{result['dominance_status']}

Alt Season:
{result['altseason_status']}

🎯 سطوح معامله:
{trade_levels}

دلایل تحلیل:
{reasons_text}

⚠️ این تحلیل تضمین سود نیست. حتماً با حد ضرر، حجم کم و مدیریت ریسک وارد شو.
""")


def send_best_signals(message):
    bot.reply_to(message, "⏳ در حال اسکن بازار...")

    results = get_best_signals(limit=5)

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
قیمت: {r['price']}
قدرت خرید: {r['buy_power']}٪
قدرت فروش: {r['sell_power']}٪
آلت‌سیزن: {r['altseason_status']}
"""

    bot.reply_to(message, msg)


def auto_signal_loop():
    while True:
        for symbol in SCAN_SYMBOLS:
            try:
                result = analyze_symbol(symbol)

                if should_send_auto_signal(result):
                    direction_fa = "لانگ" if result["direction"] == "LONG" else "شورت"

                    bot.send_message(OWNER_ID, f"""
🚨 سیگنال خودکار قوی

ارز:
{result['symbol']}

جهت:
{direction_fa}

امتیاز:
{result['score']}/100

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

Fear & Greed:
{result['fear_value']} - {result['fear_text']}

BTC Dominance:
{result['btc_dominance']}٪

Alt Season:
{result['altseason_status']}

⚠️ مدیریت ریسک فراموش نشود.
""")

            except Exception:
                continue

        time.sleep(AUTO_SCAN_INTERVAL_MINUTES * 60)


@bot.message_handler(commands=["start"])
def start(message):
    if not is_user_allowed(message.from_user.id):
        bot.reply_to(message, "⛔ شما مجاز به استفاده از این ربات نیستید.")
        return

    bot.reply_to(message, """
سلام محسن 👋

ربات دستیار فیوچرز کریپتو فعال است.

مثال:
بیتکوین
اتریوم
تحلیل دوج
سیگنال سولانا
بهترین سیگنال الان

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

    if "بهترین سیگنال" in text or "بهترین فرصت" in text:
        send_best_signals(message)
        return

    symbol = find_symbol(text)

    if not symbol:
        bot.reply_to(message, "ارز رو متوجه نشدم. مثلا بنویس: بیتکوین یا اتریوم")
        return

    send_analysis(message, symbol)


threading.Thread(target=auto_signal_loop, daemon=True).start()

print("Bot is running...")
bot.
bot.infinity_polling()
