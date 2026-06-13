# -*- coding: utf-8 -*-
from typing import List, Dict, Any
from coin_risk import get_direction_risk_state
try:
    from config import SCAN_SYMBOLS
except Exception:
    SCAN_SYMBOLS = []

def get_coin_rotation_score(symbol: str) -> Dict[str, Any]:
    long_r = get_direction_risk_state(symbol, 'LONG'); short_r = get_direction_risk_state(symbol, 'SHORT')
    risk = int(long_r.get('risk_score',0)) + int(short_r.get('risk_score',0))
    score = max(0, min(100, 70 - risk))
    return {'symbol': symbol, 'rotation_score': score, 'priority_score': score, 'risk_score': risk, 'status': 'REDUCE' if score < 30 else 'NORMAL'}

def sort_symbols_by_rotation(symbols: List[str]) -> List[str]:
    return sorted(symbols, key=lambda s: get_coin_rotation_score(s).get('rotation_score', 50), reverse=True)

def format_rotation_report() -> str:
    rows = [get_coin_rotation_score(s) for s in SCAN_SYMBOLS[:40]]
    rows.sort(key=lambda x: x['rotation_score'], reverse=True)
    best = rows[:5]; worst = rows[-5:]
    lines = ['🔄 Coin Rotation']
    lines.append('بهترین‌ها:')
    for r in best: lines.append(f"{r['symbol']} | {r['rotation_score']}")
    lines.append('ضعیف‌ترین‌ها:')
    for r in worst: lines.append(f"{r['symbol']} | {r['rotation_score']}")
    return '\n'.join(lines)
