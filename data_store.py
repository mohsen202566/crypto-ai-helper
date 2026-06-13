# -*- coding: utf-8 -*-
import json, os, shutil, time
from typing import Any

DATA_DIR = os.getenv('BOT_DATA_DIR', 'data')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

def _path(name: str) -> str:
    return name if os.path.isabs(name) else os.path.join(DATA_DIR, name)

def load_json(name: str, default: Any = None) -> Any:
    path = _path(name)
    if default is None:
        default = {}
    try:
        if not os.path.exists(path):
            return default
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default

def save_json(name: str, data: Any, backup: bool = True) -> bool:
    path = _path(name)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    if backup and os.path.exists(path):
        try:
            base = os.path.basename(path)
            shutil.copy2(path, os.path.join(BACKUP_DIR, f'{base}.{int(time.time())}.bak'))
        except Exception:
            pass
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return True
