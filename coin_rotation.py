# -*- coding: utf-8 -*-

from datetime import datetime

from data_store import load_json, save_json

from coin_learning import (
    load_learning,
    calculate_win_rate,
    average_move,
)

from coin_risk import (
    get_risk,
)

ROTATION_FILE = "coin_rotation.json"


def _now():
    return datetime.utcnow().isoformat()


def _empty_data():
    return {
        "version": 2,
        "updated_at": None,
        "best": [],
        "worst": [],
        "recommended": [],
        "reduce": []
    }


def load_rotation():
    return load_json(ROTATION_FILE, _empty_data())


def save_rotation(data):
    data["updated_at"] = _now()
    return save_json(ROTATION_FILE, data)


def _rotation_score(stats):
    wr = calculate_win_rate(stats)

    move = average_move(stats)

    tp = (
        int(stats.get("tp1", 0))
        + int(stats.get("tp2", 0))
    )

    sl = int(stats.get("sl", 0))

    risk = get_risk(
        stats.get("symbol"),
        stats.get("direction")
    )

    risk_score = int(risk.get("risk_score", 0))
    priority = int(risk.get("priority_score", 100))

    score = (
        (wr * 0.60)
        + (move * 5)
        + (tp * 2)
        - (sl * 4)
        - (risk_score * 0.25)
        + (priority * 0.10)
    )

    return round(score, 4)


def _build_row(stats):
    risk = get_risk(
        stats.get("symbol"),
        stats.get("direction")
    )

    return {
        "symbol": stats.get("symbol"),
        "direction": stats.get("direction"),

        "win_rate": calculate_win_rate(stats),
        "avg_move": average_move(stats),

        "tp": (
            int(stats.get("tp1", 0))
            + int(stats.get("tp2", 0))
        ),

        "sl": int(stats.get("sl", 0)),

        "trades": (
            int(stats.get("tp1", 0))
            + int(stats.get("tp2", 0))
            + int(stats.get("sl", 0))
        ),

        "risk_score": int(
            risk.get("risk_score", 0)
        ),

        "priority_score": int(
            risk.get("priority_score", 100)
        ),

        "recommend_reduce": bool(
            risk.get("recommend_reduce", False)
        ),

        "rotation_score": _rotation_score(stats)
    }


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

        rows.append(
            _build_row(stats)
        )

    best = sorted(
        rows,
        key=lambda x: (
            x["rotation_score"],
            x["win_rate"],
            x["avg_move"]
        ),
        reverse=True
    )

    worst = sorted(
        rows,
        key=lambda x: (
            x["risk_score"],
            x["sl"],
            -x["win_rate"]
        ),
        reverse=True
    )

recommended = [
        x for x in best
        if not x.get("recommend_reduce")
        and int(x.get("risk_score", 0)) < 60
    ]

    reduce = [
        x for x in worst
        if x.get("recommend_reduce")
        or int(x.get("risk_score", 0)) >= 80
    ]

    data = {
        "version": 2,
        "updated_at": _now(),
        "best": best[:30],
        "worst": worst[:30],
        "recommended": recommended[:30],
        "reduce": reduce[:30],
    }

    save_rotation(data)
    return data


def get_best_coins(limit=10):
    data = load_rotation()
    return data.get("best", [])[:limit]


def get_worst_coins(limit=10):
    data = load_rotation()
    return data.get("worst", [])[:limit]


def get_recommended_coins(limit=10):
    data = load_rotation()
    return data.get("recommended", [])[:limit]


def get_reduce_coins(limit=10):
    data = load_rotation()
    return data.get("reduce", [])[:limit]


def format_best_coins():
    rows = get_best_coins()

    if not rows:
        return "هنوز داده کافی برای بهترین کوین‌ها وجود ندارد."

    text = "🏆 بهترین کوین‌ها\n\n"

    for item in rows:
        text += (
            f"{item.get('symbol')} {item.get('direction')}\n"
            f"Rotation Score: {item.get('rotation_score')}\n"
            f"Win Rate: {item.get('win_rate')}%\n"
            f"Trades: {item.get('trades')}\n"
            f"Avg Move: {item.get('avg_move')}%\n"
            f"Risk: {item.get('risk_score')}\n\n"
        )

    return text.strip()


def format_worst_coins():
    rows = get_worst_coins()

    if not rows:
        return "هنوز داده کافی برای بدترین کوین‌ها وجود ندارد."

    text = "⚠️ بدترین کوین‌ها\n\n"

    for item in rows:
        text += (
            f"{item.get('symbol')} {item.get('direction')}\n"
            f"SL: {item.get('sl')}\n"
            f"Win Rate: {item.get('win_rate')}%\n"
            f"Risk Score: {item.get('risk_score')}\n"
            f"Priority: {item.get('priority_score')}\n\n"
        )

    return text.strip()


def format_recommended_coins():
    rows = get_recommended_coins()

    if not rows:
        return "هنوز کوین پیشنهادی کافی وجود ندارد."

    text = "✅ کوین‌های پیشنهادی برای اولویت بیشتر\n\n"

    for item in rows:
        text += (
            f"{item.get('symbol')} {item.get('direction')}\n"
            f"Score: {item.get('rotation_score')}\n"
            f"WR: {item.get('win_rate')}%\n"
            f"Risk: {item.get('risk_score')}\n\n"
        )

    return text.strip()


def format_reduce_coins():
    rows = get_reduce_coins()

    if not rows:
        return "فعلاً کوینی برای کاهش اولویت پیشنهاد نشده."

    text = "⛔ کوین‌های پیشنهادی برای کاهش اولویت امروز\n\n"

    for item in rows:
        text += (
            f"{item.get('symbol')} {item.get('direction')}\n"
            f"SL: {item.get('sl')}\n"
            f"Risk: {item.get('risk_score')}\n"
            f"پیشنهاد کاهش: {'بله' if item.get('recommend_reduce') else 'خیر'}\n\n"
        )

    return text.strip()
