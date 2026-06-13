# -*- coding: utf-8 -*-
from datetime import datetime
from data_store import load_json, save_json
from ai_memory import is_learning_enabled, update_summary

LEARNING_FILE = "coin_learning.json"
MAX_SIGNALS = 20000
MAX_VALUES = 500


def _now():
    return datetime.utcnow().isoformat()


def _empty_data():
    return {
        "version": 1,
        "signals": [],
        "coins": {},
        "updated_at": None
    }


def load_learning():
    data = load_json(LEARNING_FILE, _empty_data())
    data.setdefault("signals", [])
    data.setdefault("coins", {})
    return data


def save_learning(data):
    data["updated_at"] = _now()
    return save_json(LEARNING_FILE, data)


def _safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _safe_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def _avg(values):
    clean = []
    for item in values or []:
        x = _safe_float(item)
        if x is not None:
            clean.append(x)

    if not clean:
        return 0.0

    return round(sum(clean) / len(clean), 4)


def _append_limited(values, value, limit=MAX_VALUES):
    x = _safe_float(value)
    if x is None:
        return

    values.append(x)
    del values[:-limit]


def _coin_key(symbol, direction):
    return f"{symbol}_{direction}"


def _signal_id(result):
    return (
        result.get("signal_id")
        or result.get("id")
        or f"{result.get('symbol')}_{result.get('direction')}_{int(datetime.utcnow().timestamp())}"
    )


def _ensure_coin_stats(data, symbol, direction):
    key = _coin_key(symbol, direction)

    if key not in data["coins"]:
        data["coins"][key] = {
            "symbol": symbol,
            "direction": direction,

            "total": 0,
            "tp1": 0,
            "tp2": 0,
            "sl": 0,

            "ghost_total": 0,
            "ghost_tp1": 0,
            "ghost_tp2": 0,
            "ghost_sl": 0,

            "moves": [],
            "rsi_values": [],
            "adx_values": [],
            "macd_values": [],
            "macd_signal_values": [],
            "macd_hist_values": [],

            "power2_buy_values": [],
            "power2_sell_values": [],
            "power3_buy_values": [],
            "power3_sell_values": [],
            "buy_power_values": [],
            "sell_power_values": [],

            "atr_values": [],
            "score_values": [],
            "confirmation_values": [],

            "market_modes": {},
            "coin_behaviors": {},
            "btc_biases": {},

            "last_updated": None
        }

    return data["coins"][key]


def _count_value(counter_dict, value):
    if value is None:
        return

    key = str(value)
    counter_dict[key] = int(counter_dict.get(key, 0)) + 1


def create_signal_snapshot(result, signal_type="REAL"):
    return {
        "id": _signal_id(result),
        "signal_type": signal_type,

        "symbol": result.get("symbol"),
        "direction": result.get("direction"),

        "entry": result.get("entry") or result.get("price"),
        "price": result.get("price") or result.get("entry"),
        "tp1": result.get("tp1"),
        "tp2": result.get("tp2"),
        "stop_loss": result.get("stop_loss"),

        "score": result.get("score"),
        "confirmations": result.get("confirmations"),
        "risk_level": result.get("risk_level"),
        "risk_reward": result.get("risk_reward"),
        "entry_mode": result.get("entry_mode"),
        "freshness": result.get("freshness"),

        "rsi": result.get("rsi"),
        "adx": result.get("adx"),
        "macd": result.get("macd"),
        "macd_signal": result.get("macd_signal"),
        "macd_hist": result.get("macd_hist"),

"power2_buy": result.get("power2_buy") or result.get("buy_power_2"),
        "power2_sell": result.get("power2_sell") or result.get("sell_power_2"),
        "power3_buy": result.get("power3_buy") or result.get("buy_power_3"),
        "power3_sell": result.get("power3_sell") or result.get("sell_power_3"),

        "buy_power": result.get("buy_power"),
        "sell_power": result.get("sell_power"),

        "atr": result.get("atr"),
        "market_mode": result.get("market_mode") or result.get("market_regime"),
        "coin_behavior": result.get("coin_behavior"),
        "btc_bias": result.get("btc_bias"),

        "support": result.get("support"),
        "resistance": result.get("resistance"),
        "sr_timeframe": result.get("sr_timeframe"),

        "reasons": result.get("reasons", []),

        "created_at": _now(),
        "closed_at": None,
        "result": None,
        "exit_price": None,
        "move_percent": None,
        "holding_minutes": None
    }


def _update_indicator_memory(stats, snapshot):
    _append_limited(stats["rsi_values"], snapshot.get("rsi"))
    _append_limited(stats["adx_values"], snapshot.get("adx"))
    _append_limited(stats["macd_values"], snapshot.get("macd"))
    _append_limited(stats["macd_signal_values"], snapshot.get("macd_signal"))
    _append_limited(stats["macd_hist_values"], snapshot.get("macd_hist"))

    _append_limited(stats["power2_buy_values"], snapshot.get("power2_buy"))
    _append_limited(stats["power2_sell_values"], snapshot.get("power2_sell"))
    _append_limited(stats["power3_buy_values"], snapshot.get("power3_buy"))
    _append_limited(stats["power3_sell_values"], snapshot.get("power3_sell"))
    _append_limited(stats["buy_power_values"], snapshot.get("buy_power"))
    _append_limited(stats["sell_power_values"], snapshot.get("sell_power"))

    _append_limited(stats["atr_values"], snapshot.get("atr"))
    _append_limited(stats["score_values"], snapshot.get("score"))
    _append_limited(stats["confirmation_values"], snapshot.get("confirmations"))

    _count_value(stats["market_modes"], snapshot.get("market_mode"))
    _count_value(stats["coin_behaviors"], snapshot.get("coin_behavior"))
    _count_value(stats["btc_biases"], snapshot.get("btc_bias"))


def record_signal(result, signal_type="REAL"):
    if not is_learning_enabled():
        return False

    if not result or result.get("direction") not in ["LONG", "SHORT"]:
        return False

    data = load_learning()
    snapshot = create_signal_snapshot(result, signal_type=signal_type)

    existing_ids = {str(x.get("id")) for x in data["signals"]}
    if str(snapshot["id"]) in existing_ids:
        return snapshot["id"]

    data["signals"].append(snapshot)
    data["signals"] = data["signals"][-MAX_SIGNALS:]

    stats = _ensure_coin_stats(data, snapshot["symbol"], snapshot["direction"])

    if signal_type == "GHOST":
        stats["ghost_total"] += 1
        update_summary(ghost=1)
    else:
        stats["total"] += 1
        update_summary(signals=1)

    _update_indicator_memory(stats, snapshot)
    stats["last_updated"] = _now()

    save_learning(data)
    return snapshot["id"]

def update_signal_result(signal_id, result, exit_price=None, move_percent=None, holding_minutes=None):
    if not is_learning_enabled():
        return False

    data = load_learning()
    found = None

    for item in data["signals"]:
        if str(item.get("id")) == str(signal_id):
            found = item
            break

    if not found:
        return False

    if found.get("result"):
        return False

    result = str(result).upper()
    if result not in ["TP1", "TP2", "SL"]:
        return False

    found["result"] = result
    found["exit_price"] = exit_price
    found["move_percent"] = move_percent
    found["holding_minutes"] = holding_minutes
    found["closed_at"] = _now()

    stats = _ensure_coin_stats(data, found.get("symbol"), found.get("direction"))
    signal_type = found.get("signal_type", "REAL")

    if signal_type == "GHOST":
        if result == "TP1":
            stats["ghost_tp1"] += 1
            update_summary(tp1=1)
        elif result == "TP2":
            stats["ghost_tp2"] += 1
            update_summary(tp2=1)
        elif result == "SL":
            stats["ghost_sl"] += 1
            update_summary(sl=1)
    else:
        if result == "TP1":
            stats["tp1"] += 1
            update_summary(tp1=1)
        elif result == "TP2":
            stats["tp2"] += 1
            update_summary(tp2=1)
        elif result == "SL":
            stats["sl"] += 1
            update_summary(sl=1)

    if move_percent is not None:
        _append_limited(stats["moves"], move_percent)

    stats["last_updated"] = _now()
    save_learning(data)
    return True


def get_coin_stats(symbol, direction=None):
    data = load_learning()
    rows = []

    for stats in data.get("coins", {}).values():
        if stats.get("symbol") != symbol:
            continue
        if direction and stats.get("direction") != direction:
            continue
        rows.append(stats)

    return rows


def calculate_win_rate(stats):
    closed = (
        int(stats.get("tp1", 0))
        + int(stats.get("tp2", 0))
        + int(stats.get("sl", 0))
    )

    if closed <= 0:
        return 0.0

    wins = int(stats.get("tp1", 0)) + int(stats.get("tp2", 0))
    return round((wins / closed) * 100, 2)


def calculate_ghost_win_rate(stats):
    closed = (
        int(stats.get("ghost_tp1", 0))
        + int(stats.get("ghost_tp2", 0))
        + int(stats.get("ghost_sl", 0))
    )

    if closed <= 0:
        return 0.0

    wins = int(stats.get("ghost_tp1", 0)) + int(stats.get("ghost_tp2", 0))
    return round((wins / closed) * 100, 2)


def average_move(stats):
    return _avg(stats.get("moves") or [])


def _top_counter(counter_dict):
    if not counter_dict:
        return "نامشخص"

    rows = sorted(
        counter_dict.items(),
        key=lambda x: int(x[1]),
        reverse=True
    )

    return rows[0][0] if rows else "نامشخص"


def get_coin_learning_profile(symbol, direction):
    rows = get_coin_stats(symbol, direction)
    if not rows:
        return None

    stats = rows[0]

    closed = (
        int(stats.get("tp1", 0))
        + int(stats.get("tp2", 0))
        + int(stats.get("sl", 0))
    )

    return {
        "symbol": stats.get("symbol"),
        "direction": stats.get("direction"),
        "total": int(stats.get("total", 0)),
        "closed": closed,
        "tp1": int(stats.get("tp1", 0)),
        "tp2": int(stats.get("tp2", 0)),
        "sl": int(stats.get("sl", 0)),
        "win_rate": calculate_win_rate(stats),
        "ghost_total": int(stats.get("ghost_total", 0)),
        "ghost_win_rate": calculate_ghost_win_rate(stats),
        "average_move": average_move(stats),
        "avg_rsi": _avg(stats.get("rsi_values")),
        "avg_adx": _avg(stats.get("adx_values")),
        "avg_macd_hist": _avg(stats.get("macd_hist_values")),

"avg_power2_buy": _avg(stats.get("power2_buy_values")),
        "avg_power2_sell": _avg(stats.get("power2_sell_values")),
        "avg_power3_buy": _avg(stats.get("power3_buy_values")),
        "avg_power3_sell": _avg(stats.get("power3_sell_values")),
        "avg_score": _avg(stats.get("score_values")),
        "avg_confirmations": _avg(stats.get("confirmation_values")),
        "common_market_mode": _top_counter(stats.get("market_modes")),
        "common_coin_behavior": _top_counter(stats.get("coin_behaviors")),
        "common_btc_bias": _top_counter(stats.get("btc_biases")),
    }


def should_require_extra_strength(symbol, direction):
    """Learning-only helper for future analysis.py.

    فعلاً قانون خیلی نرم:
    اگر حداقل 3 معامله بسته‌شده داشته باشد و وین‌ریت زیر 40 باشد، باید کمی سختگیرتر شود.
    این تابع به تنهایی سیگنال را رد نمی‌کند؛ فقط به analysis.py می‌گوید قوی‌تر لازم است.
    """
    profile = get_coin_learning_profile(symbol, direction)

    if not profile:
        return False, "داده کافی نیست"

    if profile["closed"] < 3:
        return False, "داده بسته‌شده کمتر از 3 است"

    if profile["win_rate"] < 40:
        return True, "وین‌ریت تاریخی این کوین/جهت پایین است"

    return False, "نیاز به سختگیری اضافه نیست"


def get_smart_tp_suggestion(symbol, direction, current_tp_percent=None):
    """TP Memory helper.

    فعلاً فقط میانگین حرکت ثبت‌شده را برمی‌گرداند.
    بعداً analysis.py می‌تواند از این مقدار برای TP هوشمند استفاده کند.
    """
    profile = get_coin_learning_profile(symbol, direction)

    if not profile:
        return None

    avg_move = profile.get("average_move", 0)

    if avg_move <= 0:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "average_move_percent": avg_move,
        "current_tp_percent": current_tp_percent,
    }


def format_coin_behavior(symbol):
    rows = get_coin_stats(symbol)

    if not rows:
        return f"هنوز داده یادگیری کافی برای {symbol} وجود ندارد."

    text = f"🧠 رفتار کوین {symbol}\n\n"

    for stats in rows:
        direction = stats.get("direction")
        closed = (
            int(stats.get("tp1", 0))
            + int(stats.get("tp2", 0))
            + int(stats.get("sl", 0))
        )

        text += (
            f"{direction}\n"
            f"سیگنال واقعی: {stats.get('total', 0)} | بسته‌شده: {closed}\n"
            f"TP1: {stats.get('tp1', 0)} | TP2: {stats.get('tp2', 0)} | SL: {stats.get('sl', 0)}\n"
            f"Win Rate: {calculate_win_rate(stats)}%\n"
            f"میانگین حرکت: {average_move(stats)}%\n"
            f"میانگین RSI: {_avg(stats.get('rsi_values'))}\n"
            f"میانگین ADX: {_avg(stats.get('adx_values'))}\n"
            f"میانگین MACD Hist: {_avg(stats.get('macd_hist_values'))}\n"
            f"Ghost Total: {stats.get('ghost_total', 0)} | Ghost WR: {calculate_ghost_win_rate(stats)}%\n"
            f"بازار رایج: {_top_counter(stats.get('market_modes'))}\n\n"
        )

    return text.strip()


def format_learning_summary():
    data = load_learning()
    coins = data.get("coins", {})

    total_signals = len(data.get("signals", []))
    total_coin_directions = len(coins)

    total_real = sum(int(x.get("total", 0)) for x in coins.values())
    total_tp1 = sum(int(x.get("tp1", 0)) for x in coins.values())
    total_tp2 = sum(int(x.get("tp2", 0)) for x in coins.values())
    total_sl = sum(int(x.get("sl", 0)) for x in coins.values())

    total_ghost = sum(int(x.get("ghost_total", 0)) for x in coins.values())
    ghost_tp1 = sum(int(x.get("ghost_tp1", 0)) for x in coins.values())
    ghost_tp2 = sum(int(x.get("ghost_tp2", 0)) for x in coins.values())
    ghost_sl = sum(int(x.get("ghost_sl", 0)) for x in coins.values())

    closed = total_tp1 + total_tp2 + total_sl
    wr = round(((total_tp1 + total_tp2) / closed) * 100, 2) if closed else 0.0

    ghost_closed = ghost_tp1 + ghost_tp2 + ghost_sl
    ghost_wr = round(((ghost_tp1 + ghost_tp2) / ghost_closed) * 100, 2) if ghost_closed else 0.0

return (
        "🧠 حافظه یادگیری ربات\n\n"
        f"کل رکوردها: {total_signals}\n"
        f"کوین/جهت‌های ثبت‌شده: {total_coin_directions}\n\n"
        f"واقعی:\n"
        f"سیگنال‌ها: {total_real}\n"
        f"TP1: {total_tp1} | TP2: {total_tp2} | SL: {total_sl}\n"
        f"Win Rate: {wr}%\n\n"
        f"مخفی:\n"
        f"Ghost Total: {total_ghost}\n"
        f"Ghost TP1: {ghost_tp1} | Ghost TP2: {ghost_tp2} | Ghost SL: {ghost_sl}\n"
        f"Ghost WR: {ghost_wr}%"
    )


def format_smart_stats():
    data = load_learning()
    rows = []

    for stats in data.get("coins", {}).values():
        closed = (
            int(stats.get("tp1", 0))
            + int(stats.get("tp2", 0))
            + int(stats.get("sl", 0))
        )

        if closed <= 0:
            continue

        rows.append({
            "symbol": stats.get("symbol"),
            "direction": stats.get("direction"),
            "closed": closed,
            "tp": int(stats.get("tp1", 0)) + int(stats.get("tp2", 0)),
            "sl": int(stats.get("sl", 0)),
            "wr": calculate_win_rate(stats),
            "move": average_move(stats),
        })

    if not rows:
        return "هنوز نتیجه کافی برای آمار هوشمند ثبت نشده."

    best = sorted(rows, key=lambda x: (x["wr"], x["closed"]), reverse=True)[:5]
    worst = sorted(rows, key=lambda x: (x["sl"], -x["wr"], x["closed"]), reverse=True)[:5]

    text = "📊 آمار هوشمند\n\n🏆 بهترین‌ها:\n"

    for x in best:
        text += (
            f"{x['symbol']} {x['direction']} | "
            f"WR: {x['wr']}% | "
            f"معاملات: {x['closed']} | "
            f"حرکت: {x['move']}%\n"
        )

    text += "\n⚠️ ضعیف‌ترین‌ها:\n"

    for x in worst:
        text += (
            f"{x['symbol']} {x['direction']} | "
            f"SL: {x['sl']} | "
            f"WR: {x['wr']}% | "
            f"معاملات: {x['closed']}\n"
        )

    return text.strip()
