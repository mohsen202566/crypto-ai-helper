# -*- coding: utf-8 -*-
from datetime import datetime
from data_store import load_json, save_json

AI_MEMORY_FILE = "ai_memory.json"


DEFAULT_AI_MEMORY = {
    "version": 1,
    "settings": {
        "ai_enabled": True,
        "learning_enabled": True,
        "daily_report_enabled": False,
        "mode": "normal"
    },
    "summary": {
        "total_signals": 0,
        "total_tp1": 0,
        "total_tp2": 0,
        "total_sl": 0
    },
    "coins": {},
    "updated_at": None
}


def load_ai_memory():
    data = load_json(AI_MEMORY_FILE, DEFAULT_AI_MEMORY)

    for key, value in DEFAULT_AI_MEMORY.items():
        if key not in data:
            data[key] = value

    return data


def save_ai_memory(data):
    data["updated_at"] = datetime.utcnow().isoformat()
    return save_json(AI_MEMORY_FILE, data)


def get_ai_settings():
    data = load_ai_memory()
    return data.get("settings", {})


def update_ai_setting(key, value):
    data = load_ai_memory()
    data.setdefault("settings", {})
    data["settings"][key] = value
    save_ai_memory(data)
    return data["settings"]


def is_ai_enabled():
    return bool(get_ai_settings().get("ai_enabled", True))


def is_learning_enabled():
    return bool(get_ai_settings().get("learning_enabled", True))


def set_ai_enabled(enabled):
    return update_ai_setting("ai_enabled", bool(enabled))


def set_learning_enabled(enabled):
    return update_ai_setting("learning_enabled", bool(enabled))


def set_daily_report_enabled(enabled):
    return update_ai_setting("daily_report_enabled", bool(enabled))


def set_mode(mode):
    if mode not in ["normal", "conservative"]:
        mode = "normal"
    return update_ai_setting("mode", mode)


def format_ai_status():
    settings = get_ai_settings()

    ai_status = "روشن" if settings.get("ai_enabled", True) else "خاموش"
    learning_status = "روشن" if settings.get("learning_enabled", True) else "خاموش"
    report_status = "روشن" if settings.get("daily_report_enabled", False) else "خاموش"
    mode = settings.get("mode", "normal")

    mode_fa = "عادی" if mode == "normal" else "محافظه‌کار"

    return (
        "🧠 وضعیت هوش مصنوعی ربات\n\n"
        f"AI: {ai_status}\n"
        f"یادگیری: {learning_status}\n"
        f"گزارش روزانه: {report_status}\n"
        f"حالت: {mode_fa}"
    )
