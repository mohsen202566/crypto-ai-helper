from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
import indicators as ind

@dataclass
class MarketAnalysis:
    symbol_id: str
    regime: str
    primary_direction: str
    direction_score: float
    opposite_score: float
    strength_score: float
    fragility_score: float
    freshness_score: float
    exhaustion_risk: float
    confidence: float
    context_15m: str
    movement_stage: str
    value_distance_atr: float
    atr_pct: float
    features: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    hard_veto: bool = False

class MarketEngine:
    def analyze(self, symbol_id, c5, c15):
        if len(c5) < 80 or len(c15) < 55:
            return MarketAnalysis(symbol_id,'UNKNOWN','NEUTRAL',0,0,0,100,0,100,0,'UNKNOWN','UNKNOWN',0,0,hard_veto=True,reasons=['داده کافی نیست'])
        v = ind.closes(c5); v15 = ind.closes(c15)
        e9,e21,e50 = ind.ema(v,9), ind.ema(v,21), ind.ema(v,50)
        e20h,e50h = ind.ema(v15,20), ind.ema(v15,50)
        atr_series = ind.atr(c5,14); atrv = atr_series[-1]; px = v[-1]
        if px <= 0 or atrv <= 0:
            return MarketAnalysis(symbol_id,'UNKNOWN','NEUTRAL',0,0,0,100,0,100,0,'UNKNOWN','UNKNOWN',0,0,hard_veto=True,reasons=['قیمت یا ATR نامعتبر'])
        atr_pct = atrv/px*100
        hs,ls = ind.swing_points(c5[-60:],2)
        up_struct = len(hs)>=2 and len(ls)>=2 and hs[-1][1]>hs[-2][1] and ls[-1][1]>ls[-2][1]
        dn_struct = len(hs)>=2 and len(ls)>=2 and hs[-1][1]<hs[-2][1] and ls[-1][1]<ls[-2][1]
        up_ema = e9[-1]>e21[-1]>e50[-1] and e21[-1]>e21[-4]
        dn_ema = e9[-1]<e21[-1]<e50[-1] and e21[-1]<e21[-4]
        context = 'BULLISH' if e20h[-1]>e50h[-1] and e20h[-1]>e20h[-3] else 'BEARISH' if e20h[-1]<e50h[-1] and e20h[-1]<e20h[-3] else 'NEUTRAL'
        eff = ind.efficiency(v,12); overlap = ind.overlap_ratio(c5,12)
        rs = ind.rsi(v,14); _,_,hist = ind.macd(v)
        plus_di,minus_di,adx = ind.dmi_adx(c5,14)
        cf = [ind.candle_features(x) for x in c5[-6:]]
        pressure = sum(x['direction']*x['body_ratio'] for x in cf)/len(cf)
        vr = ind.volume_ratio(c5)
        vwap = ind.session_vwap(c5)
        long = 30*(1 if up_struct else .45 if up_ema else .1)+18*(1 if up_ema else .2)+17*(1 if context=='BULLISH' else .45 if context=='NEUTRAL' else 0)+14*max(0,min(1,(pressure+1)/2))+10*max(0,min(1,(rs[-1]-40)/25))+7*(1 if px>=e21[-1] else .2)+4*min(1,vr/1.5)
        short = 30*(1 if dn_struct else .45 if dn_ema else .1)+18*(1 if dn_ema else .2)+17*(1 if context=='BEARISH' else .45 if context=='NEUTRAL' else 0)+14*max(0,min(1,(1-pressure)/2))+10*max(0,min(1,(60-rs[-1])/25))+7*(1 if px<=e21[-1] else .2)+4*min(1,vr/1.5)
        side = 'LONG' if long-short>=12 else 'SHORT' if short-long>=12 else 'NEUTRAL'
        d,opp = max(long,short),min(long,short)
        di_dom = max(0.0,(plus_di[-1]-minus_di[-1]) if side=='LONG' else (minus_di[-1]-plus_di[-1]))
        dmi_score = min(100.0, adx[-1]*2.2 + di_dom)
        progress = min(100.0,eff*125)
        momentum = min(100.0,50 + abs(hist[-1])*10000/max(px,1e-9))
        strength = .25*progress+.25*d+.25*dmi_score+.15*momentum+.10*min(100,vr*55)
        value_ref = (e21[-1]+vwap)/2 if vwap else e21[-1]
        value_dist = abs(px-value_ref)/atrv
        displacement = abs(px-v[-8])/atrv
        decel = abs(hist[-1])<abs(hist[-3]) and abs(hist[-2])<abs(hist[-4])
        exhaustion = min(100,14*value_dist+7*displacement+(18 if decel else 0))
        fresh = max(0,100-exhaustion)
        frag = min(100,(1-eff)*50+overlap*30+(20 if abs(pressure)<.12 else 0))
        if side=='NEUTRAL' or eff<.13 or overlap>.80:
            regime='CHAOTIC' if overlap>.72 or eff<.10 else 'RANGE_NOISY'
        elif strength>=72:
            regime='STRONG_TREND_UP' if side=='LONG' else 'STRONG_TREND_DOWN'
        else:
            regime='WEAK_TREND_UP' if side=='LONG' else 'WEAK_TREND_DOWN'
        stage='EARLY' if displacement<1.2 else 'DEVELOPING' if displacement<2.2 else 'MATURE' if displacement<3.2 else 'LATE' if displacement<4.2 else 'EXHAUSTED'
        veto = (regime=='CHAOTIC' and overlap>0.86 and eff<0.07) or exhaustion>=94 or atr_pct<=0
        contradictions=[]
        if exhaustion>60: contradictions.append('حرکت فرسوده')
        if context!='NEUTRAL' and ((side=='LONG' and context=='BEARISH') or (side=='SHORT' and context=='BULLISH')): contradictions.append('تضاد تایم 15M')
        confidence=max(0,min(100,d-frag*.2-8*len(contradictions)))
        return MarketAnalysis(symbol_id,regime,side,round(d,2),round(opp,2),round(strength,2),round(frag,2),round(fresh,2),round(exhaustion,2),round(confidence,2),context,stage,round(value_dist,3),round(atr_pct,4),features={'ema9':e9[-1],'ema21':e21[-1],'ema50':e50[-1],'vwap':vwap,'atr':atrv,'rsi':rs[-1],'adx':adx[-1],'plus_di':plus_di[-1],'minus_di':minus_di[-1],'efficiency':eff,'overlap':overlap,'pressure':pressure,'volume_ratio':vr,'last_price':px,'recent_swing_high':hs[-1][1] if hs else None,'recent_swing_low':ls[-1][1] if ls else None},reasons=[f'ساختار/EMA جهت {side}',f'زمینه 15M: {context}',f'ADX {adx[-1]:.1f}',f'کارایی حرکت {eff:.2f}'],contradictions=contradictions,hard_veto=veto)
