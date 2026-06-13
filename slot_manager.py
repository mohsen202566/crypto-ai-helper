# -*- coding: utf-8 -*-

from datetime import datetime

from data_store import load_json, save_json

SLOT_FILE = "slot_state.json"

DEFAULT_MAX_SLOTS = 10


def _now():
    return datetime.utcnow().isoformat()


def _empty_data():
    return {
        "version": 2,

        "max_slots": DEFAULT_MAX_SLOTS,

        "active_positions": [],

        "total_opened": 0,
        "total_closed": 0,

        "updated_at": None
    }


def load_slots():
    data = load_json(SLOT_FILE, _empty_data())

    data.setdefault("active_positions", [])

    return data


def save_slots(data):
    data["updated_at"] = _now()
    return save_json(SLOT_FILE, data)


def get_max_slots():
    return int(
        load_slots().get(
            "max_slots",
            DEFAULT_MAX_SLOTS
        )
    )


def set_max_slots(count):
    data = load_slots()

    count = max(
        1,
        int(count)
    )

    data["max_slots"] = count

    save_slots(data)

    return count


def active_count():
    return len(
        load_slots().get(
            "active_positions",
            []
        )
    )


def free_slots():
    data = load_slots()

    return max(
        0,
        int(data["max_slots"])
        - len(data["active_positions"])
    )


def has_free_slot():
    return free_slots() > 0


def slot_usage_percent():
    data = load_slots()

    total = int(data["max_slots"])

    if total <= 0:
        return 0

    return round(
        (
            len(data["active_positions"])
            / total
        )
        * 100,
        2
    )


def add_position(
    signal_id,
    symbol,
    direction,
    score=None
):

    data = load_slots()

    for pos in data["active_positions"]:
        if str(pos.get("signal_id")) == str(signal_id):
            return False

    if len(data["active_positions"]) >= int(data["max_slots"]):
        return False

    data["active_positions"].append({
        "signal_id": signal_id,
        "symbol": symbol,
        "direction": direction,

        "score": score,

        "opened_at": _now()
    })

    data["total_opened"] = (
        int(data.get("total_opened", 0))
        + 1
    )

    save_slots(data)

    return True

def close_position(signal_id):
    data = load_slots()

    before = len(data["active_positions"])

    data["active_positions"] = [
        x for x in data["active_positions"]
        if str(x.get("signal_id")) != str(signal_id)
    ]

    changed = len(data["active_positions"]) != before

    if changed:
        data["total_closed"] = (
            int(data.get("total_closed", 0))
            + 1
        )
        save_slots(data)

    return changed


def get_active_positions():
    return load_slots().get(
        "active_positions",
        []
    )


def is_symbol_active(symbol, direction=None):
    active = get_active_positions()

    for pos in active:
        if pos.get("symbol") != symbol:
            continue

        if direction and pos.get("direction") != direction:
            continue

        return True

    return False


def can_open_new_position(symbol=None, direction=None):
    if not has_free_slot():
        return False, "NO_FREE_SLOT"

    if symbol and is_symbol_active(symbol, direction):
        return False, "DUPLICATE_ACTIVE_SYMBOL"

    return True, "OK"


def get_free_slot_count():
    return free_slots()


def get_slot_state():
    data = load_slots()

    return {
        "max_slots": int(data.get("max_slots", DEFAULT_MAX_SLOTS)),
        "active": len(data.get("active_positions", [])),
        "free": free_slots(),
        "usage_percent": slot_usage_percent(),
        "total_opened": int(data.get("total_opened", 0)),
        "total_closed": int(data.get("total_closed", 0)),
    }


def select_best_candidates(candidates, limit=None):
    """Sort candidate signals for slot filling.

    candidates: list of analysis result dicts.
    This does not open positions; it only sorts.
    """

    if not candidates:
        return []

    if limit is None:
        limit = free_slots()

    clean = []

    for item in candidates:
        if not item:
            continue

        if item.get("direction") not in ["LONG", "SHORT"]:
            continue

        if item.get("status") == "NO_TRADE":
            continue

        clean.append(item)

    clean.sort(
        key=lambda x: (
            int(x.get("score") or 0),
            int(x.get("confirmations") or 0),
            -float(x.get("risk_reward") or 0)
        ),
        reverse=True
    )

    return clean[:max(0, int(limit))]


def format_slots_report():
    data = load_slots()

    active = data.get("active_positions", [])
    free = free_slots()

    text = (
        "🎯 وضعیت اسلات‌ها\n\n"
        f"حداکثر اسلات: {data.get('max_slots', DEFAULT_MAX_SLOTS)}\n"
        f"فعال: {len(active)}\n"
        f"خالی: {free}\n"
        f"درصد استفاده: {slot_usage_percent()}٪\n\n"
        f"کل بازشده: {data.get('total_opened', 0)}\n"
        f"کل بسته‌شده: {data.get('total_closed', 0)}\n\n"
    )

    if not active:
        text += "هیچ پوزیشن فعالی وجود ندارد."
        return text

    text += "پوزیشن‌های فعال:\n\n"

    for pos in active[:20]:
        text += (
            f"{pos.get('symbol')} "
            f"{pos.get('direction')} | "
            f"Score: {pos.get('score', 'نامشخص')}\n"
        )

    return text.strip()
