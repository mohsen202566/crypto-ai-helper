"""نقطه شروع ربات؛ تمام فایل‌ها مستقیم در ریشه پروژه هستند."""
from __future__ import annotations

import signal
import threading
import time
from typing import Any, Callable

import config
from bot import BotEngine
from storage import Storage
from telegram_bot import TelegramBot
from toobit_client import ToobitClient
from utils import logger


class Application:
    def __init__(self):
        self.storage = Storage()
        self.toobit = ToobitClient()
        self.engine = BotEngine(self.storage, self.toobit)
        self.telegram = TelegramBot(self.storage, self.engine, self.toobit)
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.closed = False

    def _spawn(self, name: str, target: Callable[[], Any]) -> None:
        def runner() -> None:
            try:
                target()
            except Exception as exc:
                self.storage.set_health(name, "warning", str(exc))
                logger.exception("Worker %s stopped", name)
        thread = threading.Thread(name=name, target=runner, daemon=True)
        thread.start()
        self.threads.append(thread)

    def _periodic(self, name: str, seconds: float, fn: Callable[[], Any], *, immediate: bool = False, ready: bool = True) -> None:
        def loop() -> None:
            if not immediate and self.stop_event.wait(seconds):
                return
            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    if not ready or self.storage.get_setting("startup_ready", False):
                        fn()
                except Exception as exc:
                    self.storage.set_health(name, "warning", str(exc))
                    logger.warning("%s | %s", name, exc)
                elapsed = time.monotonic() - started
                if self.stop_event.wait(max(0.1, seconds - elapsed)):
                    break
        self._spawn(name, loop)

    def _startup_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.engine.startup()
                self.telegram.send_message("✅ ربات شکار پایان پامپ آماده شد.\nترید واقعی بعد از استارت خاموش است؛ دستور «ترید فعال» آن را روشن می‌کند.")
                return
            except Exception as exc:
                self.storage.set_setting("startup_ready", False)
                self.storage.set_setting("startup_phase", f"خطای Toobit: {str(exc)[:160]}")
                self.storage.set_health("startup", "warning", str(exc))
                if self.stop_event.wait(10):
                    return

    def start(self) -> None:
        self.storage.set_health("main", "ok", "process started; real trading OFF")
        logger.info("ربات شروع شد؛ ترید واقعی اجباری خاموش است")
        self._spawn("telegram-poll", self.telegram.poll_loop)
        self._spawn("telegram-notify", self.telegram.notification_loop)
        self._spawn("trade-execution", self._trade_loop)
        self._spawn("startup", self._startup_loop)

        # مانیتور Real حتی قبل از آماده‌شدن اسکنر اجرا می‌شود تا پوزیشن قدیمی گم نشود.
        self._periodic("real-monitor", config.REAL_MONITOR_SECONDS, self.engine.monitor_real, immediate=True, ready=False)
        self._periodic("real-confirm", config.PENDING_CHECK_SECONDS, self.engine.confirm_pending, immediate=True, ready=False)
        self._periodic("price-monitor", config.POSITION_PRICE_SECONDS, self.engine.monitor_prices, immediate=True, ready=True)
        self._periodic("scanner", config.MARKET_SCAN_SECONDS, self.engine.scan_once, immediate=True, ready=True)

    def _trade_loop(self) -> None:
        while not self.stop_event.is_set():
            self.engine.process_trade_one(timeout=1.0)

    def run_forever(self) -> None:
        self.start()
        while not self.stop_event.wait(1):
            pass

    def stop(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stop_event.set()
        self.telegram.stop()
        for thread in self.threads:
            if thread is threading.current_thread():
                continue
            thread.join(timeout=3)
        self.toobit.close()
        self.storage.close()
        logger.info("ربات با حفظ دیتابیس خاموش شد")


def main() -> int:
    app = Application()

    def request_stop(_signum: int, _frame: Any) -> None:
        app.stop_event.set()
        app.telegram.stop()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        app.run_forever()
        return 0
    finally:
        app.stop()


if __name__ == "__main__":
    raise SystemExit(main())
