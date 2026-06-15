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


def _learning_counts() -> Dict[str, int]:
    data = load_json('coin_learning.json', {'signals': {}})
    signals = data.get('signals', {}) if isinstance(data, dict) else {}
    if not isinstance(signals, dict):
        signals = {}
    real = 0; ghost = 0; tp = 0; sl = 0
    for item in signals.values():
        if not isinstance(item, dict):
            continue
        signal_type = str(item.get('signal_type') or item.get('type') or 'REAL').upper()
        if signal_type == 'GHOST':
            ghost += 1
        else:
            real += 1
        result = str(item.get('result') or '').upper()
        if result in ['TP', 'TP1', 'TP2']:
            tp += 1
        elif result == 'SL':
            sl += 1
    return {'real': real, 'ghost': ghost, 'tp': tp, 'sl': sl}


def _ghost_counts() -> Dict[str, int]:
    data = load_json('ghost_signals.json', {'open': {}, 'closed': []})
    if not isinstance(data, dict):
        return {'open': 0, 'closed': 0, 'tp': 0, 'sl': 0}
    closed = data.get('closed', []) if isinstance(data.get('closed', []), list) else []
    tp = len([x for x in closed if isinstance(x, dict) and str(x.get('result')).upper() in ['TP', 'TP1', 'TP2']])
    sl = len([x for x in closed if isinstance(x, dict) and str(x.get('result')).upper() == 'SL'])
    open_count = len(data.get('open', {}) if isinstance(data.get('open', {}), dict) else {})
    return {'open': open_count, 'closed': len(closed), 'tp': tp, 'sl': sl}


def get_ai_summary_counts() -> Dict[str, int]:
    """Return display-safe AI counters from real stored learning/Ghost data.

    The old summary counters are kept for backward compatibility, but status
    display should prefer actual stored records so numbers do not drift.
    """
    s = _state(); sm = s.get('summary', {})
    lc = _learning_counts(); gc = _ghost_counts()
    real = int(lc.get('real', 0) or 0)
    ghost_learning = int(lc.get('ghost', 0) or 0)
    ghost_file_total = int(gc.get('open', 0) or 0) + int(gc.get('closed', 0) or 0)
    ghost_total = max(ghost_learning, ghost_file_total, int(sm.get('total_ghost_signals', 0) or 0))
    total_signals = real if real > 0 else int(sm.get('total_signals', 0) or 0)
    return {
        'total_signals': total_signals,
        'total_ghost_signals': ghost_total,
        'real_learning': real,
        'ghost_learning': ghost_learning,
        'ghost_open': int(gc.get('open', 0) or 0),
        'ghost_closed': int(gc.get('closed', 0) or 0),
        'tp': int(lc.get('tp', 0) or 0),
        'sl': int(lc.get('sl', 0) or 0),
    }


def format_ai_status() -> str:
    s = _state(); st = s.get('settings', {})
    sm = get_ai_summary_counts()
    return (
        '🤖 وضعیت AI\n'
        f"فعال: {'بله' if st.get('enabled') else 'خیر'}\n"
        f"یادگیری: {'بله' if st.get('learning_enabled') else 'خیر'}\n"
        f"سیگنال‌های واقعی ثبت‌شده: {sm.get('total_signals', 0)}\n"
        f"Ghost Signals: {sm.get('total_ghost_signals', 0)}"
    )
