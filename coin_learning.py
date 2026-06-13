# -*- coding: utf-8 -*-
from datetime import datetime
from data_store import load_json, save_json
from ai_memory import is_learning_enabled

LEARNING_FILE = "coin_learning.json"


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


def _coin_key(symbol, direction):
    return f"{symbol}_{direction}"


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
            "power2_buy_values": [],
            "power2_sell_values": [],

            "last_updated": None
        }

    return data["coins"][key]


def _get_signal_id(result):
    return (
        result.get("signal_id")
        or result.get("id")
        or f"{result.get('symbol')}_{result.get('direction')}_{int(datetime.utcnow().timestamp())}"
    )


def create_signal_snapshot(result, signal_type="REAL"):
    return {
        "id": _get_signal_id(result),
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

        "buy_power": result.get("buy_power"),
        "sell_power": result.get("sell_power"),

        "power2_buy": result.get("power2_buy") or result.get("buy_power_2"),
        "power2_sell": result.get("power2_sell") or result.get("sell_power_2"),
        "power3_buy": result.get("power3_buy") or result.get("buy_power_3"),
        "power3_sell": result.get("power3_sell") or result.get("sell_power_3"),

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
    data["signals"] = data["signals"][-10000:]

    stats = _ensure_coin_stats(data, snapshot["symbol"], snapshot["direction"])

    if signal_type == "GHOST":
        stats["ghost_total"] += 1
    else:
        stats["total"] += 1

    _append_indicator_values(stats, snapshot)
    stats["last_updated"] = _now()

    save_learning(data)
    return snapshot["id"]


def _append_float(target_list, value, max_len=300):
    try:
        if value is None:
            return
        target_list.append(float(value))
        del target_list[:-max_len]
    except Exception:
        return


def _append_indicator_values(stats, snapshot):
    _append_float(stats.setdefault("rsi_values", []), snapshot.get("rsi"))
    _append_float(stats.setdefault("adx_values", []), snapshot.get("adx"))
    _append_float(stats.setdefault("power2_buy_values", []), snapshot.get("power2_buy"))
    _append_float(stats.setdefault("power2_sell_values", []), snapshot.get("power2_sell"))


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

    symbol = found.get("symbol")
    direction = found.get("direction")
    signal_type = found.get("signal_type", "REAL")

    stats = _ensure_coin_stats(data, symbol, direction)

    if signal_type == "GHOST":
        if result == "TP1":
            stats["ghost_tp1"] += 1
        elif result == "TP2":
            stats["ghost_tp2"] += 1
        elif result == "SL":
            stats["ghost_sl"] += 1
    else:
        if result == "TP1":
            stats["tp1"] += 1
        elif result == "TP2":
            stats["tp2"] += 1
        elif result == "SL":
            stats["sl"] += 1

    if move_percent is not None:
        _append_float(stats.setdefault("moves", []), move_percent, max_len=500)

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
    total_closed = int(stats.get("tp1", 0)) + int(stats.get("tp2", 0)) + int(stats.get("sl", 0))
    if total_closed <= 0:
        return 0.0

    wins = int(stats.get("tp1", 0)) + int(stats.get("tp2", 0))
    return round((wins / total_closed) * 100, 2)


def average_list(values):
    if not values:
        return 0.0
    try:
        return round(sum(float(x) for x in values) / len(values), 4)
    except Exception:
        return 0.0


def average_move(stats):
    return average_list(stats.get("moves") or [])


def format_coin_behavior(symbol):
    rows = get_coin_stats(symbol)

    if not rows:
        return f"هنوز داده یادگیری کافی برای {symbol} وجود ندارد."

    text = f"🧠 رفتار کوین {symbol}\n\n"

    for stats in rows:
        direction = stats.get("direction")
        wr = calculate_win_rate(stats)
        avg_move = average_move(stats)
        avg_rsi = average_list(stats.get("rsi_values") or [])
        avg_adx = average_list(stats.get("adx_values") or [])

        text += (
            f"{direction}\n"
            f"معاملات: {stats.get('total', 0)}\n"
            f"TP1: {stats.get('tp1', 0)} | TP2: {stats.get('tp2', 0)} | SL: {stats.get('sl', 0)}\n"
            f"Win Rate: {wr}%\n"
            f"میانگین حرکت: {avg_move}%\n"
            f"میانگین RSI: {avg_rsi}\n"
            f"میانگین ADX: {avg_adx}\n\n"
        )

    return text.strip()


def format_learning_summary():
    data = load_learning()
    coins = data.get("coins", {})

    total_signals = len(data.get("signals", []))
    total_coins = len(coins)

    total_tp1 = sum(int(x.get("tp1", 0)) for x in coins.values())
    total_tp2 = sum(int(x.get("tp2", 0)) for x in coins.values())
    total_sl = sum(int(x.get("sl", 0)) for x in coins.values())

    ghost_total = sum(int(x.get("ghost_total", 0)) for x in coins.values())
    ghost_tp1 = sum(int(x.get("ghost_tp1", 0)) for x in coins.values())
    ghost_tp2 = sum(int(x.get("ghost_tp2", 0)) for x in coins.values())
    ghost_sl = sum(int(x.get("ghost_sl", 0)) for x in coins.values())

    return (
        "🧠 حافظه یادگیری ربات\n\n"
        f"کل رکوردها: {total_signals}\n"
        f"کوین/جهت‌های ثبت‌شده: {total_coins}\n\n"
        f"واقعی:\n"
        f"TP1: {total_tp1}\n"
        f"TP2: {total_tp2}\n"
        f"SL: {total_sl}\n\n"
        f"مخفی:\n"
        f"Ghost Total: {ghost_total}\n"
        f"Ghost TP1: {ghost_tp1}\n"
        f"Ghost TP2: {ghost_tp2}\n"
        f"Ghost SL: {ghost_sl}"
    )


def format_smart_stats():
    data = load_learning()
    coins = list(data.get("coins", {}).values())

    if not coins:
        return "هنوز داده‌ای برای آمار هوشمند ثبت نشده."

    rows = []
    for stats in coins:
        closed = int(stats.get("tp1", 0)) + int(stats.get("tp2", 0)) + int(stats.get("sl", 0))
        if closed <= 0:
            continue
        rows.append({
            "symbol": stats.get("symbol"),
            "direction": stats.get("direction"),
            "closed": closed,
            "wr": calculate_win_rate(stats),
            "sl": int(stats.get("sl", 0)),
            "avg_move": average_move(stats)
        })

    if not rows:
        return "هنوز نتیجه TP/SL کافی برای آمار هوشمند ثبت نشده."

    best = sorted(rows, key=lambda x: (x["wr"], x["closed"]), reverse=True)[:5]
    worst = sorted(rows, key=lambda x: (x["sl"], -x["wr"]), reverse=True)[:5]

    text = "📊 آمار هوشمند\n\n🏆 بهترین‌ها:\n"
    for x in best:
        text += f"{x['symbol']} {x['direction']} | WR: {x['wr']}% | معاملات: {x['closed']} | حرکت: {x['avg_move']}%\n"

    text += "\n⚠️ ضعیف‌ترین‌ها:\n"
    for x in worst:
        text += f"{x['symbol']} {x['direction']} | SL: {x['sl']} | WR: {x['wr']}% | معاملات: {x['closed']}\n"

    return text.strip()
