from __future__ import annotations
import signal,threading,time
from bot import BotEngine
from storage import Storage
from telegram_bot import TelegramBot
from toobit_client import ToobitClient
from utils import logger
class Application:
    def __init__(self):
        self.storage=Storage();self.toobit=ToobitClient();self.engine=BotEngine(self.storage,self.toobit);self.telegram=TelegramBot(self.storage,self.engine);self.engine.bind_telegram(self.telegram);self.stop_event=threading.Event()
    def start(self):
        self.engine.startup();threading.Thread(target=self.telegram.poll_loop,daemon=True).start()
        threading.Thread(target=self._full,daemon=True).start();threading.Thread(target=self._watch,daemon=True).start()
        self.telegram.send_message('✅ ربات دیده‌بان پامپ فعال شد. دستورات: اسکن، واچ، RLC، تحلیل خودکار فعال/خاموش')
    def _full(self):
        while not self.stop_event.is_set():
            try:self.engine.scan_deep()
            except Exception as e:logger.warning('FULL_SCAN %s',e)
            if self.stop_event.wait(120):break
    def _watch(self):
        while not self.stop_event.wait(5):
            try:self.engine.watch_tick()
            except Exception as e:logger.warning('WATCH %s',e)
    def run(self):
        self.start()
        while not self.stop_event.wait(1):pass
    def stop(self):
        self.stop_event.set();self.telegram.stop();self.engine.close();self.toobit.close();self.storage.close()
def main():
    a=Application();signal.signal(signal.SIGINT,lambda *_:a.stop_event.set());signal.signal(signal.SIGTERM,lambda *_:a.stop_event.set())
    try:a.run()
    finally:a.stop()
if __name__=='__main__':main()
