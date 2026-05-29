import telebot

from config import BOT_TOKEN, ALLOWED_USERS
from coins_fa import COINS_FA
from analysis import analyze_symbol

bot = telebot.TeleBot(BOT_TOKEN)


def is_allowed(user_id):
    return user_id in ALLOWED_USERS


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


@bot.message_handler(commands=["start"])
def start(message):
    if not is_allowed(message.from_user.id):
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
""")


@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if not is_allowed(message.from_user.id):
        bot.reply_to(message, "⛔ شما مجاز به استفاده از این ربات نیستید.")
        return

    symbol = find_symbol(message.text)

    if not symbol:
        bot.reply_to(message, "ارز رو متوجه نشدم. مثلا بنویس: بیتکوین یا اتریوم")
        return

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

وضعیت بریک‌اوت:
{result['breakout']}

Fear & Greed:
{result['fear_value']} - {result['fear_text']}

🎯 سطوح معامله:
{trade_levels}

دلایل تحلیل:
{reasons_text}

⚠️ این تحلیل تضمین سود نیست. حتماً با حد ضرر، حجم کم و مدیریت ریسک وارد شو.
""")


print("Bot is running...")
bot.infinity_polling()
