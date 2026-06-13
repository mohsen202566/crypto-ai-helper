# -*- coding: utf-8 -*-

from datetime import datetime

from data_store import load_json, save_json

from ai_memory import is_learning_enabled

from coin_learning import (
    record_signal,
    update_signal_result,
)

GHOST_FILE = "ghost_signals.json"

MAX_GHOSTS = 5000


def _now():
    return datetime.utcnow().isoformat()


def _empty_data():
    return {
        "version": 2,

        "ghost_signals": [],

        "stats": {
            "created": 0,
            "closed": 0,
            "tp1": 0,
            "tp2": 0,
            "sl": 0
        },

        "updated_at": None
    }


def load_ghosts():
    data = load_json(
        GHOST_FILE,
        _empty_data()
    )

    data.setdefault(
        "ghost_signals",
        []
    )

    data.setdefault(
        "stats",
        {}
    )

    return data


def save_ghosts(data):
    data["updated_at"] = _now()

    return save_json(
        GHOST_FILE,
        data
    )


def create_ghost_signal(result):

    if not is_learning_enabled():
        return False

    if not result:
        return False

    if result.get("direction") not in [
        "LONG",
        "SHORT"
    ]:
        return False

    learning_id = record_signal(
        result,
        signal_type="GHOST"
    )

    if not learning_id:
        return False

    data = load_ghosts()

    ghost = {
        "learning_id": learning_id,

        "symbol": result.get("symbol"),
        "direction": result.get("direction"),

        "entry": result.get("entry"),
        "tp1": result.get("tp1"),
        "tp2": result.get("tp2"),
        "stop_loss": result.get("stop_loss"),

        "score": result.get("score"),
        "confirmations": result.get("confirmations"),

        "status": "ACTIVE",

        "created_at": _now(),

        "closed_at": None,

        "result": None
    }

    data["ghost_signals"].append(
        ghost
    )

    data["ghost_signals"] = (
        data["ghost_signals"][-MAX_GHOSTS:]
    )

    data["stats"]["created"] = (
        int(data["stats"].get("created", 0))
        + 1
    )

    save_ghosts(data)

    return learning_id

def close_ghost_signal(
    learning_id,
    result,
    exit_price=None,
    move_percent=None,
    holding_minutes=None
):

    data = load_ghosts()

    found = None

    for item in data["ghost_signals"]:
        if str(item.get("learning_id")) == str(learning_id):
            found = item
            break

    if not found:
        return False

    if found.get("status") != "ACTIVE":
        return False

    result = str(result).upper()

    if result not in ["TP1", "TP2", "SL"]:
        return False

    found["status"] = "CLOSED"
    found["result"] = result
    found["closed_at"] = _now()
    found["exit_price"] = exit_price
    found["move_percent"] = move_percent
    found["holding_minutes"] = holding_minutes

    update_signal_result(
        learning_id,
        result,
        exit_price=exit_price,
        move_percent=move_percent,
        holding_minutes=holding_minutes
    )

    data["stats"]["closed"] = (
        int(data["stats"].get("closed", 0))
        + 1
    )

    if result == "TP1":
        data["stats"]["tp1"] = int(data["stats"].get("tp1", 0)) + 1
    elif result == "TP2":
        data["stats"]["tp2"] = int(data["stats"].get("tp2", 0)) + 1
    elif result == "SL":
        data["stats"]["sl"] = int(data["stats"].get("sl", 0)) + 1

    save_ghosts(data)

    return True


def get_active_ghosts():
    data = load_ghosts()

    return [
        x for x in data.get("ghost_signals", [])
        if x.get("status") == "ACTIVE"
    ]


def get_closed_ghosts():
    data = load_ghosts()

    return [
        x for x in data.get("ghost_signals", [])
        if x.get("status") == "CLOSED"
    ]


def get_ghost_by_learning_id(learning_id):
    data = load_ghosts()

    for item in data.get("ghost_signals", []):
        if str(item.get("learning_id")) == str(learning_id):
            return item

    return None


def count_active_ghosts():
    return len(get_active_ghosts())


def format_ghost_report():
    data = load_ghosts()

    ghosts = data.get("ghost_signals", [])
    stats = data.get("stats", {})

    total = len(ghosts)
    active = len([
        x for x in ghosts
        if x.get("status") == "ACTIVE"
    ])

    closed = len([
        x for x in ghosts
        if x.get("status") == "CLOSED"
    ])

    tp1 = int(stats.get("tp1", 0))
    tp2 = int(stats.get("tp2", 0))
    sl = int(stats.get("sl", 0))

    closed_results = tp1 + tp2 + sl

    wr = (
        round(((tp1 + tp2) / closed_results) * 100, 2)
        if closed_results
        else 0.0
    )

    return (
        "👻 سیگنال‌های مخفی\n\n"
        f"کل رکوردها: {total}\n"
        f"فعال: {active}\n"
        f"بسته‌شده: {closed}\n\n"
        f"TP1: {tp1}\n"
        f"TP2: {tp2}\n"
        f"SL: {sl}\n"
        f"Ghost WR: {wr}%"
    )
