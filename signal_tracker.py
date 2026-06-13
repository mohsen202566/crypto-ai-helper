# -*- coding: utf-8 -*-
import time, uuid, re
from datetime import datetime
from typing import Dict, List, Any, Optional
import ccxt
from data_store import load_json, save_json
try:
    from paper_trader import open_paper_position, close_paper_position_by_signal_id
except Exception:
    open_paper_position=None; close_paper_position_by_signal_id=None
try:
    from coin_learning import record_signal, update_signal_result
except Exception:
    record_signal=None; update_signal_result=None
try:
    from coin_risk import register_result
except Exception:
    register_result=None
try:
    from slot_manager import add_position, close_position
except Exception:
    add_position=None; close_position=None

ACTIVE_FILE='active_signals.json'; STATS_FILE='signal_stats.json'; TRACKER_OHLCV_TIMEFRAME='1m'; TRACKER_LOOKBACK_BUFFER_SECONDS=90; TRACKER_MAX_OHLCV_LIMIT=180; SAME_CANDLE_HIT_POLICY='SL_FIRST'
exchange=ccxt.okx({'enableRateLimit':True,'timeout':20000,'options':{'defaultType':'swap'}})

def to_okx_symbol(symbol): return f"{str(symbol).upper().replace('USDT','')}/USDT:USDT"
def now_ts(): return int(time.time())
def now_text(): return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
def fa_direction(d): return 'لانگ' if d=='LONG' else 'شورت' if d=='SHORT' else str(d)
def get_active_signals(): return load_json(ACTIVE_FILE, [])
def save_active_signals(s): save_json(ACTIVE_FILE, s)
def get_signal_stats(): return load_json(STATS_FILE, [])
def save_signal_stats(s): save_json(STATS_FILE, s)
def reset_signal_stats(): save_signal_stats([]); return True
reset_stats=reset_signal_stats

def _signal_id(signal): return signal.get('signal_id') or signal.get('id') or f"{signal.get('symbol')}_{now_ts()}_{uuid.uuid4().hex[:6]}"
def has_active_symbol(active, user_id, symbol): return any(int(x.get('user_id',0))==int(user_id) and x.get('symbol')==symbol and x.get('status')=='ACTIVE' for x in active)
def can_add_automatic_signal(user_id, symbol):
    return (False,'duplicate') if has_active_symbol(get_active_signals(), user_id, symbol) else (True,'ok')

def record_stat_event(signal, event_type, exit_price=None, move_percent=None):
    stats=get_signal_stats(); item=dict(signal); item['signal_id']=_signal_id(signal); item['event_type']=event_type; item['status']=event_type; item['event_at']=now_ts(); item['event_at_text']=now_text()
    if exit_price is not None: item['exit_price']=exit_price
    if move_percent is not None: item['move_percent']=move_percent
    stats.append(item); save_signal_stats(stats)

def ai_record_signal(signal):
    if record_signal:
        try: record_signal(signal, signal_type='REAL')
        except Exception as e: print('AI RECORD SIGNAL ERROR:', e)

def ai_record_result(signal, hit_type, exit_price, pct):
    sid=signal.get('signal_id') or signal.get('id')
    if update_signal_result:
        try: update_signal_result(sid, hit_type, exit_price=exit_price, move_percent=pct)
        except Exception as e: print('AI UPDATE RESULT ERROR:', e)
    if register_result:
        try: register_result(signal.get('symbol'), signal.get('direction'), hit_type)
        except Exception as e: print('AI RISK REGISTER ERROR:', e)

def ai_open_slot(signal):
    if add_position:
        try: add_position(signal.get('signal_id') or signal.get('id'), signal.get('symbol'), signal.get('direction'), score=signal.get('score'))
        except Exception as e: print('AI SLOT OPEN ERROR:', e)

def ai_close_slot(signal):
    if close_position:
        try: close_position(signal.get('signal_id') or signal.get('id'))
        except Exception as e: print('AI SLOT CLOSE ERROR:', e)

def _normalize_add_args(*args, **kwargs):
    if args and isinstance(args[0], dict):
        result=dict(args[0]); user_id=kwargs.get('user_id') or result.get('user_id') or result.get('chat_id') or 0; chat_id=kwargs.get('chat_id') or result.get('chat_id') or user_id; message_id=kwargs.get('telegram_message_id') or kwargs.get('message_id') or result.get('telegram_message_id') or result.get('message_id') or 0; return int(user_id or 0), int(chat_id or 0), int(message_id or 0), result
    if len(args)>=4: return int(args[0]), int(args[1]), int(args[2]), dict(args[3])
    return int(kwargs.get('user_id',0)), int(kwargs.get('chat_id',0)), int(kwargs.get('message_id') or kwargs.get('telegram_message_id') or 0), dict(kwargs.get('result') or {})

def add_signal_to_tracking(*args, **kwargs):
    user_id, chat_id, message_id, result = _normalize_add_args(*args, **kwargs)
    if result.get('direction') not in ['LONG','SHORT']: return False, 'این تحلیل سیگنال قابل پیگیری ندارد.'
    if result.get('stop_loss') is None or result.get('tp1') is None: return False, 'برای این سیگنال TP/SL کامل نیست.'
    active=get_active_signals(); symbol=result.get('symbol')
    if has_active_symbol(active,user_id,symbol): return False, f'⚠️ {symbol} از قبل زیر نظر است.'
    sid=result.get('signal_id') or f"{symbol}_{message_id}_{now_ts()}_{uuid.uuid4().hex[:6]}"
    signal={'id':sid,'signal_id':sid,'user_id':int(user_id),'chat_id':int(chat_id),'message_id':int(message_id),'reply_to_message_id':int(message_id),'symbol':symbol,'direction':result['direction'],'status':'ACTIVE','entry':float(result.get('entry') or result.get('price')),'price':float(result.get('price') or result.get('entry')),'stop_loss':float(result['stop_loss']),'tp1':float(result['tp1']),'tp2':None if result.get('tp2') is None else float(result['tp2']),'score':result.get('score'),'risk_level':result.get('risk_level'),'risk_reward':result.get('risk_reward'),'entry_mode':result.get('entry_mode') or 'AI_CLASSIC_DIRECT','confirmations':result.get('confirmations'),'freshness':result.get('freshness'),'rsi':result.get('rsi'),'adx':result.get('adx'),'macd':result.get('macd'),'macd_signal':result.get('macd_signal'),'macd_hist':result.get('macd_hist'),'power2_buy':result.get('power2_buy'),'power2_sell':result.get('power2_sell'),'power3_buy':result.get('power3_buy'),'power3_sell':result.get('power3_sell'),'buy_power':result.get('buy_power'),'sell_power':result.get('sell_power'),'atr':result.get('atr'),'market_mode':result.get('market_mode') or result.get('market_regime'),'coin_behavior':result.get('coin_behavior'),'btc_bias':result.get('btc_bias'),'support':result.get('support'),'resistance':result.get('resistance'),'snapshot':result.get('snapshot',{}),'reasons':result.get('reasons',[]),'created_at':now_ts(),'created_at_text':now_text(),'last_checked_at':now_ts()}
    active.append(signal); save_active_signals(active); record_stat_event(signal,'SIGNAL_CREATED'); ai_record_signal(signal); ai_open_slot(signal)
    if open_paper_position:
        try: open_paper_position(signal, telegram_message_id=message_id, chat_id=chat_id)
        except Exception: pass
    return True, f"✅ سیگنال زیر نظر گرفته شد\n\nارز: {signal['symbol']}\nجهت: {fa_direction(signal['direction'])}\nورود: {signal['entry']}\nTP1: {signal['tp1']}\nSL: {signal['stop_loss']}"

def get_recent_1m_candles_since(symbol, since_ts):
    since_ts=int(since_ts or now_ts()-5*60); since_ms=max(0,(since_ts-TRACKER_LOOKBACK_BUFFER_SECONDS)*1000); minutes=max(5,int((now_ts()-since_ts)/60)+4); limit=min(TRACKER_MAX_OHLCV_LIMIT,max(10,minutes))
    return exchange.fetch_ohlcv(to_okx_symbol(symbol), timeframe=TRACKER_OHLCV_TIMEFRAME, since=since_ms, limit=limit) or []

def candle_path_hit(signal, candle):
    high=float(candle[2]); low=float(candle[3]); direction=signal.get('direction'); tp1=float(signal['tp1']); sl=float(signal['stop_loss'])
    if direction=='LONG': tp_hit=high>=tp1; sl_hit=low<=sl
    elif direction=='SHORT': tp_hit=low<=tp1; sl_hit=high>=sl
    else: return None,None
    if tp_hit and sl_hit: return ('SL',sl) if SAME_CANDLE_HIT_POLICY=='SL_FIRST' else ('TP1',tp1)
    if tp_hit: return 'TP1',tp1
    if sl_hit: return 'SL',sl
    return None,None

def move_percent(signal, exit_price):
    entry=float(signal.get('entry') or 0)
    if entry<=0: return 0.0
    return round(((float(exit_price)-entry)/entry)*100,4) if signal.get('direction')=='LONG' else round(((entry-float(exit_price))/entry)*100,4)

def check_active_signals():
    active=get_active_signals(); remaining=[]; messages=[]
    for signal in active:
        if signal.get('status')!='ACTIVE': continue
        try:
            hit_type=None; exit_price=None; candles=get_recent_1m_candles_since(signal['symbol'], signal.get('last_checked_at') or signal.get('created_at'))
            for candle in candles:
                hit_type,exit_price=candle_path_hit(signal,candle)
                if hit_type: break
            signal['last_checked_at']=now_ts()
            if hit_type:
                pct=move_percent(signal,exit_price); record_stat_event(signal,hit_type,exit_price,pct); ai_record_result(signal,hit_type,exit_price,pct); ai_close_slot(signal)
                paper_msg=None
                if close_paper_position_by_signal_id:
                    try:
                        closed=close_paper_position_by_signal_id(signal.get('signal_id') or signal.get('id'), exit_price, hit_type)
                        if closed: paper_msg=f"Paper بسته شد: {closed.get('pnl_percent')}%"
                    except Exception: pass
                icon='✅' if hit_type=='TP1' else '❌'; result_fa='حد سود 1' if hit_type=='TP1' else 'حد ضرر'
                text=f"{icon} نتیجه سیگنال {signal.get('symbol')}\nجهت: {fa_direction(signal.get('direction'))}\nورود: {signal.get('entry')}\nقیمت خروج: {exit_price}\nنتیجه: {result_fa}\nدرصد حرکت: {pct}٪"
                if paper_msg: text+='\n\n'+paper_msg
                messages.append({'chat_id':signal.get('chat_id'),'message':text,'text':text,'reply_to_message_id':signal.get('message_id')})
            else: remaining.append(signal)
        except Exception as e:
            signal['last_checked_at']=now_ts(); signal['last_error']=str(e)[:250]; remaining.append(signal)
    save_active_signals(remaining); return messages

def format_active_signals():
    a=get_active_signals()
    if not a: return 'سیگنال فعالی وجود ندارد.'
    lines=['📌 سیگنال‌های فعال:']
    for s in a: lines.append(f"{s.get('symbol')} | {fa_direction(s.get('direction'))}\nEntry: {s.get('entry')} | TP1: {s.get('tp1')} | SL: {s.get('stop_loss')}")
    return '\n\n'.join(lines)

def parse_days_from_text(text):
    m=re.search(r'(\d+)', text or '')
    if m: return int(m.group(1))
    if text and 'کل' in text: return 3650
    return 7
parse_days_from_report_text=parse_days_from_text

def format_signal_stats(days=7):
    stats=get_signal_stats(); since=now_ts()-int(days)*86400 if int(days)<3650 else 0; data=[s for s in stats if int(s.get('event_at',s.get('created_at',0)) or 0)>=since]
    created=[s for s in data if s.get('event_type')=='SIGNAL_CREATED']; tp1=[s for s in data if s.get('event_type')=='TP1']; sl=[s for s in data if s.get('event_type')=='SL']; total=len(tp1)+len(sl); wr=round(len(tp1)/total*100,1) if total else 0; active=len(get_active_signals())
    longs=[s for s in tp1+sl if s.get('direction')=='LONG']; shorts=[s for s in tp1+sl if s.get('direction')=='SHORT']
    return f"📊 آمار {days} روز اخیر\n\nسیگنال ثبت‌شده: {len(created)}\nمعاملات فعال: {active}\nTP1: {len(tp1)}\nSL: {len(sl)}\nWin Rate: {wr}%\nلانگ: {len(longs)} | شورت: {len(shorts)}\nمعماری: AI_CLASSIC_DIRECT + LEARNING"
get_stats_report=format_signal_stats

def reset_stats(): return reset_signal_stats()

def get_symbol_stats_report(days=3650, mode='all'):
    stats=get_signal_stats(); closed=[s for s in stats if s.get('event_type') in ['TP1','SL'] and s.get('symbol')]
    if not closed: return '📊 آمار ارزها\n\nهنوز نتیجه TP1/SL ثبت نشده است.'
    by={}
    for x in closed:
        r=by.setdefault(x['symbol'], {'tp1':0,'sl':0,'total':0})
        r['total']+=1; r['tp1']+=1 if x.get('event_type')=='TP1' else 0; r['sl']+=1 if x.get('event_type')=='SL' else 0
    lines=['📊 آمار ارزها']
    for sym,r in sorted(by.items(), key=lambda kv: kv[1]['total'], reverse=True)[:30]:
        wr=round(r['tp1']/max(r['total'],1)*100,1); lines.append(f"{sym} | TP1:{r['tp1']} SL:{r['sl']} WR:{wr}%")
    return '\n'.join(lines)
