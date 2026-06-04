# -*- coding: utf-8 -*-
import json
import os
import re
import time
from datetime import datetime

import ccxt

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


def reset_stats():
    """
    فقط آمار سیگنال‌های بسته‌شده را پاک می‌کند.
    سیگنال‌های فعال زیرنظر، کاربران و تنظیمات ربات حذف نمی‌شوند.
    """
    try:
        save_signal_stats([])
        return True
    except Exception as e:
        print("RESET STATS ERROR:", str(e))
        return False


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
        "buy_power": result.get("buy_power"),
        "sell_power": result.get("sell_power"),
        "adx": result.get("adx"),
        "rsi": result.get("rsi"),
        "vwap_status": result.get("vwap_status"),
        "order_block": result.get("order_block"),
        "fvg": result.get("fvg"),
        "candle_pattern": result.get("candle_pattern"),
        "multi_candle": result.get("multi_candle"),
        "market_structure": result.get("market_structure"),
        "trendline": result.get("trendline"),
        "breakout": result.get("breakout"),
        "rsi_divergence": result.get("rsi_divergence"),
        "macd_divergence": result.get("macd_divergence"),
        "fake_breakout": result.get("fake_breakout"),
        "trend_exhaustion": result.get("trend_exhaustion"),
        "created_at": now_ts(),
        "created_at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "ACTIVE",
    }
    active.append(signal)
    save_active_signals(active)
    return True, f"✅ سیگنال {signal['symbol']} زیر نظر گرفته شد."


def price_hit_tp1(signal, price):
    if signal["direction"] == "LONG":
        return price >= signal["tp1"]
    if signal["direction"] == "SHORT":
        return price <= signal["tp1"]
    return False


def price_hit_sl(signal, price):
    if signal["direction"] == "LONG":
        return price <= signal["stop_loss"]
    if signal["direction"] == "SHORT":
        return price >= signal["stop_loss"]
    return False


def calculate_result_percent(signal, exit_price):
    entry = float(signal["entry"])
    if entry == 0:
        return 0
    if signal["direction"] == "LONG":
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


def check_active_signals():
    active = get_active_signals()
    remaining = []
    messages = []

    for signal in active:
        try:
            price = get_current_price(signal["symbol"])
            if price_hit_tp1(signal, price):
                messages.append({"chat_id": signal["chat_id"], "message": close_signal(signal, "TP1", price)})
                continue
            if price_hit_sl(signal, price):
                messages.append({"chat_id": signal["chat_id"], "message": close_signal(signal, "SL", price)})
                continue
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
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 7


def get_stats_report(days=None):
    stats = get_signal_stats()
    if days is not None:
        start = now_ts() - days * 24 * 60 * 60
        stats = [s for s in stats if s.get("closed_at", 0) >= start]

    total = len(stats)
    if total == 0:
        return "📊 هنوز هیچ سیگنال بسته‌شده‌ای برای آمار وجود ندارد."

    wins = [s for s in stats if s.get("status") == "TP1"]
    losses = [s for s in stats if s.get("status") == "SL"]
    win_rate = round(len(wins) / total * 100, 1)

    avg_win = round(sum(float(s.get("result_percent", 0)) for s in wins) / len(wins), 3) if wins else 0
    avg_loss = round(abs(sum(float(s.get("result_percent", 0)) for s in losses) / len(losses)), 3) if losses else 0

    def dir_report(direction):
        items = [s for s in stats if s.get("direction") == direction]
        if not items:
            return "0 سیگنال | برد: 0 | باخت: 0 | Win Rate: 0٪"
        w = len([s for s in items if s.get("status") == "TP1"])
        l = len([s for s in items if s.get("status") == "SL"])
        wr = round(w / len(items) * 100, 1)
        return f"{len(items)} سیگنال | برد: {w} | باخت: {l} | Win Rate: {wr}٪"

    period = "کل" if days is None else f"{days} روز اخیر"
    out = (
        f"📊 آمار {period}\n\n"
        f"کل سیگنال‌های زیرنظرگرفته‌شده: {total}\n"
        f"✅ TP1: {len(wins)}\n"
        f"❌ SL: {len(losses)}\n"
        f"Win Rate: {win_rate}٪\n"
        f"میانگین برد: {avg_win}٪\n"
        f"میانگین باخت: {avg_loss}٪\n\n"
        f"لانگ: {dir_report('LONG')}\n"
        f"شورت: {dir_report('SHORT')}\n"
    )
    return out


def normalize_number_text(text):
    mapping = {
        "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
        "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
        "٫": ".", ",": "."
    }
    for a, b in mapping.items():
        text = text.replace(a, b)
    return text


def parse_profit_calc_text(text):
    if not text:
        return None
    clean = normalize_number_text(text.lower()).replace("$", " دلار ").replace("x", " لوریج ")
    if "لوریج" not in clean and "دلار" not in clean:
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", clean)
    if len(nums) < 2:
        return None
    margin = float(nums[0])
    leverage = float(nums[1])
    if margin <= 0 or leverage <= 0:
        return None
    return margin, leverage


def extract_number_after_labels(text, labels):
    text = normalize_number_text(text)
    for label in labels:
        m = re.search(rf"{label}\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)", text, flags=re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def format_money(value):
    sign = "+" if value > 0 else ""
    return f"{sign}{round(value, 4)}$"


def calc_percent(direction, entry, level):
    if direction == "LONG":
        return ((level - entry) / entry) * 100
    if direction == "SHORT":
        return ((entry - level) / entry) * 100
    return None


def get_profit_for_signal_text(reply_text, margin, leverage):
    if not reply_text:
        return None

    text = normalize_number_text(reply_text)
    symbol_match = re.search(r"([A-Z0-9]+USDT)", text)
    symbol = symbol_match.group(1) if symbol_match else "نامشخص"

    direction = None
    if "شورت" in text or "SHORT" in text:
        direction = "SHORT"
    elif "لانگ" in text or "LONG" in text:
        direction = "LONG"

    entry = extract_number_after_labels(text, ["ورود تقریبی", "ورود", "قیمت فعلی", "قیمت"])
    tp1 = extract_number_after_labels(text, ["حد سود 1", "TP1", "تیپی 1", "تی پی 1"])
    tp2 = extract_number_after_labels(text, ["حد سود 2", "TP2", "تیپی 2", "تی پی 2"])
    sl = extract_number_after_labels(text, ["حد ضرر", "SL", "استاپ"])

    if not direction or entry is None or (tp1 is None and tp2 is None and sl is None):
        return None

    lines = [
        "💰 محاسبه سود و ضرر معامله",
        f"ارز: {symbol}",
        f"جهت: {'لانگ' if direction == 'LONG' else 'شورت'}",
        f"سرمایه: {margin}$",
        f"لوریج: {leverage}x",
        f"ورود: {entry}",
        "",
    ]

    for title, level in [("TP1", tp1), ("TP2", tp2), ("SL", sl)]:
        if level is None:
            continue
        pct = calc_percent(direction, entry, level)
        pnl = margin * leverage * (pct / 100)
        label = "سود" if pnl >= 0 else "ضرر"
        lines += [
            f"{title}: {level}",
            f"درصد حرکت: {round(pct, 3)}٪",
            f"{label} تقریبی {title}: {format_money(pnl)}",
            ""
        ]

    return "\n".join(lines).strip()


def parse_days_from_report_text(reply_text):
    if not reply_text:
        return 7
    if "کل" in reply_text:
        return None
    m = re.search(r"آمار\s+(\d+)", normalize_number_text(reply_text))
    return int(m.group(1)) if m else 7


def get_profit_simulation_report(margin, leverage, days=None):
    stats = get_signal_stats()
    if days is not None:
        start = now_ts() - days * 24 * 60 * 60
        stats = [s for s in stats if s.get("closed_at", 0) >= start]
    if not stats:
        return "برای محاسبه سود/ضرر، هنوز آمار بسته‌شده‌ای وجود ندارد."

    total_pnl = 0
    wins = 0
    losses = 0
    for s in stats:
        pct = float(s.get("result_percent", 0))
        pnl = margin * leverage * (pct / 100)
        total_pnl += pnl
        if pnl >= 0:
            wins += 1
        else:
            losses += 1

    period = "کل" if days is None else f"{days} روز اخیر"
    return (
        f"💰 شبیه‌سازی سود/ضرر آمار {period}\n\n"
        f"سرمایه هر معامله: {margin}$\n"
        f"لوریج: {leverage}x\n"
        f"تعداد معاملات: {len(stats)}\n"
        f"بردها: {wins}\n"
        f"باخت‌ها: {losses}\n"
        f"سود/ضرر خالص تقریبی: {format_money(total_pnl)}"
    )
