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


def _full_path(filename):
    ensure_data_dirs()
    return os.path.join(DATA_DIR, filename)


def load_json(filename, default=None):
    path = _full_path(filename)

    if default is None:
        default = {}

    if not os.path.exists(path):
        save_json(filename, default)
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        backup_file(filename)
        save_json(filename, default)
        return default


def save_json(filename, data):
    path = _full_path(filename)
    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    os.replace(temp_path, path)
    return True


def backup_file(filename):
    ensure_data_dirs()
    path = _full_path(filename)

    if not os.path.exists(path):
        return False

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{filename}.{timestamp}.bak"
    backup_path = os.path.join(BACKUP_DIR, backup_name)

    try:
        shutil.copy2(path, backup_path)
        return True
    except Exception:
        return False


def append_event(filename, event):
    data = load_json(filename, default=[])

    if not isinstance(data, list):
        backup_file(filename)
        data = []

    event["created_at"] = datetime.utcnow().isoformat()
    data.append(event)
    save_json(filename, data)
    return True
