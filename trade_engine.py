"""Worker مستقل اجرای واقعی؛ هیچ sleep و تایید ۷۰ثانیه‌ای در این مسیر وجود ندارد."""
from __future__ import annotations

import logging
import queue
from typing import Any

import config
from storage import Storage
from toobit_client import ToobitClient
from utils import now_ms

logger = logging.getLogger("adaptive_bot")


class TradeEngine:
    def __init__(
        self,
        storage: Storage,
        toobit: ToobitClient,
        trade_queue: queue.Queue[int],
    ):
        self.storage = storage
        self.toobit = toobit
        self.trade_queue = trade_queue

    def process_one(self, timeout: float = 1.0) -> bool:
        try:
            signal_id = self.trade_queue.get(timeout=timeout)
        except queue.Empty:
            return False
        try:
            self.execute(signal_id)
            return True
        finally:
            self.trade_queue.task_done()

    def execute(self, signal_id: int) -> None:
        signal = self.storage.runtime.get_signal(signal_id)
        if not signal or signal.get("tier") != "REAL" or signal.get("status") != "PENDING_OPEN":
            return
        # Command path has priority: a late «ترید خاموش» blocks the order immediately.
        if not self.storage.runtime.get_setting("real_trade_enabled", False):
            self.storage.runtime.finalize_signal(signal_id, "CANCELLED", None, None, metadata={"reason": "TRADING_DISABLED_BEFORE_SUBMIT"})
            self.storage.runtime.add_event("REAL_CANCELLED", "ترید قبل از ارسال خاموش شد", signal["canonical"])
            return

        if not self.toobit.has_credentials:
            self.storage.runtime.finalize_signal(
                signal_id, "FAILED_OPEN", None, None, metadata={"reason": "TOOBIT_CREDENTIALS_MISSING_BEFORE_SUBMIT"}
            )
            self.storage.runtime.add_event("REAL_FAILED", "کلید Toobit پیش از ارسال موجود نبود", signal["canonical"])
            return

        client_order_id = f"ab{signal_id}{now_ms()}"[-32:]
        mapping_rows = {x["canonical"]: x for x in self.storage.learning.symbols()}
        mapping = mapping_rows.get(signal["canonical"], {})
        symbol_info = {
            "tickSize": mapping.get("tick_size"),
            "stepSize": mapping.get("quantity_step"),
            "minQty": mapping.get("min_qty"),
            "minNotional": mapping.get("min_notional"),
            "contractMultiplier": mapping.get("contract_multiplier", 1.0),
        }
        submitted_at = now_ms()
        confirm_after = submitted_at + config.PENDING_CONFIRM_AFTER_SECONDS * 1000
        # The 70-second confirmation window starts at the actual submit attempt, not
        # when the analytical signal was queued. This prevents a busy queue from
        # shortening the exchange confirmation window.
        self.storage.runtime.update_signal(
            signal_id,
            client_order_id=client_order_id,
            order_submitted_at=submitted_at,
            confirm_after=confirm_after,
        )
        self.storage.runtime.update_position(
            signal_id,
            client_order_id=client_order_id,
            submitted_at=submitted_at,
            confirm_after=confirm_after,
        )
        try:
            response = self.toobit.place_market_order(
                symbol=signal["exchange_symbol"],
                side=signal["side"],
                entry_price=float(signal["entry"]),
                trade_amount_usdt=float(signal["margin_usdt"]),
                leverage=int(signal["leverage"]),
                tp_price=float(signal["tp"]),
                sl_price=float(signal["sl"]),
                client_order_id=client_order_id,
                symbol_info=symbol_info,
            )
            order_id = response.get("order_id")
            self.storage.runtime.update_signal(
                signal_id,
                order_id=order_id,
                client_order_id=client_order_id,
                order_submitted_at=submitted_at,
                order_response=response,
            )
            self.storage.runtime.update_position(
                signal_id,
                order_id=order_id,
                client_order_id=client_order_id,
                submitted_at=submitted_at,
                order_response=response,
            )
            self.storage.runtime.add_event("ORDER_SUBMITTED", "سفارش واقعی با TP/SL ارسال شد", signal["canonical"], response)
        except Exception as exc:
            # Do not release the slot. A timeout can happen after the exchange accepted the order.
            # RealMonitor decides after 70 seconds using a fresh positions read.
            self.storage.runtime.update_signal(
                signal_id,
                order_submit_error=str(exc)[:1000],
                order_submitted_at=submitted_at,
                client_order_id=client_order_id,
            )
            self.storage.runtime.update_position(
                signal_id,
                submit_error=str(exc)[:1000],
                submitted_at=submitted_at,
                client_order_id=client_order_id,
            )
            self.storage.runtime.add_event("ORDER_SUBMIT_ERROR", str(exc), signal["canonical"])
            logger.warning("ORDER_SUBMIT_ERROR | %s | %s", signal["canonical"], str(exc)[:240])
