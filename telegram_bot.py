from __future__ import annotations
import os,re,threading,requests
import config
from utils import safe_int,logger
class TelegramBot:
    def __init__(self,storage,engine):
        self.storage=storage; self.engine=engine; self.token=config.TELEGRAM_BOT_TOKEN; self.owner_id=str(config.TELEGRAM_CHAT_ID or storage.get_setting('bound_chat_id','') or '').strip(); self.session=requests.Session(); self.offset=0; self._stop=threading.Event()
    def _url(self,m): return f'https://api.telegram.org/bot{self.token}/{m}'
    def _authorized(self,m):
        cid=str(m.get('chat',{}).get('id') or '')
        if not self.owner_id and cid: self.owner_id=cid; self.storage.set_setting('bound_chat_id',cid)
        return cid==self.owner_id or str(m.get('from',{}).get('id') or '')==self.owner_id
    def send_message(self,text,reply_to=None):
        if not self.token or not self.owner_id:return
        p={'chat_id':self.owner_id,'text':text[:3900]};
        if reply_to:p['reply_to_message_id']=reply_to
        try:self.session.post(self._url('sendMessage'),json=p,timeout=15)
        except Exception as e:logger.warning('TG_SEND %s',e)
    def send_photo(self,path,caption=''):
        if not self.token or not self.owner_id:return
        try:
            with open(path,'rb') as f:self.session.post(self._url('sendPhoto'),data={'chat_id':self.owner_id,'caption':caption[:1024]},files={'photo':f},timeout=30)
            if len(caption)>1024:self.send_message(caption[1024:])
        except Exception as e: self.send_message(f'تحلیل انجام شد ولی ارسال چارت خطا داد: {e}\n{caption}')
    def help_text(self):
        auto='فعال' if self.storage.get_setting('auto_analysis_enabled',False) else 'خاموش'
        return '\n'.join(['📋 دستورات ربات تحلیلگر','','اسکن — ۱۰ ارز صدر Top 24h و کاندیداها','RLC — تحلیل فوری هر ارز فقط با فرستادن اسم آن','تحلیل RLC — همان تحلیل فوری با دستور کامل',f'تحلیل خودکار فعال / تحلیل خودکار خاموش — الان: {auto}','وضعیت — وضعیت موتور تحلیل','لایه‌ها — نمایش ۱۰ لایه تحلیل','دستورات / راهنما — همین فهرست','','ترید، پوزیشن‌گیری و ارسال سفارش کاملاً حذف شده‌اند.'])
    def layers(self): return '🧩 ۱۰ لایه\n1) شدت/ساختار Pump 1H\n2) افت شتاب Momentum\n3) رفتار زنده نزدیک Peak\n4) عرضه و Upper Wick\n5) Volume/Sell pressure\n6) کارایی خریدار و Exhaustion\n7) Micro structure 1m\n8) کشیدگی 5m/15m\n9) Order Book imbalance\n10) Recent Trades / Order Flow\n\nتأیید کندل نزولی شرط نیست؛ وزن لایه‌های مهم بیشتر است.'
    def handle(self,text):
        t=text.strip(); n=t.lower().replace('/','')
        if n in ('اسکن','scan'): return self.engine.scan_text(),None
        if n in ('دستورات','راهنما','help','start'): return self.help_text(),None
        if n=='لایه‌ها': return self.layers(),None
        if n=='وضعیت': return f"🩺 موتور تحلیل: آماده\nتحلیل خودکار: {'فعال' if self.storage.get_setting('auto_analysis_enabled',False) else 'خاموش'}\nترید: حذف‌شده",None
        if n in ('تحلیل خودکار فعال','خودکار فعال'):
            self.storage.set_setting('auto_analysis_enabled',True); return '✅ تحلیل خودکار فعال شد. Top 10 پایش می‌شود و فقط هشدارهای قوی با چارت ارسال می‌شوند.',None
        if n in ('تحلیل خودکار خاموش','خودکار خاموش'):
            self.storage.set_setting('auto_analysis_enabled',False); return '⏸ تحلیل خودکار خاموش شد. تحلیل دستی با اسم ارز همچنان فعال است.',None
        coin=t
        if n.startswith('تحلیل '): coin=t.split(' ',1)[1]
        if re.fullmatch(r'[A-Za-z0-9_-]{2,20}',coin):
            try:return self.engine.analyze_now(coin)
            except Exception as e:return f'❌ تحلیل {coin.upper()} انجام نشد: {e}',None
        return 'دستور شناخته نشد. «دستورات» را بفرست.',None
    def poll_loop(self):
        while not self._stop.is_set():
            try:
                d=self.session.get(self._url('getUpdates'),params={'offset':self.offset,'timeout':25,'allowed_updates':'["message"]'},timeout=35).json()
                for u in d.get('result',[]):
                    self.offset=max(self.offset,safe_int(u.get('update_id'))+1); m=u.get('message') or {}; txt=str(m.get('text') or '').strip()
                    if not txt or not self._authorized(m):continue
                    self.send_message('⏳ در حال تحلیل…',safe_int(m.get('message_id'))) if (re.fullmatch(r'[A-Za-z0-9_-]{2,20}',txt) or txt.startswith('تحلیل ')) else None
                    reply,photo=self.handle(txt)
                    if photo:self.send_photo(photo,reply)
                    else:self.send_message(reply,safe_int(m.get('message_id')))
            except requests.Timeout:pass
            except Exception as e:logger.warning('TG_POLL %s',e)
    def stop(self):self._stop.set();self.session.close()
