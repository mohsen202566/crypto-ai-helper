# -*- coding: utf-8 -*-
import json
import os
import shutil
from datetime import datetime

DATA_DIR = "data"
BACKUP_DIR = os.path.join(DATA_DIR, "backups")


def ensure_data_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)


def data_path(filename):
    ensure_data_dirs()
    return os.path.join(DATA_DIR, filename)


def utc_now():
    return datetime.utcnow().isoformat()


def backup_file(filename):
    ensure_data_dirs()

    path = data_path(filename)
    if not os.path.exists(path):
        return False

    try:
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{filename}.{timestamp}.bak"
        backup_path = os.path.join(BACKUP_DIR, backup_name)
        shutil.copy2(path, backup_path)
        return True
    except Exception:
        return False


def load_json(filename, default=None):
    if default is None:
        default = {}

    path = data_path(filename)

    if not os.path.exists(path):
        save_json(filename, default)
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, type(default)):
            backup_file(filename)
            save_json(filename, default)
            return default

        return data

    except Exception:
        backup_file(filename)
        save_json(filename, default)
        return default


def save_json(filename, data):
    path = data_path(filename)
    tmp_path = path + ".tmp"

    ensure_data_dirs()

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    os.replace(tmp_path, path)
    return True


def append_event(filename, event):
    data = load_json(filename, default=[])

    if not isinstance(data, list):
        backup_file(filename)
        data = []

    if isinstance(event, dict):
        event.setdefault("created_at", utc_now())

    data.append(event)
    save_json(filename, data)
    return True
