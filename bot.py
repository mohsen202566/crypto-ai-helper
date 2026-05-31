import telebot
import threading
import time

from config import BOT_TOKEN, AUTO_SCAN_INTERVAL_MINUTES
from coins_fa import COINS_FA
from analysis import analyze_symbol
from scanner import get_best_signals, SCAN_SYMBOLS, should_send_auto_signal
from users import is_user_allowed, is_owner, add_user, remove_user, list_users

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


def fa_trendline(value):
    data = {
        "uptrend": "صعودی",
        "downtrend": "نزولی",
        "sideways": "خنثی",
    }
    return data.get(value, value)


def fa_structure(value):
    data = {
        "bullish_structure": "ساختار صعودی",
        "bearish_structure": "ساختار نزولی",
        "range_structure": "رنج / بدون روند واضح",
    }
    return data.get(value, value)


def fa_breakout(value):
    data = {
        "bullish_breakout": "بریک‌اوت صعودی",
        "bearish_breakout": "بریک‌اوت نزولی",
        "fake_bullish_breakout": "فیک بریک‌اوت صعودی",
        "fake_bearish_breakout": "فیک بریک‌اوت نزولی",
        "no_breakout": "بدون بریک‌اوت",
    }
    return data.get(value, value)


def build_analysis_text(result):
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

    return f"""
📊 تحلیل فیوچرز {result['symbol']}

قیمت فعلی:
{result['price']}

جهت پیشنهادی:
{fa_direction(result['direction'])}

امتیاز سیگنال:
{result['score']}/100

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

MACD:
{result['macd']}

حمایت:
{result['support']}

مقاومت:
{result['resistance']}

خط روند:
{fa_trendline(result['trendline'])}

ساختار بازار:
{fa_structure(result['market_structure'])}

وضعیت بریک‌اوت:
{fa_breakout(result['breakout'])}

Fear & Greed:
{result['fear_value']} - {result['fear_text']}

BTC Dominance:
{result['btc_dominance']}٪

Alt Season:
{result['altseason_status']}

🎯 سطوح معامله:
{trade_levels}

دلایل تحلیل:
{reasons_text}

⚠️ این تحلیل تضمین سود نیست. حتماً با حد ضرر و مدیریت ریسک وارد شو.
"""


def send_analysis(message, symbol):
    bot.reply_to(message, f"⏳ در حال تحلیل {symbol} ...")

    try:
        result = analyze_symbol(symbol)
    except Exception as e:
        print("ANALYSIS ERROR:", str(e))
        bot.reply_to(message, f"❌ خطا در تحلیل {symbol}\n\nعلت خطا:\n{e}")
        return

    bot.reply_to(message, build_analysis_text(result))


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
اعتبار: {r['validity']}
تایم‌فریم: {r['signal_timeframe']}
قیمت: {r['price']}
قدرت خرید: {r['buy_power']}٪
قدرت فروش: {r['sell_power']}٪
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

⚠️ مدیریت ریسک فراموش نشود.
"""

    for user_id in list_users():
        try:
            bot.send_message(user_id, text)
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
bot.infinity_polling()
