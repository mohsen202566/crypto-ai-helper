# -*- coding: utf-8 -*-
import time, uuid
from typing import Dict, Any, List
from data_store import load_json, save_json
from config import MAX_GHOST_SIGNALS, GHOST_LEARNING_ENABLED
try:
    from coin_learning import record_signal, update_signal_result
    from ai_memory import update_ai_summary
except Exception:
    record_signal = None; update_signal_result = None; update_ai_summary = None

GHOST_FILE = 'ghost_signals.json'

def _state():
    s = load_json(GHOST_FILE, {'open': {}, 'closed': []})
    if not isinstance(s, dict): s = {'open': {}, 'closed': []}
    s.setdefault('open', {}); s.setdefault('closed', [])
    return s

def create_ghost_signal(symbol: str, direction: str, entry: float, stop_loss: float, tp1: float, tp2=None, score=None, snapshot=None, source='scanner', reason='SLOT_FULL') -> Dict[str, Any]:
    if not GHOST_LEARNING_ENABLED: return {}
    s = _state(); gid = f"ghost_{symbol}_{direction}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    g = {'signal_id': gid, 'id': gid, 'symbol': str(symbol).upper(), 'direction': str(direction).upper(), 'entry': float(entry), 'price': float(entry), 'stop_loss': float(stop_loss), 'tp1': float(tp1), 'tp2': tp2, 'score': score, 'snapshot': snapshot or {}, 'source': source, 'reason': reason, 'created_at': int(time.time()), 'status': 'OPEN'}
    s['open'][gid] = g
    while len(s['open']) > MAX_GHOST_SIGNALS:
        first = sorted(s['open'].keys())[0]; del s['open'][first]
    save_json(GHOST_FILE, s)
    if record_signal: 
        try: record_signal(g, signal_type='GHOST')
        except Exception: pass
    if update_ai_summary:
        try: update_ai_summary(total_ghost_signals=1)
        except Exception: pass
    return g

def close_ghost_signal(signal_id: str, result: str, exit_price: float, move_percent: float = 0.0) -> bool:
    s = _state(); g = s['open'].pop(signal_id, None)
    if not g: return False
    g.update({'status': 'CLOSED', 'result': result, 'exit_price': exit_price, 'move_percent': move_percent, 'closed_at': int(time.time())})
    s['closed'].append(g); s['closed'] = s['closed'][-MAX_GHOST_SIGNALS:]
    save_json(GHOST_FILE, s)
    if update_signal_result:
        try: update_signal_result(signal_id, result, exit_price=exit_price, move_percent=move_percent)
        except Exception: pass
    return True

def get_ghost_stats() -> Dict[str, Any]:
    s = _state(); closed = s.get('closed', [])
    tp = len([x for x in closed if str(x.get('result')).upper() in ['TP1','TP2','TP']]); sl = len([x for x in closed if str(x.get('result')).upper() == 'SL'])
    return {'open': len(s.get('open', {})), 'closed': len(closed), 'tp': tp, 'sl': sl}

def format_ghost_report() -> str:
    st = get_ghost_stats()
    return f"👻 Ghost Signals\nباز: {st['open']}\nبسته: {st['closed']}\nTP: {st['tp']} | SL: {st['sl']}"
