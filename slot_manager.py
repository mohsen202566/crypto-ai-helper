# -*- coding: utf-8 -*-
from datetime import datetime
from data_store import load_json, save_json

SLOT_FILE = "slot_state.json"

DEFAULT_MAX_SLOTS = 10


def _now():
    return datetime.utcnow().isoformat()


def _empty_data():
    return {
        "version": 1,
        "max_slots": DEFAULT_MAX_SLOTS,
        "active_positions": [],
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
    return int(load_slots().get("max_slots", DEFAULT_MAX_SLOTS))


def set_max_slots(count):
    data = load_slots()
    data["max_slots"] = max(1, int(count))
    save_slots(data)
    return data["max_slots"]


def active_count():
    data = load_slots()
    return len(data.get("active_positions", []))


def free_slots():
    data = load_slots()
    return max(0, int(data["max_slots"]) - len(data["active_positions"]))


def has_free_slot():
    return free_slots() > 0


def add_position(signal_id, symbol, direction):
    data = load_slots()

    for pos in data["active_positions"]:
        if str(pos.get("signal_id")) == str(signal_id):
            return False

    data["active_positions"].append({
        "signal_id": signal_id,
        "symbol": symbol,
        "direction": direction,
        "opened_at": _now()
    })

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
        save_slots(data)

    return changed


def get_active_positions():
    return load_slots().get("active_positions", [])


def format_slots_report():
    data = load_slots()

    active = data.get("active_positions", [])
    free = free_slots()

    text = (
        "🎯 وضعیت اسلات‌ها\n\n"
        f"حداکثر اسلات: {data['max_slots']}\n"
        f"فعال: {len(active)}\n"
        f"خالی: {free}\n\n"
    )

    if not active:
        text += "هیچ پوزیشن فعالی وجود ندارد."
        return text

    text += "پوزیشن‌های فعال:\n\n"

    for pos in active[:20]:
        text += (
            f"{pos['symbol']} "
            f"{pos['direction']}\n"
        )

    return text.strip()
