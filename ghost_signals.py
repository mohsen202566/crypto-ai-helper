# -*- coding: utf-8 -*-
import time, uuid
from typing import Dict, Any, List
from data_store import load_json, save_json
from config import MAX_GHOST_SIGNALS, GHOST_LEARNING_ENABLED
try:
    import ccxt
except Exception:
    ccxt = None
try:
    from coin_learning import record_signal, update_signal_result
    from ai_memory import update_ai_summary
except Exception:
    record_signal = None; update_signal_result = None; update_ai_summary = None

GHOST_FILE = 'ghost_signals.json'
_GHOST_PRICE_CACHE = {'ts': 0, 'prices': {}}
_GHOST_PRICE_TTL_SECONDS = 20


def _state():
    s = load_json(GHOST_FILE, {'open': {}, 'closed': []})
    if not isinstance(s, dict): s = {'open': {}, 'closed': []}
    s.setdefault('open', {}); s.setdefault('closed', [])
    return s


def _to_okx_symbol(symbol: str) -> str:
    coin = str(symbol).upper().replace('USDT', '').strip()
    return f'{coin}/USDT:USDT'


def _get_exchange():
    if ccxt is None:
        return None
    try:
        return ccxt.okx({'enableRateLimit': True, 'timeout': 15000, 'options': {'defaultType': 'swap'}})
    except Exception:
        return None


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _move_percent(direction: str, entry: float, exit_price: float) -> float:
    entry = _safe_float(entry, 0.0) or 0.0
    exit_price = _safe_float(exit_price, 0.0) or 0.0
    if entry <= 0 or exit_price <= 0:
        return 0.0
    direction = str(direction).upper()
    if direction == 'LONG':
        return round((exit_price - entry) / entry * 100, 4)
    if direction == 'SHORT':
        return round((entry - exit_price) / entry * 100, 4)
    return 0.0


def _fetch_prices(symbols: List[str]) -> Dict[str, float]:
    now = int(time.time())
    cached_ts = int(_GHOST_PRICE_CACHE.get('ts') or 0)
    cached_prices = _GHOST_PRICE_CACHE.setdefault('prices', {})
    if cached_prices and now - cached_ts <= _GHOST_PRICE_TTL_SECONDS:
        return {s: cached_prices.get(s) for s in symbols if cached_prices.get(s) is not None}

    prices = {}
    ex = _get_exchange()
    if ex is None:
        return prices
    for symbol in symbols:
        try:
            ticker = ex.fetch_ticker(_to_okx_symbol(symbol))
            price = _safe_float(ticker.get('last') or ticker.get('close'))
            if price and price > 0:
                prices[str(symbol).upper()] = price
        except Exception:
            continue
    _GHOST_PRICE_CACHE['ts'] = now
    _GHOST_PRICE_CACHE['prices'] = dict(prices)
    return prices


def _ghost_hit_result(g: Dict[str, Any], current_price: float):
    direction = str(g.get('direction', '')).upper()
    sl = _safe_float(g.get('stop_loss'))
    tp1 = _safe_float(g.get('tp1'))
    tp2 = _safe_float(g.get('tp2'))
    price = _safe_float(current_price)
    if price is None or sl is None or tp1 is None:
        return None, None

    if direction == 'LONG':
        if price <= sl:
            return 'SL', sl
        if tp2 is not None and price >= tp2:
            return 'TP2', tp2
        if price >= tp1:
            return 'TP1', tp1
    elif direction == 'SHORT':
        if price >= sl:
            return 'SL', sl
        if tp2 is not None and price <= tp2:
            return 'TP2', tp2
        if price <= tp1:
            return 'TP1', tp1
    return None, None


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


def check_open_ghost_signals(max_checks: int = 120) -> Dict[str, Any]:
    """Check open Ghost signals against live price and close TP/SL hits.

    This does not change scanner/analysis behavior. It only turns already-open
    Ghost records into CLOSED records when their TP1/TP2/SL has been reached,
    so Ghost learning can actually receive outcomes.
    """
    s = _state()
    open_items = list(s.get('open', {}).items())[:max_checks]
    if not open_items:
        return {'checked': 0, 'closed': 0, 'tp': 0, 'sl': 0, 'errors': 0}

    symbols = sorted({str(g.get('symbol', '')).upper() for _, g in open_items if g.get('symbol')})
    prices = _fetch_prices(symbols)
    closed_count = 0; tp_count = 0; sl_count = 0; errors = 0

    for gid, g in open_items:
        try:
            symbol = str(g.get('symbol', '')).upper()
            price = prices.get(symbol)
            if price is None:
                errors += 1
                continue
            result, exit_price = _ghost_hit_result(g, price)
            if not result:
                continue
            pct = _move_percent(g.get('direction'), g.get('entry'), exit_price)
            if close_ghost_signal(gid, result, exit_price, pct):
                closed_count += 1
                if str(result).upper() == 'SL':
                    sl_count += 1
                else:
                    tp_count += 1
        except Exception:
            errors += 1
            continue
    return {'checked': len(open_items), 'closed': closed_count, 'tp': tp_count, 'sl': sl_count, 'errors': errors}


def get_ghost_stats(auto_check: bool = True) -> Dict[str, Any]:
    checked = None
    if auto_check:
        try:
            checked = check_open_ghost_signals()
        except Exception:
            checked = None
    s = _state(); closed = s.get('closed', [])
    tp = len([x for x in closed if str(x.get('result')).upper() in ['TP1','TP2','TP']]); sl = len([x for x in closed if str(x.get('result')).upper() == 'SL'])
    out = {'open': len(s.get('open', {})), 'closed': len(closed), 'tp': tp, 'sl': sl}
    if checked is not None:
        out['checked'] = checked
    return out


def format_ghost_report() -> str:
    st = get_ghost_stats(auto_check=True)
    checked = st.get('checked') or {}
    extra = ''
    if checked:
        extra = f"\nبررسی اخیر: {checked.get('checked', 0)} | بسته‌شده جدید: {checked.get('closed', 0)}"
    return f"👻 Ghost Signals\nباز: {st['open']}\nبسته: {st['closed']}\nTP: {st['tp']} | SL: {st['sl']}{extra}"
