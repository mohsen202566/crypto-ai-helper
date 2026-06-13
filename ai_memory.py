# -*- coding: utf-8 -*-
import time
from typing import Dict, Any
from data_store import load_json, save_json

AI_MEMORY_FILE = 'ai_memory.json'
DEFAULT_STATE = {
    'settings': {'enabled': True, 'learning_enabled': True, 'soft_mode': True},
    'summary': {'total_signals': 0, 'total_ghost_signals': 0, 'last_update': None},
}

def _state() -> Dict[str, Any]:
    s = load_json(AI_MEMORY_FILE, DEFAULT_STATE.copy())
    if not isinstance(s, dict):
        s = DEFAULT_STATE.copy()
    s.setdefault('settings', DEFAULT_STATE['settings'].copy())
    s.setdefault('summary', DEFAULT_STATE['summary'].copy())
    return s

def get_ai_settings() -> Dict[str, Any]:
    return dict(_state().get('settings', {}))

def update_ai_summary(total_signals: int = 0, total_ghost_signals: int = 0) -> Dict[str, Any]:
    s = _state()
    sm = s.setdefault('summary', {})
    sm['total_signals'] = int(sm.get('total_signals', 0)) + int(total_signals or 0)
    sm['total_ghost_signals'] = int(sm.get('total_ghost_signals', 0)) + int(total_ghost_signals or 0)
    sm['last_update'] = int(time.time())
    save_json(AI_MEMORY_FILE, s)
    return sm

def format_ai_status() -> str:
    s = _state(); st = s.get('settings', {}); sm = s.get('summary', {})
    return (
        '🤖 وضعیت AI\n'
        f"فعال: {'بله' if st.get('enabled') else 'خیر'}\n"
        f"یادگیری: {'بله' if st.get('learning_enabled') else 'خیر'}\n"
        f"سیگنال‌های واقعی ثبت‌شده: {sm.get('total_signals', 0)}\n"
        f"Ghost Signals: {sm.get('total_ghost_signals', 0)}"
    )
