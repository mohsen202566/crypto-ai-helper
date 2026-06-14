# -*- coding: utf-8 -*-
"""AI Classic Direct Analysis Engine - complete integrated version."""
import math
import time
from typing import Dict, List, Optional, Tuple, Any
import ccxt
import pandas as pd
import ta
try:
    from config import MIN_DIRECT_SCORE, MIN_ADX_FOR_TREND, MIN_MANUAL_CONFIRMATIONS
except Exception:
    MIN_DIRECT_SCORE = 82; MIN_ADX_FOR_TREND = 20; MIN_MANUAL_CONFIRMATIONS = 3
try:
    from coin_learning import build_signal_snapshot, get_smart_tp_suggestion, should_require_extra_strength
except Exception:
    build_signal_snapshot = None; get_smart_tp_suggestion = None; should_require_extra_strength = None
try:
    from coin_risk import get_direction_risk_state
except Exception:
    get_direction_risk_state = None
try:
    from coin_rotation import get_coin_rotation_score
except Exception:
    get_coin_rotation_score = None
try:
    from ai_memory import update_ai_summary
except Exception:
    update_ai_summary = None

exchange = ccxt.okx({'enableRateLimit': True, 'timeout': 20000, 'options': {'defaultType': 'swap'}})
_SOFT_MARKET_CONTEXT_CACHE = {'ts': 0, 'data': None}
SOFT_MARKET_CONTEXT_TTL_SECONDS = 120
AUTO_DIRECT_SCORE_MIN = 82
ADX_HARD_MIN = max(float(MIN_ADX_FOR_TREND), 20.0)
LONG_DIRECT_SCORE_BONUS_REQUIREMENT = 0
LONG_MIN_1H_STRICT = False
LONG_BLOCK_IF_AGAINST_VWAP = False
MIN_SL_ATR_MULTIPLIER = 1.30
TP1_FALLBACK_ATR = 0.75
TP2_FALLBACK_ATR = 1.40
MAX_REASONABLE_SL_ATR = 2.40
MIN_TP1_ATR = 0.55
LEVEL_BUFFER_ATR = 0.14
SL_BUFFER_ATR = 0.25
TF_LEVEL_WEIGHTS = {'5M': 1.0, '15M': 1.6, '30M': 2.2}
LEVEL_LOOKBACK = 160
SWING_WINDOW = 3

def to_okx_symbol(symbol: str) -> str:
    coin = str(symbol).upper().replace('USDT','')
    return f'{coin}/USDT:USDT'

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None: return default
        v = float(value)
        return default if math.isnan(v) or math.isinf(v) else v
    except Exception: return default

def safe_round(value: Any, digits: int = 8):
    try: return None if value is None else round(float(value), digits)
    except Exception: return None

def cap_score(value: Any) -> int:
    try: return max(0, min(100, int(round(float(value)))))
    except Exception: return 0

def get_klines(symbol: str, interval: str = '15m', limit: int = 260, include_current: bool = False) -> pd.DataFrame:
    data = exchange.fetch_ohlcv(to_okx_symbol(symbol), timeframe=interval, limit=limit)
    if not data or len(data) < 220: raise Exception(f'داده کافی برای {symbol} در تایم {interval} دریافت نشد')
    df = pd.DataFrame(data, columns=['time','open','high','low','close','volume'])
    for c in ['open','high','low','close','volume']: df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna()
    if not include_current: df = df.iloc[:-1]
    if len(df) < 210: raise Exception(f'داده کندل کافی برای {symbol} در تایم {interval} کامل نیست')
    return df

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['ema20'] = ta.trend.ema_indicator(df['close'], window=20)
    df['ema50'] = ta.trend.ema_indicator(df['close'], window=50)
    df['ema200'] = ta.trend.ema_indicator(df['close'], window=200)
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    macd = ta.trend.MACD(df['close']); df['macd'] = macd.macd(); df['macd_signal'] = macd.macd_signal(); df['macd_hist'] = macd.macd_diff()
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    adx = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14); df['adx'] = adx.adx()
    typical = (df['high'] + df['low'] + df['close']) / 3
    volume_sum = df['volume'].cumsum().replace(0, pd.NA)
    df['vwap'] = (typical * df['volume']).cumsum() / volume_sum
    df['volume_ma20'] = df['volume'].rolling(20).mean(); df['volume_ratio'] = df['volume'] / df['volume_ma20'].replace(0, pd.NA)
    df = df.dropna()
    if len(df) < 60: raise Exception('اندیکاتورها کامل محاسبه نشدند')
    return df

def ema_direction(df):
    last=df.iloc[-1]
    if last['ema50'] > last['ema200']: return 'bullish'
    if last['ema50'] < last['ema200']: return 'bearish'
    return 'range'

def trend_direction(df):
    last=df.iloc[-1]; close=safe_float(last['close'])
    if last['ema50'] > last['ema200']: return 'bullish' if close > last['ema50'] else 'weak_bullish'
    if last['ema50'] < last['ema200']: return 'bearish' if close < last['ema50'] else 'weak_bearish'
    return 'range'

def vwap_status(df):
    last=df.iloc[-1]
    if last['close'] > last['vwap']: return 'above_vwap'
    if last['close'] < last['vwap']: return 'below_vwap'
    return 'near_vwap'

def distance_from_ema20_atr(df):
    last=df.iloc[-1]; price=safe_float(last['close']); atr=max(safe_float(last['atr']), price*0.0015)
    return abs(price-safe_float(last['ema20']))/atr

def volume_quality(df):
    ratio=safe_float(df.iloc[-1].get('volume_ratio',1.0),1.0)
    if ratio>=1.35: return 'high_volume', ratio
    if ratio>=0.90: return 'normal_volume', ratio
    if ratio<=0.65: return 'weak_volume', ratio
    return 'neutral_volume', ratio

def buy_sell_power(df, candles=20):
    r=df.tail(candles); green=r[r['close']>r['open']]['volume'].sum(); red=r[r['close']<r['open']]['volume'].sum(); total=green+red
    if total<=0: return 50.0, 50.0
    return round(green/total*100,1), round(red/total*100,1)

def get_coin_risk(symbol, direction):
    default={'sl_count':0,'tp_count':0,'strictness_level':0,'risk_score':0}
    if not get_direction_risk_state: return default
    try:
        r=get_direction_risk_state(symbol,direction); default.update(r if isinstance(r,dict) else {})
    except Exception: pass
    return default

def get_rotation_context(symbol):
    default={'rotation_score':50,'priority_score':50,'risk_score':0,'status':'NORMAL'}
    if not get_coin_rotation_score: return default
    try:
        r=get_coin_rotation_score(symbol); default.update(r if isinstance(r,dict) else {})
    except Exception: pass
    return default

def ai_extra_strength_required(symbol, direction, snapshot):
    default={'required':False,'extra_score':0,'extra_confirmations':0,'reason':None}
    if not should_require_extra_strength: return default
    try:
        r=should_require_extra_strength(symbol,direction,snapshot)
        if isinstance(r,dict): default.update(r)
    except Exception: pass
    return default

def build_local_snapshot(symbol, direction, df_4h, df_1h, df_30m, df_15m, df_5m, score_pack, market_context):
    l15=df_15m.iloc[-1]; l5=df_5m.iloc[-1]; buy2,sell2=buy_sell_power(df_5m,2); buy3,sell3=buy_sell_power(df_5m,3); buy20,sell20=buy_sell_power(df_5m,20)
    snap={'symbol':symbol,'direction':direction,'price':safe_float(l15['close']),'entry':safe_float(l15['close']),'rsi':safe_float(l15['rsi']),'rsi_5m':safe_float(l5['rsi']),'macd':safe_float(l15['macd']),'macd_signal':safe_float(l15['macd_signal']),'macd_hist':safe_float(l15['macd_hist']),'adx':safe_float(l15['adx']),'atr':safe_float(l15['atr']),'ema20':safe_float(l15['ema20']),'ema50':safe_float(l15['ema50']),'ema200':safe_float(l15['ema200']),'vwap':safe_float(l15['vwap']),'vwap_status':vwap_status(df_15m),'power2_buy':buy2,'power2_sell':sell2,'power3_buy':buy3,'power3_sell':sell3,'buy_power':buy20,'sell_power':sell20,'trends':score_pack.get('trends',{}),'long_score':score_pack.get('long_score',0),'short_score':score_pack.get('short_score',0),'market_regime':market_context.get('market_regime','neutral'),'btc_bias':market_context.get('btc_bias','neutral')}
    if build_signal_snapshot:
        try:
            extra=build_signal_snapshot(symbol, direction, snap, market_context)
            if isinstance(extra,dict): snap.update(extra)
        except Exception: pass
    return snap

def simple_classic_score(symbol, df_4h, df_1h, df_30m, df_15m, df_5m, market_context=None):
    market_context=market_context or {}; long_score=0.0; short_score=0.0; long_reasons=[]; short_reasons=[]; cl=0; cs=0
    trends={'4H':ema_direction(df_4h),'1H':ema_direction(df_1h),'30M':ema_direction(df_30m),'15M':ema_direction(df_15m),'5M':ema_direction(df_5m)}
    l1=df_1h.iloc[-1]; l30=df_30m.iloc[-1]; l15=df_15m.iloc[-1]; p15=df_15m.iloc[-2]; l5=df_5m.iloc[-1]; p5=df_5m.iloc[-2]
    weights={'4H':8,'1H':22,'30M':12,'15M':18,'5M':10}
    for tf,tr in trends.items():
        if tr=='bullish': long_score+=weights[tf]; cl += 1 if tf in ['1H','30M','15M'] else 0; long_reasons.append(f'{tf}: روند صعودی')
        elif tr=='bearish': short_score+=weights[tf]; cs += 1 if tf in ['1H','30M','15M'] else 0; short_reasons.append(f'{tf}: روند نزولی')
    if l15['ema20']>l15['ema50']>l15['ema200']: long_score+=10; cl+=1; long_reasons.append('15M EMA stack صعودی')
    elif l15['ema20']<l15['ema50']<l15['ema200']: short_score+=10; cs+=1; short_reasons.append('15M EMA stack نزولی')
    if l1['ema20']>l1['ema50']>l1['ema200']: long_score+=7; cl+=1
    elif l1['ema20']<l1['ema50']<l1['ema200']: short_score+=6
    if l15['close']>l15['ema20']: long_score+=10; cl+=1
    else: short_score+=10; cs+=1
    if l5['close']>l5['ema20']: long_score+=6
    else: short_score+=6
    rsi15=safe_float(l15['rsi']); rsi30=safe_float(l30['rsi']); rsi5=safe_float(l5['rsi'])
    if 52<=rsi15<=66: long_score+=12; cl+=1
    elif 32<=rsi15<=50: short_score+=12; cs+=1
    elif rsi15>72: long_score-=6
    elif rsi15<28: short_score-=5
    if rsi15>safe_float(p15['rsi']): long_score+=4
    elif rsi15<safe_float(p15['rsi']): short_score+=4
    if rsi30>=50: long_score+=4
    else: short_score+=4
    if rsi5>=50: long_score+=3
    else: short_score+=3
    if rsi5>safe_float(p5['rsi']): long_score+=2
    elif rsi5<safe_float(p5['rsi']): short_score+=2
    if l15['macd']>l15['macd_signal']: long_score+=15; cl+=1
    else: short_score+=15; cs+=1
    if l15['macd_hist']>0: long_score+=5
    else: short_score+=5
    if l15['macd_hist']>p15['macd_hist']: long_score+=5
    else: short_score+=5
    if l30['macd']>l30['macd_signal']: long_score+=7
    else: short_score+=7
    if l5['macd']>l5['macd_signal']: long_score+=6
    else: short_score+=6
    adx=safe_float(l15['adx'])
    if adx>=35: long_score+=8; short_score+=8
    elif adx>=25: long_score+=5; short_score+=5
    elif adx>=ADX_HARD_MIN: long_score+=1; short_score+=1
    else: long_score=min(long_score,69); short_score=min(short_score,69); long_reasons.append('رد: ADX پایین'); short_reasons.append('رد: ADX پایین')
    if l15['close']>l15['vwap']: long_score+=4; short_score-=4
    else: short_score+=4; long_score-=4
    mb=market_context.get('market_regime','neutral')
    if mb=='bullish': long_score+=3; short_score-=3
    elif mb=='bearish': short_score+=3; long_score-=3
    buy2,sell2=buy_sell_power(df_5m,2); buy3,sell3=buy_sell_power(df_5m,3); buy20,sell20=buy_sell_power(df_5m,20)
    if buy3>=62: long_score+=3
    if sell3>=62: short_score+=3
    # Balanced auto-signal rules: not too strict at start; AI/risk modules can tighten later.
    long_direction_ok=(trends['15M']=='bullish') or (trends['1H']=='bullish' and trends['5M']=='bullish') or (trends['30M']=='bullish' and safe_float(l15['rsi'])>=50)
    short_direction_ok=(trends['15M']=='bearish') or (trends['1H']=='bearish' and trends['5M']=='bearish') or (trends['30M']=='bearish' and safe_float(l15['rsi'])<=50)
    long_macd_ok=(l15['macd']>l15['macd_signal']) or (l5['macd']>l5['macd_signal'] and l15['macd_hist']>=safe_float(p15['macd_hist']))
    short_macd_ok=(l15['macd']<=l15['macd_signal']) or (l5['macd']<=l5['macd_signal'] and l15['macd_hist']<=safe_float(p15['macd_hist']))
    long_1h_ok=(trends['1H']=='bullish' and l1['close']>l1['ema20'] and l1['macd']>=l1['macd_signal']) if LONG_MIN_1H_STRICT else True
    long_vwap_ok=(l15['close']>=l15['vwap']) if LONG_BLOCK_IF_AGAINST_VWAP else True
    if not long_direction_ok: long_reasons.append('رد لانگ: 1H و 15M صعودی نیستند')
    if not long_macd_ok: long_reasons.append('رد لانگ: MACD کافی نیست')
    if not long_1h_ok: long_reasons.append('رد لانگ: تایید 1H کافی نیست')
    if not long_vwap_ok: long_reasons.append('رد لانگ: خلاف VWAP')
    if not short_direction_ok: short_reasons.append('رد شورت: جهت کافی نیست')
    if not short_macd_ok: short_reasons.append('رد شورت: MACD کافی نیست')
    return {'long_score':cap_score(long_score),'short_score':cap_score(short_score),'long_reasons':long_reasons,'short_reasons':short_reasons,'confirmations_long':cl,'confirmations_short':cs,'trends':trends,'distance_ema20_atr':round(distance_from_ema20_atr(df_15m),2),'volume_status':volume_quality(df_15m)[0],'volume_ratio':round(volume_quality(df_15m)[1],2),'power2_buy':buy2,'power2_sell':sell2,'power3_buy':buy3,'power3_sell':sell3,'buy_power':buy20,'sell_power':sell20,'long_valid':adx>=ADX_HARD_MIN and long_direction_ok and long_macd_ok and long_1h_ok and long_vwap_ok,'short_valid':adx>=ADX_HARD_MIN and short_direction_ok and short_macd_ok,'adx_15':adx,'market_regime':mb}

def find_swing_levels(df, timeframe, lookback=LEVEL_LOOKBACK, window=SWING_WINDOW):
    recent=df.tail(lookback).copy(); levels=[]; w=TF_LEVEL_WEIGHTS.get(timeframe,1.0)
    if len(recent)<window*2+10: return levels
    for i in range(window, len(recent)-window):
        row=recent.iloc[i]; left=recent.iloc[i-window:i]; right=recent.iloc[i+1:i+1+window]; rec=1.0+(i/max(len(recent),1))*0.8
        if row['low']<=left['low'].min() and row['low']<=right['low'].min(): levels.append({'price':safe_float(row['low']),'kind':'support','timeframe':timeframe,'strength':w*rec})
        if row['high']>=left['high'].max() and row['high']>=right['high'].max(): levels.append({'price':safe_float(row['high']),'kind':'resistance','timeframe':timeframe,'strength':w*rec})
    return levels

def cluster_levels(raw, price, atr):
    if not raw: return []
    raw=sorted(raw,key=lambda x:x['price']); md=max(atr*0.25, price*0.001); clusters=[]
    for lv in raw:
        if not clusters or abs(lv['price']-clusters[-1][-1]['price'])>md: clusters.append([lv])
        else: clusters[-1].append(lv)
    out=[]
    for g in clusters:
        strength=sum(safe_float(x.get('strength')) for x in g); wp=sum(safe_float(x['price'])*safe_float(x['strength']) for x in g)/max(strength,1e-9); tfs=sorted(set(x['timeframe'] for x in g)); sup=sum(1 for x in g if x['kind']=='support'); kind='support' if sup>=len(g)-sup else 'resistance'
        out.append({'price':wp,'kind':kind,'strength':round(strength+min(len(g),5)*0.9+len(tfs)*0.8,2),'touches':len(g),'timeframes':tfs})
    return out

def get_strong_levels(df_5m, df_15m, df_30m, price, atr):
    raw=find_swing_levels(df_5m,'5M')+find_swing_levels(df_15m,'15M')+find_swing_levels(df_30m,'30M')
    clustered=cluster_levels(raw,price,atr); supports=[x for x in clustered if x['price']<price]; resistances=[x for x in clustered if x['price']>price]
    supports=sorted(supports,key=lambda x:(x['strength'],-abs(price-x['price'])),reverse=True); resistances=sorted(resistances,key=lambda x:(x['strength'],-abs(x['price']-price)),reverse=True)
    return {'supports':supports,'resistances':resistances,'nearest_support':max([x['price'] for x in supports], default=price-atr*MIN_SL_ATR_MULTIPLIER),'nearest_resistance':min([x['price'] for x in resistances], default=price+atr*TP1_FALLBACK_ATR)}

def coin_volatility_factor(df_15m, price):
    try:
        atr_pct=safe_float(df_15m.iloc[-1]['atr'])/max(price,1e-12); avg=((df_15m.tail(96)['high']-df_15m.tail(96)['low'])/df_15m.tail(96)['close'].replace(0,pd.NA)).mean(); raw=max(float(atr_pct),float(avg))
    except Exception: raw=0.004
    if raw>=0.012: return 1.25
    if raw>=0.008: return 1.15
    if raw<=0.003: return 0.95
    return 1.0

def select_level_for_sl(direction, price, atr, levels, base_distance):
    valid=[lv for lv in levels if base_distance*0.45 <= abs(price-safe_float(lv['price'])) <= atr*MAX_REASONABLE_SL_ATR]
    if not valid: return None
    valid.sort(key=lambda x:x['strength'], reverse=True); return safe_float(valid[0]['price'])

def select_level_for_tp(direction, price, atr, levels, fallback_mult, buffer):
    candidates=[]; min_d=atr*MIN_TP1_ATR; max_d=atr*3.0
    for lv in levels:
        lp=safe_float(lv['price']); target=lp-buffer if direction=='LONG' else lp+buffer; d=abs(target-price)
        if direction=='LONG' and target<=price: continue
        if direction=='SHORT' and target>=price: continue
        if min_d<=d<=max_d: candidates.append((lv['strength'],-d,target))
    if candidates: candidates.sort(reverse=True); return safe_float(candidates[0][2])
    return price+atr*fallback_mult if direction=='LONG' else price-atr*fallback_mult

def get_ai_tp_memory(symbol, direction, price, atr, snapshot):
    if not get_smart_tp_suggestion: return {}
    try: return get_smart_tp_suggestion(symbol, direction, snapshot) or {}
    except Exception: return {}

def merge_tp_with_ai_memory(direction, price, atr, sr_tp1, sr_tp2, ai_tp):
    tp1, tp2=sr_tp1, sr_tp2; mind=max(atr*MIN_TP1_ATR, price*0.0015)
    a1=ai_tp.get('tp1') if isinstance(ai_tp,dict) else None; a2=ai_tp.get('tp2') if isinstance(ai_tp,dict) else None
    if a1 is not None:
        a1=safe_float(a1)
        if direction=='LONG' and price+mind<=a1<=price+atr*2.5: tp1=min(tp1,a1)
        if direction=='SHORT' and price-atr*2.5<=a1<=price-mind: tp1=max(tp1,a1)
    if a2 is not None:
        a2=safe_float(a2)
        if direction=='LONG' and price+mind*1.35<=a2<=price+atr*4: tp2=min(tp2,a2)
        if direction=='SHORT' and price-atr*4<=a2<=price-mind*1.35: tp2=max(tp2,a2)
    if direction=='LONG' and tp2<=tp1: tp2=tp1+atr*0.45
    if direction=='SHORT' and tp2>=tp1: tp2=tp1-atr*0.45
    return tp1,tp2

def build_trade_levels(direction, price, atr, df_5m, df_15m, df_30m, snapshot=None, symbol=None):
    price=safe_float(price); atr=max(safe_float(atr), price*0.0015); vf=coin_volatility_factor(df_15m,price); min_sl=atr*MIN_SL_ATR_MULTIPLIER*vf; buf_tp=max(atr*LEVEL_BUFFER_ATR*vf, price*0.0007); buf_sl=max(atr*SL_BUFFER_ATR*vf, price*0.001)
    levels=get_strong_levels(df_5m,df_15m,df_30m,price,atr); supports=levels['supports']; res=levels['resistances']; ai_tp=get_ai_tp_memory(symbol,direction,price,atr,snapshot) if symbol and snapshot else {}
    if direction=='LONG':
        classic_sl=price-min_sl; sp=select_level_for_sl(direction,price,atr,supports,min_sl); sl=min(sp-buf_sl, classic_sl) if sp else classic_sl
        if abs(price-sl)>atr*MAX_REASONABLE_SL_ATR*vf: sl=classic_sl
        sr_tp1=select_level_for_tp(direction,price,atr,res,TP1_FALLBACK_ATR*vf,buf_tp); sr_tp2=select_level_for_tp(direction,price,atr,[x for x in res if x['price']>sr_tp1],TP2_FALLBACK_ATR*vf,buf_tp)
        if sr_tp2<=sr_tp1: sr_tp2=price+atr*TP2_FALLBACK_ATR*vf
    else:
        classic_sl=price+min_sl; rp=select_level_for_sl(direction,price,atr,res,min_sl); sl=max(rp+buf_sl, classic_sl) if rp else classic_sl
        if abs(price-sl)>atr*MAX_REASONABLE_SL_ATR*vf: sl=classic_sl
        sr_tp1=select_level_for_tp(direction,price,atr,supports,TP1_FALLBACK_ATR*vf,buf_tp); sr_tp2=select_level_for_tp(direction,price,atr,[x for x in supports if x['price']<sr_tp1],TP2_FALLBACK_ATR*vf,buf_tp)
        if sr_tp2>=sr_tp1: sr_tp2=price-atr*TP2_FALLBACK_ATR*vf
    tp1,tp2=merge_tp_with_ai_memory(direction,price,atr,sr_tp1,sr_tp2,ai_tp); risk=abs(price-sl); reward=abs(tp1-price); rr=round(reward/risk,2) if risk>0 else 0
    return safe_round(sl), safe_round(tp1), safe_round(tp2), rr, {'volatility_factor':round(vf,3),'ai_tp_used':bool(ai_tp),'ai_tp':ai_tp,'nearest_support':levels.get('nearest_support'),'nearest_resistance':levels.get('nearest_resistance')}

def get_soft_market_context():
    now = time.time() if 'time' in globals() else 0
    try:
        cached = _SOFT_MARKET_CONTEXT_CACHE.get('data')
        cached_ts = float(_SOFT_MARKET_CONTEXT_CACHE.get('ts') or 0)
        if cached and now and (now - cached_ts) <= SOFT_MARKET_CONTEXT_TTL_SECONDS:
            return dict(cached)
    except Exception:
        pass
    try:
        b4=add_indicators(get_klines('BTCUSDT','4h')); b1=add_indicators(get_klines('BTCUSDT','1h')); b15=add_indicators(get_klines('BTCUSDT','15m'))
        t4=ema_direction(b4); t1=ema_direction(b1); t15=ema_direction(b15); last=b15.iloc[-1]
        regime='bullish' if t4=='bullish' and t1=='bullish' else 'bearish' if t4=='bearish' and t1=='bearish' else 'neutral'
        btc_bias='bullish' if regime=='bullish' and last['macd']>=last['macd_signal'] else 'bearish' if regime=='bearish' and last['macd']<=last['macd_signal'] else 'neutral'
        data={'market_regime':regime,'btc_bias':btc_bias,'btc_4h':t4,'btc_1h':t1,'btc_15m':t15}
        try:
            _SOFT_MARKET_CONTEXT_CACHE['data'] = dict(data)
            _SOFT_MARKET_CONTEXT_CACHE['ts'] = now or 0
        except Exception:
            pass
        return data
    except Exception:
        return {'market_regime':'neutral','btc_bias':'neutral'}

def analyze_symbol(symbol: str) -> Dict:
    symbol=str(symbol).upper().strip()
    try:
        df_4h=add_indicators(get_klines(symbol,'4h')); df_1h=add_indicators(get_klines(symbol,'1h')); df_30m=add_indicators(get_klines(symbol,'30m')); df_15m=add_indicators(get_klines(symbol,'15m')); df_5m=add_indicators(get_klines(symbol,'5m'))
        market_context=get_soft_market_context(); sp=simple_classic_score(symbol,df_4h,df_1h,df_30m,df_15m,df_5m,market_context); price=safe_float(df_15m.iloc[-1]['close']); atr=safe_float(df_15m.iloc[-1]['atr']); long_score=int(sp['long_score']); short_score=int(sp['short_score'])
        if long_score>=short_score: direction='LONG'; final_score=long_score; confirmations=int(sp['confirmations_long']); reasons=list(sp['long_reasons']); valid=bool(sp['long_valid'])
        else: direction='SHORT'; final_score=short_score; confirmations=int(sp['confirmations_short']); reasons=list(sp['short_reasons']); valid=bool(sp['short_valid'])
        snapshot=build_local_snapshot(symbol,direction,df_4h,df_1h,df_30m,df_15m,df_5m,sp,market_context)
        risk_state=get_coin_risk(symbol,direction); strict=int(risk_state.get('strictness_level',0) or 0)
        if strict: final_score-=strict; reasons.append(f'AI Risk: سختگیری سطح {strict}')
        rotation=get_rotation_context(symbol); rs=safe_float(rotation.get('rotation_score',50),50)
        if rs>=75: final_score+=2
        elif rs<=25: final_score-=2
        extra=ai_extra_strength_required(symbol,direction,snapshot); min_score=82+int(extra.get('extra_score',0) or 0); base_conf=min(3, int(MIN_MANUAL_CONFIRMATIONS)); req_conf=base_conf+int(extra.get('extra_confirmations',0) or 0)
        if extra.get('required'): reasons.append(extra.get('reason') or 'AI تایید بیشتر می‌خواهد')
        level_pack=get_strong_levels(df_5m,df_15m,df_30m,price,atr); support=level_pack.get('nearest_support'); resistance=level_pack.get('nearest_resistance')
        # If score is strong, allow one fewer confirmation so the bot does not miss early moves.
        effective_req_conf = max(2, req_conf - 1) if final_score >= 88 else req_conf
        entry_confirmed=valid and final_score>=min_score and confirmations>=effective_req_conf
        common={'symbol':symbol,'score':cap_score(final_score),'long_score':long_score,'short_score':short_score,'price':safe_round(price),'atr':safe_round(atr),'market_regime':market_context.get('market_regime','neutral'),'btc_bias':market_context.get('btc_bias','neutral'),'confirmations':confirmations,'required_confirmations':effective_req_conf,'rsi':safe_round(df_15m.iloc[-1]['rsi'],2),'macd':safe_round(df_15m.iloc[-1]['macd'],6),'macd_signal':safe_round(df_15m.iloc[-1]['macd_signal'],6),'macd_hist':safe_round(df_15m.iloc[-1]['macd_hist'],6),'adx':safe_round(df_15m.iloc[-1]['adx'],2),'vwap_status':vwap_status(df_15m),'support':safe_round(support),'resistance':safe_round(resistance),'trends':sp.get('trends',{}),'distance_ema20_atr':sp.get('distance_ema20_atr'),'volume_status':sp.get('volume_status'),'volume_ratio':sp.get('volume_ratio'),'buy_power':sp.get('buy_power'),'sell_power':sp.get('sell_power'),'power2_buy':sp.get('power2_buy'),'power2_sell':sp.get('power2_sell'),'power3_buy':sp.get('power3_buy'),'power3_sell':sp.get('power3_sell'),'snapshot':snapshot,'coin_risk':risk_state,'rotation':rotation,'reasons':reasons[:20],'signal_timeframe':'AI Classic Direct'}
        if not entry_confirmed:
            return {**common,'direction':'NO TRADE','status':'NO_TRADE','entry_confirmed':False,'entry_mode':'NO_ENTRY','entry':None,'stop_loss':None,'tp1':None,'tp2':None,'risk_reward':0,'risk_level':'UNKNOWN','freshness':'LOW','tp_meta':{},'validity':'سیگنال معتبر نیست','valid_gate':valid,'min_score':min_score}
        sl,tp1,tp2,rr,tp_meta=build_trade_levels(direction,price,atr,df_5m,df_15m,df_30m,snapshot,symbol); risk_level='LOW' if final_score>=92 and confirmations>=6 else 'MEDIUM' if final_score>=86 and confirmations>=5 else 'HIGH'; freshness='HIGH' if confirmations>=6 else 'MEDIUM' if confirmations>=5 else 'LOW'
        if update_ai_summary:
            try: update_ai_summary(total_signals=1)
            except Exception: pass
        return {**common,'direction':direction,'status':'ACTIVE','entry_confirmed':True,'entry_mode':'AI_CLASSIC_DIRECT','entry':safe_round(price),'stop_loss':sl,'tp1':tp1,'tp2':tp2,'risk_reward':rr,'risk_level':risk_level,'freshness':freshness,'tp_meta':tp_meta,'validity':'15 تا 45 دقیقه'}
    except Exception as e:
        return {'symbol':symbol,'direction':'NO TRADE','status':'NO_TRADE','entry_confirmed':False,'entry_mode':'ERROR','score':0,'long_score':0,'short_score':0,'price':None,'entry':None,'stop_loss':None,'tp1':None,'tp2':None,'atr':None,'risk_reward':0,'risk_level':'UNKNOWN','market_regime':'unknown','btc_bias':'unknown','freshness':'LOW','confirmations':0,'required_confirmations':0,'rsi':None,'macd':None,'macd_signal':None,'macd_hist':None,'adx':None,'vwap_status':None,'support':None,'resistance':None,'trends':{},'snapshot':{},'coin_risk':{},'rotation':{},'tp_meta':{},'reasons':[f'Analysis Error: {str(e)[:200]}'],'signal_timeframe':'AI Classic Direct','validity':'سیگنال معتبر نیست'}
