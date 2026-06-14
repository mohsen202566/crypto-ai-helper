# -*- coding: utf-8 -*-
import time
from typing import Dict, Any, Optional
from data_store import load_json, save_json

LEARNING_FILE = 'coin_learning.json'

def _key(symbol: str, direction: str) -> str:
    return f"{str(symbol).upper()}:{str(direction).upper()}"

def _state() -> Dict[str, Any]:
    s = load_json(LEARNING_FILE, {'signals': {}, 'by_coin_direction': {}, 'ghost': {}})
    if not isinstance(s, dict):
        s = {'signals': {}, 'by_coin_direction': {}, 'ghost': {}}
    s.setdefault('signals', {}); s.setdefault('by_coin_direction', {}); s.setdefault('ghost', {})
    return s

def build_signal_snapshot(symbol: str, direction: str, technical_snapshot: Optional[Dict] = None, market_context: Optional[Dict] = None) -> Dict:
    snap = dict(technical_snapshot or {})
    snap.update({'symbol': symbol, 'direction': direction, 'market_context': market_context or {}, 'snapshot_at': int(time.time())})
    return snap

def _bucket(s: Dict, symbol: str, direction: str) -> Dict:
    k = _key(symbol, direction)
    b = s['by_coin_direction'].setdefault(k, {
        'symbol': str(symbol).upper(), 'direction': str(direction).upper(), 'real_total': 0, 'ghost_total': 0,
        'tp1': 0, 'tp2': 0, 'sl': 0, 'move_sum': 0.0, 'tp_distance_sum': 0.0, 'sl_patterns': []
    })
    return b

def record_signal(signal: Dict, signal_type: str = 'REAL') -> bool:
    s = _state()
    sid = signal.get('signal_id') or signal.get('id') or f"{signal.get('symbol')}_{int(time.time())}"
    item = dict(signal); item['signal_id'] = sid; item['signal_type'] = signal_type; item['recorded_at'] = int(time.time())
    s['signals'][sid] = item
    b = _bucket(s, item.get('symbol'), item.get('direction'))
    if signal_type == 'GHOST': b['ghost_total'] = int(b.get('ghost_total', 0)) + 1
    else: b['real_total'] = int(b.get('real_total', 0)) + 1
    save_json(LEARNING_FILE, s)
    return True

def update_signal_result(signal_id: str, result: str, exit_price: float = None, move_percent: float = None) -> bool:
    s = _state(); sig = s.get('signals', {}).get(signal_id)
    if not sig: return False
    sig['result'] = result; sig['exit_price'] = exit_price; sig['move_percent'] = move_percent; sig['closed_at'] = int(time.time())
    b = _bucket(s, sig.get('symbol'), sig.get('direction'))
    r = str(result).upper()
    if r in ['TP1','TP']: b['tp1'] = int(b.get('tp1', 0)) + 1
    elif r == 'TP2': b['tp2'] = int(b.get('tp2', 0)) + 1
    elif r == 'SL':
        b['sl'] = int(b.get('sl', 0)) + 1
        b.setdefault('sl_patterns', []).append({'at': int(time.time()), 'snapshot': sig.get('snapshot', {}), 'move_percent': move_percent})
        b['sl_patterns'] = b['sl_patterns'][-30:]
    b['move_sum'] = float(b.get('move_sum', 0.0)) + float(move_percent or 0.0)
    try:
        entry = float(sig.get('entry') or sig.get('price') or 0)
        if r in ['TP1','TP2','TP'] and exit_price and entry:
            b['tp_distance_sum'] = float(b.get('tp_distance_sum', 0.0)) + abs(float(exit_price) - entry)
    except Exception: pass
    save_json(LEARNING_FILE, s)
    return True

def get_smart_tp_suggestion(symbol: str, direction: str, snapshot: Optional[Dict] = None) -> Dict:
    s = _state(); b = s.get('by_coin_direction', {}).get(_key(symbol, direction), {})
    wins = int(b.get('tp1', 0)) + int(b.get('tp2', 0))
    if wins < 3: return {}
    avg_dist = float(b.get('tp_distance_sum', 0.0)) / max(wins, 1)
    price = float((snapshot or {}).get('price') or (snapshot or {}).get('entry') or 0)
    if price <= 0 or avg_dist <= 0: return {}
    if str(direction).upper() == 'LONG':
        return {'tp1': price + avg_dist * 0.85, 'tp2': price + avg_dist * 1.45, 'confidence': 'medium'}
    return {'tp1': price - avg_dist * 0.85, 'tp2': price - avg_dist * 1.45, 'confidence': 'medium'}

def should_require_extra_strength(symbol: str, direction: str, snapshot: Optional[Dict] = None) -> Dict:
    s = _state(); b = s.get('by_coin_direction', {}).get(_key(symbol, direction), {})
    sl = int(b.get('sl', 0)); tp = int(b.get('tp1', 0)) + int(b.get('tp2', 0)); total = sl + tp
    if total >= 4 and sl / max(total, 1) >= 0.6:
        return {'required': True, 'extra_score': 3, 'extra_confirmations': 1, 'reason': 'AI Learning: سابقه این کوین/جهت نیاز به تایید بیشتر دارد'}
    return {'required': False, 'extra_score': 0, 'extra_confirmations': 0}

def format_learning_summary() -> str:
    s = _state(); rows = list(s.get('by_coin_direction', {}).values())
    total = sum(int(r.get('real_total', 0)) for r in rows); ghost = sum(int(r.get('ghost_total', 0)) for r in rows)
    sl = sum(int(r.get('sl', 0)) for r in rows); tp = sum(int(r.get('tp1', 0)) + int(r.get('tp2', 0)) for r in rows)
    wr = round(tp / max(tp + sl, 1) * 100, 1) if (tp + sl) else 0
    return f"🧠 خلاصه یادگیری\nReal: {total}\nGhost: {ghost}\nTP: {tp} | SL: {sl}\nWinRate: {wr}%"

def format_coin_behavior(symbol: str = None) -> str:
    s = _state(); rows = list(s.get('by_coin_direction', {}).values())
    if symbol: rows = [r for r in rows if r.get('symbol') == symbol.upper()]
    if not rows: return 'رفتار کوین هنوز داده کافی ندارد.'
    lines = ['🧠 رفتار کوین‌ها']
    for r in rows[:20]:
        tp = int(r.get('tp1',0))+int(r.get('tp2',0)); sl = int(r.get('sl',0)); wr = round(tp/max(tp+sl,1)*100,1) if tp+sl else 0
        lines.append(f"{r.get('symbol')} {r.get('direction')} | TP:{tp} SL:{sl} WR:{wr}%")
    return '\n'.join(lines)

def format_smart_stats() -> str:
    return format_learning_summary()
