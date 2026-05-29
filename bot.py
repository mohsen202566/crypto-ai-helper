import telebot
import requests

from config import BOT_TOKEN, ALLOWED_USERS
from coins_fa import COINS_FA

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


def get_price(symbol):
    url = "https://api.binance.com/api/v3/ticker/price"
    response = requests.get(url, params={"symbol": symbol}, timeout=10)
    data = response.json()

    if "price" not in data:
        return None

    return float(data["price"])


@bot.message_handler(commands=["start"])
def start(message):
    if not is_allowed(message.from_user.id):
