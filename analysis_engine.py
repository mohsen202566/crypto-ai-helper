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

class MarketAnalyzer:
    def __init__(self, client): self.client=client; self._contracts_cache=set(); self._contracts_at=0.0

    @staticmethod
    def _is_crypto_contract(info):
        """Reject Toobit TradFi/stock/index/forex contracts before Top ranking.

        Toobit may expose these products through the same USDT-M contract API, so
        `USDT + TRADING` alone is not enough to prove that an instrument is crypto.
        We intentionally use several metadata fields because exchangeInfo field names
        can differ between product generations. Unknown/empty metadata remains allowed
        so newly listed crypto coins are not accidentally lost.
        """
        deny = ('STOCK','EQUITY','SHARE','INDEX','INDICES','FOREX','FX','TRADFI',
                'COMMODITY','ETF','CFD','PRECIOUS','METAL')
        fields = ('type','contractType','assetType','productType','category','sector',
                  'marketType','underlyingType','tag','tags','label','labels')
        vals=[]
        for k in fields:
            v=info.get(k)
            if isinstance(v,(list,tuple,set)): vals.extend(str(x).upper() for x in v)
            elif v is not None: vals.append(str(v).upper())
        blob=' '.join(vals)
        return not any(word in blob for word in deny)

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
            # IMPORTANT: rank Top 24h only AFTER removing Toobit non-crypto products.
            # This prevents Stock / Indices / TradFi / Forex contracts from occupying
            # Top slots that belong to actual crypto futures.
            self._contracts_cache={canonical_symbol(v.get('canonical') or k) for k,v in contracts.items() if self._is_crypto_contract(v)}
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
        # 3 local-top failure. The historical pump peak is REFERENCE ONLY.
        # A reversal may start after price has already slipped below the exact peak;
        # therefore neither touching the peak nor making a fresh high is required.
        off=(peak-price)/peak*100
        local_anchor=max(x['high'] for x in k1[-30:])
        anchor_off=(local_anchor-price)/max(local_anchor,1e-12)*100
        last_high=max(x['high'] for x in k1[-8:]); prior_high=max(x['high'] for x in k1[-20:-8])
        failed=max(0.0,(prior_high-last_high)/max(prior_high,1e-12)*100)
        # No free points for merely being X% below the old peak. Score comes from
        # actual short-term failure; distance is only reported as context.
        s3=min(100,failed*35)
        scores.append(('شکست سقف محلی',s3,f'فاصله از Peak مرجع {off:.2f}%؛ فاصله از سقف 30m {anchor_off:.2f}%؛ ضعف سقف کوتاه‌مدت {failed:.2f}%'))
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
        # Multi-timeframe structural support/resistance. These levels also drive SL/TP selection.
        levels={'5m':self._sr_levels(k5[-120:],price),'15m':self._sr_levels(k15[-100:],price),'1h':self._sr_levels(k60[-72:],price)}
        local_high=max(x['high'] for x in k1[-12:]); entry_low=min(x['low'] for x in k1[-3:]); entry_high=max(x['high'] for x in k1[-3:])
        resist=[]
        for tf in ('5m','15m','1h'): resist += levels[tf]['resistance']
        invalid=min(resist, key=lambda x:abs(x-price)) if resist else max(peak,local_high)
        invalid=max(invalid,entry_high)
        # TP1/2/3 are actual supports across 5m -> 15m -> 1h, de-duplicated and below entry.
        candidates=[]
        for tf in ('5m','15m','1h'):
            for v in levels[tf]['support']:
                if v < entry_low and all(abs(v-x)/price>0.004 for x in candidates): candidates.append(v)
        candidates=sorted(candidates, reverse=True)
        tps=candidates[:3]
        # If structure exposes fewer than 3 meaningful supports, do not invent tiny fixed-percent TPs; use broader swing lows.
        if len(tps)<3:
            swings=sorted({x['low'] for x in k60[-72:]+k15[-100:] if x['low']<entry_low}, reverse=True)
            for v in swings:
                if all(abs(v-x)/price>0.008 for x in tps): tps.append(v)
                if len(tps)==3: break
        while len(tps)<3: tps.append(min(tps[-1] if tps else entry_low, pump_start))
        path=self._chart(symbol,k5,peak,pump_start,(entry_low,entry_high),invalid,tps,score,verdict,levels)
        return AnalysisResult(symbol,score,verdict,price,peak,pump_start,entry_low,entry_high,invalid,tps,reasons,against,scores,path,levels)
    def _chart(self,symbol,ks,peak,start,entry,invalid,tps,score,verdict,levels):
        data=ks[-90:]; fig,ax=plt.subplots(figsize=(12,7));
        for i,c in enumerate(data):
            up=c['close']>=c['open']; col='green' if up else 'red'; ax.vlines(i,c['low'],c['high'],color=col,linewidth=.8); lo=min(c['open'],c['close']); h=max(abs(c['close']-c['open']),1e-10); ax.add_patch(Rectangle((i-.32,lo),.64,h,facecolor=col,edgecolor=col,alpha=.75))
        ax.axhline(peak,linestyle='--',linewidth=1,label=f'Reference peak {peak:.6g}'); ax.axhspan(entry[0],entry[1],alpha=.12,label='Entry zone'); ax.axhline(invalid,linestyle=':',linewidth=1,label='Invalidation')
        for i,tp in enumerate(tps,1): ax.axhline(tp,linestyle='--',linewidth=.8,label=f'TP{i} {tp:.6g}')
        # Show nearest S/R from each timeframe on the 5m chart.
        for tf in ('5m','15m','1h'):
            for kind,label in (('support','S'),('resistance','R')):
                vals=levels.get(tf,{}).get(kind,[])[:2]
                for j,v in enumerate(vals,1): ax.axhline(v,linestyle=':',linewidth=.65,alpha=.55,label=f'{tf} {label}{j} {v:.6g}')
        ax.set_title(f'{canonical_base(symbol)} | 5m | Reversal score {score}/100 | {verdict}'); ax.grid(alpha=.18); ax.legend(loc='best',fontsize=8); fig.tight_layout()
        os.makedirs('/tmp/staged_analysis',exist_ok=True); path=f'/tmp/staged_analysis/{canonical_base(symbol)}_{int(time.time())}.png'; fig.savefig(path,dpi=150); plt.close(fig); return path
