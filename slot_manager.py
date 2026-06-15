# -*- coding: utf-8 -*-
import time
from typing import Dict, List, Any
from data_store import load_json, save_json
from config import MAX_ACTIVE_POSITIONS, MAX_POSITIONS_PER_SYMBOL
try:
    from coin_rotation import get_coin_rotation_score
except Exception:
    get_coin_rotation_score = None
try:
    from coin_risk import get_direction_risk_state
except Exception:
    get_direction_risk_state = None


SLOT_FILE = 'slot_state.json'

def _state():
    s = load_json(SLOT_FILE, {'positions': {}})
    if not isinstance(s, dict): s = {'positions': {}}
    s.setdefault('positions', {})
    return s

def get_active_positions() -> List[Dict[str, Any]]:
    return list(_state().get('positions', {}).values())

def get_free_slots() -> int:
    return max(0, int(MAX_ACTIVE_POSITIONS) - len(get_active_positions()))

def is_symbol_direction_active(symbol: str, direction: str = None) -> bool:
    for p in get_active_positions():
        if p.get('symbol') == str(symbol).upper() and (direction is None or p.get('direction') == str(direction).upper()):
            return True
    return False

def can_open_new_position(symbol: str = None, direction: str = None):
    if get_free_slots() <= 0: return False, 'slot_full'
    if symbol:
        count = sum(1 for p in get_active_positions() if p.get('symbol') == str(symbol).upper())
        if count >= MAX_POSITIONS_PER_SYMBOL: return False, 'symbol_limit'
        if direction and is_symbol_direction_active(symbol, direction): return False, 'duplicate'
    return True, 'ok'

def add_position(signal_id: str, symbol: str, direction: str, score=None, **kwargs):
    ok, reason = can_open_new_position(symbol, direction)
    if not ok: return False, reason
    s = _state(); sid = str(signal_id)
    s['positions'][sid] = {'signal_id': sid, 'symbol': str(symbol).upper(), 'direction': str(direction).upper(), 'score': score, 'opened_at': int(time.time()), **kwargs}
    save_json(SLOT_FILE, s); return True, 'ok'

def close_position(signal_id: str):
    s = _state(); sid = str(signal_id)
    if sid in s['positions']:
        del s['positions'][sid]; save_json(SLOT_FILE, s); return True
    return False

def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _learning_context(symbol: str, direction: str) -> Dict[str, Any]:
    """Read learned per-coin/per-direction results without changing learning storage."""
    try:
        data = load_json('coin_learning.json', {'by_coin_direction': {}})
        rows = data.get('by_coin_direction', {}) if isinstance(data, dict) else {}
        key = f"{str(symbol).upper()}:{str(direction).upper()}"
        row = rows.get(key, {}) if isinstance(rows, dict) else {}
        tp = int(row.get('tp1', 0) or 0) + int(row.get('tp2', 0) or 0)
        sl = int(row.get('sl', 0) or 0)
        total = tp + sl
        winrate = (tp / total * 100.0) if total > 0 else 50.0
        return {'tp': tp, 'sl': sl, 'total': total, 'winrate': winrate}
    except Exception:
        return {'tp': 0, 'sl': 0, 'total': 0, 'winrate': 50.0}


def select_best_candidates(candidates: List[Dict], limit: int = 1) -> List[Dict]:
    def rank(x):
        symbol = str(x.get('symbol') or '').upper()
        direction = str(x.get('direction') or '').upper()

        base_score = _safe_float(x.get('score'), 0.0)
        confirmations = _safe_float(x.get('confirmations'), 0.0)
        rr = _safe_float(x.get('risk_reward'), 0.0)

        rotation_score = 70.0
        rotation_risk = 0.0
        if get_coin_rotation_score and symbol:
            try:
                rot = get_coin_rotation_score(symbol) or {}
                rotation_score = _safe_float(rot.get('rotation_score'), 70.0)
                rotation_risk = _safe_float(rot.get('risk_score'), 0.0)
            except Exception:
                pass

        risk_score = rotation_risk
        strictness = 0.0
        sl_count = 0
        if get_direction_risk_state and symbol and direction:
            try:
                risk = get_direction_risk_state(symbol, direction) or {}
                risk_score = max(risk_score, _safe_float(risk.get('risk_score'), 0.0))
                strictness = _safe_float(risk.get('strictness_level'), 0.0)
                sl_count = int(risk.get('sl_count', 0) or 0)
            except Exception:
                pass

        learned = _learning_context(symbol, direction)
        learned_total = int(learned.get('total', 0) or 0)
        learned_wr = _safe_float(learned.get('winrate'), 50.0)
        learned_sl = int(learned.get('sl', 0) or 0)

        # Keep selection soft: do not reject signals here; only rank better candidates higher.
        rotation_bonus = (rotation_score - 70.0) * 0.35
        learning_bonus = 0.0
        if learned_total >= 3:
            learning_bonus = max(-8.0, min(8.0, (learned_wr - 50.0) * 0.16))

        # User rule: after 2 SL on the same coin/direction, the 3rd signal should be harder.
        sl_for_strictness = max(sl_count, learned_sl)
        after_two_sl_penalty = 0.0
        if sl_for_strictness >= 2:
            after_two_sl_penalty = min(12.0, (sl_for_strictness - 1) * 4.0)

        risk_penalty = min(14.0, risk_score * 0.12) + strictness * 2.0 + after_two_sl_penalty

        return (
            base_score
            + confirmations * 1.5
            + rr * 2.0
            + rotation_bonus
            + learning_bonus
            - risk_penalty
        )

    return sorted(candidates or [], key=rank, reverse=True)[:limit]

def format_slot_report() -> str:
    ps = get_active_positions()
    lines = [f"📌 Slot ها: {len(ps)}/{MAX_ACTIVE_POSITIONS}"]
    for p in ps: lines.append(f"{p.get('symbol')} {p.get('direction')} | {p.get('score')}")
    return '\n'.join(lines)
