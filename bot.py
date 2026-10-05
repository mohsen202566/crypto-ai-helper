from __future__ import annotations
import time
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
        rows=self.analyzer.top_pumps(10); lines=['🚀 ۱۰ ارز صدر Top 24h — کاندیدای بررسی','']
        for i,(s,ch,p) in enumerate(rows,1): lines.append(f'{i}. {canonical_base(s)}  {ch:+.2f}%  | {p:.8g}')
        lines+=['','برای تحلیل فوری فقط اسم ارز را بفرست؛ مثال: RLC']
        return '\n'.join(lines)
    def format_analysis(self,r):
        lines=[f'🧠 {canonical_base(r.symbol)} — تحلیل Pump → Reversal',f'امتیاز احتمال ریزش: {r.score}/100',f'نتیجه: {r.verdict}','',f'قیمت: {r.price:.8g}',f'Pump start: {r.pump_start:.8g}',f'Peak مرجع (شرط ورود نیست): {r.peak:.8g}','','📍 حمایت / مقاومت چندتایم‌فریم']
        for tf in ('5m','15m','1h'):
            lv=r.levels.get(tf,{})
            ss=' | '.join(f'S{i+1}: {v:.8g}' for i,v in enumerate(lv.get('support',[])[:3])) or 'S: یافت نشد'
            rr=' | '.join(f'R{i+1}: {v:.8g}' for i,v in enumerate(lv.get('resistance',[])[:3])) or 'R: یافت نشد'
            lines += [f'{tf} → {ss}',f'{tf} → {rr}']
        lines += ['',f'Entry zone: {r.entry_low:.8g} — {r.entry_high:.8g}',f'Invalidation / SL: {r.invalidation:.8g}',f'TP1: {r.tps[0]:.8g} | TP2: {r.tps[1]:.8g} | TP3: {r.tps[2]:.8g}','','✅ دلایل موافق:']
        lines += [f'• {x}' for x in r.reasons] or ['• تأیید قوی کافی نیست']
        if r.against: lines += ['','⚠️ دلایل مخالف:']+[f'• {x}' for x in r.against]
        lines += ['','نکته: گرفتن دقیق Peak یا ثبت سقف جدید شرط نیست؛ اگر قیمت کمی پایین‌تر از Peak باشد و لایه‌های مستقل ضعف/ریزش را تأیید کنند، هشدار صادر می‌شود. هیچ تأیید کندل نزولی اجباری نیست.']
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
            if r.score>=config.AUTO_ALERT_SCORE:
                self.telegram.send_photo(r.chart_path,self.format_analysis(r)); self.last_auto[base]=now
