# -*- coding: utf-8 -*-
from datetime import datetime
from data_store import load_json, save_json
from ai_memory import is_learning_enabled

SR_FILE = "sr_memory.json"


def _now():
    return datetime.utcnow().isoformat()


def _empty_data():
    return {
        "version": 1,
        "events": [],
        "summary": {},
        "updated_at": None
    }


def load_sr_memory():
    data = load_json(SR_FILE, _empty_data())
    data.setdefault("events", [])
    data.setdefault("summary", {})
    return data


def save_sr_memory(data):
    data["updated_at"] = _now()
    return save_json(SR_FILE, data)


def _key(symbol, timeframe, level_type, action, direction):
    return f"{symbol}_{timeframe}_{level_type}_{action}_{direction}"


def _ensure_summary(data, symbol, timeframe, level_type, action, direction):
    key = _key(symbol, timeframe, level_type, action, direction)

    if key not in data["summary"]:
        data["summary"][key] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "level_type": level_type,  # SUPPORT / RESISTANCE
            "action": action,          # BREAK / BOUNCE / FAKE_BREAK
            "direction": direction,    # LONG / SHORT
            "count": 0,
            "success": 0,
            "fail": 0,
            "moves": [],
            "last_updated": None
        }

    return data["summary"][key]


def record_sr_event(
    symbol,
    timeframe,
    level_type,
    action,
    direction,
    price=None,
    result=None,
    move_percent=None,
    extra=None
):
    if not is_learning_enabled():
        return False

    if direction not in ["LONG", "SHORT"]:
        return False

    level_type = str(level_type).upper()
    action = str(action).upper()
    result = str(result).upper() if result else None

    data = load_sr_memory()

    event = {
        "symbol": symbol,
        "timeframe": timeframe,
        "level_type": level_type,
        "action": action,
        "direction": direction,
        "price": price,
        "result": result,
        "move_percent": move_percent,
        "extra": extra or {},
        "created_at": _now()
    }

    data["events"].append(event)
    data["events"] = data["events"][-5000:]

    summary = _ensure_summary(
        data,
        symbol,
        timeframe,
        level_type,
        action,
        direction
    )

    summary["count"] += 1

    if result in ["TP1", "TP2", "SUCCESS"]:
        summary["success"] += 1
    elif result in ["SL", "FAIL"]:
        summary["fail"] += 1

    if move_percent is not None:
        try:
            summary["moves"].append(float(move_percent))
            summary["moves"] = summary["moves"][-300:]
        except Exception:
            pass

    summary["last_updated"] = _now()
    save_sr_memory(data)
    return True


def average_move(summary):
    moves = summary.get("moves") or []
    if not moves:
        return 0.0
    return round(sum(moves) / len(moves), 4)


def success_rate(summary):
    total = int(summary.get("success", 0)) + int(summary.get("fail", 0))
    if total <= 0:
        return 0.0
    return round((int(summary.get("success", 0)) / total) * 100, 2)


def get_symbol_sr_summary(symbol):
    data = load_sr_memory()
    rows = []

    for item in data.get("summary", {}).values():
        if item.get("symbol") == symbol:
            rows.append(item)

    return rows


def format_sr_summary(symbol):
    rows = get_symbol_sr_summary(symbol)

    if not rows:
        return f"هنوز داده حمایت/مقاومت برای {symbol} وجود ندارد."

    rows = sorted(
        rows,
        key=lambda x: (int(x.get("count", 0)), success_rate(x)),
        reverse=True
    )

    text = f"📊 حافظه حمایت/مقاومت {symbol}\n\n"

    for item in rows[:10]:
        text += (
            f"{item.get('timeframe')} "
            f"{item.get('level_type')} "
            f"{item.get('action')} "
            f"{item.get('direction')}\n"
            f"تعداد: {item.get('count', 0)} | موفقیت: {success_rate(item)}%\n"
            f"میانگین حرکت: {average_move(item)}%\n\n"
        )

    return text.strip()


def format_sr_global_summary():
    data = load_sr_memory()
    events = data.get("events", [])
    summary = data.get("summary", {})

    return (
        "📊 حافظه حمایت/مقاومت\n\n"
        f"کل رویدادها: {len(events)}\n"
        f"الگوهای ذخیره‌شده: {len(summary)}"
    )
