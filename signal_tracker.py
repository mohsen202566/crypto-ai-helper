# -*- coding: utf-8 -*-
import json
import os
import re
import time
from datetime import datetime, timedelta

import ccxt
from analysis import analyze_symbol


ACTIVE_SIGNALS_FILE = "active_signals.json"
SIGNAL_STATS_FILE = "signal_stats.json"

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
        "reasons": result.get("reasons", []),

        "warning_reasons": [],
        "warning_time": None,
        "warning_time_text": None,

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
        reasons.append("ورود دیرهنگام در تحلیل اولیه دیده شده بود")
    if signal.get("tp_space_ok") is False:
        reasons.append("فضای کافی تا TP وجود نداشت")
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
            if float(result.get("macd_hist", 0)) < 0:
                warnings.append("MACD هیستوگرام برای لانگ ضعیف شده است")
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
            if float(result.get("macd_hist", 0)) > 0:
                warnings.append("MACD هیستوگرام برای شورت ضعیف شده است")
        except Exception:
            pass
        if result.get("fake_breakout") == "fake_bearish_breakout":
            warnings.append("احتمال فیک بریک‌اوت نزولی وجود دارد")

    if result.get("late_entry"):
        warnings.append("ورود از نظر Late Entry پرریسک شده است")
    if result.get("tp_space_ok") is False:
        warnings.append("فضای TP ضعیف شده یا حمایت/مقاومت نزدیک است")
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

    wins_list = [s for s in stats if s.get("status") == "TP1"]
    losses_list = [s for s in stats if s.get("status") == "SL"]

    tp1_count = len(wins_list)
    sl_count = len(losses_list)

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

    for sym, data in sorted_symbols[:7]:
        wr = round((data["wins"] / data["total"]) * 100, 1)
        top_symbols_text += f"\n{sym}: {data['wins']}/{data['total']} برد | {wr}٪"

    avg_win = 0
    avg_loss = 0

    if wins_list:
        avg_win = round(sum([abs(float(s.get("result_percent", 0))) for s in wins_list]) / len(wins_list), 3)

    if losses_list:
        avg_loss = round(sum([abs(float(s.get("result_percent", 0))) for s in losses_list]) / len(losses_list), 3)

    title = "آمار کل" if days is None else f"آمار {days} روز اخیر"

    report = f"""
📊 {title}

کل سیگنال‌های زیرنظرگرفته‌شده:
{total}

✅ TP1:
{tp1_count}

❌ SL:
{sl_count}

Win Rate:
{win_rate}٪

میانگین برد:
{avg_win}٪

میانگین باخت:
{avg_loss}٪

لانگ:
{direction_report(long_stats)}

شورت:
{direction_report(short_stats)}

عملکرد ارزها:
{top_symbols_text}
"""

    report += format_signal_details(
        wins_list,
        f"✅ لیست بردها ({len(wins_list)}):",
        limit=12,
        include_reasons=False
    )

    report += format_signal_details(
        losses_list,
        f"❌ لیست استاپ‌ها ({len(losses_list)}):",
        limit=12,
        include_reasons=True
    )

    return report

