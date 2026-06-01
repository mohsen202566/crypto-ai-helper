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

MESSAGE_RESULTS = {}

TRACK_COMMANDS = ["夭蹖乇 賳馗乇", "夭蹖乇賳馗乇", "夭蹖乇 賳馗乇 亘诏蹖乇", "賳馗乇"]


def safe(value, default="賳丕賲卮禺氐"):
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
    return clean == "丌賲丕乇" or clean.startswith("丌賲丕乇 ")


def find_symbol(text):
    text = text.lower().strip()

    for name, symbol in COINS_FA.items():
        if name.lower() in text:
            return symbol

    text = text.replace("鬲丨賱蹖賱", "").replace("爻蹖诏賳丕賱", "").strip().upper()

    if text.endswith("USDT"):
        return text

    return None


def fa_direction(direction):
    return {
        "LONG": "馃煝 賱丕賳诏",
        "SHORT": "馃敶 卮賵乇鬲",
        "NO TRADE": "鈿� 賮毓賱丕賸 賵乇賵丿 賲賳丕爻亘 賳蹖爻鬲"
    }.get(direction, direction)


def fa_general(value):
    data = {
        "bullish": "氐毓賵丿蹖",
        "bearish": "賳夭賵賱蹖",
        "neutral": "禺賳孬蹖",
        "range": "乇賳噩",
        "weak": "囟毓蹖賮",
        "none": "賳丿丕乇丿",
        "unknown": "賳丕賲卮禺氐",
        "ok": "鬲兀蹖蹖丿 卮丿賴",

        "uptrend": "氐毓賵丿蹖",
        "downtrend": "賳夭賵賱蹖",
        "sideways": "禺賳孬蹖",

        "bullish_structure": "爻丕禺鬲丕乇 氐毓賵丿蹖",
        "bearish_structure": "爻丕禺鬲丕乇 賳夭賵賱蹖",
        "range_structure": "乇賳噩 / 亘丿賵賳 乇賵賳丿 賵丕囟丨",

        "bullish_breakout": "亘乇蹖讴鈥屫з堌� 氐毓賵丿蹖",
        "bearish_breakout": "亘乇蹖讴鈥屫з堌� 賳夭賵賱蹖",
        "fake_bullish_breakout": "賮蹖讴 亘乇蹖讴鈥屫з堌� 氐毓賵丿蹖",
        "fake_bearish_breakout": "賮蹖讴 亘乇蹖讴鈥屫з堌� 賳夭賵賱蹖",
        "no_breakout": "亘丿賵賳 亘乇蹖讴鈥屫з堌�",

        "bullish_engulfing": "丕賳诏丕賱賮 氐毓賵丿蹖",
        "bearish_engulfing": "丕賳诏丕賱賮 賳夭賵賱蹖",
        "bullish_pinbar": "倬蹖賳鈥屫ㄘж� 氐毓賵丿蹖",
        "bearish_pinbar": "倬蹖賳鈥屫ㄘж� 賳夭賵賱蹖",
        "bullish_strong": "讴賳丿賱 氐毓賵丿蹖 賯賵蹖",
        "bearish_strong": "讴賳丿賱 賳夭賵賱蹖 賯賵蹖",

        "bullish_liquidity_grab": "噩賲毓鈥屫①堌臂� 賳賯丿蹖賳诏蹖 氐毓賵丿蹖",
        "bearish_liquidity_grab": "噩賲毓鈥屫①堌臂� 賳賯丿蹖賳诏蹖 賳夭賵賱蹖",
        "bullish_stop_hunt": "丕爻鬲丕倬鈥屬囏з嗀� 氐毓賵丿蹖",
        "bearish_stop_hunt": "丕爻鬲丕倬鈥屬囏з嗀� 賳夭賵賱蹖",

        "bullish_fvg": "FVG 氐毓賵丿蹖",
        "bearish_fvg": "FVG 賳夭賵賱蹖",

        "bullish_order_block": "Order Block 氐毓賵丿蹖",
        "bearish_order_block": "Order Block 賳夭賵賱蹖",

        "bullish_rsi_divergence": "賵丕诏乇丕蹖蹖 賲孬亘鬲 RSI",
        "bearish_rsi_divergence": "賵丕诏乇丕蹖蹖 賲賳賮蹖 RSI",
        "bullish_macd_divergence": "賵丕诏乇丕蹖蹖 賲孬亘鬲 MACD",
        "bearish_macd_divergence": "賵丕诏乇丕蹖蹖 賲賳賮蹖 MACD",

        "bullish_exhaustion": "禺爻鬲诏蹖 乇賵賳丿 氐毓賵丿蹖",
        "bearish_exhaustion": "禺爻鬲诏蹖 乇賵賳丿 賳夭賵賱蹖",

        "above_vwap": "亘丕賱丕蹖 VWAP",
        "below_vwap": "倬丕蹖蹖賳 VWAP",
        "near_vwap": "賳夭丿蹖讴 VWAP",

        "above_poc": "亘丕賱丕蹖 賳丕丨蹖賴 丨噩賲蹖 丕氐賱蹖",
        "below_poc": "倬丕蹖蹖賳 賳丕丨蹖賴 丨噩賲蹖 丕氐賱蹖",
        "near_poc": "賳夭丿蹖讴 賳丕丨蹖賴 丨噩賲蹖 丕氐賱蹖",
    }
    return data.get(value, value)


def build_trade_levels(result):
    if result.get("stop_loss") is None:
        return f"""
亘乇丕蹖 丕蹖賳 賵囟毓蹖鬲貙 賵乇賵丿 倬蹖卮賳賴丕丿 賳賲蹖鈥屫促堌�.

爻胤賵丨 丕丨鬲賲丕賱蹖 賮賯胤 亘乇丕蹖 亘乇乇爻蹖:
丨丿 囟乇乇 丕丨鬲賲丕賱蹖:
{safe(result.get('candidate_stop_loss'))}

丨丿 爻賵丿 1 丕丨鬲賲丕賱蹖:
{safe(result.get('candidate_tp1'))}

丨丿 爻賵丿 2 丕丨鬲賲丕賱蹖:
{safe(result.get('candidate_tp2'))}
"""

    return f"""
賵乇賵丿 鬲賯乇蹖亘蹖:
{result['price']}

丨丿 囟乇乇:
{result['stop_loss']}

丨丿 爻賵丿 1:
{result['tp1']}

丨丿 爻賵丿 2:
{result['tp2']}
"""


def build_analysis_text(result):
    reasons_text = "\n".join([f"鉁� {r}" for r in result.get("reasons", [])])
    trade_levels = build_trade_levels(result)

    return f"""
馃搳 鬲丨賱蹖賱 賮蹖賵趩乇夭 {result['symbol']}

賯蹖賲鬲 賮毓賱蹖:
{result['price']}

噩賴鬲 賳賴丕蹖蹖:
{fa_direction(result['direction'])}

噩賴鬲 禺丕賲 鬲丨賱蹖賱:
{fa_direction(result.get('raw_direction'))}

丕賲鬲蹖丕夭 爻蹖诏賳丕賱:
{result['score']}/100

丕丨鬲賲丕賱 賲賵賮賯蹖鬲 鬲賯乇蹖亘蹖:
{safe(result.get('win_probability'))}侏

诏乇蹖丿 賵乇賵丿:
{safe(result.get('entry_grade'))}

爻胤丨 乇蹖爻讴:
{safe(result.get('risk_level'))}

乇蹖爻讴 亘賴 乇蹖賵丕乇丿:
{safe(result.get('risk_reward'))}

乇蹖爻讴 賱蹖讴賵蹖蹖丿蹖鬲蹖:
{safe(result.get('liquidity_risk'))}

鈴� 丕毓鬲亘丕乇 爻蹖诏賳丕賱:
{result['validity']}

鈴� 鬲丕蹖賲鈥屬佖臂屬� 賲賳丕爻亘:
{result['signal_timeframe']}

丕賲鬲蹖丕夭 賱丕賳诏:
{result['long_score']}

丕賲鬲蹖丕夭 卮賵乇鬲:
{result['short_score']}

賯丿乇鬲 禺乇蹖丿:
{result['buy_power']}侏

賯丿乇鬲 賮乇賵卮:
{result['sell_power']}侏

RSI:
{result['rsi']}

ADX 賯丿乇鬲 乇賵賳丿:
{safe(result.get('adx'))}

MACD:
{result['macd']}

MACD Histogram:
{safe(result.get('macd_hist'))}

VWAP:
{safe(result.get('vwap'))}

賵囟毓蹖鬲 VWAP:
{fa_general(result.get('vwap_status'))}

POC 丨噩賲蹖:
{safe(result.get('poc_price'))}

賵囟毓蹖鬲 丨噩賲:
{fa_general(result.get('volume_profile_status'))}

Funding Rate:
{safe(result.get('funding_rate'))}侏

Open Interest:
{safe(result.get('open_interest'))}

Spread:
{safe(result.get('spread_percent'))}侏

BTC Filter:
{fa_general(result.get('btc_filter'))}

讴賳丿賱 鬲丕蹖蹖丿蹖:
{fa_general(result.get('candle_pattern'))}

鬲丕蹖蹖丿 趩賳丿 讴賳丿賱蹖:
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

丨賲丕蹖鬲:
{result['support']}

賲賯丕賵賲鬲:
{result['resistance']}

禺胤 乇賵賳丿:
{fa_general(result['trendline'])}

爻丕禺鬲丕乇 亘丕夭丕乇:
{fa_general(result['market_structure'])}

賵囟毓蹖鬲 亘乇蹖讴鈥屫з堌�:
{fa_general(result['breakout'])}

Fear & Greed:
{safe(result.get('fear_value'))} - {safe(result.get('fear_text'))}

BTC Dominance:
{safe(result.get('btc_dominance'))}侏

賵囟毓蹖鬲 丿丕賲蹖賳賳爻:
{safe(result.get('dominance_status'))}

Alt Season:
{safe(result.get('altseason_status'))}

馃幆 爻胤賵丨 賲毓丕賲賱賴:
{trade_levels}

丿賱丕蹖賱 鬲丨賱蹖賱:
{reasons_text}

鈿狅笍 丕蹖賳 鬲丨賱蹖賱 鬲囟賲蹖賳 爻賵丿 賳蹖爻鬲. 丨鬲賲丕賸 亘丕 丨丿 囟乇乇貙 丨噩賲 讴賲 賵 賲丿蹖乇蹖鬲 乇蹖爻讴 賵丕乇丿 卮賵.
"""


def send_analysis(message, symbol):
    bot.reply_to(message, f"鈴� 丿乇 丨丕賱 鬲丨賱蹖賱 {symbol} ...")

    try:
        result = analyze_symbol(symbol)
    except Exception as e:
        print("ANALYSIS ERROR:", str(e))
        bot.reply_to(message, f"鉂� 禺胤丕 丿乇 鬲丨賱蹖賱 {symbol}\n\n毓賱鬲 禺胤丕:\n{e}")
        return

    sent = bot.reply_to(message, build_analysis_text(result))
    remember_signal_result(sent, result)


def send_best_signals(message):
    bot.reply_to(message, "鈴� 丿乇 丨丕賱 丕爻讴賳 亘丕夭丕乇...")

    try:
        results = get_best_signals(limit=5)
    except Exception as e:
        print("BEST SIGNAL ERROR:", str(e))
        bot.reply_to(message, f"鉂� 禺胤丕 丿乇 丕爻讴賳 亘丕夭丕乇:\n{e}")
        return

    if not results:
        bot.reply_to(message, "賮毓賱丕賸 爻蹖诏賳丕賱 賲賳丕爻亘蹖 倬蹖丿丕 賳卮丿.")
        return

    msg = "馃弳 亘賴鬲乇蹖賳 爻蹖诏賳丕賱鈥屬囏й� 丕賱丕賳:\n\n"
    medals = ["馃", "馃", "馃", "4锔忊儯", "5锔忊儯"]

    for i, r in enumerate(results):
        direction_fa = "賱丕賳诏" if r["direction"] == "LONG" else "卮賵乇鬲"

        msg += f"""
{medals[i]} {r['symbol']}
噩賴鬲: {direction_fa}
丕賲鬲蹖丕夭: {r['score']}/100
丕丨鬲賲丕賱 賲賵賮賯蹖鬲: {safe(r.get('win_probability'))}侏
诏乇蹖丿: {safe(r.get('entry_grade'))}
乇蹖爻讴: {safe(r.get('risk_level'))}
R/R: {safe(r.get('risk_reward'))}
丕毓鬲亘丕乇: {r['validity']}
鬲丕蹖賲鈥屬佖臂屬�: {r['signal_timeframe']}
賯蹖賲鬲: {r['price']}
ADX: {safe(r.get('adx'))}
Spread: {safe(r.get('spread_percent'))}侏
Funding: {safe(r.get('funding_rate'))}侏
"""

    bot.reply_to(message, msg)


def send_auto_signal_to_all_users(result):
    direction_fa = "賱丕賳诏" if result["direction"] == "LONG" else "卮賵乇鬲"

    text = f"""
馃毃 爻蹖诏賳丕賱 禺賵丿讴丕乇 賯賵蹖

丕乇夭:
{result['symbol']}

噩賴鬲:
{direction_fa}

丕賲鬲蹖丕夭:
{result['score']}/100

丕丨鬲賲丕賱 賲賵賮賯蹖鬲:
{safe(result.get('win_probability'))}侏

诏乇蹖丿:
{safe(result.get('entry_grade'))}

乇蹖爻讴:
{safe(result.get('risk_level'))}

R/R:
{safe(result.get('risk_reward'))}

丕毓鬲亘丕乇 爻蹖诏賳丕賱:
{result['validity']}

鬲丕蹖賲鈥屬佖臂屬� 賲賳丕爻亘:
{result['signal_timeframe']}

賯蹖賲鬲:
{result['price']}

丨丿 囟乇乇:
{result['stop_loss']}

丨丿 爻賵丿 1:
{result['tp1']}

丨丿 爻賵丿 2:
{result['tp2']}

賯丿乇鬲 禺乇蹖丿:
{result['buy_power']}侏

賯丿乇鬲 賮乇賵卮:
{result['sell_power']}侏

ADX:
{safe(result.get('adx'))}

Funding:
{safe(result.get('funding_rate'))}侏

VWAP:
{fa_general(result.get('vwap_status'))}

FVG:
{fa_general(result.get('fvg'))}

Order Block:
{fa_general(result.get('order_block'))}

鈿狅笍 賲丿蹖乇蹖鬲 乇蹖爻讴 賮乇丕賲賵卮 賳卮賵丿.
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
        bot.reply_to(message, "鉀� 卮賲丕 賲噩丕夭 亘賴 丕爻鬲賮丕丿賴 丕夭 丕蹖賳 乇亘丕鬲 賳蹖爻鬲蹖丿.")
        return

    bot.reply_to(message, """
爻賱丕賲 馃憢

乇亘丕鬲 丿爻鬲蹖丕乇 賮蹖賵趩乇夭 讴乇蹖倬鬲賵 賮毓丕賱 丕爻鬲.

賲孬丕賱:
亘蹖鬲讴賵蹖賳
丕鬲乇蹖賵賲
鬲丨賱蹖賱 丿賵噩
爻蹖诏賳丕賱 爻賵賱丕賳丕
亘賴鬲乇蹖賳 爻蹖诏賳丕賱 丕賱丕賳

賯丕亘賱蹖鬲 夭蹖乇賳馗乇 诏乇賮鬲賳:
乇賵蹖 倬蹖丕賲 爻蹖诏賳丕賱 乇蹖倬賱丕蹖 讴賳 賵 亘賳賵蹖爻:
夭蹖乇 賳馗乇
蹖丕
夭蹖乇 賳馗乇 亘诏蹖乇

丌賲丕乇:
丌賲丕乇
丌賲丕乇 7 乇賵夭
丌賲丕乇 30 乇賵夭
丌賲丕乇 讴賱

丿爻鬲賵乇丕鬲 丕丿賲蹖賳:
/adduser 123456789
/removeuser 123456789
/listusers
""")


@bot.message_handler(commands=["adduser"])
def add_user_command(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "鉀� 賮賯胤 賲丕賱讴 乇亘丕鬲 賲蹖鈥屫堌з嗀� 讴丕乇亘乇 丕囟丕賮賴 讴賳丿.")
        return

    try:
        user_id = int(message.text.split()[1])
        add_user(user_id)
        bot.reply_to(message, f"鉁� 讴丕乇亘乇 {user_id} 丕囟丕賮賴 卮丿.")
    except Exception:
        bot.reply_to(message, "賮乇賲鬲 丿乇爻鬲:\n/adduser 123456789")


@bot.message_handler(commands=["removeuser"])
def remove_user_command(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "鉀� 賮賯胤 賲丕賱讴 乇亘丕鬲 賲蹖鈥屫堌з嗀� 讴丕乇亘乇 丨匕賮 讴賳丿.")
        return

    try:
        user_id = int(message.text.split()[1])
        ok = remove_user(user_id)

        if ok:
            bot.reply_to(message, f"鉁� 讴丕乇亘乇 {user_id} 丨匕賮 卮丿.")
        else:
            bot.reply_to(message, "鉂� 賲丕賱讴 丕氐賱蹖 賯丕亘賱 丨匕賮 賳蹖爻鬲 蹖丕 讴丕乇亘乇 賵噩賵丿 賳丿丕乇丿.")
    except Exception:
        bot.reply_to(message, "賮乇賲鬲 丿乇爻鬲:\n/removeuser 123456789")


@bot.message_handler(commands=["listusers"])
def list_users_command(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "鉀� 賮賯胤 賲丕賱讴 乇亘丕鬲 賲蹖鈥屫堌з嗀� 賱蹖爻鬲 讴丕乇亘乇丕賳 乇丕 亘亘蹖賳丿.")
        return

    users = list_users()
    users_text = "\n".join([str(u) for u in users])
    bot.reply_to(message, f"馃懃 讴丕乇亘乇丕賳 賲噩丕夭:\n{users_text}")


@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if not is_user_allowed(message.from_user.id):
        bot.reply_to(message, "鉀� 卮賲丕 賲噩丕夭 亘賴 丕爻鬲賮丕丿賴 丕夭 丕蹖賳 乇亘丕鬲 賳蹖爻鬲蹖丿.")
        return

    text = message.text.strip()

    if is_track_command(text):
        result = get_replied_signal_result(message)

        if not result:
            bot.reply_to(
                message,
                "鉂� 亘乇丕蹖 夭蹖乇 賳馗乇 诏乇賮鬲賳貙 亘丕蹖丿 乇賵蹖 倬蹖丕賲 鬲丨賱蹖賱 蹖丕 爻蹖诏賳丕賱 禺賵丿讴丕乇 乇蹖倬賱丕蹖 亘夭賳蹖.\n"
                "丕诏乇 乇亘丕鬲 乇蹖鈥屫ж池ж必� 卮丿賴 亘丕卮丿貙 賱胤賮丕賸 丿賵亘丕乇賴 賴賲丕賳 丕乇夭 乇丕 鬲丨賱蹖賱 亘诏蹖乇 賵 亘毓丿 乇蹖倬賱丕蹖 讴賳."
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

    if "亘賴鬲乇蹖賳 爻蹖诏賳丕賱" in text or "亘賴鬲乇蹖賳 賮乇氐鬲" in text:
        send_best_signals(message)
        return

    symbol = find_symbol(text)

    if not symbol:
        bot.reply_to(message, "丕乇夭 乇賵 賲鬲賵噩賴 賳卮丿賲. 賲孬賱丕 亘賳賵蹖爻: 亘蹖鬲讴賵蹖賳 蹖丕 丕鬲乇蹖賵賲")
        return

    send_analysis(message, symbol)


threading.Thread(target=auto_signal_loop, daemon=True).start()
threading.Thread(target=signal_tracking_loop, daemon=True).start()

print("Bot is running...")
bot.infinity_polling()
