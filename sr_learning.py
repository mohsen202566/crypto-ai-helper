# -*- coding: utf-8 -*-
from typing import Dict, Any
from data_store import load_json, save_json
SR_FILE = 'sr_learning.json'
def record_sr_event(symbol: str, direction: str, level_type: str, price: float, result: str = None) -> bool:
    s = load_json(SR_FILE, {'events': []})
    s.setdefault('events', []).append({'symbol': symbol, 'direction': direction, 'level_type': level_type, 'price': price, 'result': result})
    s['events'] = s['events'][-1000:]
    save_json(SR_FILE, s); return True
def format_sr_report() -> str:
    s = load_json(SR_FILE, {'events': []})
    return f"📐 SR Learning\nرویدادها: {len(s.get('events', []))}"
