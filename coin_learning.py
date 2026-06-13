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
            "last_updated": None
        }

    return data["coins"][key]


def create_signal_snapshot(result, signal_type="REAL"):
    return {
        "id": result.get("signal_id") or f"{result.get('symbol')}_{int(datetime.utcnow().timestamp())}",
        "signal_type": signal_type,

        "symbol": result.get("symbol"),
        "direction": result.get("direction"),

        "entry": result.get("entry"),
        "tp1": result.get("tp1"),
        "tp2": result.get("tp2"),
        "stop_loss": result.get("stop_loss"),

        "score": result.get("score"),
        "confirmations": result.get("confirmations"),
        "risk_level": result.get("risk_level"),

        "rsi": result.get("rsi"),
        "adx": result.get("adx"),
        "macd": result.get("macd"),
        "macd_signal": result.get("macd_signal"),
        "macd_hist": result.get("macd_hist"),

        "buy_power": result.get("buy_power"),
        "sell_power": result.get("sell_power"),
        "buy_power_2": result.get("buy_power_2"),
        "sell_power_2": result.get("sell_power_2"),
        "buy_power_3": result.get("buy_power_3"),
        "sell_power_3": result.get("sell_power_3"),

        "atr": result.get("atr"),
        "market_mode": result.get("market_mode"),
        "coin_behavior": result.get("coin_behavior"),
        "btc_bias": result.get("btc_bias"),

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

    data["signals"].append(snapshot)

    stats = _ensure_coin_stats(data, snapshot["symbol"], snapshot["direction"])
    if signal_type == "GHOST":
        stats["ghost_total"] += 1
    else:
        stats["total"] += 1

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
        try:
            stats["moves"].append(float(move_percent))
            stats["moves"] = stats["moves"][-200:]
        except Exception:
            pass

    stats["last_updated"] = _now()
    save_learning(data)
    return True


def get_coin_stats(symbol, direction=None):
    data = load_learning()
    results = []

    for key, stats in data.get("coins", {}).items():
        if stats.get("symbol") != symbol:
            continue
        if direction and stats.get("direction") != direction:
            continue
        results.append(stats)

    return results


def calculate_win_rate(stats):
    total_closed = int(stats.get("tp1", 0)) + int(stats.get("tp2", 0)) + int(stats.get("sl", 0))
    if total_closed <= 0:
        return 0.0

    wins = int(stats.get("tp1", 0)) + int(stats.get("tp2", 0))
    return round((wins / total_closed) * 100, 2)


def average_move(stats):
    moves = stats.get("moves") or []
    if not moves:
        return 0.0
    return round(sum(moves) / len(moves), 4)


def format_coin_behavior(symbol):
    rows = get_coin_stats(symbol)

    if not rows:
        return f"هنوز داده یادگیری کافی برای {symbol} وجود ندارد."

    text = f"🧠 رفتار کوین {symbol}\n\n"

    for stats in rows:
        direction = stats.get("direction")
        wr = calculate_win_rate(stats)
        avg_move = average_move(stats)

        text += (
            f"{direction}\n"
            f"معاملات: {stats.get('total', 0)}\n"
            f"TP1: {stats.get('tp1', 0)} | TP2: {stats.get('tp2', 0)} | SL: {stats.get('sl', 0)}\n"
            f"Win Rate: {wr}%\n"
            f"میانگین حرکت: {avg_move}%\n\n"
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

    return (
        "🧠 حافظه یادگیری ربات\n\n"
        f"کل رکوردها: {total_signals}\n"
        f"کوین/جهت‌های ثبت‌شده: {total_coins}\n"
        f"TP1: {total_tp1}\n"
        f"TP2: {total_tp2}\n"
        f"SL: {total_sl}"
    )
