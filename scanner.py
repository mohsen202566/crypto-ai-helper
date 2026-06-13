# -*- coding: utf-8 -*-
import time
from typing import Dict, List, Optional, Any
from analysis import analyze_symbol
try:
    from config import SCAN_SYMBOLS, AUTO_DIRECT_SCORE_MIN
except Exception:
    SCAN_SYMBOLS=['BTCUSDT','ETHUSDT','SOLUSDT']; AUTO_DIRECT_SCORE_MIN=82
try:
    from slot_manager import get_free_slots, is_symbol_direction_active, select_best_candidates
except Exception:
    get_free_slots=None; is_symbol_direction_active=None; select_best_candidates=None
try:
    from ghost_signals import create_ghost_signal
except Exception:
    create_ghost_signal=None
try:
    from coin_rotation import sort_symbols_by_rotation
except Exception:
    sort_symbols_by_rotation=None
SCAN_DELAY_SECONDS=0.20; MAX_SCAN_RESULTS=10; MIN_SCANNER_SCORE=AUTO_DIRECT_SCORE_MIN

def normalize_symbol(symbol):
    s=str(symbol).upper().strip(); return s if s.endswith('USDT') else f'{s}USDT'

def get_scan_symbols():
    symbols=list(dict.fromkeys([normalize_symbol(x) for x in SCAN_SYMBOLS if str(x).strip()]))
    if sort_symbols_by_rotation:
        try: return sort_symbols_by_rotation(symbols)
        except Exception: pass
    return symbols

def is_valid_signal(r):
    return isinstance(r,dict) and r.get('status')=='ACTIVE' and r.get('entry_confirmed') and r.get('direction') in ['LONG','SHORT'] and int(r.get('score') or 0)>=MIN_SCANNER_SCORE and r.get('entry') is not None and r.get('stop_loss') is not None and r.get('tp1') is not None

def signal_rank_value(r):
    score=float(r.get('score') or 0); conf=float(r.get('confirmations') or 0); rr=float(r.get('risk_reward') or 0); risk={'LOW':4,'MEDIUM':2}.get(r.get('risk_level'),0); fresh={'HIGH':3,'MEDIUM':1}.get(r.get('freshness'),0)
    return score+conf*1.5+rr*2+risk+fresh

def should_skip_duplicate(r):
    if not is_symbol_direction_active: return False
    try: return bool(is_symbol_direction_active(r.get('symbol'), r.get('direction')))
    except Exception: return False

def scan_market(symbols: Optional[List[str]]=None, max_results:int=MAX_SCAN_RESULTS, allow_ghost:bool=True):
    symbols=symbols or get_scan_symbols(); valid=[]; no_trade=0; errors=0
    for sym in symbols:
        try:
            res=analyze_symbol(normalize_symbol(sym))
            if not is_valid_signal(res): no_trade+=1; continue
            if should_skip_duplicate(res): continue
            valid.append(res)
        except Exception: errors+=1
        time.sleep(SCAN_DELAY_SECONDS)
    valid.sort(key=signal_rank_value, reverse=True)
    return {'signals':valid[:max_results],'all_valid_signals':valid,'scanned':len(symbols),'no_trade_count':no_trade,'error_count':errors,'ghost_count':0,'timestamp':int(time.time())}

def get_available_slots():
    if get_free_slots is None: return 1
    try: return max(0,int(get_free_slots()))
    except Exception: return 1

def save_as_ghost(r, reason='SLOT_FULL'):
    if not create_ghost_signal: return False
    try:
        create_ghost_signal(r.get('symbol'), r.get('direction'), r.get('entry'), r.get('stop_loss'), r.get('tp1'), r.get('tp2'), r.get('score'), r.get('snapshot',{}), 'scanner', reason); return True
    except Exception: return False

def scan_for_auto_signals(symbols: Optional[List[str]]=None, max_results:int=MAX_SCAN_RESULTS, allow_ghost:bool=True):
    sr=scan_market(symbols,max_results,allow_ghost); valid=sr.get('all_valid_signals',[])
    if not valid: sr['signals']=[]; sr['mode']='NO_SIGNAL'; return sr
    free=get_available_slots(); sr['free_slots']=free
    if free<=0:
        gc=0
        if allow_ghost:
            for sig in valid:
                if save_as_ghost(sig): gc+=1
        sr['signals']=[]; sr['ghost_count']=gc; sr['mode']='GHOST_ONLY'; return sr
    candidates=valid
    if select_best_candidates:
        try:
            selected=select_best_candidates(valid, min(max_results, free))
            if isinstance(selected,list): candidates=selected
        except Exception: candidates=valid
    candidates=sorted(candidates,key=signal_rank_value,reverse=True)
    sr['signals']=candidates[:min(max_results,free)]; sr['mode']='ACTIVE_SIGNALS'; return sr

def get_best_signal(symbols=None):
    r=scan_for_auto_signals(symbols,1,False); return (r.get('signals') or [None])[0]

def get_top_signals(symbols=None, limit=5): return scan_for_auto_signals(symbols,limit,False).get('signals',[])[:limit]

def scan_market_overview(symbols=None, limit=40):
    symbols=(symbols or get_scan_symbols())[:limit]; bullish=bearish=neutral=errors=0; details=[]
    for sym in symbols:
        try:
            r=analyze_symbol(normalize_symbol(sym)); t=r.get('trends',{}); one=t.get('1H'); fifteen=t.get('15M')
            if one=='bullish' and fifteen=='bullish': bullish+=1; bias='bullish'
            elif one=='bearish' and fifteen=='bearish': bearish+=1; bias='bearish'
            else: neutral+=1; bias='neutral'
            details.append({'symbol':sym,'bias':bias,'direction':r.get('direction'),'score':r.get('score')})
        except Exception: errors+=1
        time.sleep(SCAN_DELAY_SECONDS)
    total=max(bullish+bearish+neutral,1); bp=round(bullish/total*100,1); sp=round(bearish/total*100,1); np=round(neutral/total*100,1)
    if bp>=50: mb='bullish'; summary='بازار بیشتر صعودی است'
    elif sp>=50: mb='bearish'; summary='بازار بیشتر نزولی است'
    elif np>=45: mb='neutral'; summary='بازار بیشتر رنج یا نامشخص است'
    elif bp>sp: mb='slightly_bullish'; summary='بازار کمی تمایل صعودی دارد'
    elif sp>bp: mb='slightly_bearish'; summary='بازار کمی تمایل نزولی دارد'
    else: mb='neutral'; summary='بازار جهت مشخصی ندارد'
    return {'market_bias':mb,'summary':summary,'bullish':bullish,'bearish':bearish,'neutral':neutral,'errors':errors,'bullish_pct':bp,'bearish_pct':sp,'neutral_pct':np,'details':details,'scanned':len(symbols),'timestamp':int(time.time())}

def scan_symbols_for_signals(symbols=None,max_results=MAX_SCAN_RESULTS): return scan_for_auto_signals(symbols,max_results,True).get('signals',[])
def find_best_signal(symbols=None): return get_best_signal(symbols)
def find_top_signals(symbols=None,limit=5): return get_top_signals(symbols,limit)
