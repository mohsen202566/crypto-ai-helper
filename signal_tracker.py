# -*- coding: utf-8 -*-
import json
import os
import re
import time
from datetime import datetime, timedelta

import ccxt

try:
    from paper_trader import open_paper_trade, close_paper_trade_by_signal
except Exception:
    open_paper_trade = None
    close_paper_trade_by_signal = None

ACTIVE_SIGNALS_FILE = "active_signals.json"
SIGNAL_STATS_FILE = "signal_stats.json"
TRACKER_OHLCV_TIMEFRAME = "1m"
TRACKER_LOOKBACK_BUFFER_SECONDS = 90
TRACKER_MAX_OHLCV_LIMIT = 180
SAME_CANDLE_HIT_POLICY = "SL_FIRST"

exchange = ccxt.okx({
    "enableRateLimit": True,
    "timeout": 20000,
    "options": {"defaultType": "swap"},
})


def to_okx_symbol(symbol):
    coin = str(symbol).upper().replace("USDT", "")
    return f"{coin}/USDT:USDT"


def now_ts():
    return int(time.time())


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_active_signals():
    return load_json(ACTIVE_SIGNALS_FILE, [])


def save_active_signals(signals):
    save_json(ACTIVE_SIGNALS_FILE, signals)


def get_signal_stats():
    return load_json(SIGNAL_STATS_FILE, [])


def save_signal_stats(stats):
    save_json(SIGNAL_STATS_FILE, stats)


def reset_stats():
    try:
        save_signal_stats([])
        return True
    except Exception:
        return False


def fa_direction(direction):
    return "لانگ" if direction == "LONG" else "شورت" if direction == "SHORT" else str(direction)


def has_active_symbol(active, user_id, symbol):
    for item in active:
        if int(item.get("user_id", 0)) == int(user_id) and item.get("symbol") == symbol and item.get("status") == "ACTIVE":
            return True
    return False


def can_add_automatic_signal(user_id, symbol):
    active = get_active_signals()
    if has_active_symbol(active, user_id, symbol):
        return False, "duplicate"
    return True, "ok"


def _signal_id(signal):
    return signal.get("signal_id") or signal.get("id") or f"{signal.get('symbol')}_{signal.get('message_id')}_{signal.get('created_at')}"


def record_stat_event(signal, event_type, exit_price=None, move_percent=None):
    stats = get_signal_stats()
    item = dict(signal)
    item["signal_id"] = _signal_id(signal)
    item["event_type"] = event_type
    item["status"] = event_type
    item["event_at"] = now_ts()
    item["event_at_text"] = now_text()
    if exit_price is not None:
        item["exit_price"] = exit_price
    if move_percent is not None:
        item["move_percent"] = move_percent
    stats.append(item)
    save_signal_stats(stats)


def try_open_paper_trade(signal):
    if not open_paper_trade:
        return None
    try:
        ok, msg = open_paper_trade(signal)
        return msg
    except Exception as e:
        return f"⚠️ خطا در باز کردن Paper Trade برای {signal.get('symbol')}\nعلت: {str(e)[:250]}"


def try_close_paper_trade(signal, result_type, exit_price):
    if not close_paper_trade_by_signal:
        return None
    try:
        ok, msg = close_paper_trade_by_signal(signal, result_type, exit_price)
        return msg
    except Exception as e:
        return f"⚠️ خطا در بستن Paper Trade برای {signal.get('symbol')}\nعلت: {str(e)[:250]}"


def add_signal_to_tracking(user_id, chat_id, message_id, result):
    if result.get("direction") not in ["LONG", "SHORT"]:
        return False, "این تحلیل سیگنال قابل پیگیری ندارد."
    if result.get("stop_loss") is None or result.get("tp1") is None:
        return False, "برای این سیگنال TP1 یا SL وجود ندارد."

    active = get_active_signals()
    if has_active_symbol(active, user_id, result.get("symbol")):
        return False, f"⚠️ {result.get('symbol')} از قبل زیر نظر است."

    signal_uid = f"{result['symbol']}_{message_id}_{now_ts()}"
    signal = {
        "id": signal_uid,
        "signal_id": signal_uid,
        "user_id": int(user_id),
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "symbol": result["symbol"],
        "direction": result["direction"],
        "status": "ACTIVE",
        "entry": float(result.get("entry") or result.get("price")),
        "price": float(result.get("price") or result.get("entry")),
        "stop_loss": float(result["stop_loss"]),
        "tp1": float(result["tp1"]),
        "tp2": None if result.get("tp2") is None else float(result["tp2"]),
        "score": result.get("score"),
        "risk_level": result.get("risk_level"),
        "risk_reward": result.get("risk_reward"),
        "entry_mode": "CLASSIC_TECHNICAL",
        "confirmations": result.get("confirmations"),
        "freshness": result.get("freshness"),
        "rsi": result.get("rsi"),
        "adx": result.get("adx"),
        "power2_buy": result.get("power2_buy"),
        "power2_sell": result.get("power2_sell"),
        "reasons": result.get("reasons", []),
        "created_at": now_ts(),
        "created_at_text": now_text(),
        "last_checked_at": now_ts(),
    }
    active.append(signal)
    save_active_signals(active)
    record_stat_event(signal, "SIGNAL_CREATED")

    paper_msg = try_open_paper_trade(signal)
    msg = (
        f"✅ سیگنال زیر نظر گرفته شد\n\n"
        f"ارز: {signal['symbol']}\n"
        f"جهت: {fa_direction(signal['direction'])}\n"
        f"ورود: {signal['entry']}\n"
        f"TP1: {signal['tp1']}\n"
        f"SL: {signal['stop_loss']}"
    )
    if paper_msg:
        msg += "\n\n" + paper_msg
    return True, msg


def get_recent_1m_candles_since(symbol, since_ts):
    since_ts = int(since_ts or now_ts() - 5 * 60)
    since_ms = max(0, (since_ts - TRACKER_LOOKBACK_BUFFER_SECONDS) * 1000)
    minutes = max(5, int((now_ts() - since_ts) / 60) + 4)
    limit = min(TRACKER_MAX_OHLCV_LIMIT, max(10, minutes))
    return exchange.fetch_ohlcv(to_okx_symbol(symbol), timeframe=TRACKER_OHLCV_TIMEFRAME, since=since_ms, limit=limit) or []


def candle_path_hit(signal, candle):
    high = float(candle[2]); low = float(candle[3])
    direction = signal.get("direction")
    tp1 = float(signal["tp1"]); sl = float(signal["stop_loss"])
    if direction == "LONG":
        tp_hit = high >= tp1; sl_hit = low <= sl
    elif direction == "SHORT":
        tp_hit = low <= tp1; sl_hit = high >= sl
    else:
        return None, None
    if tp_hit and sl_hit:
        return ("SL", sl) if SAME_CANDLE_HIT_POLICY == "SL_FIRST" else ("TP1", tp1)
    if tp_hit:
        return "TP1", tp1
    if sl_hit:
        return "SL", sl
    return None, None


def move_percent(signal, exit_price):
    entry = float(signal.get("entry") or 0)
    if entry <= 0:
        return 0.0
    if signal.get("direction") == "LONG":
        return round(((float(exit_price) - entry) / entry) * 100, 4)
    return round(((entry - float(exit_price)) / entry) * 100, 4)


def check_active_signals():
    active = get_active_signals()
    remaining = []
    messages = []
    for signal in active:
        if signal.get("status") != "ACTIVE":
            continue
        try:
            hit_type = None; exit_price = None
            candles = get_recent_1m_candles_since(signal["symbol"], signal.get("last_checked_at") or signal.get("created_at"))
            for candle in candles:
                hit_type, exit_price = candle_path_hit(signal, candle)
                if hit_type:
                    break
            signal["last_checked_at"] = now_ts()
            if hit_type:
                pct = move_percent(signal, exit_price)
                record_stat_event(signal, hit_type, exit_price, pct)
                paper_msg = try_close_paper_trade(signal, hit_type, exit_price)
                icon = "✅" if hit_type == "TP1" else "❌"
                text = (
                    f"{icon} نتیجه سیگنال {signal.get('symbol')}\n"
                    f"جهت: {fa_direction(signal.get('direction'))}\n"
                    f"ورود: {signal.get('entry')}\n"
                    f"قیمت خروج: {exit_price}\n"
                    f"نتیجه: {'حد سود 1' if hit_type == 'TP1' else 'حد ضرر'}\n"
                    f"درصد حرکت: {pct}٪"
                )
                if paper_msg:
                    text += "\n\n" + paper_msg
                messages.append({"chat_id": signal["chat_id"], "message": text, "reply_to_message_id": signal.get("message_id")})
            else:
                remaining.append(signal)
        except Exception as e:
            signal["last_checked_at"] = now_ts()
            signal["last_error"] = str(e)[:250]
            remaining.append(signal)
    save_active_signals(remaining)
    return messages


def parse_days_from_text(text):
    m = re.search(r"(\d+)", text or "")
    if m:
        return int(m.group(1))
    if text and "کل" in text:
        return 3650
    return 7


def parse_days_from_report_text(text):
    return parse_days_from_text(text or "")


def parse_profit_calc_text(text):
    if not text:
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", text)
    if "سود" in text or "محاسبه" in text or "درآمد" in text:
        if len(nums) >= 2:
            return float(nums[0]), float(nums[1])
    return None


def get_profit_for_signal_text(reply_text, margin, leverage):
    return None


def get_profit_simulation_report(margin, leverage, days=7):
    stats = get_signal_stats()
    since = now_ts() - int(days) * 86400
    closed = [s for s in stats if int(s.get("event_at", 0)) >= since and s.get("event_type") in ["TP1", "SL"]]
    wins = len([s for s in closed if s.get("event_type") == "TP1"])
    losses = len([s for s in closed if s.get("event_type") == "SL"])
    return f"📊 شبیه‌سازی سود {days} روز\nTP1: {wins}\nSL: {losses}\nمارجین: {margin}$\nلوریج: {leverage}x"


def get_stats_report(days=7):
    stats = get_signal_stats()
    since = now_ts() - int(days) * 86400
    data = [s for s in stats if int(s.get("event_at", s.get("created_at", 0)) or 0) >= since]
    created = [s for s in data if s.get("event_type") == "SIGNAL_CREATED"]
    tp1 = [s for s in data if s.get("event_type") == "TP1"]
    sl = [s for s in data if s.get("event_type") == "SL"]
    total = len(tp1) + len(sl)
    win_rate = round((len(tp1) / total) * 100, 1) if total else 0
    active_count = len(get_active_signals())
    longs = [s for s in tp1 + sl if s.get("direction") == "LONG"]
    shorts = [s for s in tp1 + sl if s.get("direction") == "SHORT"]
    long_tp = len([s for s in longs if s.get("event_type") == "TP1"])
    short_tp = len([s for s in shorts if s.get("event_type") == "TP1"])
    return (
        f"📊 آمار {days} روز اخیر\n\n"
        f"سیگنال مستقیم صادرشده: {len(created)}\n"
        f"معاملات فعال باز: {active_count}\n"
        f"--------------------\n"
        f"TP1: {len(tp1)}\n"
        f"SL: {len(sl)}\n"
        f"Win Rate: {win_rate}%\n"
        f"--------------------\n"
        f"لانگ: {len(longs)} | TP1: {long_tp}\n"
        f"شورت: {len(shorts)} | TP1: {short_tp}\n"
        f"\nمعماری: CLASSIC_TECHNICAL"
    )
