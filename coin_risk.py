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
            "updated_at": None
        }

    return day_data[key]


def register_result(symbol, direction, result):
    if not symbol or direction not in ["LONG", "SHORT"]:
        return None

    data = load_risk()
    day_data = _ensure_today(data)
    item = _ensure_coin(day_data, symbol, direction)

    result = str(result).upper()

    if result == "SL":
        item["sl_count"] += 1
    elif result in ["TP1", "TP2"]:
        item["tp_count"] += 1

    sl = item["sl_count"]

    if sl <= 2:
        item["risk_level"] = "NORMAL"
        item["strictness"] = 0
    elif sl == 3:
        item["risk_level"] = "CAUTION"
        item["strictness"] = 1
    elif sl == 4:
        item["risk_level"] = "HIGH"
        item["strictness"] = 2
    else:
        item["risk_level"] = "EXTREME"
        item["strictness"] = 3

    item["updated_at"] = _now()
    save_risk(data)
    return item


def get_risk(symbol, direction):
    data = load_risk()
    day_data = data.get("daily", {}).get(_today(), {})
    return day_data.get(_key(symbol, direction), {
        "symbol": symbol,
        "direction": direction,
        "sl_count": 0,
        "tp_count": 0,
        "risk_level": "NORMAL",
        "strictness": 0
    })


def get_strictness(symbol, direction):
    if not is_ai_enabled():
        return 0

    risk = get_risk(symbol, direction)
    return int(risk.get("strictness", 0))


def should_be_extra_strict(symbol, direction):
    return get_strictness(symbol, direction) > 0


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

    risky.sort(key=lambda x: int(x.get("sl_count", 0)), reverse=True)

    text = "⚠️ ریسک کوین‌ها امروز\n\n"

    for item in risky[:20]:
        text += (
            f"{item.get('symbol')} {item.get('direction')}\n"
            f"SL: {item.get('sl_count', 0)} | TP: {item.get('tp_count', 0)}\n"
            f"Risk: {item.get('risk_level')} | Strictness: {item.get('strictness')}\n\n"
        )

    return text.strip()
