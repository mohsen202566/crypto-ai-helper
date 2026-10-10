from __future__ import annotations
import time,threading
from concurrent.futures import ThreadPoolExecutor,as_completed
from pump_detector import PumpDetector
from utils import canonical_base,logger

class BotEngine:
    def __init__(self,storage,toobit):
        self.storage=storage;self.toobit=toobit;self.detector=PumpDetector(toobit);self.telegram=None
        self.pool=ThreadPoolExecutor(max_workers=4);self._busy=set();self._lock=threading.RLock();self._last_deep={}
    def bind_telegram(self,tg):self.telegram=tg
    def startup(self):
        if self.storage.get_setting('auto_analysis_enabled',None) is None:self.storage.set_setting('auto_analysis_enabled',True)
    def scan_text(self):
        rows=self.detector.refresh();self.scan_deep(rows,wait=True)
        matches=sorted(self.detector.watched(),key=lambda r:r.strength,reverse=True)
        lines=['🔎 اسکن ۴۰ ارز برتر Toobit USDT-M','پامپ‌های فعال و واچ اولیه:']
        for r in matches[:20]:lines.append(f'{canonical_base(r.symbol)} | +{r.growth:.1f}% | شدت {r.strength}/100 | {r.stage}')
        if not matches:lines.append('فعلاً مورد واجد شرایط پیدا نشد.')
        return '\n'.join(lines)
    def watch_text(self):
        rows=self.detector.watched()
        return '\n'.join(['👀 واچ‌لیست']+[f'{canonical_base(r.symbol)} +{r.growth:.1f}% | {r.stage} | {r.strength}/100' for r in rows]) if rows else 'واچ‌لیست فعلاً خالی است.'
    def analyze_now(self,name):
        symbol=self.detector.analyzer.resolve(name);r=self.detector.analyze(symbol,chart=True)
        if not r:raise RuntimeError('کندل کافی در دسترس نیست')
        return self.detector.message(r),r.chart
    def _analyze_symbol(self,symbol,alert=True):
        try:
            r=self.detector.analyze(symbol)
            self.detector.update(symbol,r)
            if r and alert and self.telegram and self.storage.get_setting('auto_analysis_enabled',True) and self.detector.due(r):
                # Send text immediately; chart generation and photo follow separately.
                self.telegram.send_message(self.detector.message(r))
                try:
                    r.chart=self.detector.chart(symbol,self.toobit.get_klines(symbol,'1h',32),r)
                    self.telegram.send_photo(r.chart,f'📊 {canonical_base(symbol)} | {r.stage} | {r.strength}/100')
                except Exception as e:logger.warning('PUMP_CHART %s %s',symbol,e)
        except Exception as e:logger.warning('PUMP_ANALYZE %s %s',symbol,e)
        finally:
            with self._lock:self._busy.discard(symbol)
    def scan_deep(self,rows=None,wait=False):
        if rows is None:rows=self.detector.refresh()
        futures=[]
        for symbol,_,_ in rows:
            with self._lock:
                if symbol in self._busy:continue
                self._busy.add(symbol)
            futures.append(self.pool.submit(self._analyze_symbol,symbol))
        if wait:
            for f in as_completed(futures):f.result()
    def watch_tick(self):
        now=time.time()
        for r in self.detector.watched():
            if now-self._last_deep.get(r.symbol,0)<15:continue
            self._last_deep[r.symbol]=now
            with self._lock:
                if r.symbol in self._busy:continue
                self._busy.add(r.symbol)
            self.pool.submit(self._analyze_symbol,r.symbol)
    def close(self):self.pool.shutdown(wait=False,cancel_futures=True)
