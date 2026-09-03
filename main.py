"""نقطهٔ شروع ربات اسکن چندارزی."""
from __future__ import annotations

import signal as sigmod
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
    def __init__(self) -> None:
        self.storage = Storage()
        self.toobit = ToobitClient()
        self.engine = BotEngine(self.storage, self.toobit)
        # «زنده» باید قیمت لحظه‌ای بگیرد، پس به موتور وصل می‌شود.
        self.telegram = TelegramBot(self.storage, live_provider=self.engine.live_report_text)
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.closed = False

    # ------------------------------------------------------------------
    def _spawn(self, name: str, target: Callable[[], Any]) -> None:
        def runner() -> None:
            logger.info("WORKER_START | %s", name)
            try:
                target()
            except Exception as exc:
                self.storage.set_health(name, "warning", str(exc))
                logger.exception("WORKER_CRASH | %s", name)
        thread = threading.Thread(name=name, target=runner, daemon=True)
        thread.start()
        self.threads.append(thread)

    def _periodic(self, name: str, seconds: float, fn: Callable[[], Any],
                  *, require_ready: bool = True) -> None:
        def loop() -> None:
            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    if not require_ready or self.storage.get_setting("startup_ready", False):
                        fn()
                except Exception as exc:
                    self.storage.set_health(name, "warning", str(exc))
                    logger.warning("%s | %s", name, exc)
                elapsed = time.monotonic() - started
                if self.stop_event.wait(max(0.5, seconds - elapsed)):
                    return
        self._spawn(name, loop)

    def _startup_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.engine.startup()
                universe = self.storage.get_setting("universe", []) or []
                self.storage.queue_message(
                    "✅ ربات آماده شد.\n"
                    f"ارزهای تحت اسکن: {len(universe)}\n"
                    f"تایم‌فریم: {config.ENTRY_TIMEFRAME}\n"
                    "ترید واقعی خاموش است؛ با دستور «ترید فعال» روشن می‌شود.\n"
                    "برای دیدن وضعیت: «پنل»"
                )
                return
            except Exception as exc:
                self.storage.set_setting("startup_ready", False)
                self.storage.set_setting("startup_phase", f"خطای اتصال: {str(exc)[:150]}")
                self.storage.set_health("startup", "warning", str(exc))
                logger.warning("STARTUP_RETRY | %s", exc)
                if self.stop_event.wait(15):
                    return

    # ------------------------------------------------------------------
    def start(self) -> None:
        self.storage.set_health("main", "ok", "ربات شروع شد؛ ترید واقعی خاموش")
        # ترید واقعی هرگز به‌صورت خودکار پس از ری‌استارت روشن نمی‌ماند نیست —
        # ولی تنظیم کاربر حفظ می‌شود تا systemd پس از کرش رفتار را عوض نکند.
        logger.info("BOT_START | build=%s | db=%s", config.BUILD_VERSION, config.RUNTIME_DB)

        self._spawn("telegram-poll", self.telegram.poll_loop)
        self._spawn("telegram-notify", self.telegram.notification_loop)
        self._spawn("startup", self._startup_loop)

        self._periodic("engine-tick", config.POSITION_MONITOR_SECONDS, self.engine.tick)
        self._periodic("real-monitor", config.REAL_MONITOR_SECONDS,
                       self.engine.monitor_real, require_ready=False)
        self._periodic("balance-refresh", config.BALANCE_REFRESH_SECONDS,
                       lambda: self.engine.refresh_balance(force=True), require_ready=False)
        self._periodic("universe-refresh", config.SYMBOL_REFRESH_SECONDS,
                       lambda: self.engine.refresh_universe(force=True))

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
        self.toobit.close()
        deadline = time.monotonic() + 5.0
        for thread in self.threads:
            if thread is threading.current_thread():
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        self.storage.close()
        logger.info("ربات با حفظ دیتابیس خاموش شد")


def main() -> int:
    app = Application()

    def request_stop(_signum: int, _frame: Any) -> None:
        app.stop_event.set()
        app.telegram.stop()

    sigmod.signal(sigmod.SIGINT, request_stop)
    sigmod.signal(sigmod.SIGTERM, request_stop)
    try:
        app.run_forever()
        return 0
    finally:
        app.stop()


if __name__ == "__main__":
    raise SystemExit(main())
