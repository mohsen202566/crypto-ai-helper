"""نقطه ورود ربات تطبیقی کریپتو.

مسیر فرمان و اجرای واقعی از تحلیل و یادگیری جداست. راه‌اندازی اولیه تا ساخت
پروفایل‌های مستقل 30m/1H/4H/1D تمام ۳۵ ارز فعال، دروازه صدور سیگنال را بسته نگه می‌دارند.
"""
from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from collections.abc import Callable
from typing import Any

import config
from behavior_engine import BehaviorEngine
from command_router import CommandRouter
from feature_engine import FeatureEngine
from learning_engine import LearningEngine
from logger_setup import RejectLogger, configure_logging
from market_data import MarketDataClient
from news_filter import NewsFilter
from real_monitor import RealMonitor
from scenario_lab import ScenarioLab
from signal_engine import SignalEngine
from storage import Storage
from symbol_registry import SymbolRegistry
from telegram_bot import TelegramBot
from telegram_ui import ready_message, startup_message
from toobit_client import ToobitClient
from tp_sl_engine import TPSLEngine
from trade_engine import TradeEngine
from validator import Validator
from virtual_monitor import VirtualMonitor

logger = logging.getLogger("adaptive_bot")


class BotApplication:
    """مالک چرخه عمر، صف‌ها و Workerهای مستقل ربات."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self._closed = False
        self.storage = Storage()

        # صف‌ها مسیرهای سنگین را از مسیر فرمان جدا می‌کنند.
        self.trade_queue: queue.Queue[int] = queue.Queue()
        self.scenario_queue: queue.Queue[int] = queue.Queue()
        self.result_queue: queue.Queue[int] = queue.Queue()
        self.notification_queue: queue.Queue[dict[str, Any]] = queue.Queue()

        self.toobit = ToobitClient()
        self.registry = SymbolRegistry(self.storage, self.toobit)
        self.market = MarketDataClient()
        self.news = NewsFilter()
        self.features = FeatureEngine()
        self.behaviors = BehaviorEngine()
        self.tp_sl = TPSLEngine()
        self.rejects = RejectLogger(
            lambda: bool(self.storage.runtime.get_setting("reject_log_enabled", False)),
            logger,
        )

        self.signal_engine = SignalEngine(
            self.storage,
            self.registry,
            self.market,
            self.features,
            self.behaviors,
            self.tp_sl,
            self.trade_queue,
            self.scenario_queue,
            self.notification_queue,
            self.rejects,
            self.news,
        )
        self.trade_engine = TradeEngine(self.storage, self.toobit, self.trade_queue)
        self.real_monitor = RealMonitor(self.storage, self.toobit, self.result_queue, self.notification_queue)
        self.virtual_monitor = VirtualMonitor(self.storage, self.registry, self.market, self.tp_sl, self.result_queue)
        self.scenario_lab = ScenarioLab(self.storage, self.scenario_queue)
        self.learning_engine = LearningEngine(self.storage, self.result_queue, self.notification_queue)
        self.validator = Validator(self.storage)

        self.router = CommandRouter(self.storage)
        self.telegram = TelegramBot(self.storage, self.router, self.notification_queue)
        self.threads: list[threading.Thread] = []

    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        thread = threading.Thread(name=name, target=target, daemon=True)
        thread.start()
        self.threads.append(thread)

    def _queue_worker(self, name: str, component: str, process_one: Callable[[float], Any]) -> None:
        def loop() -> None:
            self.storage.runtime.set_health(component, "ok", "worker started")
            while not self.stop_event.is_set():
                try:
                    process_one(1.0)
                except Exception as exc:  # one item must never kill the worker
                    logger.exception("%s worker error: %s", component, exc)
                    self.storage.runtime.set_health(component, "warning", str(exc)[:300])
                    self.stop_event.wait(1.0)
        self._spawn(name, loop)

    def _periodic_worker(
        self,
        name: str,
        component: str,
        interval_seconds: float,
        function: Callable[[], Any],
        run_immediately: bool = True,
        ready_required: bool = False,
    ) -> None:
        def loop() -> None:
            next_run = time.monotonic() if run_immediately else time.monotonic() + interval_seconds
            while not self.stop_event.is_set():
                wait_for = max(0.0, next_run - time.monotonic())
                if self.stop_event.wait(wait_for):
                    break
                if ready_required and not self.storage.runtime.get_setting("startup_ready", False):
                    next_run = time.monotonic() + min(interval_seconds, 5.0)
                    continue
                started = time.monotonic()
                try:
                    function()
                except Exception as exc:
                    logger.exception("%s periodic error: %s", component, exc)
                    self.storage.runtime.set_health(component, "warning", str(exc)[:300])
                elapsed = time.monotonic() - started
                # No overlapping runs. A slow cycle schedules the next cycle after completion.
                next_run = time.monotonic() + max(0.2, interval_seconds - min(elapsed, interval_seconds * 0.25))
        self._spawn(name, loop)

    def _progress(self, message: str) -> None:
        self.storage.runtime.set_setting("startup_phase", message)
        self.storage.runtime.set_health("startup", "warning", message)
        logger.info("STARTUP | %s", message)

    def _startup_gate_loop(self) -> None:
        """Retry-safe online validation and profile gate; never exposes partial readiness."""
        retry = 15.0
        initial_notice_sent = False
        while not self.stop_event.is_set():
            try:
                self.storage.runtime.set_setting("startup_ready", False)
                self._progress("اعتبارسنجی ۱۰۰ نماد مشترک")
                if not initial_notice_sent:
                    self.notification_queue.put({"type": "plain", "text": startup_message("اعتبارسنجی نمادها و ساخت پروفایل ۳۵ ارز فعال")})
                    initial_notice_sent = True

                mappings = self.registry.validate_universe(progress=self._progress)
                self._progress("ساخت یا بازیابی پروفایل‌های چندتایمی ارزهای فعال")
                self.signal_engine.prepare_profiles(mappings, progress=self._progress)

                active_count = len(self.registry.active())
                if active_count != config.ACTIVE_SYMBOLS:
                    raise RuntimeError(f"active count mismatch {active_count}/{config.ACTIVE_SYMBOLS}")
                self.storage.runtime.set_health("startup", "ok", f"READY {active_count}/{active_count}")
                self.notification_queue.put({"type": "plain", "text": ready_message(active_count)})

                # ذخیره‌ها بعد از بازشدن دروازه سیگنال، با اولویت پایین آماده می‌شوند.
                self._spawn(
                    "reserve-profiler",
                    lambda: self.signal_engine.prepare_reserve_profiles(
                        mappings,
                        stop_check=self.stop_event.is_set,
                    ),
                )
                return
            except Exception as exc:
                self.storage.runtime.set_setting("startup_ready", False)
                self.storage.runtime.set_setting("startup_phase", "STARTUP_RETRY")
                self.storage.runtime.set_health("startup", "warning", str(exc)[:300])
                logger.warning("STARTUP_RETRY | %s | retry %.0fs", str(exc)[:300], retry)
                if self.stop_event.wait(retry):
                    return
                retry = min(300.0, retry * 1.7)

    def _backup_once(self) -> None:
        runtime_path, learning_path = self.storage.backup_all("scheduled")
        self.storage.runtime.set_health(
            "backup", "ok", f"{runtime_path.name}, {learning_path.name}"
        )

    def start(self) -> None:
        # Trading OFF is enforced by RuntimeStore on every process start.
        self.storage.runtime.set_health("main", "ok", "process started; real trading OFF")
        logger.info("ربات شروع شد؛ ترید واقعی به‌صورت اجباری خاموش است")

        # High-priority command/output paths start before market warm-up.
        self._spawn("telegram-poll", self.telegram.poll_loop)
        self._spawn("telegram-notify", self.telegram.notification_loop)
        self._queue_worker("trade-execution", "trade_engine", self.trade_engine.process_one)
        self._queue_worker("learning-results", "learning_engine", self.learning_engine.process_one)
        self._queue_worker("scenario-create", "scenario_lab", self.scenario_lab.process_one)

        # Existing real positions are recovered/monitored even while startup profiles build.
        self._periodic_worker(
            "real-monitor",
            "real_monitor",
            config.REAL_MONITOR_SECONDS,
            self.real_monitor.tick,
            run_immediately=True,
            ready_required=False,
        )
        # Lightweight deadline watcher: no API call until a PENDING_OPEN reaches 70s.
        self._periodic_worker(
            "real-confirm",
            "real_confirm",
            5.0,
            self.real_monitor.confirm_due,
            run_immediately=True,
            ready_required=False,
        )

        # The startup gate is isolated and retry-safe.
        self._spawn("startup-gate", self._startup_gate_loop)

        # Analysis and learning validation only run after READY.
        self._periodic_worker(
            "virtual-monitor",
            "virtual_monitor",
            config.VIRTUAL_MONITOR_SECONDS,
            self.virtual_monitor.tick,
            ready_required=True,
        )
        self._periodic_worker(
            "signal-scanner",
            "scanner",
            config.SCAN_INTERVAL_SECONDS,
            self.signal_engine.scan_once,
            ready_required=True,
        )
        self._periodic_worker(
            "profile-refresh",
            "profile_refresh",
            config.PROFILE_REFRESH_STEP_SECONDS,
            self.signal_engine.refresh_one_stale_active_profile,
            run_immediately=False,
            ready_required=True,
        )
        self._periodic_worker(
            "validator",
            "validator",
            config.VALIDATOR_INTERVAL_SECONDS,
            self.validator.run_once,
            run_immediately=False,
            ready_required=True,
        )
        self._periodic_worker(
            "backup",
            "backup",
            config.BACKUP_INTERVAL_SECONDS,
            self._backup_once,
            run_immediately=False,
            ready_required=False,
        )

    def run_forever(self) -> None:
        self.start()
        while not self.stop_event.wait(1.0):
            pass

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        logger.info("درخواست خاموش‌کردن امن دریافت شد")
        self.stop_event.set()
        self.telegram.stop()
        # A final backup is best-effort and never blocks shutdown indefinitely.
        try:
            self.storage.backup_all("shutdown")
        except Exception as exc:
            logger.warning("shutdown backup failed: %s", exc)
        for thread in self.threads:
            if thread is threading.current_thread():
                continue
            thread.join(timeout=3.0)
        try:
            self.market.close()
        except Exception:
            pass
        for session in (
            getattr(self.toobit, "session", None),
            getattr(self.registry, "session", None),
            getattr(self.news, "session", None),
        ):
            try:
                if session is not None:
                    session.close()
            except Exception:
                pass
        self.storage.close()
        logger.info("ربات با حفظ دیتابیس‌ها خاموش شد")


def main() -> int:
    configure_logging()
    app = BotApplication()

    def request_stop(_signum: int, _frame: Any) -> None:
        app.stop_event.set()
        app.telegram.stop()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        app.run_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        app.stop()


if __name__ == "__main__":
    raise SystemExit(main())
