# -*- coding: utf-8 -*-
from datetime import datetime
from data_store import load_json, save_json
from ai_memory import is_ai_enabled

RISK_FILE = "coin_risk.json"


def _today():
    return datetime.utcnow().strftime("%Y-%m-%d")


def _now():
    return datetime.utcnow().isoformat()


def _empty_data():
    return {
        "version": 1,
        "daily": {},
        "updated_at": None
    }


def load_risk():
    data = load_json(RISK_FILE, _empty_data())
    data.setdefault("daily", {})
    return data


def save_risk(data):
    data["updated_at"] = _now()
    return save_json(RISK_FILE, data)


def _key(symbol, direction):
    return f"{symbol}_{direction}"


def _ensure_today(data):
    day = _today()
    data["daily"].setdefault(day, {})
    return data["daily"][day]


def _ensure_coin(day_data, symbol, direction):
    key = _key(symbol, direction)

    if key not in day_data:
        day_data[key] = {
            "symbol": symbol,
            "direction": direction,

            "sl_count": 0,
            "tp_count": 0,

            "risk_level": "NORMAL",
            "strictness": 0,

            "risk_score": 0,
            "priority_score": 100,

            "bad_day": False,
            "recommend_reduce": False,

            "updated_at": None
        }

    return day_data[key]


def _recalculate_risk(item):
    sl = int(item.get("sl_count", 0))
    tp = int(item.get("tp_count", 0))

    if sl <= 2:
        item["risk_level"] = "NORMAL"
        item["strictness"] = 0
        item["risk_score"] = sl * 10

    elif sl == 3:
        item["risk_level"] = "CAUTION"
        item["strictness"] = 1
        item["risk_score"] = 40

    elif sl == 4:
        item["risk_level"] = "HIGH"
        item["strictness"] = 2
        item["risk_score"] = 60

    elif sl == 5:
        item["risk_level"] = "VERY_HIGH"
        item["strictness"] = 3
        item["risk_score"] = 80

    else:
        item["risk_level"] = "EXTREME"
        item["strictness"] = 4
        item["risk_score"] = 100

    item["priority_score"] = max(
        0,
        100 + (tp * 3) - (sl * 10)
    )

    item["bad_day"] = sl >= 5
    item["recommend_reduce"] = sl >= 6
    item["updated_at"] = _now()

    return item


def register_result(symbol, direction, result):
    if not symbol or direction not in ["LONG", "SHORT"]:
        return None

    data = load_risk()
    day_data = _ensure_today(data)
    item = _ensure_coin(day_data, symbol, direction)

    result = str(result).upper()

    if result == "SL":
        item["sl_count"] = int(item.get("sl_count", 0)) + 1

    elif result in ["TP1", "TP2"]:
        item["tp_count"] = int(item.get("tp_count", 0)) + 1

    _recalculate_risk(item)
    save_risk(data)
    return item


def get_risk(symbol, direction):
    data = load_risk()
    day_data = data.get("daily", {}).get(_today(), {})

    default = {
        "symbol": symbol,
        "direction": direction,
        "sl_count": 0,
        "tp_count": 0,
        "risk_level": "NORMAL",
        "strictness": 0,
        "risk_score": 0,
        "priority_score": 100,
        "bad_day": False,
        "recommend_reduce": False
    }

    return day_data.get(_key(symbol, direction), default)


def get_strictness(symbol, direction):
    if not is_ai_enabled():
        return 0

    risk = get_risk(symbol, direction)
    return int(risk.get("strictness", 0))


def should_be_extra_strict(symbol, direction):
    return get_strictness(symbol, direction) > 0


def get_risk_score(symbol, direction):
    if not is_ai_enabled():
        return 0

    risk = get_risk(symbol, direction)
    return int(risk.get("risk_score", 0))


def get_priority_score(symbol, direction):
    if not is_ai_enabled():
        return 100

    risk = get_risk(symbol, direction)
    return int(risk.get("priority_score", 100))


def should_reduce_symbol(symbol, direction):
    if not is_ai_enabled():
        return False

    risk = get_risk(symbol, direction)
    return bool(risk.get("recommend_reduce", False))


def get_risky_coins(limit=20):
    data = load_risk()
    day_data = data.get("daily", {}).get(_today(), {})

    rows = list(day_data.values())

    rows.sort(
        key=lambda x: (
            int(x.get("risk_score", 0)),
            int(x.get("sl_count", 0))
        ),
        reverse=True
    )

    return rows[:limit]


def get_best_priority_coins(limit=20):
    data = load_risk()
    day_data = data.get("daily", {}).get(_today(), {})

    rows = list(day_data.values())

    rows.sort(
        key=lambda x: (
            int(x.get("priority_score", 0)),
            -int(x.get("risk_score", 0))
        ),
        reverse=True
    )

    return rows[:limit]


def format_risk_report():
    data = load_risk()
    day_data = data.get("daily", {}).get(_today(), {})

    if not day_data:
        return "امروز هنوز داده ریسک برای کوین‌ها ثبت نشده."

    risky = [
        x for x in day_data.values()
        if int(x.get("sl_count", 0)) >= 3
    ]

    if not risky:
        return "✅ امروز هنوز هیچ کوین/جهتی وارد حالت ریسک بالا نشده."

    risky.sort(
        key=lambda x: (
            int(x.get("risk_score", 0)),
            int(x.get("sl_count", 0))
        ),
        reverse=True
    )

    text = "⚠️ ریسک کوین‌ها امروز\n\n"

    for item in risky[:20]:
        text += (
            f"{item.get('symbol')} {item.get('direction')}\n"
            f"SL: {item.get('sl_count', 0)} | TP: {item.get('tp_count', 0)}\n"
            f"Risk: {item.get('risk_level')} | Strictness: {item.get('strictness')}\n"
            f"Risk Score: {item.get('risk_score', 0)} | Priority: {item.get('priority_score', 0)}\n"
        )

        if item.get("recommend_reduce"):
            text += "پیشنهاد: کاهش اولویت امروز\n"

        text += "\n"

    return text.strip()


def format_priority_report():
    rows = get_best_priority_coins(limit=15)

    if not rows:
        return "هنوز داده‌ای برای اولویت‌بندی کوین‌ها وجود ندارد."

    text = "🏆 اولویت کوین‌ها امروز\n\n"

    for item in rows:
        text += (
            f"{item.get('symbol')} {item.get('direction')}\n"
            f"Priority: {item.get('priority_score', 100)} | "
            f"Risk: {item.get('risk_score', 0)} | "
            f"TP: {item.get('tp_count', 0)} | "
            f"SL: {item.get('sl_count', 0)}\n\n"
        )

    return text.strip()
