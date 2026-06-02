# -*- coding: utf-8 -*-
import json
import os
import time
from datetime import datetime, timedelta

import ccxt
from analysis import analyze_symbol


ACTIVE_SIGNALS_FILE = "active_signals.json"
SIGNAL_STATS_FILE = "signal_stats.json"

exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"}
})


def to_okx_symbol(symbol):
    coin = symbol.replace("USDT", "")
    return f"{coin}/USDT:USDT"


def now_ts():
    return int(time.time())


def load_json(path, default):
    if not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_current_price(symbol):
    ticker = exchange.fetch_ticker(to_okx_symbol(symbol))
    price = ticker.get("last") or ticker.get("close")

    if price is None:
        raise Exception(f"قیمت {symbol} دریافت نشد")

    return float(price)


def get_active_signals():
    return load_json(ACTIVE_SIGNALS_FILE, [])


def save_active_signals(signals):
    save_json(ACTIVE_SIGNALS_FILE, signals)


def get_signal_stats():
    return load_json(SIGNAL_STATS_FILE, [])


def save_signal_stats(stats):
    save_json(SIGNAL_STATS_FILE, stats)


def add_signal_to_tracking(user_id, chat_id, message_id, result):
    if result.get("direction") == "NO TRADE":
        return False, "این تحلیل سیگنال قابل پیگیری ندارد."

    if result.get("stop_loss") is None or result.get("tp1") is None:
        return False, "برای این سیگنال TP1 یا SL وجود ندارد."

    active = get_active_signals()

    signal = {
        "id": f"{result['symbol']}_{message_id}_{now_ts()}",
        "user_id": int(user_id),
        "chat_id": int(chat_id),
        "message_id": int(message_id),

        "symbol": result["symbol"],
        "direction": result["direction"],

        "entry": float(result["price"]),
        "stop_loss": float(result["stop_loss"]),
        "tp1": float(result["tp1"]),
        "tp2": None if result.get("tp2") is None else float(result["tp2"]),

        "score": result.get("score"),
        "win_probability": result.get("win_probability"),
        "entry_grade": result.get("entry_grade"),
        "risk_level": result.get("risk_level"),
        "risk_reward": result.get("risk_reward"),

        "created_at": now_ts(),
        "created_at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),

        "status": "ACTIVE",
        "warning_sent": False
    }

    active.append(signal)
    save_active_signals(active)

    return True, f"✅ سیگنال {signal['symbol']} زیر نظر گرفته شد."


def price_hit_tp1(signal, price):
    direction = signal["direction"]

    if direction == "LONG":
        return price >= signal["tp1"]

    if direction == "SHORT":
        return price <= signal["tp1"]

    return False


def price_hit_sl(signal, price):
    direction = signal["direction"]

    if direction == "LONG":
        return price <= signal["stop_loss"]

    if direction == "SHORT":
        return price >= signal["stop_loss"]

    return False


def calculate_result_percent(signal, exit_price):
    entry = float(signal["entry"])
    direction = signal["direction"]

    if entry == 0:
        return 0

    if direction == "LONG":
        percent = ((exit_price - entry) / entry) * 100
    else:
        percent = ((entry - exit_price) / entry) * 100

    return round(percent, 3)


def close_signal(signal, result_type, exit_price):
    stats = get_signal_stats()

    closed = dict(signal)
    closed["status"] = result_type
    closed["exit_price"] = float(exit_price)
    closed["closed_at"] = now_ts()
    closed["closed_at_text"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    closed["result_percent"] = calculate_result_percent(signal, exit_price)

    stats.append(closed)
    save_signal_stats(stats)

    if result_type == "TP1":
        return (
            f"✅ نتیجه سیگنال {signal['symbol']}\n\n"
            f"جهت: {'لانگ' if signal['direction'] == 'LONG' else 'شورت'}\n"
            f"ورود: {signal['entry']}\n"
            f"TP1: {signal['tp1']}\n"
            f"قیمت خروج: {exit_price}\n"
            f"نتیجه: موفق ✅\n"
            f"درصد حرکت: {closed['result_percent']}٪"
        )

    return (
        f"❌ نتیجه سیگنال {signal['symbol']}\n\n"
        f"جهت: {'لانگ' if signal['direction'] == 'LONG' else 'شورت'}\n"
        f"ورود: {signal['entry']}\n"
        f"SL: {signal['stop_loss']}\n"
        f"قیمت خروج: {exit_price}\n"
        f"نتیجه: حد ضرر ❌\n"
        f"درصد حرکت: {closed['result_percent']}٪"
    )



def weakness_warning_for_signal(signal, result, price):
    direction = signal.get("direction")
    warnings = []

    if direction == "LONG":
        if result.get("raw_direction") == "SHORT" or result.get("direction") == "SHORT":
            warnings.append("جهت تحلیل جدید به شورت تغییر کرده است")
        if result.get("vwap_status") == "below_vwap":
            warnings.append("قیمت زیر VWAP رفته است")
        if result.get("sell_power", 0) >= result.get("buy_power", 0) + 12:
            warnings.append("قدرت فروش نسبت به خرید بیشتر شده است")
        if result.get("market_structure") == "bearish_structure":
            warnings.append("ساختار کوتاه‌مدت نزولی شده است")
        if result.get("rsi_divergence") == "bearish_rsi_divergence":
            warnings.append("واگرایی منفی RSI دیده شده است")
        if result.get("macd_divergence") == "bearish_macd_divergence":
            warnings.append("واگرایی منفی MACD دیده شده است")
        if result.get("fake_breakout") == "fake_bullish_breakout":
            warnings.append("احتمال فیک بریک‌اوت صعودی وجود دارد")

    elif direction == "SHORT":
        if result.get("raw_direction") == "LONG" or result.get("direction") == "LONG":
            warnings.append("جهت تحلیل جدید به لانگ تغییر کرده است")
        if result.get("vwap_status") == "above_vwap":
            warnings.append("قیمت بالای VWAP رفته است")
        if result.get("buy_power", 0) >= result.get("sell_power", 0) + 12:
            warnings.append("قدرت خرید نسبت به فروش بیشتر شده است")
        if result.get("market_structure") == "bullish_structure":
            warnings.append("ساختار کوتاه‌مدت صعودی شده است")
        if result.get("rsi_divergence") == "bullish_rsi_divergence":
            warnings.append("واگرایی مثبت RSI دیده شده است")
        if result.get("macd_divergence") == "bullish_macd_divergence":
            warnings.append("واگرایی مثبت MACD دیده شده است")
        if result.get("fake_breakout") == "fake_bearish_breakout":
            warnings.append("احتمال فیک بریک‌اوت نزولی وجود دارد")

    if len(warnings) >= 3:
        text = "\n".join([f"⚠️ {w}" for w in warnings[:5]])
        return (
            f"⚠️ هشدار ضعف سیگنال {signal['symbol']}\n\n"
            f"جهت سیگنال: {'لانگ' if direction == 'LONG' else 'شورت'}\n"
            f"ورود: {signal['entry']}\n"
            f"قیمت فعلی: {price}\n\n"
            f"{text}\n\n"
            f"ریسک معامله بالا رفته؛ بستن معامله یا کاهش ریسک را بررسی کن."
        )

    return None


def check_active_signals():
    active = get_active_signals()
    remaining = []
    messages = []

    for signal in active:
        try:
            price = get_current_price(signal["symbol"])

            if price_hit_tp1(signal, price):
                msg = close_signal(signal, "TP1", price)
                messages.append({
                    "chat_id": signal["chat_id"],
                    "message": msg
                })
                continue

            if price_hit_sl(signal, price):
                msg = close_signal(signal, "SL", price)
                messages.append({
                    "chat_id": signal["chat_id"],
                    "message": msg
                })
                continue

            if not signal.get("warning_sent", False):
                try:
                    result = analyze_symbol(signal["symbol"])
                    warning_msg = weakness_warning_for_signal(signal, result, price)

                    if warning_msg:
                        signal["warning_sent"] = True
                        messages.append({
                            "chat_id": signal["chat_id"],
                            "message": warning_msg
                        })

                except Exception as e:
                    print("WARNING CHECK ERROR:", signal.get("symbol"), str(e))

            remaining.append(signal)

        except Exception as e:
            print("TRACK SIGNAL ERROR:", signal.get("symbol"), str(e))
            remaining.append(signal)

    save_active_signals(remaining)
    return messages


def parse_days_from_text(text):
    text = text.strip()

    if "کل" in text:
        return None

    digits = ""
    for ch in text:
        if ch.isdigit():
            digits += ch

    if digits:
        return int(digits)

    return 7


def get_stats_report(days=None):
    stats = get_signal_stats()

    if days is not None:
        start_ts = now_ts() - (days * 24 * 60 * 60)
        stats = [s for s in stats if s.get("closed_at", 0) >= start_ts]

    total = len(stats)

    if total == 0:
        if days is None:
            return "📊 هنوز هیچ سیگنال بسته‌شده‌ای در آمار کل وجود ندارد."
        return f"📊 در {days} روز اخیر هیچ سیگنال بسته‌شده‌ای وجود ندارد."

    tp1_count = len([s for s in stats if s.get("status") == "TP1"])
    sl_count = len([s for s in stats if s.get("status") == "SL"])

    win_rate = round((tp1_count / total) * 100, 1)

    long_stats = [s for s in stats if s.get("direction") == "LONG"]
    short_stats = [s for s in stats if s.get("direction") == "SHORT"]

    def direction_report(items):
        if not items:
            return "0 سیگنال | برد: 0 | باخت: 0 | Win Rate: 0٪"

        wins = len([x for x in items if x.get("status") == "TP1"])
        losses = len([x for x in items if x.get("status") == "SL"])
        wr = round((wins / len(items)) * 100, 1)

        return f"{len(items)} سیگنال | برد: {wins} | باخت: {losses} | Win Rate: {wr}٪"

    symbols = {}

    for s in stats:
        sym = s.get("symbol")
        if sym not in symbols:
            symbols[sym] = {"total": 0, "wins": 0, "losses": 0}

        symbols[sym]["total"] += 1

        if s.get("status") == "TP1":
            symbols[sym]["wins"] += 1
        elif s.get("status") == "SL":
            symbols[sym]["losses"] += 1

    sorted_symbols = sorted(
        symbols.items(),
        key=lambda x: (x[1]["wins"], x[1]["total"]),
        reverse=True
    )

    top_symbols_text = ""

    for sym, data in sorted_symbols[:5]:
        wr = round((data["wins"] / data["total"]) * 100, 1)
        top_symbols_text += f"\n{sym}: {data['wins']}/{data['total']} برد | {wr}٪"

    title = "آمار کل" if days is None else f"آمار {days} روز اخیر"

    return f"""
📊 {title}

کل سیگنال‌های زیرنظرگرفته‌شده:
{total}

✅ TP1:
{tp1_count}

❌ SL:
{sl_count}

Win Rate:
{win_rate}٪

لانگ:
{direction_report(long_stats)}

شورت:
{direction_report(short_stats)}

عملکرد ارزها:
{top_symbols_text}
"""
