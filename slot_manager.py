# -*- coding: utf-8 -*-
import json
import os
import time
from typing import Dict, List, Any
from data_store import load_json, save_json

try:
    from config import MAX_ACTIVE_POSITIONS, MAX_POSITIONS_PER_SYMBOL
except Exception:
    MAX_ACTIVE_POSITIONS = 5
    MAX_POSITIONS_PER_SYMBOL = 1

SLOT_FILE = 'slot_state.json'
TRADE_SETTINGS_FILE = os.path.join('data', 'trade_settings.json')

def _state():
    s = load_json(SLOT_FILE, {'positions': {}})
    if not isinstance(s, dict):
        s = {'positions': {}}
    s.setdefault('positions', {})
    return s

def _read_trade_settings() -> Dict[str, Any]:
    # data_store usually stores files under data/, but bot writes data/trade_settings.json directly.
    paths = [TRADE_SETTINGS_FILE, 'trade_settings.json']
    for path in paths:
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    try:
        data = load_json('trade_settings.json', {})
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}

def get_max_slots() -> int:
    # Single source of truth: user command `حداکثر پوزیشن` writes max_positions into trade_settings.json.
    try:
        st = _read_trade_settings()
        if 'max_positions' in st:
            return max(1, min(int(float(st.get('max_positions'))), 50))
    except Exception:
        pass
    return max(1, int(MAX_ACTIVE_POSITIONS))

def get_active_positions() -> List[Dict[str, Any]]:
    return list(_state().get('positions', {}).values())

def get_free_slots() -> int:
    return max(0, get_max_slots() - len(get_active_positions()))

def is_symbol_direction_active(symbol: str, direction: str = None) -> bool:
    for p in get_active_positions():
        if p.get('symbol') == str(symbol).upper() and (direction is None or p.get('direction') == str(direction).upper()):
            return True
    return False

def can_open_new_position(symbol: str = None, direction: str = None):
    if get_free_slots() <= 0:
        return False, 'slot_full'
    if symbol:
        count = sum(1 for p in get_active_positions() if p.get('symbol') == str(symbol).upper())
        if count >= int(MAX_POSITIONS_PER_SYMBOL):
            return False, 'symbol_limit'
        if direction and is_symbol_direction_active(symbol, direction):
            return False, 'duplicate'
    return True, 'ok'

def add_position(signal_id: str, symbol: str, direction: str, score=None, **kwargs):
    ok, reason = can_open_new_position(symbol, direction)
    if not ok:
        return False, reason
    s = _state(); sid = str(signal_id)
    s['positions'][sid] = {
        'signal_id': sid,
        'symbol': str(symbol).upper(),
        'direction': str(direction).upper(),
        'score': score,
        'opened_at': int(time.time()),
        **kwargs,
    }
    save_json(SLOT_FILE, s)
    return True, 'ok'

def close_position(signal_id: str):
    s = _state(); sid = str(signal_id)
    if sid in s['positions']:
        del s['positions'][sid]
        save_json(SLOT_FILE, s)
        return True
    return False

def select_best_candidates(candidates: List[Dict], limit: int = 1) -> List[Dict]:
    def rank(x):
        return (
            float(x.get('score') or 0)
            + float(x.get('confirmations') or 0) * 1.5
            + float(x.get('risk_reward') or 0) * 2
            + float((x.get('rotation') or {}).get('rotation_score') or 0) * 0.05
        )
    return sorted(candidates or [], key=rank, reverse=True)[:max(0, int(limit))]

def format_slot_report() -> str:
    ps = get_active_positions()
    max_slots = get_max_slots()
    free = max(0, max_slots - len(ps))
    lines = [f'📌 Slot ها: {len(ps)}/{max_slots}', f'اسلات خالی: {free}']
    for p in ps:
        lines.append(f"{p.get('symbol')} {p.get('direction')} | {p.get('score')}")
    return '\n'.join(lines)
