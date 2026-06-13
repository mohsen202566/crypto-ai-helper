# -*- coding: utf-8 -*-
import time, uuid
from typing import Dict, List, Optional, Any
from data_store import load_json, save_json

PAPER_FILE = 'paper_trades.json'

def now_ts(): return int(time.time())
def _state():
    s = load_json(PAPER_FILE, {'open_positions': {}, 'closed_positions': [], 'stats': {'total':0,'tp1':0,'tp2':0,'sl':0,'manual_closed':0}})
    if not isinstance(s, dict): s = {'open_positions': {}, 'closed_positions': [], 'stats': {'total':0,'tp1':0,'tp2':0,'sl':0,'manual_closed':0}}
    s.setdefault('open_positions', {}); s.setdefault('closed_positions', []); s.setdefault('stats', {'total':0,'tp1':0,'tp2':0,'sl':0,'manual_closed':0})
    return s

def _save(s): save_json(PAPER_FILE, s)
def normalize_direction(d):
    d = str(d).upper().strip()
    if d in ['LONG','BUY','لانگ']: return 'LONG'
    if d in ['SHORT','SELL','شورت']: return 'SHORT'
    return d

def calculate_pnl_percent(direction, entry, exit_price):
    entry=float(entry); exit_price=float(exit_price)
    if entry <= 0: return 0.0
    return round(((exit_price-entry)/entry)*100,4) if direction=='LONG' else round(((entry-exit_price)/entry)*100,4)

def has_open_position(symbol, direction=None):
    for p in _state().get('open_positions', {}).values():
        if p.get('symbol') == str(symbol).upper() and (direction is None or p.get('direction') == normalize_direction(direction)): return True
    return False

def open_paper_position(signal: Dict[str, Any], telegram_message_id=None, chat_id=None) -> Optional[Dict]:
    symbol=str(signal.get('symbol','')).upper(); direction=normalize_direction(signal.get('direction'))
    if not symbol or direction not in ['LONG','SHORT'] or has_open_position(symbol, direction): return None
    entry=signal.get('entry') or signal.get('price'); sl=signal.get('stop_loss'); tp1=signal.get('tp1')
    if entry is None or sl is None or tp1 is None: return None
    s=_state(); pid=f"paper_{symbol}_{direction}_{now_ts()}_{uuid.uuid4().hex[:6]}"
    p={'position_id':pid,'signal_id':signal.get('signal_id'),'symbol':symbol,'direction':direction,'entry':float(entry),'stop_loss':float(sl),'tp1':float(tp1),'tp2': None if signal.get('tp2') is None else float(signal.get('tp2')),'score':signal.get('score'),'risk_level':signal.get('risk_level'),'risk_reward':signal.get('risk_reward'),'status':'OPEN','opened_at':now_ts(),'telegram_message_id':telegram_message_id,'chat_id':chat_id,'snapshot':signal.get('snapshot',{}),'source':signal.get('source','auto_signal')}
    s['open_positions'][pid]=p; _save(s); return p

def open_paper_trade(signal):
    p = open_paper_position(signal)
    return (bool(p), '✅ Paper Trade باز شد.' if p else 'Paper Trade باز نشد یا تکراری بود.')

def close_paper_position(symbol: str, direction: str, exit_price: float, result: str, signal_id: str=None) -> Optional[Dict]:
    s=_state(); target_id=None; target=None; direction=normalize_direction(direction)
    for pid,p in s.get('open_positions',{}).items():
        if signal_id and p.get('signal_id') == signal_id: target_id=pid; target=p; break
        if p.get('symbol') == str(symbol).upper() and p.get('direction') == direction: target_id=pid; target=p; break
    if not target_id: return None
    closed=dict(target); closed.update({'status':'CLOSED','result':result,'exit_price':float(exit_price),'pnl_percent':calculate_pnl_percent(direction,target.get('entry'),exit_price),'closed_at':now_ts()})
    del s['open_positions'][target_id]; s['closed_positions'].append(closed); s['closed_positions']=s['closed_positions'][-1000:]
    stats=s.setdefault('stats', {'total':0,'tp1':0,'tp2':0,'sl':0,'manual_closed':0}); stats['total']=int(stats.get('total',0))+1
    rk=str(result).lower(); stats['tp1' if rk in ['tp','tp1'] else 'tp2' if rk=='tp2' else 'sl' if rk=='sl' else 'manual_closed'] += 1
    _save(s); return closed

def close_paper_position_by_signal_id(signal_id, exit_price, result):
    for p in _state().get('open_positions',{}).values():
        if p.get('signal_id') == signal_id: return close_paper_position(p.get('symbol'), p.get('direction'), exit_price, result, signal_id)
    return None

def close_paper_trade_by_signal(signal, result_type, exit_price):
    c = close_paper_position(signal.get('symbol'), signal.get('direction'), exit_price, result_type, signal.get('signal_id') or signal.get('id'))
    return (bool(c), f"Paper بسته شد: {c.get('pnl_percent')}%" if c else 'پوزیشن Paper مربوط به این سیگنال پیدا نشد.')

def get_open_positions(): return list(_state().get('open_positions',{}).values())
def get_paper_stats():
    s=_state(); st=s.get('stats',{}); total=int(st.get('total',0)); wins=int(st.get('tp1',0))+int(st.get('tp2',0))
    return {'total':total,'tp1':int(st.get('tp1',0)),'tp2':int(st.get('tp2',0)),'sl':int(st.get('sl',0)),'manual_closed':int(st.get('manual_closed',0)),'win_rate':round(wins/max(total,1)*100,2) if total else 0,'open_positions':len(s.get('open_positions',{}))}

def format_paper_stats():
    st=get_paper_stats(); return f"📊 Paper Trade\nکل: {st['total']}\nTP1: {st['tp1']} | TP2: {st['tp2']} | SL: {st['sl']}\nWinRate: {st['win_rate']}%\nباز: {st['open_positions']}"

def format_open_positions():
    ps=get_open_positions()
    if not ps: return 'پوزیشن Paper بازی وجود ندارد.'
    return '\n'.join(['📌 پوزیشن‌های Paper باز:']+[f"{p.get('symbol')} {p.get('direction')}\nEntry:{p.get('entry')} SL:{p.get('stop_loss')} TP1:{p.get('tp1')}" for p in ps])

def reset_paper_trades(): _save({'open_positions': {}, 'closed_positions': [], 'stats': {'total':0,'tp1':0,'tp2':0,'sl':0,'manual_closed':0}}); return True
