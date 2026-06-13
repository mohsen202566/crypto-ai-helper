# -*- coding: utf-8 -*-
from datetime import datetime
from data_store import load_json, save_json
from coin_learning import load_learning, calculate_win_rate

ROTATION_FILE = "coin_rotation.json"


def _now():
    return datetime.utcnow().isoformat()


def _empty_data():
    return {
        "version": 1,
        "updated_at": None,
        "best": [],
        "worst": []
    }


def load_rotation():
    return load_json(ROTATION_FILE, _empty_data())


def save_rotation(data):
    data["updated_at"] = _now()
    return save_json(ROTATION_FILE, data)


def rebuild_rotation():
    learning = load_learning()

    rows = []

    for stats in learning.get("coins", {}).values():

        closed = (
            int(stats.get("tp1", 0))
            + int(stats.get("tp2", 0))
            + int(stats.get("sl", 0))
        )

        if closed < 3:
            continue

        wr = calculate_win_rate(stats)

        rows.append({
            "symbol": stats["symbol"],
            "direction": stats["direction"],
            "win_rate": wr,
            "trades": closed,
            "tp": int(stats.get("tp1", 0)) + int(stats.get("tp2", 0)),
            "sl": int(stats.get("sl", 0))
        })

    best = sorted(
        rows,
        key=lambda x: (x["win_rate"], x["trades"]),
        reverse=True
    )

    worst = sorted(
        rows,
        key=lambda x: (x["sl"], -x["win_rate"]),
        reverse=True
    )

    data = {
        "version": 1,
        "updated_at": _now(),
        "best": best[:30],
        "worst": worst[:30]
    }

    save_rotation(data)
    return data


def get_best_coins(limit=10):
    data = load_rotation()
    return data.get("best", [])[:limit]


def get_worst_coins(limit=10):
    data = load_rotation()
    return data.get("worst", [])[:limit]


def format_best_coins():
    rows = get_best_coins()

    if not rows:
        return "هنوز داده کافی برای بهترین کوین‌ها وجود ندارد."

    text = "🏆 بهترین کوین‌ها\n\n"

    for item in rows:
        text += (
            f"{item['symbol']} {item['direction']}\n"
            f"Win Rate: {item['win_rate']}%\n"
            f"Trades: {item['trades']}\n\n"
        )

    return text.strip()


def format_worst_coins():
    rows = get_worst_coins()

    if not rows:
        return "هنوز داده کافی برای بدترین کوین‌ها وجود ندارد."

    text = "⚠️ بدترین کوین‌ها\n\n"

    for item in rows:
        text += (
            f"{item['symbol']} {item['direction']}\n"
            f"SL: {item['sl']}\n"
            f"Win Rate: {item['win_rate']}%\n\n"
        )

    return text.strip()
