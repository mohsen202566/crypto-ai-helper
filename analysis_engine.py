from __future__ import annotations
import math, os, time
from dataclasses import dataclass
from typing import Any
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import config
from utils import canonical_base, canonical_symbol, safe_float

@dataclass
class AnalysisResult:
    symbol: str; score: int; verdict: str; price: float; peak: float; pump_start: float
    entry_low: float; entry_high: float; invalidation: float; tps: list[float]
    reasons: list[str]; against: list[str]; layers: list[tuple[str,float,str]]; chart_path: str

class MarketAnalyzer:
    def __init__(self, client): self.client=client
    @staticmethod
    def _change(row):
        for k in ('pcp','priceChangePercent','changeRate','rose'):
            if k in row:
                v=safe_float(row.get(k)); return v*100 if abs(v)<=1.5 else v
        o=safe_float(row.get('o') or row.get('openPrice')); c=safe_float(row.get('c') or row.get('lastPrice'))
        return ((c/o)-1)*100 if o and c else 0
    def top_pumps(self,n=10):
        rows=[]
        for r in self.client.get_24h_tickers():
            s=str(r.get('s') or r.get('symbol') or '').upper()
            if not s or canonical_base(s) in set(config.SYMBOL_BLACKLIST): continue
            ch=self._change(r); p=safe_float(r.get('c') or r.get('lastPrice') or r.get('close'))
            if p>0: rows.append((s,ch,p))
        return sorted(rows,key=lambda x:x[1],reverse=True)[:n]
    def resolve(self,name):
        raw=name.strip().upper().replace('/','').replace('-','').replace('_','')
        base=raw.removesuffix('USDT').removesuffix('PERP')
        tops=self.client.get_24h_tickers()
        candidates=[]
        for r in tops:
            s=str(r.get('s') or r.get('symbol') or '').upper()
            if canonical_base(s)==base: candidates.append(s)
        return candidates[0] if candidates else canonical_symbol(base+'USDT')
    @staticmethod
    def _ret(a,b): return (b/a-1)*100 if a else 0
    def analyze(self,name):
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
        # 3 distance/rejection from live peak (continuous, not threshold gate)
        off=(peak-price)/peak*100; scores.append(('رفتار نزدیک پیک',max(0,min(100,35+off*12)),f'فاصله فعلی از پیک {off:.2f}%'))
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
        weights=[1.25,1.35,1.0,1.0,1.2,1.15,1.3,.8,1.1,1.25]
        score=round(sum(s*w for (_,s,_),w in zip(scores,weights))/sum(weights))
        strong=sum(1 for _,s,_ in scores if s>=65); critical=sum(1 for i,(_,s,_) in enumerate(scores) if i in (1,4,5,6,8,9) and s>=70)
        if score>=72 and strong>=5 and critical>=2: verdict='ورود شورت قابل بررسی'
        elif score>=58 and strong>=4: verdict='هشدار زودهنگام — هنوز ورود پرریسک'
        else: verdict='فعلاً ورود نکن'
        reasons=[f'{n}: {d}' for n,s,d in scores if s>=65][:6]; against=[f'{n}: {d}' for n,s,d in scores if s<45][:4]
        # structural levels, not fixed-percent triggers
        local_high=max(x['high'] for x in k1[-12:]); entry_low=min(x['low'] for x in k1[-3:]); entry_high=max(x['high'] for x in k1[-3:]); invalid=max(peak,local_high)
        supports=sorted({x['low'] for x in k5[-60:] if x['low']<price}, reverse=True)
        tps=[]
        for v in supports:
            if not tps or abs(v-tps[-1])/price>.012: tps.append(v)
            if len(tps)==3: break
        while len(tps)<3: tps.append(price*(1-[.04,.08,.13][len(tps)]))
        path=self._chart(symbol,k5,peak,pump_start,(entry_low,entry_high),invalid,tps,score,verdict)
        return AnalysisResult(symbol,score,verdict,price,peak,pump_start,entry_low,entry_high,invalid,tps,reasons,against,scores,path)
    def _chart(self,symbol,ks,peak,start,entry,invalid,tps,score,verdict):
        data=ks[-90:]; fig,ax=plt.subplots(figsize=(12,7));
        for i,c in enumerate(data):
            up=c['close']>=c['open']; col='green' if up else 'red'; ax.vlines(i,c['low'],c['high'],color=col,linewidth=.8); lo=min(c['open'],c['close']); h=max(abs(c['close']-c['open']),1e-10); ax.add_patch(Rectangle((i-.32,lo),.64,h,facecolor=col,edgecolor=col,alpha=.75))
        ax.axhline(peak,linestyle='--',linewidth=1,label=f'Peak {peak:.6g}'); ax.axhspan(entry[0],entry[1],alpha=.12,label='Entry zone'); ax.axhline(invalid,linestyle=':',linewidth=1,label='Invalidation')
        for i,tp in enumerate(tps,1): ax.axhline(tp,linestyle='--',linewidth=.8,label=f'TP{i} {tp:.6g}')
        ax.set_title(f'{canonical_base(symbol)} | 5m | Reversal score {score}/100 | {verdict}'); ax.grid(alpha=.18); ax.legend(loc='best',fontsize=8); fig.tight_layout()
        os.makedirs('/tmp/staged_analysis',exist_ok=True); path=f'/tmp/staged_analysis/{canonical_base(symbol)}_{int(time.time())}.png'; fig.savefig(path,dpi=150); plt.close(fig); return path
