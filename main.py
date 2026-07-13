"""نقطه شروع ربات رفتارمحور پنج‌دقیقه‌ای؛ بدون Watch."""
from __future__ import annotations

import logging
import threading
import time

import config
from health import HealthManager
from market_engine import analyze_market_diagnostic
from models import BehaviorState, MarketSignal, RiskPlan
from monitor import Monitor
from okx_client import OKXClient
from reject_logger import RejectLogger, configure_application_logging
from risk_engine import build_risk_plan_diagnostic
from storage import Storage
from symbols import SYMBOLS, SymbolMap
from telegram_bot import TelegramBot
from toobit_client import ToobitFuturesClient

configure_application_logging()
logger = logging.getLogger("uem_5m")


class TradingBotApp:
    def __init__(self) -> None:
        self.storage = Storage()
        self.health = HealthManager(self.storage)
        self.okx = OKXClient()
        self.toobit = ToobitFuturesClient()
        self.telegram = TelegramBot(self.storage, self.health)
        self.monitor = Monitor(self.okx, self.toobit, self.storage, self.telegram, self.health)
        self.rejects = RejectLogger()
        self.stop_event = threading.Event()
        self.states: dict[str, BehaviorState] = {s.id: BehaviorState(s.id) for s in SYMBOLS}

    def _reject(self, stage: str, symbol_id: str, reason: str, metrics: dict | None = None, *, force: bool = False) -> None:
        self.rejects.write(stage, symbol_id, reason, metrics, force=force)
        detail = " | ".join(f"{k}={v}" for k, v in sorted((metrics or {}).items()))
        logger.info("رد | مرحله=%s | ارز=%s | علت=%s%s", stage, symbol_id, reason, f" | {detail}" if detail else "")

    def _eligibility(self, sym: SymbolMap) -> tuple[bool, str, dict]:
        if self.storage.is_blacklisted(sym.id):
            return False, "ارز به‌دلیل خطای داده در بلک‌لیست موقت است", {}
        open_rows = self.storage.get_open_signals()
        blocking = [x for x in open_rows if x["symbol_id"] == sym.id]
        if blocking:
            return False, "برای این ارز سیگنال باز یا Pending وجود دارد", {
                "blocking_signal_ids": [int(x["id"]) for x in blocking],
                "statuses": [str(x["status"]) for x in blocking],
            }
        return True, "مجاز", {}

    def _reserve_real_slot(self) -> int | None:
        max_pos = int(self.storage.get("max_positions", config.MAX_POSITIONS_DEFAULT))
        count = self.storage.count_real_open()
        return count + 1 if count < max_pos else None

    def _signal_message(self, signal_id: int, sig: MarketSignal, risk: RiskPlan, mode: str, trade_usdt: float, leverage: int) -> str:
        icon = "🟢" if sig.side == "LONG" else "🔴"
        return (
            f"📊 سیگنال رفتارمحور 5m\n\n#{signal_id} | {sig.symbol_id}\n"
            f"{icon} {sig.side} | {'واقعی' if mode == 'real' else 'عادی'}\n"
            f"قدرت: {sig.strength}\nEntry: {risk.entry:.8g}\nTP: {risk.tp:.8g}\nSL: {risk.sl:.8g}\n"
            f"RR خالص: {risk.rr_net:.3f}\nدلار: {trade_usdt:g} | لوریج: {leverage}x | "
            f"ارزش پوزیشن: {risk.notional:.4f} USDT\n"
            f"سود ناخالص تخمینی TP: {risk.estimated_tp_gross:.4f} USDT\n"
            f"سود خالص تخمینی TP: {risk.estimated_tp_net:.4f} USDT\n"
            f"زیان خالص تخمینی SL: {risk.estimated_sl_net_loss:.4f} USDT\n"
            f"جهت: {sig.direction_reason}\nقدرت: {sig.strength_reason}\nورود: {sig.entry_reason}\n"
            f"TP/SL: {risk.reason}"
        )

    def publish_signal(self, sig: MarketSignal) -> bool:
        trade_usdt = float(self.storage.get("trade_usdt", config.TRADE_USDT_DEFAULT))
        leverage = int(self.storage.get("leverage", config.LEVERAGE_DEFAULT))
        risk, reject_reason, metrics = build_risk_plan_diagnostic(sig, trade_usdt, leverage)
        if not risk:
            self._reject("risk", sig.symbol_id, reject_reason, metrics, force=True)
            return False

        trading = bool(self.storage.get("trading_enabled", False))
        auto = bool(self.storage.get("auto_signal_enabled", True))
        connected = bool(self.storage.get("toobit_connected", False))
        slot = self._reserve_real_slot() if trading and auto and connected else None
        is_real = slot is not None
        mode = "real" if is_real else "virtual"
        data = {
            "symbol_id": sig.symbol_id,
            "okx_symbol": sig.okx_symbol,
            "toobit_symbol": sig.toobit_symbol,
            "side": sig.side,
            "strength": sig.strength,
            "entry": risk.entry,
            "tp": risk.tp,
            "sl": risk.sl,
            "rr": risk.rr_net,
            "trade_mode": mode,
            "status": "pending" if is_real else "open",
            "is_real": is_real,
            "slot_id": slot,
            "message_id": None,
            "created_at": int(time.time()),
            "opened_at": None,
            "entry_real": None,
            "trade_usdt": trade_usdt,
            "leverage": leverage,
            "notional": risk.notional,
            "order_id": None,
            "raw": {
                "direction": sig.direction_reason,
                "strength": sig.strength_reason,
                "entry": sig.entry_reason,
                "risk": risk.reason,
            },
        }
        signal_id = self.storage.create_signal(data)
        msg_id = self.telegram.send_message(self._signal_message(signal_id, sig, risk, mode, trade_usdt, leverage))
        if msg_id:
            self.storage.update_signal(signal_id, message_id=msg_id)

        if is_real:
            try:
                result = self.toobit.open_futures_position_with_tpsl(
                    sig.toobit_symbol,
                    sig.side,
                    trade_usdt,
                    leverage,
                    risk.entry,
                    risk.tp,
                    risk.sl,
                    f"uem5m_{signal_id}_{int(time.time())}",
                )
                self.storage.update_signal(signal_id, order_id=result.get("order_id"))
                threading.Thread(
                    target=self._check_pending_real,
                    args=(signal_id,),
                    daemon=True,
                    name=f"real-check-{sig.symbol_id}",
                ).start()
            except Exception as exc:
                self.storage.update_signal(
                    signal_id,
                    status="open",
                    is_real=0,
                    trade_mode="virtual",
                    slot_id=None,
                    close_reason="REAL_OPEN_FAILED_TO_VIRTUAL",
                )
                self.storage.add_health_event("toobit_order", "warning", str(exc), sig.symbol_id)
                self.telegram.send_message(
                    f"⚠️ سفارش واقعی سیگنال #{signal_id} باز نشد و همان سیگنال به حالت عادی منتقل شد.\nخطا: {exc}",
                    reply_to_message_id=msg_id,
                )
                logger.exception("بازکردن سفارش واقعی ناموفق | signal=%s symbol=%s", signal_id, sig.symbol_id)

        logger.info("سیگنال | id=%s | ارز=%s | جهت=%s | حالت=%s", signal_id, sig.symbol_id, sig.side, mode)
        return True

    def _check_pending_real(self, signal_id: int) -> None:
        time.sleep(config.ORDER_OPEN_CHECK_SECONDS)
        try:
            state = self.monitor.reconcile_pending_real(signal_id)
            sig = self.storage.get_signal(signal_id) or {}
            message_id = sig.get("message_id")
            if state == "opened":
                self.telegram.send_message(f"✅ پوزیشن واقعی سیگنال #{signal_id} در توبیت تأیید شد.", reply_to_message_id=message_id)
            elif state == "not_found":
                self.storage.add_health_event(
                    "toobit_position",
                    "warning",
                    "نه پوزیشن باز و نه نتیجه قطعی پیدا شد؛ وضعیت Pending و بررسی ادامه دارد",
                    sig.get("symbol_id"),
                )
                self.telegram.send_message(
                    f"⚠️ وضعیت سفارش واقعی #{signal_id} هنوز قطعی نیست؛ بررسی توبیت ادامه دارد.",
                    reply_to_message_id=message_id,
                )
        except Exception as exc:
            sig = self.storage.get_signal(signal_id) or {}
            self.storage.add_health_event("toobit_position", "warning", f"pending check failed: {exc}", sig.get("symbol_id"))
            logger.exception("بررسی سفارش Pending ناموفق | signal=%s", signal_id)

    def scan_once(self) -> None:
        for sym in SYMBOLS:
            if self.stop_event.is_set():
                return
            eligible, reason, eligibility_metrics = self._eligibility(sym)
            if not eligible:
                self._reject("eligibility", sym.id, reason, eligibility_metrics)
                continue
            try:
                candles = self.okx.get_candles(sym.okx, bar=config.OKX_PRIMARY_BAR, limit=config.OKX_CANDLE_LIMIT)
                snapshot = self.okx.get_micro_snapshot(sym.okx)
                signal, reject_reason, metrics = analyze_market_diagnostic(sym, candles, snapshot, self.states[sym.id])
                self.health.mark("okx")
                if signal:
                    self.publish_signal(signal)
                else:
                    self._reject("behavior", sym.id, reject_reason, metrics)
                self.storage.clear_health_component("okx", sym.id)
            except Exception as exc:
                message = str(exc)
                self._reject("data-error", sym.id, message, {}, force=True)
                logger.exception("خطای اسکن | ارز=%s", sym.id)
                global_error = any(x in message.lower() for x in ("connection", "timeout", "http 5"))
                if global_error:
                    self.storage.add_health_event("okx", "warning", message)
                else:
                    self.storage.blacklist(sym.id, message, config.SYMBOL_ERROR_BLACKLIST_SECONDS)
        self.health.mark("signal")

    def signal_loop(self) -> None:
        while not self.stop_event.is_set():
            start = time.time()
            self.scan_once()
            self.stop_event.wait(max(0.2, config.SCAN_INTERVAL_SECONDS - (time.time() - start)))

    def monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            self.monitor.run_once()
            self.stop_event.wait(config.MONITOR_INTERVAL_SECONDS)

    def toobit_status_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                bal = self.toobit.get_futures_balance()
                self.storage.set("toobit_connected", True)
                self.storage.set("toobit_available_usdt", bal["available"])
                self.storage.set("toobit_total_usdt", bal["total"])
                self.storage.set("toobit_margin_usdt", bal["margin"])
                self.storage.set("toobit_last_error", "")
                self.storage.set("toobit_last_update", int(time.time()))
                self.storage.clear_health_component("toobit")
                self.health.mark("toobit")
            except Exception as exc:
                self.storage.set("toobit_connected", False)
                self.storage.set("toobit_last_error", str(exc))
                self.storage.set("toobit_last_update", int(time.time()))
                self.storage.add_health_event("toobit", "warning", str(exc))
                logger.warning("خطای وضعیت توبیت: %s", exc)
            self.stop_event.wait(config.TOOBIT_STATUS_INTERVAL_SECONDS)

    def telegram_loop(self) -> None:
        while not self.stop_event.is_set():
            self.telegram.poll_once()
            self.stop_event.wait(config.TELEGRAM_POLL_SECONDS)

    def run(self) -> None:
        threads = [
            threading.Thread(target=self.signal_loop, daemon=True, name="behavior-scan"),
            threading.Thread(target=self.monitor_loop, daemon=True, name="monitor"),
            threading.Thread(target=self.toobit_status_loop, daemon=True, name="toobit-status"),
            threading.Thread(target=self.telegram_loop, daemon=True, name="telegram"),
        ]
        for thread in threads:
            thread.start()
        logger.info("UEM 5m started | symbols=%d | watch=removed", len(SYMBOLS))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop_event.set()
            for thread in threads:
                thread.join(timeout=3)


if __name__ == "__main__":
    TradingBotApp().run()
