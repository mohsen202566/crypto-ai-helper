from __future__ import annotations
import time, threading, os
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor,as_completed
from analysis_engine import MarketAnalyzer
from utils import canonical_base

@dataclass
class Pump:
    symbol:str; price:float; start:float; peak:float; growth:float; dd:float; strength:int; fatigue:int; stage:str; one:float; four:float; chart:str=''

class PumpDetector:
    def __init__(self,client):
        self.client=client; self.analyzer=MarketAnalyzer(client); self.lock=threading.RLock(); self.top=[]; self.watch={}; self.notice={}; self.last_scan=0
    def refresh(self):
        rows=self.analyzer.top_pumps(40)
        with self.lock:
            self.top=rows; self.last_scan=time.time(); valid={s for s,_,_ in rows}
            self.watch={s:r for s,r in self.watch.items() if s in valid}
        return rows
    @staticmethod
    def change(a,b):return (b/a-1)*100 if a>0 else 0
    def analyze(self,symbol,chart=False):
        k1=self.client.get_klines(symbol,'1h',32);k4=self.client.get_klines(symbol,'4h',12);k5=self.client.get_klines(symbol,'5m',20)
        if min(len(k1),len(k4),len(k5))<8:return None
        price=k5[-1]['close']; recent=k1[-13:]
        peak_i=max(range(len(recent)),key=lambda i:recent[i]['high']);peak=max(price,recent[peak_i]['high'])
        start=min(x['low'] for x in recent[:peak_i+1]); growth=self.change(start,peak); dd=max(0,(peak-price)/peak*100)
        one=self.change(k1[-1]['open'],price);four=self.change(k4[-1]['open'],price)
        speeds=[self.change(x['open'],x['close']) for x in k5[-7:]]
        old=max([v for v in speeds[:-2] if v>0] or [0]);new=max(0,(speeds[-1]+speeds[-2])/2)
        fatigue=round(max(0,min(100,(1-new/max(old,.01))*100))) if old>=1 else 0
        strength=round(max(1,min(100,max(0,one)*5+growth*1.2+sum(max(0,v) for v in speeds[-4:])*3+max(0,four)*.3-dd*6)))
        retained=(price-start)/max(peak-start,1e-12)
        active=dd<=max(3,min(10,growth*.22)) and retained>=.65
        if growth<3 or not active:stage='تخلیه‌شده'
        elif growth>=5 and (one>=2 or sum(max(0,v) for v in speeds[-4:])>=2 or four>=5):stage='پامپ فعال'
        else:stage='واچ اولیه'
        if stage=='پامپ فعال' and fatigue>=55:stage='خستگی پامپ'
        r=Pump(symbol,price,start,peak,round(growth,2),round(dd,2),strength,fatigue,stage,round(one,2),round(four,2))
        if chart:r.chart=self.chart(symbol,k1,r)
        return r
    def chart(self,symbol,klines,r):
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
        os.makedirs('/tmp/pump_watch_charts',exist_ok=True)
        path=f'/tmp/pump_watch_charts/{symbol}.png';fig,ax=plt.subplots(figsize=(9,4.6))
        for i,k in enumerate(klines[-30:]):
            o,h,l,c=(k[z] for z in ('open','high','low','close'));color='green' if c>=o else 'red'
            ax.vlines(i,l,h,color=color);ax.add_patch(Rectangle((i-.3,min(o,c)),.6,max(abs(c-o),c*.0001),color=color))
        ax.axhline(r.peak,ls='--',color='orange',label='Peak');ax.axhline(r.start,ls=':',color='blue',label='Start')
        ax.set_title(f'{symbol} 1H live | +{r.growth:.1f}% | {r.strength}/100');ax.legend();fig.tight_layout();fig.savefig(path,dpi=120);plt.close(fig)
        return path
    def message(self,r):
        return (f'🚀 {canonical_base(r.symbol)} — {r.stage}\nشروع موج: {r.start:.9g}\nقیمت فعلی: {r.price:.9g}\n'
                f'رشد موج: +{r.growth:.2f}% | شدت: {r.strength}/100\n'
                f'خستگی سرعت 5m: {r.fatigue}%\n1H: {r.one:+.2f}% | 4H: {r.four:+.2f}%\n'
                f'فاصله از سقف: {r.dd:.2f}%\n⚠️ هشدار دیده‌بانی؛ دستور SHORT نیست.')
    def due(self,r):
        if r.stage not in ('پامپ فعال','خستگی پامپ'):return False
        with self.lock:
            prev=self.notice.get(r.symbol);now=time.time()
            if prev and now-prev[0]<900 and r.peak<prev[1]*1.03 and prev[2]==r.stage:return False
            self.notice[r.symbol]=(now,r.peak,r.stage)
        return True
    def update(self,symbol,r):
        with self.lock:
            if r and r.stage!='تخلیه‌شده':self.watch[symbol]=r
            else:self.watch.pop(symbol,None)
    def watched(self):
        with self.lock:return list(self.watch.values())
