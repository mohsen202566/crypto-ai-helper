# -*- coding: utf-8 -*-
from typing import List, Dict, Any

from coin_risk import get_direction_risk_state

try:
    from config import SCAN_SYMBOLS
except Exception:
    SCAN_SYMBOLS = []

try:
    from data_store import load_json
except Exception:
    load_json = None

LEARNING_FILE = 'coin_learning.json'


def _clamp(value, low=0, high=100):
    try:
        return max(low, min(high, int(round(float(value)))))
    except Exception:
        return low


def _safe_int(value, default=0):
    try:
        return int(value or 0)
    except Exception:
        return default


def _load_learning_state() -> Dict[str, Any]:
    if load_json:
        try:
            data = load_json(LEARNING_FILE, {'by_coin_direction': {}})
            return data if isinstance(data, dict) else {'by_coin_direction': {}}
        except Exception:
            pass
    return {'by_coin_direction': {}}


def _learning_bucket(symbol: str, direction: str) -> Dict[str, Any]:
    try:
        data = _load_learning_state()
        key = f"{str(symbol).upper()}:{str(direction).upper()}"
        bucket = data.get('by_coin_direction', {}).get(key, {})
        return bucket if isinstance(bucket, dict) else {}
    except Exception:
        return {}


def _direction_learning_score(symbol: str, direction: str) -> Dict[str, Any]:
    b = _learning_bucket(symbol, direction)
    tp = _safe_int(b.get('tp1')) + _safe_int(b.get('tp2'))
    sl = _safe_int(b.get('sl'))
    total = tp + sl
    win_rate = (tp / total) if total else None

    adjustment = 0
    if total >= 3 and win_rate is not None:
        # Soft learning effect: good history helps, bad history reduces priority.
        adjustment += max(-12, min(12, int(round((win_rate - 0.50) * 30))))
        if sl >= 2 and sl > tp:
            adjustment -= min(12, (sl - 1) * 4)
        if tp >= 3 and tp > sl:
            adjustment += min(6, tp - sl)

    return {
        'tp': tp,
        'sl': sl,
        'total': total,
        'win_rate': None if win_rate is None else round(win_rate * 100, 1),
        'adjustment': adjustment,
    }


def _direction_daily_risk(symbol: str, direction: str) -> Dict[str, Any]:
    try:
        r = get_direction_risk_state(symbol, direction)
        if not isinstance(r, dict):
            r = {}
    except Exception:
        r = {}

    sl_count = _safe_int(r.get('sl_count', r.get('sl')))
    tp_count = _safe_int(r.get('tp_count', r.get('tp')))
    risk_score = _safe_int(r.get('risk_score'))

    # User rule: after 2 SL on the same coin/direction, the 3rd signal must be stricter.
    # This is a soft rotation penalty; coin_risk/analysis can still add their own confirmation rules.
    strict_after_two_sl = max(0, sl_count - 1)
    strict_penalty = min(20, strict_after_two_sl * 7)

    return {
        'sl_count': sl_count,
        'tp_count': tp_count,
        'risk_score': risk_score,
        'strict_penalty': strict_penalty,
    }


def get_coin_rotation_score(symbol: str) -> Dict[str, Any]:
    symbol = str(symbol).upper()

    long_risk = _direction_daily_risk(symbol, 'LONG')
    short_risk = _direction_daily_risk(symbol, 'SHORT')
    long_learn = _direction_learning_score(symbol, 'LONG')
    short_learn = _direction_learning_score(symbol, 'SHORT')

    risk = _safe_int(long_risk.get('risk_score')) + _safe_int(short_risk.get('risk_score'))
    daily_penalty = min(30, int(round(risk * 0.5)))
    strict_penalty = _safe_int(long_risk.get('strict_penalty')) + _safe_int(short_risk.get('strict_penalty'))

    learning_adjustment = _safe_int(long_learn.get('adjustment')) + _safe_int(short_learn.get('adjustment'))
    score = _clamp(70 + learning_adjustment - daily_penalty - strict_penalty)

    if score >= 78:
        status = 'FAVOR'
    elif score >= 55:
        status = 'NORMAL'
    elif score >= 35:
        status = 'REDUCE'
    else:
        status = 'AVOID'

    return {
        'symbol': symbol,
        'rotation_score': score,
        'priority_score': score,
        'risk_score': risk,
        'status': status,
        'learning_adjustment': learning_adjustment,
        'daily_penalty': daily_penalty,
        'strict_penalty': strict_penalty,
        'long': {'risk': long_risk, 'learning': long_learn},
        'short': {'risk': short_risk, 'learning': short_learn},
    }


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
