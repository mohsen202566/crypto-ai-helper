# -*- coding: utf-8 -*-
import time
from typing import Dict, Any
from data_store import load_json, save_json
from config import DAILY_SL_STRICTNESS_START, MAX_DAILY_STRICTNESS_LEVEL

RISK_FILE = 'coin_risk.json'
DAY = 86400

def _day_key(ts=None): return time.strftime('%Y-%m-%d', time.localtime(ts or time.time()))
def _key(symbol, direction): return f"{str(symbol).upper()}:{str(direction).upper()}"
def _state():
    s = load_json(RISK_FILE, {'days': {}})
    if not isinstance(s, dict): s = {'days': {}}
    s.setdefault('days', {})
    return s

def _row(s, symbol, direction):
    d = s['days'].setdefault(_day_key(), {})
    return d.setdefault(_key(symbol,direction), {'symbol': str(symbol).upper(), 'direction': str(direction).upper(), 'tp':0, 'sl':0, 'risk_score':0})

def register_result(symbol: str, direction: str, result: str) -> Dict[str, Any]:
    s = _state(); r = _row(s, symbol, direction)
    if str(result).upper() == 'SL': r['sl'] = int(r.get('sl',0)) + 1
    elif str(result).upper() in ['TP','TP1','TP2']: r['tp'] = int(r.get('tp',0)) + 1
    r['risk_score'] = max(0, int(r.get('sl',0))*20 - int(r.get('tp',0))*8)
    save_json(RISK_FILE, s); return r

def get_direction_risk_state(symbol: str, direction: str) -> Dict[str, Any]:
    s = _state(); r = _row(s, symbol, direction); sl = int(r.get('sl',0))
    strict = 0 if sl < DAILY_SL_STRICTNESS_START else min(MAX_DAILY_STRICTNESS_LEVEL, sl - DAILY_SL_STRICTNESS_START + 1)
    out = dict(r); out.update({'sl_count': sl, 'tp_count': int(r.get('tp',0)), 'strictness_level': strict, 'bad_day': sl >= DAILY_SL_STRICTNESS_START, 'recommend_reduce': sl >= DAILY_SL_STRICTNESS_START + 2})
    return out
