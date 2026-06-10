# -*- coding: utf-8 -*-
import json
import os
import re
import time
from datetime import datetime, timedelta

import ccxt
from analysis import analyze_symbol
from config import PENDING_SETUP_TIMEOUT_MINUTES, ENTRY_ACTIVATION_PRICE_TOLERANCE_ATR
try:
    from diagnostics import log_exception
except Exception:
    def log_exception(section, exc, file_name=None, function_name=None, symbol=None):
        print(section, str(exc)); return str(exc)


ACTIVE_SIGNALS_FILE = "active_signals.json"
SIGNAL_STATS_FILE = "signal_stats.json"

# Tracker برای اسکالپ نباید فقط Last Price را ببیند.
# با کندل 1m مسیر قیمت از آخرین چک بررسی می‌شود تا لمس سریع TP/SL جا نیفتد.
TRACKER_OHLCV_TIMEFRAME = "1m"
TRACKER_LOOKBACK_BUFFER_SECONDS = 90
TRACKER_MAX_OHLCV_LIMIT = 180

# اگر داخل یک کندل هم TP و هم SL لمس شده باشد، ترتیب واقعی مشخص نیست.
# حالت محافظه‌کارانه: SL اولویت دارد تا آمار بیش از حد خوش‌بینانه نشود.
SAME_CANDLE_HIT_POLICY = "SL_FIRST"

exchange = ccxt.okx({
    "enableRateLimit": True,
    "timeout": 20000,
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


def get_recent_1m_candles_since(symbol, since_ts):
    """
    کندل‌های 1 دقیقه‌ای از آخرین زمان بررسی تا الان را می‌گیرد.
    خروجی ccxt: [timestamp_ms, open, high, low, close, volume]
    """
    now = now_ts()

    try:
        since_ts = int(since_ts or 0)
    except Exception:
        since_ts = 0

    if since_ts <= 0:
        since_ts = now - 5 * 60

    since_ms = max(0, (since_ts - TRACKER_LOOKBACK_BUFFER_SECONDS) * 1000)
    minutes = max(5, int((now - since_ts) / 60) + 4)
    limit = min(TRACKER_MAX_OHLCV_LIMIT, max(10, minutes))

    candles = exchange.fetch_ohlcv(
        to_okx_symbol(symbol),
        timeframe=TRACKER_OHLCV_TIMEFRAME,
        since=since_ms,
        limit=limit
    )

    clean = []
    min_allowed_ms = max(0, (since_ts - TRACKER_LOOKBACK_BUFFER_SECONDS) * 1000)

    for c in candles or []:
        if not c or len(c) < 5:
            continue
        if int(c[0]) >= min_allowed_ms:
            clean.append(c)

    return clean


def candle_path_hit(signal, candle):
    """
    قبل از TP1: مسیر کندل برای TP1 یا SL بررسی می‌شود.
    بعد از TP1: فقط TP2 بررسی می‌شود تا Win Rate با TP2 قاطی نشود.
    """
    direction = signal.get("direction")
    high = float(candle[2])
    low = float(candle[3])

    if signal.get("tp1_hit"):
        tp2 = signal.get("tp2")
        if tp2 is None:
            return None, None, None
        tp2 = float(tp2)
        if direction == "LONG" and high >= tp2:
            return "TP2", tp2, "candle_path"
        if direction == "SHORT" and low <= tp2:
            return "TP2", tp2, "candle_path"
        return None, None, None

    tp_hit = False
    sl_hit = False

    if direction == "LONG":
        tp_hit = high >= float(signal["tp1"])
        sl_hit = low <= float(signal["stop_loss"])
    elif direction == "SHORT":
        tp_hit = low <= float(signal["tp1"])
        sl_hit = high >= float(signal["stop_loss"])

    if tp_hit and sl_hit:
        if SAME_CANDLE_HIT_POLICY == "TP_FIRST":
            return "TP1", float(signal["tp1"]), "same_candle"
        return "SL", float(signal["stop_loss"]), "same_candle"

    if tp_hit:
        return "TP1", float(signal["tp1"]), "candle_path"

    if sl_hit:
        return "SL", float(signal["stop_loss"]), "candle_path"

    return None, None, None

def detect_signal_hit_from_candles(signal):
    last_checked_at = signal.get("last_checked_at") or signal.get("created_at") or now_ts() - 5 * 60
    candles = get_recent_1m_candles_since(signal["symbol"], last_checked_at)

    last_candle_ts = None

    for candle in candles:
        last_candle_ts = int(candle[0] / 1000)
        result_type, exit_price, hit_source = candle_path_hit(signal, candle)
        if result_type:
            signal["last_checked_at"] = now_ts()
            signal["last_checked_at_text"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            signal["hit_source"] = hit_source
            signal["hit_candle_time"] = datetime.fromtimestamp(last_candle_ts).strftime("%Y-%m-%d %H:%M:%S")
            return result_type, exit_price

    # حتی اگر کندل جدیدی نبود، زمان چک آپدیت شود تا loop گیر نکند.
    signal["last_checked_at"] = now_ts()
    signal["last_checked_at_text"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return None, None


def get_last_close_from_1m_or_ticker(symbol, signal=None):
    try:
        last_checked_at = None
        if signal:
            last_checked_at = signal.get("last_checked_at") or signal.get("created_at")
        candles = get_recent_1m_candles_since(symbol, last_checked_at or now_ts() - 3 * 60)
        if candles:
            return float(candles[-1][4])
    except Exception:
        pass

    return get_current_price(symbol)


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
    except Exception as e:
        print("RESET STATS ERROR:", str(e))
        return False



def has_active_or_pending_symbol(active, user_id, symbol):
    for item in active:
        if int(item.get("user_id", 0)) == int(user_id) and item.get("symbol") == symbol:
            if item.get("status") in ["ACTIVE", "PENDING_ACTIVATION"]:
                return True
    return False

def price_in_entry_zone(signal, price):
    try:
        price = float(price)
        atr = float(signal.get("atr") or 0)
        entry = float(signal.get("entry") or 0)
        zone_low = signal.get("entry_zone_low")
        zone_high = signal.get("entry_zone_high")
        if zone_low is not None and zone_high is not None:
            return float(zone_low) <= price <= float(zone_high)
        tolerance = abs(atr) * float(ENTRY_ACTIVATION_PRICE_TOLERANCE_ATR or 0.30)
        if tolerance <= 0:
            tolerance = abs(entry) * 0.002
        return abs(price - entry) <= tolerance
    except Exception:
        return False

def activate_pending_signal(signal, price):
    signal["status"] = "ACTIVE"
    signal["activated_at"] = now_ts()
    signal["activated_at_text"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    signal["activated_price"] = float(price)
    signal["last_checked_at"] = now_ts()
    record_stat_event(signal, "ACTIVATED")
    return signal

def close_pending_setup(signal, reason):
    signal["cancel_reason"] = reason
    signal["closed_at"] = now_ts()
    signal["closed_at_text"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record_stat_event(signal, "CANCELLED", extra={"cancel_reason": reason})
    return f"🚫 ستاپ {signal.get('symbol')} لغو شد\n\nجهت: {fa_direction(signal.get('direction'))}\nعلت: {reason}"

def add_signal_to_tracking(user_id, chat_id, message_id, result):
    if result.get("direction") == "NO TRADE":
        return False, "این تحلیل سیگنال قابل پیگیری ندارد."

    if result.get("stop_loss") is None or result.get("tp1") is None:
        return False, "برای این سیگنال TP1 یا SL وجود ندارد."

    active = get_active_signals()

    if has_active_or_pending_symbol(active, user_id, result.get("symbol")):
        return False, f"⚠️ {result.get('symbol')} از قبل زیر نظر یا در انتظار فعال‌سازی است."

    entry_confirmed = bool(result.get("entry_confirmed"))
    initial_status = "ACTIVE" if entry_confirmed else "PENDING_ACTIVATION"

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
        "atr": result.get("atr"),
        "entry_confirmed": entry_confirmed,
        "entry_status": result.get("entry_status"),
        "entry_zone_low": result.get("entry_zone_low"),
        "entry_zone_high": result.get("entry_zone_high"),
        "setup_score": result.get("setup_score"),
        "setup_reasons": result.get("setup_reasons", []),
        "compression_active": result.get("compression_active"),
        "compression_label": result.get("compression_label"),

        "score": result.get("score"),
        "win_probability": result.get("win_probability"),
        "entry_grade": result.get("entry_grade"),
        "risk_level": result.get("risk_level"),
        "risk_reward": result.get("risk_reward"),
        "freshness": result.get("freshness"),
        "entry_mode": result.get("entry_mode"),
        "predictive_confirmations": result.get("predictive_confirmations"),
        "power2_buy": result.get("power2_buy"),
        "power2_sell": result.get("power2_sell"),
        "power3_buy": result.get("power3_buy"),
        "power3_sell": result.get("power3_sell"),
        "power_acceleration": result.get("power_acceleration"),

        "market_regime": result.get("market_regime"),
        "market_regime_text": result.get("market_regime_text"),
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
        "macd_hist": result.get("macd_hist"),
        "market_regime": result.get("market_regime"),
        "market_regime_label": result.get("market_regime_label"),
        "market_breadth_status": result.get("market_breadth_status"),
        "market_breadth_label": result.get("market_breadth_label"),
        "market_breadth_bullish_pct": result.get("market_breadth_bullish_pct"),
        "market_breadth_bearish_pct": result.get("market_breadth_bearish_pct"),
        "fake_breakout": result.get("fake_breakout"),
        "trend_exhaustion": result.get("trend_exhaustion"),

        # Hidden professional diagnostics from analysis.py.
        # These are NOT shown in signal text, but are saved for SL analysis/statistics.
        "late_entry": result.get("late_entry"),
        "late_entry_reason": result.get("late_entry_reason"),
        "tp_space_ok": result.get("tp_space_ok"),
        "tp_space_reason": result.get("tp_space_reason"),
        "tp_space_atr": result.get("tp_space_atr"),
        "trap_risk": result.get("trap_risk"),
        "trap_reason": result.get("trap_reason"),
        "candle_forecast": result.get("candle_forecast"),
        "candle_forecast_reason": result.get("candle_forecast_reason"),

        "reasons": result.get("reasons", []),

        "warning_reasons": [],
        "warning_time": None,
        "warning_time_text": None,

        "created_at": now_ts(),
        "created_at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_checked_at": now_ts(),
        "last_checked_at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hit_source": None,
        "hit_candle_time": None,

        "status": initial_status,
        "warning_sent": False,
        "activated_at": now_ts() if entry_confirmed else None,
        "activated_at_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S") if entry_confirmed else None,
        "activated_price": float(result.get("price")) if entry_confirmed else None
    }

    active.append(signal)
    save_active_signals(active)
    record_stat_event(signal, "SETUP_CREATED")
    if initial_status == "ACTIVE":
        record_stat_event(signal, "ACTIVATED")

    if initial_status == "PENDING_ACTIVATION":
        return True, f"👀 ستاپ {signal['symbol']} ذخیره شد و منتظر فعال‌سازی ورود است."
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
    closed = dict(signal)
    closed["status"] = result_type
    closed["exit_price"] = float(exit_price)
    closed["closed_at"] = now_ts()
    closed["closed_at_text"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    closed["result_percent"] = calculate_result_percent(signal, exit_price)
    closed["hit_source"] = signal.get("hit_source")
    closed["hit_candle_time"] = signal.get("hit_candle_time")

    record_stat_event(closed, result_type, extra={
        "exit_price": float(exit_price),
        "result_percent": closed.get("result_percent"),
        "hit_source": closed.get("hit_source"),
        "hit_candle_time": closed.get("hit_candle_time"),
    })

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

    if result_type == "TP2":
        return (
            f"🎯 نتیجه TP2 سیگنال {signal['symbol']}\n\n"
            f"جهت: {'لانگ' if signal['direction'] == 'LONG' else 'شورت'}\n"
            f"ورود: {signal['entry']}\n"
            f"TP2: {signal['tp2']}\n"
            f"قیمت خروج: {exit_price}\n"
            f"نتیجه: تارگت دوم ✅\n"
            f"درصد حرکت: {closed['result_percent']}٪"
        )

    sl_reasons = guess_sl_reasons(closed)
    reasons_text = "\n".join([f"- {r}" for r in sl_reasons[:4]])

    return (
        f"❌ نتیجه سیگنال {signal['symbol']}\n\n"
        f"جهت: {'لانگ' if signal['direction'] == 'LONG' else 'شورت'}\n"
        f"ورود: {signal['entry']}\n"
        f"SL: {signal['stop_loss']}\n"
        f"قیمت خروج: {exit_price}\n"
        f"نتیجه: حد ضرر ❌\n"
        f"درصد حرکت: {closed['result_percent']}٪\n\n"
        f"دلایل احتمالی استاپ:\n"
        f"{reasons_text}"
    )

def fa_direction(direction):
    if direction == "LONG":
        return "لانگ"
    if direction == "SHORT":
        return "شورت"
    return "نامشخص"


def format_signed_percent(value):
    try:
        value = float(value)
    except Exception:
        return "0٪"

    sign = "+" if value > 0 else ""
    return f"{sign}{value}٪"


def compact_signal_line(signal):
    return (
        f"{signal.get('symbol', 'نامشخص')} | "
        f"{fa_direction(signal.get('direction'))} | "
        f"{format_signed_percent(signal.get('result_percent', 0))} | "
        f"ورود: {signal.get('entry')} | خروج: {signal.get('exit_price')}"
    )


def guess_sl_reasons(signal):
    reasons = []

    warning_reasons = signal.get("warning_reasons") or []
    if warning_reasons:
        reasons.append("قبل از SL هشدار ضعف صادر شده بود")
        for item in warning_reasons[:3]:
            reasons.append(item)

    direction = signal.get("direction")

    buy_power = signal.get("buy_power")
    sell_power = signal.get("sell_power")

    try:
        buy_power = float(buy_power)
        sell_power = float(sell_power)

        if direction == "LONG" and sell_power >= buy_power + 10:
            reasons.append("قدرت فروش هنگام ورود از خرید بیشتر بود")

        if direction == "SHORT" and buy_power >= sell_power + 10:
            reasons.append("قدرت خرید هنگام ورود از فروش بیشتر بود")
    except Exception:
        pass

    if direction == "LONG":
        if signal.get("vwap_status") == "below_vwap":
            reasons.append("لانگ زیر VWAP ثبت شده بود")
        if signal.get("order_block") == "bearish_order_block":
            reasons.append("اوردر بلاک مخالف لانگ بود")
        if signal.get("fvg") == "bearish_fvg":
            reasons.append("FVG مخالف لانگ بود")
        if signal.get("multi_candle") == "bearish":
            reasons.append("تایید چندکندلی مخالف لانگ بود")
        if signal.get("market_regime") == "bearish":
            reasons.append("لانگ خلاف روند کلی نزولی بازار بود")

    if direction == "SHORT":
        if signal.get("vwap_status") == "above_vwap":
            reasons.append("شورت بالای VWAP ثبت شده بود")
        if signal.get("order_block") == "bullish_order_block":
            reasons.append("اوردر بلاک مخالف شورت بود")
        if signal.get("fvg") == "bullish_fvg":
            reasons.append("FVG مخالف شورت بود")
        if signal.get("multi_candle") == "bullish":
            reasons.append("تایید چندکندلی مخالف شورت بود")
        if signal.get("market_regime") == "bullish":
            reasons.append("شورت خلاف روند کلی صعودی بازار بود")

    if signal.get("fake_breakout") not in [None, "none"]:
        reasons.append("احتمال فیک بریک‌اوت در تحلیل ثبت شده بود")

    if signal.get("trend_exhaustion") not in [None, "none"]:
        reasons.append("نشانه خستگی روند وجود داشت")
    if signal.get("late_entry"):
        reasons.append(signal.get("late_entry_reason") or "ورود دیرهنگام در تحلیل اولیه دیده شده بود")
    if signal.get("tp_space_ok") is False:
        reasons.append(signal.get("tp_space_reason") or "فضای کافی تا TP وجود نداشت")
    if signal.get("trap_risk"):
        reasons.append(signal.get("trap_reason") or "ورود نزدیک حمایت/مقاومت مهم انجام شده بود")
    candle_forecast = signal.get("candle_forecast")
    direction = signal.get("direction")
    if direction == "LONG" and candle_forecast == "bearish_continuation":
        reasons.append(signal.get("candle_forecast_reason") or "پیش‌بینی کندلی خلاف لانگ بود")
    if direction == "SHORT" and candle_forecast == "bullish_continuation":
        reasons.append(signal.get("candle_forecast_reason") or "پیش‌بینی کندلی خلاف شورت بود")
    if signal.get("noise_status") in ["high_noise", "medium_noise"]:
        reasons.append("بازار هنگام ورود نویزی/رنج بوده است")
    if signal.get("volatility_status") in ["too_low", "too_high"]:
        reasons.append(signal.get("volatility_label") or "وضعیت نوسان مناسب نبود")
    if signal.get("liquidity_pool_status") not in [None, "none", "unknown"]:
        reasons.append(signal.get("liquidity_pool_label") or "Liquidity Pool مهم نزدیک قیمت بود")

    if not reasons:
        reasons.append("دلیل مشخصی در داده‌های ذخیره‌شده دیده نشد")

    clean = []
    for item in reasons:
        if item and item not in clean:
            clean.append(item)

    return clean[:5]


def format_signal_details(items, title, limit=10, include_reasons=False):
    if not items:
        return f"\n{title}\nندارد\n"

    out = f"\n{title}\n"

    for signal in items[:limit]:
        out += f"\n{compact_signal_line(signal)}"

        if include_reasons:
            reasons = guess_sl_reasons(signal)
            out += "\nدلیل احتمالی:"
            for reason in reasons[:4]:
                out += f"\n- {reason}"

        out += "\n"

    if len(items) > limit:
        out += f"\n... و {len(items) - limit} مورد دیگر\n"

    return out


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
        try:
            macd_hist_now = float(result.get("macd_hist", 0))
            # MACD Histogram به‌تنهایی هشدار ندهد؛ فقط وقتی با VWAP یا قدرت فروش همراه شود.
            if macd_hist_now < 0 and (
                result.get("vwap_status") == "below_vwap"
                or result.get("sell_power", 0) >= result.get("buy_power", 0) + 8
            ):
                warnings.append("MACD هیستوگرام برای لانگ با تایید VWAP/قدرت فروش ضعیف شده است")
        except Exception:
            pass
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
        try:
            macd_hist_now = float(result.get("macd_hist", 0))
            # MACD Histogram به‌تنهایی هشدار ندهد؛ فقط وقتی با VWAP یا قدرت خرید همراه شود.
            if macd_hist_now > 0 and (
                result.get("vwap_status") == "above_vwap"
                or result.get("buy_power", 0) >= result.get("sell_power", 0) + 8
            ):
                warnings.append("MACD هیستوگرام برای شورت با تایید VWAP/قدرت خرید ضعیف شده است")
        except Exception:
            pass
        if result.get("fake_breakout") == "fake_bearish_breakout":
            warnings.append("احتمال فیک بریک‌اوت نزولی وجود دارد")

    if result.get("late_entry"):
        warnings.append("ورود از نظر Late Entry پرریسک شده است")
    if result.get("tp_space_ok") is False:
        warnings.append("فضای TP ضعیف شده یا حمایت/مقاومت نزدیک است")
    if result.get("trap_risk"):
        warnings.append(result.get("trap_reason") or "قیمت نزدیک حمایت/مقاومت مهم است")
    if direction == "LONG" and result.get("candle_forecast") == "bearish_continuation":
        warnings.append("پیش‌بینی کندلی کوتاه‌مدت خلاف لانگ شده است")
    if direction == "SHORT" and result.get("candle_forecast") == "bullish_continuation":
        warnings.append("پیش‌بینی کندلی کوتاه‌مدت خلاف شورت شده است")
    if result.get("noise_status") == "high_noise":
        warnings.append("بازار نویزی/رنج شده است")
    if result.get("volatility_status") in ["too_low", "too_high"]:
        warnings.append(result.get("volatility_label") or "وضعیت نوسان مناسب نیست")

    if len(warnings) >= 2:
        text = "\n".join([f"⚠️ {w}" for w in warnings[:5]])
        message = (
            f"⚠️ هشدار ضعف سیگنال {signal['symbol']}\n\n"
            f"جهت سیگنال: {'لانگ' if direction == 'LONG' else 'شورت'}\n"
            f"ورود: {signal['entry']}\n"
            f"قیمت فعلی: {price}\n\n"
            f"{text}\n\n"
            f"ریسک معامله بالا رفته؛ بستن معامله یا کاهش ریسک را بررسی کن."
        )

        return message, warnings[:5]

    return None, []


def check_active_signals():
    active = get_active_signals()
    remaining = []
    messages = []

    for signal in active:
        try:
            if signal.get("status") == "PENDING_ACTIVATION":
                age = now_ts() - int(signal.get("created_at") or now_ts())
                if age > int(PENDING_SETUP_TIMEOUT_MINUTES) * 60:
                    msg = close_pending_setup(signal, "زمان انتظار فعال‌سازی تمام شد")
                    messages.append({"chat_id": signal["chat_id"], "message": msg, "reply_to_message_id": signal.get("message_id")})
                    continue
                price = get_last_close_from_1m_or_ticker(signal["symbol"], signal)
                if price_in_entry_zone(signal, price):
                    try:
                        live = analyze_symbol(signal["symbol"])
                        if live.get("direction") == signal.get("direction") and live.get("entry_confirmed"):
                            signal = activate_pending_signal(signal, price)
                            activation_msg = (
                                "✅ ورود فعال شد\n\n"
                                f"ارز: {signal['symbol']}\n"
                                f"جهت: {fa_direction(signal['direction'])}\n"
                                f"قیمت فعال‌سازی: {round(float(price), 8)}\n"
                                "تحلیل لحظه‌ای 5M با پیش‌بینی اولیه همسو شد."
                            )
                            messages.append({
                                "chat_id": signal["chat_id"],
                                "reply_to_message_id": signal.get("message_id"),
                                "message": activation_msg
                            })
                    except Exception as e:
                        log_exception("فعال‌سازی ورود", e, "signal_tracker.py", "check_active_signals", signal.get("symbol"))
                remaining.append(signal)
                continue

            result_type, exit_price = detect_signal_hit_from_candles(signal)

            if result_type:
                msg = close_signal(signal, result_type, exit_price)
                messages.append({
                    "chat_id": signal["chat_id"],
                    "message": msg,
                    "reply_to_message_id": signal.get("message_id")
                })

                if result_type == "TP1" and signal.get("tp2") is not None:
                    signal["tp1_hit"] = True
                    signal["tp1_hit_at"] = now_ts()
                    signal["tp1_hit_at_text"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    signal["last_checked_at"] = now_ts()
                    remaining.append(signal)
                    continue

                continue

            # برای هشدار ضعف، قیمت فعلی فقط جهت نمایش استفاده می‌شود؛
            # تشخیص TP/SL با مسیر کندل‌های 1m انجام شده است.
            price = get_last_close_from_1m_or_ticker(signal["symbol"], signal)

            if not signal.get("warning_sent", False):
                try:
                    result = analyze_symbol(signal["symbol"])
                    warning_msg, warning_reasons = weakness_warning_for_signal(signal, result, price)

                    if warning_msg:
                        signal["warning_sent"] = True
                        signal["warning_reasons"] = warning_reasons
                        signal["warning_time"] = now_ts()
                        signal["warning_time_text"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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



def normalize_number_text_for_calc(text):
    mapping = {
        "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
        "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
        "٫": ".", ",": "."
    }

    for src, dst in mapping.items():
        text = text.replace(src, dst)

    return text


def parse_profit_calc_text(text):
    if not text:
        return None

    normalized = normalize_number_text_for_calc(text.strip().lower())

    has_calc_word = (
        "لوریج" in normalized
        or "leverage" in normalized
        or "اهرم" in normalized
        or "دلار" in normalized
        or "$" in normalized
        or "سرمایه" in normalized
        or "محاسبه" in normalized
        or "سود" in normalized
        or "ضرر" in normalized
    )

    numbers = re.findall(r"\d+(?:\.\d+)?", normalized)

    if len(numbers) < 2 or not has_calc_word:
        return None

    margin = None
    leverage = None

    lev_match = re.search(r"(?:لوریج|leverage|اهرم)\s*(\d+(?:\.\d+)?)", normalized)

    if lev_match:
        leverage = float(lev_match.group(1))
        before_lev = normalized[:lev_match.start()]
        before_numbers = re.findall(r"\d+(?:\.\d+)?", before_lev)

        if before_numbers:
            margin = float(before_numbers[-1])

    if margin is None or leverage is None:
        margin = float(numbers[0])
        leverage = float(numbers[1])

    if margin <= 0 or leverage <= 0:
        return None

    return margin, leverage


def calculate_pnl_usdt(result_percent, margin, leverage):
    try:
        result_percent = float(result_percent)
        margin = float(margin)
        leverage = float(leverage)
    except Exception:
        return 0

    return round((margin * leverage * result_percent) / 100, 4)


def format_money(value):
    try:
        value = float(value)
    except Exception:
        value = 0

    sign = "+" if value > 0 else ""
    return f"{sign}{round(value, 4)}$"


def parse_days_from_report_text(text):
    if not text:
        return 7

    normalized = normalize_number_text_for_calc(text)

    if "آمار کل" in normalized:
        return None

    match = re.search(r"آمار\s+(\d+)\s+روز", normalized)
    if match:
        return int(match.group(1))

    return 7


def get_profit_for_signal_text(reply_text, margin, leverage):
    if not reply_text:
        return None

    normalized = normalize_number_text_for_calc(reply_text)

    percent_match = re.search(r"درصد حرکت\s*:\s*([+-]?\d+(?:\.\d+)?)\s*٪", normalized)
    if not percent_match:
        percent_match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*٪", normalized)

    if not percent_match:
        return None

    result_percent = float(percent_match.group(1))
    pnl = calculate_pnl_usdt(result_percent, margin, leverage)

    symbol = "نامشخص"
    symbol_match = re.search(r"([A-Z0-9]+USDT)", normalized)
    if symbol_match:
        symbol = symbol_match.group(1)

    result_text = "سود" if pnl > 0 else "ضرر" if pnl < 0 else "بدون سود/ضرر"

    return (
        f"💰 محاسبه معامله\n\n"
        f"ارز: {symbol}\n"
        f"سرمایه: {margin}$\n"
        f"لوریج: {leverage}x\n\n"
        f"درصد حرکت:\n"
        f"{result_percent}٪\n\n"
        f"{result_text} تقریبی:\n"
        f"{format_money(pnl)}"
    )


def get_profit_simulation_report(margin, leverage, days=None):
    stats = get_signal_stats()

    if days is not None:
        start_ts = now_ts() - (days * 24 * 60 * 60)
        stats = [s for s in stats if s.get("closed_at", 0) >= start_ts]

    total = len(stats)

    if total == 0:
        title = "آمار کل" if days is None else f"{days} روز اخیر"
        return f"📊 برای {title} معامله بسته‌شده‌ای جهت محاسبه وجود ندارد."

    wins = [s for s in stats if s.get("status") == "TP1"]
    losses = [s for s in stats if s.get("status") == "SL"]

    gross_profit = 0
    gross_loss = 0
    best_trade = None
    worst_trade = None

    for s in stats:
        pnl = calculate_pnl_usdt(s.get("result_percent", 0), margin, leverage)
        s["_calc_pnl"] = pnl

        if pnl >= 0:
            gross_profit += pnl
        else:
            gross_loss += pnl

        if best_trade is None or pnl > best_trade.get("_calc_pnl", 0):
            best_trade = s

        if worst_trade is None or pnl < worst_trade.get("_calc_pnl", 0):
            worst_trade = s

    gross_profit = round(gross_profit, 4)
    gross_loss = round(gross_loss, 4)
    net = round(gross_profit + gross_loss, 4)

    total_margin_used = margin * total
    roi = round((net / total_margin_used) * 100, 2) if total_margin_used > 0 else 0

    title = "آمار کل" if days is None else f"آمار {days} روز اخیر"

    best_text = "نامشخص"
    if best_trade:
        best_text = (
            f"{best_trade.get('symbol')} | "
            f"{fa_direction(best_trade.get('direction'))} | "
            f"{format_money(best_trade.get('_calc_pnl', 0))}"
        )

    worst_text = "نامشخص"
    if worst_trade:
        worst_text = (
            f"{worst_trade.get('symbol')} | "
            f"{fa_direction(worst_trade.get('direction'))} | "
            f"{format_money(worst_trade.get('_calc_pnl', 0))}"
        )

    return (
        f"💰 شبیه‌سازی سود و ضرر\n\n"
        f"{title}\n\n"
        f"سرمایه هر معامله: {margin}$\n"
        f"لوریج: {leverage}x\n\n"
        f"تعداد معاملات: {total}\n"
        f"بردها: {len(wins)}\n"
        f"استاپ‌ها: {len(losses)}\n\n"
        f"سود کل TPها:\n"
        f"{format_money(gross_profit)}\n\n"
        f"ضرر کل SLها:\n"
        f"{format_money(gross_loss)}\n\n"
        f"سود/ضرر خالص:\n"
        f"{format_money(net)}\n\n"
        f"بازده نسبت به مجموع سرمایه‌های واردشده:\n"
        f"{roi}٪\n\n"
        f"بهترین معامله:\n"
        f"{best_text}\n\n"
        f"بدترین معامله:\n"
        f"{worst_text}\n\n"
        f"محاسبه بدون کارمزد و اسلیپیج است."
    )


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



def get_signal_identity(signal):
    return signal.get("signal_id") or signal.get("id")


def stat_event_exists(stats, signal_id, status):
    if not signal_id:
        return False
    for item in stats:
        if item.get("signal_id") == signal_id and item.get("status") == status:
            return True
    return False


def build_stat_event(signal, status, extra=None):
    current = now_ts()
    signal_id = get_signal_identity(signal)
    event = {
        "schema": "v2_event_stats",
        "signal_id": signal_id,
        "id": signal_id,
        "status": status,
        "event_type": status,
        "event_at": current,
        "closed_at": current,
        "event_time_text": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": signal.get("symbol"),
        "direction": signal.get("direction"),
        "user_id": signal.get("user_id"),
        "chat_id": signal.get("chat_id"),
        "message_id": signal.get("message_id"),
        "entry": signal.get("entry"),
        "stop_loss": signal.get("stop_loss"),
        "tp1": signal.get("tp1"),
        "tp2": signal.get("tp2"),
        "activated_price": signal.get("activated_price"),
        "exit_price": signal.get("exit_price"),
        "result_percent": signal.get("result_percent"),
        "created_at": signal.get("created_at"),
        "created_at_text": signal.get("created_at_text"),
        "activated_at": signal.get("activated_at"),
        "activated_at_text": signal.get("activated_at_text"),
        "entry_mode": signal.get("entry_mode"),
        "entry_status": signal.get("entry_status"),
        "entry_confirmed": signal.get("entry_confirmed"),
        "entry_grade": signal.get("entry_grade"),
        "risk_level": signal.get("risk_level"),
        "risk_reward": signal.get("risk_reward"),
        "freshness": signal.get("freshness"),
        "predictive_confirmations": signal.get("predictive_confirmations"),
        "setup_score": signal.get("setup_score"),
        "adx": signal.get("adx"),
        "rsi": signal.get("rsi"),
        "macd_hist": signal.get("macd_hist"),
        "vwap_status": signal.get("vwap_status"),
        "market_regime": signal.get("market_regime"),
        "market_regime_label": signal.get("market_regime_label"),
        "market_breadth_status": signal.get("market_breadth_status"),
        "market_breadth_label": signal.get("market_breadth_label"),
        "market_breadth_bullish_pct": signal.get("market_breadth_bullish_pct"),
        "market_breadth_bearish_pct": signal.get("market_breadth_bearish_pct"),
        "compression_active": signal.get("compression_active"),
        "compression_label": signal.get("compression_label"),
        "power2_buy": signal.get("power2_buy"),
        "power2_sell": signal.get("power2_sell"),
        "power3_buy": signal.get("power3_buy"),
        "power3_sell": signal.get("power3_sell"),
        "power_acceleration": signal.get("power_acceleration"),
        "buy_power": signal.get("buy_power"),
        "sell_power": signal.get("sell_power"),
        "cancel_reason": signal.get("cancel_reason"),
        "hit_source": signal.get("hit_source"),
        "hit_candle_time": signal.get("hit_candle_time"),
        "warning_sent": signal.get("warning_sent"),
        "warning_reasons": signal.get("warning_reasons", []),
    }
    if extra:
        event.update(extra)
    return event


def record_stat_event(signal, status, extra=None, unique=True):
    try:
        stats = get_signal_stats()
        signal_id = get_signal_identity(signal)
        if unique and stat_event_exists(stats, signal_id, status):
            return False
        stats.append(build_stat_event(signal, status, extra=extra))
        save_signal_stats(stats)
        return True
    except Exception as e:
        print("STAT EVENT ERROR:", status, str(e))
        return False

def get_stats_report(days=None):
    stats = get_signal_stats()
    active = get_active_signals()

    start_ts = None
    if days is not None:
        start_ts = now_ts() - (days * 24 * 60 * 60)

    def event_time(item):
        return int(item.get("event_at") or item.get("closed_at") or item.get("created_at") or 0)

    if start_ts is not None:
        stats = [s for s in stats if event_time(s) >= start_ts]
        active = [s for s in active if int(s.get("created_at") or 0) >= start_ts]

    v2 = [s for s in stats if s.get("signal_id")]
    legacy = [s for s in stats if not s.get("signal_id") and s.get("status") in ["TP1", "SL"]]

    def ids(status):
        return set(s.get("signal_id") for s in v2 if s.get("status") == status and s.get("signal_id"))

    setup_ids = ids("SETUP_CREATED")
    activated_ids = ids("ACTIVATED")
    cancelled_ids = ids("CANCELLED")
    tp1_ids = ids("TP1")
    tp2_ids = ids("TP2")
    sl_ids = ids("SL")

    pending_active_ids = set(get_signal_identity(s) for s in active if s.get("status") == "PENDING_ACTIVATION" and get_signal_identity(s))
    active_trade_ids = set(get_signal_identity(s) for s in active if s.get("status") == "ACTIVE" and get_signal_identity(s))

    setups = len(setup_ids)
    activated = len(activated_ids)
    cancelled = len(cancelled_ids)
    pending = len(pending_active_ids)
    active_trades = len(active_trade_ids)
    tp1_count = len(tp1_ids)
    tp2_count = len(tp2_ids)
    sl_count = len(sl_ids)

    activation_rate = round((activated / setups) * 100, 1) if setups else 0
    closed_activated = tp1_count + sl_count
    activated_win_rate = round((tp1_count / closed_activated) * 100, 1) if closed_activated else 0
    tp2_rate = round((tp2_count / tp1_count) * 100, 1) if tp1_count else 0

    def latest_events(status):
        by_id = {}
        for s in v2:
            if s.get("status") == status and s.get("signal_id"):
                by_id[s.get("signal_id")] = s
        return list(by_id.values())

    activated_events = latest_events("ACTIVATED")
    tp1_events = latest_events("TP1")
    tp2_events = latest_events("TP2")
    sl_events = latest_events("SL")
    cancelled_events = latest_events("CANCELLED")
    outcome_events = tp1_events + sl_events

    def avg_minutes(items, start_key, end_key):
        vals = []
        for item in items:
            try:
                a = int(item.get(start_key) or 0)
                b = int(item.get(end_key) or item.get("event_at") or item.get("closed_at") or 0)
                if a > 0 and b > 0 and b >= a:
                    vals.append((b - a) / 60)
            except Exception:
                pass
        return round(sum(vals) / len(vals), 1) if vals else 0

    avg_activation_min = avg_minutes(activated_events, "created_at", "activated_at")
    avg_tp1_min = avg_minutes(tp1_events, "activated_at", "closed_at")
    avg_sl_min = avg_minutes(sl_events, "activated_at", "closed_at")

    def direction_report(direction):
        items = [x for x in outcome_events if x.get("direction") == direction]
        wins = len([x for x in items if x.get("status") == "TP1"])
        losses = len([x for x in items if x.get("status") == "SL"])
        total = wins + losses
        wr = round((wins / total) * 100, 1) if total else 0
        return f"{total} معامله | TP1: {wins} | SL: {losses} | Win Rate: {wr}٪"

    def group_report(field, title, limit=8):
        groups = {}
        for item in outcome_events:
            key = item.get(field)
            if key in [None, "", "نامشخص"]:
                key = "نامشخص"
            groups.setdefault(str(key), {"tp1": 0, "sl": 0})
            if item.get("status") == "TP1":
                groups[str(key)]["tp1"] += 1
            elif item.get("status") == "SL":
                groups[str(key)]["sl"] += 1
        if not groups:
            return f"\n{title}:\nندارد\n"
        rows = []
        for key, data in groups.items():
            total = data["tp1"] + data["sl"]
            wr = round((data["tp1"] / total) * 100, 1) if total else 0
            rows.append((total, wr, key, data))
        rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
        out = f"\n{title}:"
        for total, wr, key, data in rows[:limit]:
            out += f"\n{key}: {data['tp1']}/{total} TP1 | {wr}٪"
        return out + "\n"

    def adx_bucket(value):
        try:
            v = float(value)
        except Exception:
            return "ADX نامشخص"
        if v < 15:
            return "ADX زیر 15"
        if v < 20:
            return "ADX 15 تا 20"
        if v < 25:
            return "ADX 20 تا 25"
        return "ADX بالای 25"

    for item in outcome_events:
        item["adx_bucket"] = adx_bucket(item.get("adx"))

    avg_win = round(sum(abs(float(s.get("result_percent", 0) or 0)) for s in tp1_events) / len(tp1_events), 3) if tp1_events else 0
    avg_loss = round(sum(abs(float(s.get("result_percent", 0) or 0)) for s in sl_events) / len(sl_events), 3) if sl_events else 0

    title = "آمار کل" if days is None else f"آمار {days} روز اخیر"

    if not v2 and not active:
        if days is None:
            return "📊 هنوز آمار دقیق جدیدی وجود ندارد."
        return f"📊 در {days} روز اخیر آمار دقیق جدیدی وجود ندارد."

    report = f"""
📊 {title}

ستاپ ساخته‌شده:
{setups}

✅ ورود فعال‌شده:
{activated}

👀 هنوز منتظر فعال‌سازی:
{pending}

🚫 لغوشده:
{cancelled}

Activation Rate:
{activation_rate}٪

معاملات فعال باز:
{active_trades}

--------------------

TP1:
{tp1_count}

TP2:
{tp2_count}

SL:
{sl_count}

Activated Win Rate:
{activated_win_rate}٪

TP2 Rate از TP1:
{tp2_rate}٪

میانگین برد:
{avg_win}٪

میانگین باخت:
{avg_loss}٪

--------------------

میانگین زمان تا فعال‌سازی:
{avg_activation_min} دقیقه

میانگین زمان تا TP1 بعد از فعال‌سازی:
{avg_tp1_min} دقیقه

میانگین زمان تا SL بعد از فعال‌سازی:
{avg_sl_min} دقیقه

--------------------

لانگ:
{direction_report('LONG')}

شورت:
{direction_report('SHORT')}
"""
    report += group_report("symbol", "عملکرد ارزها")
    report += group_report("entry_mode", "عملکرد بر اساس حالت ورود")
    report += group_report("freshness", "عملکرد بر اساس تازگی حرکت")
    report += group_report("risk_level", "عملکرد بر اساس ریسک")
    report += group_report("adx_bucket", "عملکرد بر اساس ADX")
    report += group_report("market_regime", "عملکرد بر اساس روند کلی بازار")
    report += format_signal_details(tp1_events, f"✅ لیست TP1 ({len(tp1_events)}):", limit=10, include_reasons=False)
    report += format_signal_details(tp2_events, f"🎯 لیست TP2 ({len(tp2_events)}):", limit=10, include_reasons=False)
    report += format_signal_details(sl_events, f"❌ لیست SL ({len(sl_events)}):", limit=10, include_reasons=True)
    report += format_signal_details(cancelled_events, f"🚫 لیست لغوشده‌ها ({len(cancelled_events)}):", limit=10, include_reasons=False)
    if legacy:
        report += f"\n\nرکوردهای قدیمی بدون signal_id: {len(legacy)} مورد\nاین رکوردها در آمار دقیق جدید حساب نشده‌اند."
    return report

