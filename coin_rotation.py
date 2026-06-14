# -*- coding: utf-8 -*-
from typing import List, Dict, Any

try:
    from config import SCAN_SYMBOLS
except Exception:
    SCAN_SYMBOLS = []

try:
    from coin_risk import get_direction_risk_state
except Exception:
    def get_direction_risk_state(symbol, direction):
        return {'risk_score': 0}

try:
    from data_store import load_json
except Exception:
    import json, os
    def load_json(path, default):
        try:
            if not os.path.exists(path):
                return default
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, type(default)) else default
        except Exception:
            return default

LEARNING_FILE = 'coin_learning.json'

def _learning_state() -> Dict[str, Any]:
    s = load_json(LEARNING_FILE, {'by_coin_direction': {}})
    if not isinstance(s, dict):
        return {'by_coin_direction': {}}
    s.setdefault('by_coin_direction', {})
    return s

def _bucket(symbol: str, direction: str) -> Dict[str, Any]:
    return _learning_state().get('by_coin_direction', {}).get(f'{str(symbol).upper()}:{str(direction).upper()}', {}) or {}

def _direction_score(symbol: str, direction: str) -> Dict[str, Any]:
    b = _bucket(symbol, direction)
    tp = int(b.get('tp1', 0) or 0) + int(b.get('tp2', 0) or 0)
    sl = int(b.get('sl', 0) or 0)
    real_total = int(b.get('real_total', 0) or 0)
    ghost_total = int(b.get('ghost_total', 0) or 0)
    closed = tp + sl
    win_rate = (tp / closed * 100.0) if closed else 0.0

    score = 70.0
    if closed > 0:
        score += (win_rate - 50.0) * 0.35
        score += min(tp, 10) * 1.2
        score -= min(sl, 10) * 2.8
    # Give tiny weight to actual experience/ghost coverage, without making it noisy.
    score += min(real_total, 20) * 0.15
    score += min(ghost_total, 50) * 0.03

    try:
        risk_state = get_direction_risk_state(symbol, direction) or {}
        score -= int(risk_state.get('risk_score', 0) or 0)
    except Exception:
        risk_state = {}

    return {
        'direction': direction,
        'tp': tp,
        'sl': sl,
        'closed': closed,
        'real_total': real_total,
        'ghost_total': ghost_total,
        'win_rate': round(win_rate, 1) if closed else None,
        'score': max(0, min(100, round(score, 1))),
        'risk_score': int((risk_state or {}).get('risk_score', 0) or 0),
    }

def get_coin_rotation_score(symbol: str) -> Dict[str, Any]:
    symbol = str(symbol).upper()
    long_s = _direction_score(symbol, 'LONG')
    short_s = _direction_score(symbol, 'SHORT')
    closed = long_s['closed'] + short_s['closed']
    real_total = long_s['real_total'] + short_s['real_total']
    ghost_total = long_s['ghost_total'] + short_s['ghost_total']
    risk = long_s['risk_score'] + short_s['risk_score']

    # Before enough closed outcomes, keep score near neutral but still let risk/experience move it slightly.
    if closed == 0:
        score = 70.0 + min(real_total, 10) * 0.1 + min(ghost_total, 20) * 0.03 - risk
    else:
        score = (long_s['score'] + short_s['score']) / 2.0
        if closed >= 3:
            score += 2.0

    score = int(max(0, min(100, round(score))))
    status = 'PREFER' if score >= 78 else 'REDUCE' if score <= 55 else 'NORMAL'
    return {
        'symbol': symbol,
        'rotation_score': score,
        'priority_score': score,
        'risk_score': risk,
        'status': status,
        'real_total': real_total,
        'ghost_total': ghost_total,
        'closed': closed,
        'long': long_s,
        'short': short_s,
    }

def sort_symbols_by_rotation(symbols: List[str]) -> List[str]:
    return sorted(symbols, key=lambda s: get_coin_rotation_score(s).get('rotation_score', 50), reverse=True)

def format_rotation_report() -> str:
    rows = [get_coin_rotation_score(s) for s in SCAN_SYMBOLS[:40]]
    rows.sort(key=lambda x: x['rotation_score'], reverse=True)
    best = rows[:5]
    worst = rows[-5:]
    lines = ['🔄 Coin Rotation', 'امتیاز بر اساس TP/SL واقعی + Ghost + ریسک روزانه است.']
    lines.append('بهترین‌ها:')
    for r in best:
        lines.append(f"{r['symbol']} | {r['rotation_score']} | Real:{r['real_total']} Ghost:{r['ghost_total']} Closed:{r['closed']}")
    lines.append('ضعیف‌ترین‌ها:')
    for r in worst:
        lines.append(f"{r['symbol']} | {r['rotation_score']} | Real:{r['real_total']} Ghost:{r['ghost_total']} Closed:{r['closed']}")
    return '\n'.join(lines)
