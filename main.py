from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any

import config
from adaptive_engine import AdaptiveEngine
from decision_engine import DecisionEngine
from execution_engine import ExecutionEngine
from experience_engine import ExperienceEngine
from health import HealthManager
from learning_engine import LearningEngine
from market_engine import MarketEngine
from monitor import Monitor
from okx_client import OKXClient
from profiles import Profiles
from risk_engine import RiskEngine
from setup_engine import SetupEngine
from storage import Storage
from symbols import SYMBOLS, select_valid_symbols
from telegram_bot import TelegramBot
from toobit_client import ToobitFuturesClient
from watch_engine import WatchEngine

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("adaptive_scalper")


class App:
    def __init__(self):
        self.storage = Storage()
        archived = self.storage.quarantine_legacy_virtual_signals()
        if archived:
            log.warning("مهاجرت دیتابیس | %s معامله مجازی قدیمی آرشیو شد و اعلان نخواهد شد", archived)
        self.health = HealthManager(self.storage)
        self.okx = OKXClient()
        self.symbols = list(SYMBOLS)
        try:
            valid_ids = self.okx.get_live_swap_instrument_ids()
        except Exception as exc:
            # If the validation endpoint itself is temporarily unavailable, keep the
            # primary 40 and quarantine any bad mapping after its first API error.
            log.warning("اعتبارسنجی زنده نمادها انجام نشد؛ فهرست ثابت استفاده می‌شود | علت=%s", exc)
        else:
            selected = select_valid_symbols(valid_ids, target=40)
            self.symbols = selected
            replaced = [(a.id, b.id) for a, b in zip(SYMBOLS, selected) if a.id != b.id]
            if replaced:
                log.warning("اعتبارسنجی نمادها | جایگزینی=%s", replaced)
            if len(selected) < 40:
                missing = [s.okx for s in SYMBOLS if s.okx not in valid_ids]
                log.critical("اعتبارسنجی نمادها | فقط %s بازار معتبر پیدا شد؛ نامعتبرها=%s", len(selected), missing)
            else:
                log.info("اعتبارسنجی نمادها | 40 بازار فعال OKX تأیید شد")
        self.invalid_symbols: set[str] = set()
        self.toobit = ToobitFuturesClient()
        self.telegram = TelegramBot(self.storage, self.health)
        self.profiles = Profiles(self.storage)
        self.market = MarketEngine()
        self.setup = SetupEngine()
        self.watch = WatchEngine()
        self.decision = DecisionEngine()
        self.risk = RiskEngine()
        self.toobit_lock = threading.RLock()
        self.execution = ExecutionEngine(self.storage, self.toobit, self.okx, self.health, self.toobit_lock)
        self.experience = ExperienceEngine()
        self.learning = LearningEngine(self.storage)
        self.adaptive = AdaptiveEngine(self.storage, self.profiles)
        self.monitor = Monitor(self.okx, self.toobit, self.storage, self.telegram, self.experience, self.toobit_lock)
        self.stop = threading.Event()
        self.last_signal: dict[str, float] = {}
        self.active_watches: dict[str, dict[str, Any]] = {}
        self.last_publish_failure: dict[str, float] = {}
        self.watch_lock = threading.RLock()
        self.watch_log_state: dict[str, tuple[str, float]] = {}

    def _open_exists(self, symbol_id: str) -> bool:
        return any(x["symbol_id"] == symbol_id for x in self.storage.get_open_signals())

    @staticmethod
    def _setup_fa(value: str) -> str:
        return {
            "PULLBACK_CONTINUATION": "پولبک ادامه روند",
            "COMPRESSION_BREAKOUT": "شکست فشردگی",
            "STRUCTURE_BREAK_RETEST": "شکست ساختار و ریتست",
        }.get(value, value)

    def _publish(self, sym, market, setup, watch, decision, risk) -> None:
        data: dict[str, Any] = {
            "symbol_id": sym.id, "okx_symbol": sym.okx, "toobit_symbol": sym.toobit,
            "side": setup.side, "setup_type": setup.setup_type, "trade_mode": "virtual",
            "is_real": 0, "status": "open", "entry": risk.entry, "tp": risk.tp, "sl": risk.sl,
            "gross_rr": risk.gross_rr, "net_rr": risk.net_rr, "trade_usdt": risk.trade_usdt,
            "leverage": risk.leverage, "notional_usdt": risk.notional_usdt,
            "estimated_net_profit": risk.estimated_net_profit, "estimated_net_loss": risk.estimated_net_loss,
            "estimated_cost": risk.estimated_cost_win, "estimated_cost_win": risk.estimated_cost_win,
            "estimated_cost_loss": risk.estimated_cost_loss, "direction_score": market.direction_score,
            "strength_score": market.strength_score, "freshness_score": market.freshness_score,
            "setup_score": setup.score, "trigger_score": watch.trigger_score,
            "final_score": decision.final_score, "confidence": decision.confidence,
            "model_version": self.profiles.get(sym.id).version,
            "strategy_id": config.STRATEGY_ID, "timeframe": config.STRATEGY_TIMEFRAME,
            "raw": {"setup_id": setup.setup_id, "reasons": market.reasons + setup.reasons, "contradictions": decision.contradictions},
        }
        signal_id = self.storage.create_signal(data)
        side_icon = "🟢" if setup.side == "LONG" else "🔴"
        trading_enabled = bool(self.storage.get("trading_enabled", False))
        execution_line = "اجرای واقعی: در حال بررسی" if trading_enabled else "اجرای واقعی: ترید خاموش است"
        text = (
            f"📊 سیگنال 5M\n\n#{signal_id} | {sym.id}\n"
            f"{side_icon} {setup.side} | مجازی مرجع\n\n"
            f"سناریو: {self._setup_fa(setup.setup_type)}\n"
            f"فعال‌سازی: {watch.reason}\nاطمینان تشخیصی: {decision.confidence:.0f}٪\n\n"
            f"Entry: {risk.entry:.8g}\nTP: {risk.tp:.8g}\nSL: {risk.sl:.8g}\n\n"
            f"Net RR: {risk.net_rr:.2f}\nسود خالص تخمینی: {risk.estimated_net_profit:.4f} USDT\n"
            f"زیان خالص تخمینی: {risk.estimated_net_loss:.4f} USDT\n{execution_line}\nمدل: {data['model_version']}"
        )
        message_id = self.telegram.send_message(text)
        self.storage.update_signal(signal_id, message_id=message_id)
        if not message_id:
            self.storage.update_signal(signal_id, status="publish_failed")
            self.storage.add_health_event("signal_publish", "critical", "ارسال سیگنال تلگرام ناموفق بود؛ سیگنال وارد مانیتور و اجرای واقعی نشد", sym.id)
            self.last_publish_failure[sym.id] = time.time()
            return
        execution = self.execution.execute(sym, signal_id, setup.side, risk)
        if execution["status"] == "REAL_PENDING":
            self.storage.update_signal(signal_id, trade_mode="real", is_real=1, status="pending")
            self.telegram.send_message("✅ اجرای واقعی در توبیت ارسال شد | Isolated", reply_to_message_id=message_id)
        elif trading_enabled:
            self.telegram.send_message(f"⚠️ اجرای واقعی انجام نشد: {execution.get('reason', 'نامشخص')}", reply_to_message_id=message_id)
        self.last_signal[sym.id] = time.time()

    def scan_once(self) -> None:
        """اسکن کندلی 5M/15M برای ساخت یا تازه‌سازی واچ‌ها.

        تریگر 1M در حلقه جداگانه و سریع‌تر بررسی می‌شود تا اسکن 60 ثانیه‌ای
        باعث ورود دیرهنگام نشود.
        """
        started = time.time()
        auto_signal_enabled = bool(self.storage.get("auto_signal_enabled", True))
        if not auto_signal_enabled:
            with self.watch_lock:
                self.active_watches.clear()

        for sym in self.symbols:
            if sym.id in self.invalid_symbols:
                continue
            try:
                if self._open_exists(sym.id):
                    with self.watch_lock:
                        self.active_watches.pop(sym.id, None)
                    if config.LOG_SCAN_DETAILS:
                        log.info("اسکن | ارز=%s | نتیجه=رد | علت=پوزیشن یا سیگنال باز دارد", sym.id)
                    continue
                if (time.time() - self.last_signal.get(sym.id, 0) < config.SIGNAL_COOLDOWN_SECONDS_PER_SYMBOL
                        or time.time() - self.last_publish_failure.get(sym.id, 0) < config.SIGNAL_PUBLISH_RETRY_COOLDOWN_SECONDS
                        or self.storage.has_recent_signal(sym.id, config.SIGNAL_COOLDOWN_SECONDS_PER_SYMBOL)):
                    with self.watch_lock:
                        self.active_watches.pop(sym.id, None)
                    if config.LOG_SCAN_DETAILS:
                        log.info("اسکن | ارز=%s | نتیجه=رد | علت=Cooldown سیگنال یا شکست ارسال فعال است", sym.id)
                    continue

                c5 = self.okx.get_candles(sym.okx, bar=config.OKX_BAR, limit=300)
                c15 = self.okx.get_candles(sym.okx, bar=config.OKX_CONTEXT_BAR, limit=200)
                market = self.market.analyze(sym.id, c5, c15)
                self.storage.resolve_health("scan", sym.id)

                if market.primary_direction in {"LONG", "SHORT"} and self.storage.has_recent_stop(
                    sym.id, market.primary_direction, config.SCENARIO_REENTRY_AFTER_SL_SECONDS
                ):
                    with self.watch_lock:
                        self.active_watches.pop(sym.id, None)
                    if config.LOG_SCAN_DETAILS:
                        log.info("اسکن | ارز=%s | نتیجه=رد | علت=سناریوی هم‌جهت اخیراً SL خورده؛ ساختار تازه لازم است", sym.id)
                    continue

                with self.watch_lock:
                    existing = self.active_watches.get(sym.id)

                if existing is not None:
                    candidate = existing["setup"]
                    # If the market direction changes or becomes unsafe, discard the stale watch.
                    if market.hard_veto or market.primary_direction != candidate.side or int(time.time()) > candidate.expires_at:
                        if market.hard_veto:
                            stale_reason = "Hard Veto جدید بازار"
                        elif market.primary_direction != candidate.side:
                            stale_reason = f"تغییر جهت بازار از {candidate.side} به {market.primary_direction}"
                        else:
                            stale_reason = "انقضای واچ"
                        with self.watch_lock:
                            self.active_watches.pop(sym.id, None)
                        if config.LOG_SCAN_DETAILS:
                            log.info("اسکن | ارز=%s | نتیجه=حذف واچ | علت=%s", sym.id, stale_reason)
                        continue
                    with self.watch_lock:
                        self.active_watches[sym.id]["market"] = market
                    if config.LOG_SCAN_DETAILS:
                        log.info(
                            "اسکن | ارز=%s | نتیجه=ادامه واچ | سناریو=%s | سمت=%s | مرحله=%s | زمان‌مانده=%ss",
                            sym.id, self._setup_fa(candidate.setup_type), candidate.side, candidate.meta.get("scenario_state"),
                            max(0, candidate.expires_at-int(time.time())),
                        )
                    continue

                if not auto_signal_enabled:
                    continue
                candidate, setup_reason, setup_details = self.setup.detect_with_reason(market, c5)
                if candidate is None:
                    if config.LOG_SCAN_DETAILS:
                        log.info(
                            "اسکن | ارز=%s | نتیجه=رد | علت=%s | زمینه=%s | رژیم=%s",
                            sym.id, setup_reason, market.primary_direction, market.regime,
                        )
                    continue
                with self.watch_lock:
                    self.active_watches[sym.id] = {"symbol": sym, "market": market, "setup": candidate}
                if config.LOG_SCAN_DETAILS:
                    log.info(
                        "اسکن | ارز=%s | نتیجه=واچ | سناریو=%s | سمت=%s | مرحله=%s | Trigger=%.8g | Invalidation=%.8g | انقضا=%ss",
                        sym.id, self._setup_fa(candidate.setup_type), candidate.side, candidate.meta.get("scenario_state"),
                        candidate.trigger_price, candidate.invalidation_price, max(0, candidate.expires_at-int(time.time())),
                    )
            except Exception as exc:
                message = str(exc)
                self.storage.add_health_event("scan", "warning", message, sym.id)
                if "Instrument ID" in message or "doesn't exist" in message or "does not exist" in message:
                    self.invalid_symbols.add(sym.id)
                    with self.watch_lock:
                        self.active_watches.pop(sym.id, None)
                    log.error("نماد قرنطینه شد | ارز=%s | OKX=%s | علت=%s", sym.id, sym.okx, message)
                else:
                    log.exception("خطای اسکن %s", sym.id)

        self.storage.set("scan_last_ts", int(time.time()))
        self.storage.set("scan_last_symbols", len(self.symbols) - len(self.invalid_symbols))
        self.storage.set("scan_last_duration", time.time() - started)
        with self.watch_lock:
            watch_count = len(self.active_watches)
        self.storage.set("watch_count", watch_count)
        self.health.mark("scan")
        log.info("پایان اسکن | واچ=%s | زمان=%.2fs", watch_count, time.time() - started)

    def watch_once(self) -> None:
        """بررسی سریع 1M برای واچ‌های فعال و صدور سیگنال نهایی."""
        if not self.storage.get("auto_signal_enabled", True):
            return
        with self.watch_lock:
            items = list(self.active_watches.items())

        published = 0
        for symbol_id, state in items:
            sym = state["symbol"]
            market = state["market"]
            candidate = state["setup"]
            try:
                if self._open_exists(symbol_id):
                    with self.watch_lock:
                        self.active_watches.pop(symbol_id, None)
                    continue
                c1 = self.okx.get_candles(sym.okx, bar=config.OKX_TRIGGER_BAR, limit=100)
                watch = self.watch.evaluate(candidate, c1)
                self.storage.resolve_health("watch", symbol_id)
                if watch.state in {"EXPIRED", "INVALIDATED"}:
                    with self.watch_lock:
                        self.active_watches.pop(symbol_id, None)
                    self.watch_log_state.pop(symbol_id, None)
                    if config.LOG_WATCH_DETAILS:
                        log.info(
                            "واچ | ارز=%s | نتیجه=رد و حذف | حالت=%s | علت=%s | قیمت=%.8g | Trigger=%.8g | Invalidation=%.8g",
                            symbol_id, watch.state, watch.reason, watch.entry_price,
                            candidate.trigger_price, candidate.invalidation_price,
                        )
                    continue

                decision = self.decision.decide(market, candidate, watch)
                if not decision.allowed:
                    if config.LOG_WATCH_DETAILS:
                        def yn(key):
                            return "ناموجود" if not watch.meta.get("data_ok", True) else ("بله" if watch.meta.get(key) else "خیر")
                        sig = "|".join(str(x) for x in (decision.primary_reason, watch.meta.get("price_ok"), watch.meta.get("progress_ok"), watch.meta.get("acceptance_ok"), watch.meta.get("pressure_ok"), watch.meta.get("momentum_ok"), watch.meta.get("candle_ok")))
                        prev_sig, prev_ts = self.watch_log_state.get(symbol_id, ("", 0.0))
                        if sig != prev_sig or time.time() - prev_ts >= config.WATCH_LOG_HEARTBEAT_SECONDS:
                            log.info(
                                "واچ | ارز=%s | نتیجه=انتظار | مرحله=%s | علت=%s | قیمت=%.8g | مبدأ=%.8g | Trigger=%.8g | ATRساخت=%.8g | فاصلهTriggerATR=%+.3f | پیشرویOriginATR=%+.3f | مصرفATR=%.3f | لمس=%s | عبور=%s | پیشروی=%s | پذیرش=%s | فشار=%s | مومنتوم=%s | کندل=%s | جذب=%s | RSI=%.1f | Hist=%.6g | Body=%.2f",
                                symbol_id, candidate.meta.get("scenario_state"), decision.primary_reason,
                                float(watch.entry_price or 0), float(watch.meta.get("origin") or candidate.anchor_price), float(watch.meta.get("trigger") or candidate.trigger_price), float(watch.meta.get("atr_at_creation") or 0),
                                float(watch.meta.get("trigger_distance_atr") or 0), float(watch.meta.get("origin_progress_atr") or 0), float(watch.meta.get("late_atr") or 0), yn("touch_ok"), yn("price_ok"), yn("progress_ok"), yn("acceptance_ok"), yn("pressure_ok"), yn("momentum_ok"), yn("candle_ok"), yn("absorption"),
                                float(watch.meta.get("rsi") or 0), float(watch.meta.get("macd_hist") or 0), float(watch.meta.get("body_ratio") or 0),
                            )
                            self.watch_log_state[symbol_id] = (sig, time.time())
                    continue

                trade_usdt = float(self.storage.get("trade_usdt", config.TRADE_USDT_DEFAULT))
                leverage = int(self.storage.get("leverage", config.LEVERAGE_DEFAULT))
                risk = self.risk.build(candidate, decision, watch.entry_price, trade_usdt, leverage)
                if not risk.valid:
                    # A risk/economic failure belongs to this exact entry snapshot. Do not keep
                    # re-evaluating the same activated scenario every few seconds. A fresh setup
                    # must be built by the scanner before another attempt is allowed.
                    with self.watch_lock:
                        self.active_watches.pop(symbol_id, None)
                    self.watch_log_state.pop(symbol_id, None)
                    if config.LOG_WATCH_DETAILS:
                        log.info(
                            "واچ | ارز=%s | نتیجه=رد و حذف | مرحله=ریسک | علت=%s | مارجین=%.2f | لوریج=%sx | نُوشنال=%.2f | SL%%=%.4f | TP%%=%.4f | هزینهTP=%.4f | سودخالص=%.4f | زیانخالص=%.4f | NetRR=%.3f | حداقل‌سود=%.4f | حداقلNetRR=%.2f",
                            symbol_id, risk.reason, risk.trade_usdt, risk.leverage, risk.notional_usdt,
                            risk.sl_pct, risk.tp_pct, risk.estimated_cost_win, risk.estimated_net_profit,
                            risk.estimated_net_loss, risk.net_rr, config.MIN_NET_PROFIT_USDT,
                            config.MIN_NET_RR_ABSOLUTE,
                        )
                    continue

                if not self.telegram.enabled:
                    self.storage.add_health_event("telegram", "critical", "توکن یا Chat ID تلگرام تنظیم نشده؛ سیگنال صادر نشد", symbol_id)
                    continue
                if config.LOG_WATCH_DETAILS:
                    log.info(
                        "واچ | ارز=%s | نتیجه=سیگنال | سمت=%s | سناریو=%s | مسیر=%s | NetRR=%.2f",
                        symbol_id, candidate.side, self._setup_fa(candidate.setup_type),
                        watch.meta.get("activation_path"), risk.net_rr,
                    )
                self._publish(sym, market, candidate, watch, decision, risk)
                with self.watch_lock:
                    self.active_watches.pop(symbol_id, None)
                self.watch_log_state.pop(symbol_id, None)
                published += 1
            except Exception as exc:
                self.storage.add_health_event("watch", "warning", str(exc), symbol_id)
                log.exception("خطای واچ %s", symbol_id)

        with self.watch_lock:
            self.storage.set("watch_count", len(self.active_watches))
        self.health.mark("watch")
        if published:
            log.info("سیگنال‌های صادرشده از واچ: %s", published)

    def monitor_once(self) -> None:
        self.monitor.run_once()
        self.health.mark("monitor")

    def status_once(self) -> None:
        self.storage.ensure_daily_profit()
        if not self.toobit.has_credentials:
            self.storage.set("toobit_connected", False)
            if self.storage.get("trading_enabled", False):
                self.storage.add_health_event("toobit", "critical", "ترید فعال است اما کلیدهای API توبیت تنظیم نشده‌اند")
            else:
                self.storage.resolve_health("toobit")
            return
        try:
            with self.toobit_lock:
                balance = self.toobit.get_futures_balance()
            self.storage.set("toobit_connected", True)
            self.storage.set("toobit_available_usdt", balance["available"])
            self.storage.set("toobit_total_usdt", balance["total"])
            self.storage.set("toobit_margin_usdt", balance["margin"])
            self.storage.resolve_health("toobit")
            self.health.mark("toobit")
        except Exception as exc:
            self.storage.set("toobit_connected", False)
            self.storage.add_health_event("toobit", "warning", str(exc))

    def learning_once(self) -> None:
        for sym in self.symbols:
            if sym.id in self.invalid_symbols:
                continue
            try:
                report = self.learning.run(sym.id)
                candidate = self.adaptive.create_candidate(sym.id, report)
                if candidate:
                    log.info("کاندید یادگیری ساخته شد: %s", candidate["candidate_id"])
            except Exception as exc:
                self.storage.add_health_event("learning", "warning", str(exc), sym.id)
        self.health.mark("learning")

    def loop(self, fn, interval: float, name: str) -> None:
        while not self.stop.is_set():
            started = time.time()
            try:
                fn()
            except Exception as exc:
                self.storage.add_health_event(name, "critical", str(exc))
                log.exception("خطا در حلقه %s", name)
            self.stop.wait(max(0.1, interval - (time.time() - started)))

    def run(self) -> None:
        threads = [
            threading.Thread(target=self.loop, args=(self.scan_once, config.SCAN_INTERVAL_SECONDS, "scan"), daemon=True, name="اسکن"),
            threading.Thread(target=self.loop, args=(self.watch_once, config.WATCH_POLL_INTERVAL_SECONDS, "watch"), daemon=True, name="واچ"),
            threading.Thread(target=self.loop, args=(self.monitor_once, 5.0, "monitor"), daemon=True, name="مانیتور"),
            threading.Thread(target=self.loop, args=(self.telegram.poll_once, config.TELEGRAM_POLL_SECONDS, "telegram"), daemon=True, name="تلگرام"),
            threading.Thread(target=self.loop, args=(self.status_once, 15.0, "toobit"), daemon=True, name="توبیت"),
            threading.Thread(target=self.loop, args=(self.learning_once, config.LEARNING_INTERVAL_SECONDS, "learning"), daemon=True, name="یادگیری"),
        ]
        for thread in threads:
            thread.start()
        log.info("ربات 5M تطبیقی اجرا شد | پوزیشن‌های واقعی فقط Isolated")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop.set()
            for thread in threads:
                thread.join(timeout=3)


if __name__ == "__main__":
    App().run()
