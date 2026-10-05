from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import config
from analysis_engine import MarketAnalyzer
from utils import canonical_base

class BotEngine:
    def __init__(self,storage,toobit):
        self.storage=storage; self.toobit=toobit; self.analyzer=MarketAnalyzer(toobit); self.telegram=None; self.last_auto={}
    def bind_telegram(self,tg): self.telegram=tg
    def startup(self):
        self.storage.set_setting('startup_ready',True)
        if self.storage.get_setting('auto_analysis_enabled',None) is None: self.storage.set_setting('auto_analysis_enabled',False)
    def scan_text(self):
        rows=self.analyzer.top_pumps(10)
        results=[]
        # Analyze the actual Toobit USDT-M Top 10, not merely list their names. Parallelism keeps /scan responsive.
        with ThreadPoolExecutor(max_workers=5) as ex:
            fut={ex.submit(self.analyzer.analyze,s,False):(s,ch,p) for s,ch,p in rows}
            for f in as_completed(fut):
                s,ch,p=fut[f]
                try: results.append((f.result(),ch,p,None))
                except Exception as e: results.append((None,ch,p,(s,str(e))))
        rank={'PRE-DUMP':0,'DUMP STARTING':1,'EXTREME PUMP / REVERSAL WATCH':2,'PEAK FORMING':3,'WATCH':4,'PUMPING':5,'DUMP ACTIVE / LATE':6}
        results.sort(key=lambda x:(rank.get(x[0].stage,9) if x[0] else 9, -(x[0].dump_score if x[0] else -1)))
        lines=['🚀 اسکن تحلیلی Top 10 — Toobit USDT-M Futures','']
        for r,ch,p,err in results:
            if not r:
                lines.append(f'⚪ {canonical_base(err[0])} {ch:+.2f}% — تحلیل ناموفق')
                continue
            icon='🔴' if r.stage=='PRE-DUMP' else '🟠' if r.stage=='DUMP STARTING' else '🔥' if r.stage=='EXTREME PUMP / REVERSAL WATCH' else '🟡' if r.stage in ('PEAK FORMING','WATCH') else '⚫' if r.stage=='DUMP ACTIVE / LATE' else '⚪'
            lines.append(f'{icon} {canonical_base(r.symbol)} {ch:+.2f}% | {r.stage} | Dump {r.dump_score} | Entry {r.entry_score}')
        lines += ['','🔴 PRE-DUMP = کاندیدای هشدار قبل از ریزش','🟠 DUMP STARTING = ریزش در حال شروع','🔥 EXTREME PUMP / REVERSAL WATCH = پامپ شدید با احتمال خستگی؛ هشدار زودهنگام','⚫ DUMP ACTIVE / LATE = بخش مهم ریزش انجام شده','', 'برای تحلیل کامل + چارت فقط اسم ارز را بفرست؛ مثال: RLC']
        return '\n'.join(lines)
    def format_analysis(self,r):
        lines=[f'🧠 {canonical_base(r.symbol)} — تحلیل Pump → Reversal',f'مرحله: {r.stage}',f'Peak Probability: {r.peak_score}/100',f'Dump Imminence: {r.dump_score}/100',f'Short Entry Quality: {r.entry_score}/100',f'نتیجه: {r.verdict}','',f'قیمت: {r.price:.8g}',f'Pump start: {r.pump_start:.8g}',f'Peak احتمالی: {r.peak:.8g}','','📍 حمایت / مقاومت چندتایم‌فریم']
        for tf in ('5m','15m','1h'):
            lv=r.levels.get(tf,{})
            ss=' | '.join(f'S{i+1}: {v:.8g}' for i,v in enumerate(lv.get('support',[])[:3])) or 'S: یافت نشد'
            rr=' | '.join(f'R{i+1}: {v:.8g}' for i,v in enumerate(lv.get('resistance',[])[:3])) or 'R: یافت نشد'
            lines += [f'{tf} → {ss}',f'{tf} → {rr}']
        lines += ['',f'Entry zone: {r.entry_low:.8g} — {r.entry_high:.8g}',f'Invalidation / SL: {r.invalidation:.8g}',f'TP1: {r.tps[0]:.8g} | TP2: {r.tps[1]:.8g} | TP3: {r.tps[2]:.8g}','','✅ دلایل موافق:']
        lines += [f'• {x}' for x in r.reasons] or ['• تأیید قوی کافی نیست']
        if r.against: lines += ['','⚠️ دلایل مخالف:']+[f'• {x}' for x in r.against]
        lines += ['','نکته: هیچ تأیید کندل نزولی اجباری نیست؛ امتیاز از ۱۰ لایه مستقل و داده زنده ساخته می‌شود.']
        return '\n'.join(lines)
    def analyze_now(self,name):
        r=self.analyzer.analyze(name); return self.format_analysis(r),r.chart_path
    def auto_tick(self):
        if not self.storage.get_setting('auto_analysis_enabled',False) or not self.telegram: return
        now=time.time()
        for s,ch,p in self.analyzer.top_pumps(10):
            base=canonical_base(s)
            if now-self.last_auto.get(base,0)<config.AUTO_ALERT_COOLDOWN_SECONDS: continue
            try: r=self.analyzer.analyze(s)
            except Exception: continue
            if r.stage in ('PRE-DUMP','DUMP STARTING') and r.dump_score>=config.AUTO_ALERT_SCORE and r.entry_score>=58:
                self.telegram.send_photo(r.chart_path,self.format_analysis(r)); self.last_auto[base]=now
            elif r.stage=='EXTREME PUMP / REVERSAL WATCH' and r.peak_score>=60 and r.dump_score>=58:
                self.telegram.send_photo(r.chart_path,'🔥 هشدار پامپ شدید / خستگی احتمالی\n\n'+self.format_analysis(r)); self.last_auto[base]=now
