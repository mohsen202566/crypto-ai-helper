from __future__ import annotations
import math, os, time
from dataclasses import dataclass
from typing import Any
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import config
from utils import canonical_base, canonical_symbol, toobit_contract_symbol, safe_float

@dataclass
class AnalysisResult:
    symbol: str; score: int; verdict: str; price: float; peak: float; pump_start: float
    entry_low: float; entry_high: float; invalidation: float; tps: list[float]
    reasons: list[str]; against: list[str]; layers: list[tuple[str,float,str]]; chart_path: str
    levels: dict[str, dict[str, list[float]]]
    peak_score: int; dump_score: int; entry_score: int; stage: str

class MarketAnalyzer:
    def __init__(self, client): self.client=client; self._contracts_cache=set(); self._contracts_at=0.0
    @staticmethod
    def _change(row):
        for k in ('pcp','priceChangePercent','changeRate','rose'):
            if k in row:
                v=safe_float(row.get(k)); return v*100 if abs(v)<=1.5 else v
        o=safe_float(row.get('o') or row.get('openPrice')); c=safe_float(row.get('c') or row.get('lastPrice'))
        return ((c/o)-1)*100 if o and c else 0
    def _active_usdtm_contracts(self):
        # Source of truth for scanner: Toobit exchangeInfo contracts only.
        # get_contracts() already excludes non-TRADING, inverse and non-USDT contracts.
        now=time.time()
        if not self._contracts_cache or now-self._contracts_at>300:
            contracts=self.client.get_contracts()
            self._contracts_cache={canonical_symbol(v.get('canonical') or k) for k,v in contracts.items()}
            self._contracts_at=now
        return self._contracts_cache

    def top_pumps(self,n=10):
        allowed=self._active_usdtm_contracts()
        blacklist=set(config.SYMBOL_BLACKLIST)
        rows=[]; seen=set()
        for r in self.client.get_24h_tickers():
            raw=str(r.get('s') or r.get('symbol') or r.get('symbolId') or '').upper()
            if not raw: continue
            s=canonical_symbol(raw)
            # STRICT: only active Toobit USDT-M perpetual contracts from exchangeInfo.
            if s not in allowed or canonical_base(s) in blacklist or s in seen: continue
            ch=self._change(r); p=safe_float(r.get('c') or r.get('lastPrice') or r.get('close') or r.get('p') or r.get('price'))
            if p>0:
                rows.append((s,ch,p)); seen.add(s)
        return sorted(rows,key=lambda x:x[1],reverse=True)[:n]

    def resolve(self,name):
        raw=name.strip().upper().replace('/','').replace('-','').replace('_','')
        base=raw.removesuffix('USDT').removesuffix('PERP').removesuffix('SWAP')
        target=canonical_symbol(base)
        if target not in self._active_usdtm_contracts():
            raise RuntimeError(f'{base} قرارداد فعال Toobit USDT-M Futures نیست')
        return target
    @staticmethod
    def _ret(a,b): return (b/a-1)*100 if a else 0

    @staticmethod
    def _sr_levels(ks, price, max_each=3):
        # Pivot-based S/R with clustering. Recent touches and repeated reactions get priority.
        piv=[]
        n=len(ks)
        for i in range(2,n-2):
            h=ks[i]['high']; l=ks[i]['low']
            if h>=max(ks[j]['high'] for j in range(i-2,i+3)): piv.append((h,'R',i))
            if l<=min(ks[j]['low'] for j in range(i-2,i+3)): piv.append((l,'S',i))
        tol=max(price*0.0025, 1e-12)
        clusters=[]
        for val,typ,idx in piv:
            found=None
            for c in clusters:
                if abs(val-c['v']) <= tol:
                    found=c; break
            if found:
                w=1.0 + idx/max(1,n)
                found['v']=(found['v']*found['w']+val*w)/(found['w']+w); found['w']+=w; found['touch']+=1; found['last']=max(found['last'],idx)
            else:
                clusters.append({'v':val,'w':1.0+idx/max(1,n),'touch':1,'last':idx})
        def rank(side):
            arr=[c for c in clusters if (c['v']<price if side=='S' else c['v']>price)]
            arr.sort(key=lambda c:(-(c['touch']*2+c['last']/max(1,n)), abs(c['v']-price)))
            chosen=[]
            # Prefer useful nearby levels while retaining reaction strength.
            for c in sorted(arr[:10], key=lambda c:abs(c['v']-price)):
                if not chosen or all(abs(c['v']-x)/price>0.003 for x in chosen): chosen.append(c['v'])
                if len(chosen)>=max_each: break
            return sorted(chosen, reverse=(side=='S'))
        return {'support':rank('S'), 'resistance':rank('R')}

    @staticmethod
    def _nearest(levels, side, price):
        vals=levels.get(side,[])
        return vals[0] if vals else None
    @staticmethod
    def _clamp(v): return max(0.0,min(100.0,float(v)))

    @staticmethod
    def _merge_levels(levels, price, tol_pct=0.006):
        vals=sorted([v for v in levels if v>0])
        groups=[]
        for v in vals:
            if groups and abs(v-sum(groups[-1])/len(groups[-1]))/max(price,1e-12)<=tol_pct:
                groups[-1].append(v)
            else: groups.append([v])
        return [sum(g)/len(g) for g in groups]

    def _classify_stage(self, price, peak, pump_pct, peak_score, dump_score, k1):
        off=max(0.0,(peak-price)/max(peak,1e-12)*100)
        r3=self._ret(k1[-4]['close'],k1[-1]['close'])
        r10=self._ret(k1[-11]['close'],k1[-1]['close'])
        # Stage is descriptive, not a mandatory entry rule. No bearish candle confirmation is required.
        if off>=12 or (off>=8 and r10<=-3): return 'DUMP ACTIVE / LATE'
        if off>=5 and dump_score>=58: return 'DUMP STARTING'
        if peak_score>=62 and dump_score>=62: return 'PRE-DUMP'
        # Extreme pumps get their own watch state: strong green momentum does NOT automatically veto exhaustion.
        # This is an early-warning state only; it does not itself authorize a short.
        if pump_pct>=30 and off<7 and peak_score>=52: return 'EXTREME PUMP / REVERSAL WATCH'
        if pump_pct>=15 and (off<5 or r3>0): return 'PEAK FORMING' if peak_score>=48 else 'PUMPING'
        return 'WATCH'

    def analyze(self,name,make_chart=True):
        symbol=self.resolve(name)
        k1=self.client.get_klines(symbol,'1m',240); k5=self.client.get_klines(symbol,'5m',180); k15=self.client.get_klines(symbol,'15m',120); k60=self.client.get_klines(symbol,'1h',96)
        if min(len(k1),len(k5),len(k15),len(k60))<20: raise RuntimeError('داده کندلی کافی نیست')
        price=k1[-1]['close']; peak=max(x['high'] for x in k60[-36:]); peak_i=max(range(len(k60[-36:])),key=lambda i:k60[-36:][i]['high'])
        seg=k60[-36:]; pre=seg[:max(1,peak_i+1)]; start_i=min(range(len(pre)),key=lambda i:pre[i]['low']); pump_start=pre[start_i]['low']; pump_pct=self._ret(pump_start,peak)
        scores=[]
        # 1 pump extremity
        scores.append(('شدت پامپ 1H', min(100,max(0,(pump_pct-12)*1.7)), f'حرکت از {pump_start:.6g} تا {peak:.6g} = {pump_pct:+.1f}%'))
        # 2 acceleration exhaustion: recent 1m slope vs prior
        r_recent=self._ret(k1[-11]['close'],k1[-1]['close']); r_prior=self._ret(k1[-31]['close'],k1[-11]['close'])
        s2=max(0,min(100,50+(r_prior-r_recent)*10)); scores.append(('افت شتاب',s2,f'مومنتوم 10m={r_recent:+.2f}% در برابر 20m قبل={r_prior:+.2f}%'))
        # 3 peak behavior: distance is informational only and NEVER earns reversal points by itself
        off=(peak-price)/peak*100
        last_high=max(x['high'] for x in k1[-8:]); prior_high=max(x['high'] for x in k1[-20:-8])
        failed=max(0.0,(prior_high-last_high)/max(prior_high,1e-12)*100)
        s3=min(100,35+failed*25)
        scores.append(('رفتار نزدیک پیک',s3,f'فاصله از پیک {off:.2f}% (فقط اطلاعاتی)؛ ضعف سقف کوتاه‌مدت {failed:.2f}%'))
        # 4 upper wick pressure last 1m/5m
        def wick_ratio(c):
            rng=max(c['high']-c['low'],1e-12); return (c['high']-max(c['open'],c['close']))/rng
        wr=sum(wick_ratio(x) for x in k1[-5:])/5; scores.append(('فشار عرضه/Wick',min(100,wr*180),f'میانگین upper-wick پنج دقیقه {wr*100:.0f}%'))
        # 5 volume exhaustion / sell expansion
        vols=[x['volume'] for x in k1]; base=sum(vols[-31:-6])/25 or 1; recent=sum(vols[-5:])/5
        redvol=sum(x['volume'] for x in k1[-8:] if x['close']<x['open']); greenvol=sum(x['volume'] for x in k1[-8:] if x['close']>=x['open'])
        vr=recent/base; sellratio=redvol/(redvol+greenvol) if redvol+greenvol else .5
        scores.append(('حجم و فشار فروش',min(100,25+vr*20+sellratio*45),f'حجم اخیر {vr:.1f}× نرمال، سهم کندل‌های نزولی {sellratio*100:.0f}%'))
        # 6 high progress efficiency
        highs=[x['high'] for x in k1[-20:]]; progress=self._ret(highs[0],max(highs)); volratio=(sum(vols[-10:])/10)/(sum(vols[-30:-10])/20 or 1)
        scores.append(('کارایی خریدار',max(0,min(100,60+volratio*15-progress*12)),f'با {volratio:.1f}× حجم، پیشروی سقف فقط {progress:+.2f}%'))
        # 7 micro structure without requiring bearish candle close
        hi1=max(x['high'] for x in k1[-10:-5]); hi2=max(x['high'] for x in k1[-5:]); lo1=min(x['low'] for x in k1[-10:-5]); lo2=min(x['low'] for x in k1[-5:])
        struct=(40 if hi2<=hi1 else 10)+(35 if lo2<lo1 else 5); scores.append(('ساختار 1m',struct,f'High جدید {"ضعیف/ناموفق" if hi2<=hi1 else "هنوز فعال"}؛ Low {"پایین‌تر" if lo2<lo1 else "حفظ شده"}'))
        # 8 multi timeframe stretch
        def stretch(ks,n):
            closes=[x['close'] for x in ks[-n:]]; mean=sum(closes)/len(closes); return (closes[-1]/mean-1)*100
        st5=stretch(k5,20); st15=stretch(k15,20); scores.append(('کشیدگی چندتایم‌فریم',min(100,max(0,45+st5*4+st15*2)),f'فاصله از میانگین: 5m {st5:+.1f}% | 15m {st15:+.1f}%'))
        # 9 orderbook imbalance
        try:
            d=self.client.get_depth(symbol,20); bids=d.get('bids') or d.get('b') or []; asks=d.get('asks') or d.get('a') or []
            bq=sum(safe_float(x[1]) for x in bids[:10] if isinstance(x,(list,tuple)) and len(x)>1); aq=sum(safe_float(x[1]) for x in asks[:10] if isinstance(x,(list,tuple)) and len(x)>1); ar=aq/(aq+bq) if aq+bq else .5
            s9=min(100,max(0,ar*100)); detail=f'سهم Ask در 10 لایه {ar*100:.0f}%'
        except Exception as e: s9=50; detail='Depth در دسترس نبود؛ خنثی'
        scores.append(('Order Book',s9,detail))
        # 10 recent trade flow best-effort
        try:
            tr=self.client.get_recent_trades(symbol,60); sell=buy=0.0
            for x in tr:
                q=safe_float(x.get('q') or x.get('qty') or x.get('quantity') or x.get('v'))
                side=str(x.get('side') or x.get('S') or '').upper(); maker=x.get('m')
                if side=='SELL' or maker is True: sell+=q
                elif side=='BUY' or maker is False: buy+=q
            sr=sell/(sell+buy) if sell+buy else .5; s10=sr*100; detail=f'فشار فروش معاملات اخیر {sr*100:.0f}%'
        except Exception: s10=50; detail='Trades در دسترس نبود؛ خنثی'
        scores.append(('Order Flow',s10,detail))
        # Three separate scores: being near an exhausted peak != dump imminence != entry quality.
        # Peak distance itself has zero direct scoring weight.
        peak_idx=(0,1,3,5,7); dump_idx=(1,3,4,5,6,8,9)
        peak_w=(1.0,1.3,1.0,1.25,.7); dump_w=(1.35,.8,1.15,1.15,1.35,1.1,1.25)
        peak_score=round(sum(scores[i][1]*w for i,w in zip(peak_idx,peak_w))/sum(peak_w))
        dump_score=round(sum(scores[i][1]*w for i,w in zip(dump_idx,dump_w))/sum(dump_w))
        # Extreme-pump exhaustion overlay. It rewards independent evidence of saturation near a violent pump,
        # while positive momentum alone cannot suppress the warning. No bearish candle confirmation is used.
        extreme = pump_pct >= 30 and off < 7
        exhaustion_hits = sum(1 for i in (3,4,5,7,8,9) if scores[i][1] >= 62)
        exhaustion_critical = sum(1 for i in (4,5,8,9) if scores[i][1] >= 70)
        if extreme:
            pump_ext=self._clamp((pump_pct-20)*2.0)
            exhaustion_core=sum(scores[i][1] for i in (3,4,5,7,8,9))/6
            peak_score=round(self._clamp(max(peak_score, pump_ext*.35 + exhaustion_core*.65)))
            # Only boost dump imminence when several independent exhaustion layers agree.
            if exhaustion_hits>=3:
                dump_score=round(self._clamp(max(dump_score, exhaustion_core*.78 + pump_ext*.22)))
        stage=self._classify_stage(price,peak,pump_pct,peak_score,dump_score,k1)
        strong=sum(1 for i in dump_idx if scores[i][1]>=65)
        critical=sum(1 for i in (1,4,5,6,8,9) if scores[i][1]>=70)
        # Entry quality is finalized after structural SL/TP are known.
        score=dump_score
        verdict='در حال محاسبه کیفیت ورود'
        reasons=[f'{n}: {d}' for n,v,d in scores if v>=65][:6]; against=[f'{n}: {d}' for n,v,d in scores if v<45][:4]
        # Multi-timeframe structural S/R, merged into zones so duplicated levels do not masquerade as independent evidence.
        levels={'5m':self._sr_levels(k5[-150:],price),'15m':self._sr_levels(k15[-120:],price),'1h':self._sr_levels(k60[-96:],price)}
        # Always expose the live pump peak as a major resistance when it is above price.
        if peak>price:
            for tf in ('15m','1h'):
                vals=levels[tf]['resistance']+[peak]
                levels[tf]['resistance']=sorted(self._merge_levels(vals,price))[:3]
        local_high=max(x['high'] for x in k1[-12:]); entry_low=min(x['low'] for x in k1[-3:]); entry_high=max(x['high'] for x in k1[-3:])
        resist=[]
        for tf in ('5m','15m','1h'): resist += levels[tf]['resistance']
        merged_r=[v for v in self._merge_levels(resist+[peak,local_high],price) if v>entry_high]
        # Invalidation for a short must be ABOVE the active structural resistance/peak zone, never below the pump peak.
        base_invalid=max([peak,local_high,entry_high]+merged_r[:2])
        recent_ranges=[x['high']-x['low'] for x in k1[-20:]]
        buffer=max(price*0.0015,(sum(recent_ranges)/len(recent_ranges))*0.20)
        invalid=base_invalid+buffer
        risk=max(invalid-entry_high,price*0.001)
        # Structural TP candidates; require useful reward/risk, otherwise skip the too-near support.
        candidates=[]
        for tf in ('5m','15m','1h'):
            for v in levels[tf]['support']:
                if v<entry_low and all(abs(v-x)/price>0.004 for x in candidates): candidates.append(v)
        candidates=sorted(candidates,reverse=True)
        rr_candidates=[v for v in candidates if (entry_low-v)/risk>=0.75]
        tps=rr_candidates[:3]
        if len(tps)<3:
            swings=sorted({x['low'] for x in k60[-96:]+k15[-120:] if x['low']<entry_low},reverse=True)
            for v in swings:
                if (entry_low-v)/risk>=0.75 and all(abs(v-x)/price>0.008 for x in tps): tps.append(v)
                if len(tps)==3: break
        while len(tps)<3: tps.append(min(tps[-1] if tps else pump_start,pump_start))
        rr1=max(0,(entry_low-tps[0])/risk)
        stage_factor={'PRE-DUMP':95,'DUMP STARTING':82,'EXTREME PUMP / REVERSAL WATCH':68,'PEAK FORMING':58,'PUMPING':25,'DUMP ACTIVE / LATE':18,'WATCH':35}.get(stage,35)
        entry_score=round(self._clamp(dump_score*.55 + stage_factor*.30 + min(100,rr1*50)*.15))
        if stage=='DUMP ACTIVE / LATE': verdict='ریزش انجام شده/در حال اجرا — برای ورود زودهنگام دیر است'
        elif stage=='PRE-DUMP' and dump_score>=62 and strong>=4 and critical>=2 and entry_score>=65: verdict='کاندیدای ریزش زودهنگام — شورت قابل بررسی'
        elif stage=='DUMP STARTING' and entry_score>=58: verdict='ریزش در حال شروع — ورود فقط با نسبت ریسک/بازده مناسب'
        elif stage=='EXTREME PUMP / REVERSAL WATCH':
            if exhaustion_hits>=4 and exhaustion_critical>=2 and dump_score>=60:
                verdict='پامپ شدید + خستگی چندلایه — هشدار زودهنگام؛ آماده تبدیل به PRE-DUMP'
            else:
                verdict='پامپ شدید نزدیک ناحیه حساس — زیر نظر؛ هنوز مجوز شورت نیست'
        elif stage in ('PEAK FORMING','WATCH') and dump_score>=55: verdict='زیر نظر — شواهد هنوز برای ورود کافی نیست'
        else: verdict='فعلاً ورود نکن'
        path=self._chart(symbol,k5,peak,pump_start,(entry_low,entry_high),invalid,tps,dump_score,verdict,levels) if make_chart else ''
        return AnalysisResult(symbol,dump_score,verdict,price,peak,pump_start,entry_low,entry_high,invalid,tps,reasons,against,scores,path,levels,peak_score,dump_score,entry_score,stage)
    def _chart(self,symbol,ks,peak,start,entry,invalid,tps,score,verdict,levels):
        data=ks[-90:]; fig,ax=plt.subplots(figsize=(12,7));
        for i,c in enumerate(data):
            up=c['close']>=c['open']; col='green' if up else 'red'; ax.vlines(i,c['low'],c['high'],color=col,linewidth=.8); lo=min(c['open'],c['close']); h=max(abs(c['close']-c['open']),1e-10); ax.add_patch(Rectangle((i-.32,lo),.64,h,facecolor=col,edgecolor=col,alpha=.75))
        ax.axhline(peak,linestyle='--',linewidth=1,label=f'Peak {peak:.6g}'); ax.axhspan(entry[0],entry[1],alpha=.12,label='Entry zone'); ax.axhline(invalid,linestyle=':',linewidth=1,label='Invalidation')
        for i,tp in enumerate(tps,1): ax.axhline(tp,linestyle='--',linewidth=.8,label=f'TP{i} {tp:.6g}')
        # Show nearest S/R from each timeframe on the 5m chart.
        for tf in ('5m','15m','1h'):
            for kind,label in (('support','S'),('resistance','R')):
                vals=levels.get(tf,{}).get(kind,[])[:2]
                for j,v in enumerate(vals,1): ax.axhline(v,linestyle=':',linewidth=.65,alpha=.55,label=f'{tf} {label}{j} {v:.6g}')
        ax.set_title(f'{canonical_base(symbol)} | 5m | Reversal score {score}/100 | {verdict}'); ax.grid(alpha=.18); ax.legend(loc='best',fontsize=8); fig.tight_layout()
        os.makedirs('/tmp/staged_analysis',exist_ok=True); path=f'/tmp/staged_analysis/{canonical_base(symbol)}_{int(time.time())}.png'; fig.savefig(path,dpi=150); plt.close(fig); return path
