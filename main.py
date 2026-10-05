from __future__ import annotations
import signal,threading,time
import config
from bot import BotEngine
from storage import Storage
from telegram_bot import TelegramBot
from toobit_client import ToobitClient
from utils import logger
class Application:
    def __init__(self):
        self.storage=Storage(); self.toobit=ToobitClient(); self.engine=BotEngine(self.storage,self.toobit); self.telegram=TelegramBot(self.storage,self.engine); self.engine.bind_telegram(self.telegram); self.stop_event=threading.Event()
    def start(self):
        self.engine.startup(); threading.Thread(target=self.telegram.poll_loop,daemon=True).start(); threading.Thread(target=self._auto,daemon=True).start(); self.telegram.send_message('✅ ربات تحلیلگر Pump → Reversal آماده شد.\nترید کاملاً حذف شده.\n«اسکن» یا «دستورات» را بفرست.')
    def _auto(self):
        while not self.stop_event.wait(config.AUTO_SCAN_SECONDS):
            try:self.engine.auto_tick()
            except Exception as e:logger.warning('AUTO %s',e)
    def run(self):
        self.start()
        while not self.stop_event.wait(1):pass
    def stop(self):self.stop_event.set();self.telegram.stop();self.toobit.close();self.storage.close()
def main():
    a=Application(); signal.signal(signal.SIGINT,lambda *_:a.stop_event.set()); signal.signal(signal.SIGTERM,lambda *_:a.stop_event.set())
    try:a.run()
    finally:a.stop()
if __name__=='__main__':main()
