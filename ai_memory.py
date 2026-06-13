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
        "total_sl": 0,
        "total_ghost": 0
    },
    "updated_at": None
}


def _now():
    return datetime.utcnow().isoformat()


def load_ai_memory():
    data = load_json(AI_MEMORY_FILE, DEFAULT_AI_MEMORY)

    for key, value in DEFAULT_AI_MEMORY.items():
        if key not in data:
            data[key] = value

    data.setdefault("settings", {})
    for key, value in DEFAULT_AI_MEMORY["settings"].items():
        if key not in data["settings"]:
            data["settings"][key] = value

    data.setdefault("summary", {})
    for key, value in DEFAULT_AI_MEMORY["summary"].items():
        if key not in data["summary"]:
            data["summary"][key] = value

    return data


def save_ai_memory(data):
    data["updated_at"] = _now()
    return save_json(AI_MEMORY_FILE, data)


def get_ai_settings():
    return load_ai_memory().get("settings", {})


def update_ai_setting(key, value):
    data = load_ai_memory()
    data.setdefault("settings", {})
    data["settings"][key] = value
    save_ai_memory(data)
    return True


def is_ai_enabled():
    return bool(get_ai_settings().get("ai_enabled", True))


def is_learning_enabled():
    return bool(get_ai_settings().get("learning_enabled", True))


def get_ai_mode():
    return get_ai_settings().get("mode", "normal")


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


def update_summary(tp1=0, tp2=0, sl=0, ghost=0, signals=0):
    data = load_ai_memory()
    summary = data.setdefault("summary", {})

    summary["total_signals"] = int(summary.get("total_signals", 0)) + int(signals)
    summary["total_tp1"] = int(summary.get("total_tp1", 0)) + int(tp1)
    summary["total_tp2"] = int(summary.get("total_tp2", 0)) + int(tp2)
    summary["total_sl"] = int(summary.get("total_sl", 0)) + int(sl)
    summary["total_ghost"] = int(summary.get("total_ghost", 0)) + int(ghost)

    save_ai_memory(data)
    return True


def format_ai_status():
    data = load_ai_memory()
    settings = data.get("settings", {})
    summary = data.get("summary", {})

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
        f"حالت: {mode_fa}\n\n"
        f"سیگنال‌های ثبت‌شده: {summary.get('total_signals', 0)}\n"
        f"TP1: {summary.get('total_tp1', 0)} | "
        f"TP2: {summary.get('total_tp2', 0)} | "
        f"SL: {summary.get('total_sl', 0)}\n"
        f"Ghost: {summary.get('total_ghost', 0)}"
    )
