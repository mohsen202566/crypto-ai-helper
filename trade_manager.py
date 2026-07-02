"""مدیر اصلی سیگنال‌ها و معاملات.

قانون طلایی:
- real  => فقط Toobit
- normal => فقط OKX
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import config
from messages_fa import (
    format_buy_confirm,
    format_normal_result,
    format_real_open_failed_to_normal,
    format_real_result,
    format_signal,
)
from models import Signal
from okx_client import OkxClient
from order_manager import OrderManager
from storage import JsonStorage
from utils import estimate_round_trip_fee, logger, net_profit_estimate, pct_change

SendMessage = Callable[[str], Awaitable[int | None]]
SendReply = Callable[[int | None, str], Awaitable[int | None]]


class TradeManager:
    def __init__(self, storage: JsonStorage, okx: OkxClient, order_manager: OrderManager):
        self.storage = storage
        self.okx = okx
        self.order_manager = order_manager
        self._normal_lock = asyncio.Lock()
        self._real_lock = asyncio.Lock()

    def refresh_settings(self) -> None:
        self.order_manager.update_settings(self.storage.settings)

    async def handle_new_signal(self, signal: Signal, send_message: SendMessage, send_reply: SendReply) -> None:
        self.refresh_settings()
        if self.storage.has_active_symbol(signal.base_symbol):
            logger.info("سیگنال %s رد شد چون از این ارز سیگنال باز داریم", signal.base_symbol)
            return

        settings = self.storage.settings
        can_real = settings.trading_enabled and self.storage.free_real_slots() > 0
        if can_real:
            signal.execution_mode = config.MODE_REAL
            signal.status = config.STATUS_PENDING_BUY
        else:
            signal.execution_mode = config.MODE_NORMAL
            signal.status = config.STATUS_NORMAL_OPEN

        msg_id = await send_message(format_signal(signal, settings))
        signal.telegram_message_id = msg_id
        self.storage.add_signal(signal)

        if signal.execution_mode == config.MODE_NORMAL:
            return

        # اجرای واقعی Toobit؛ اگر نشد، تبدیل به عادی OKX می‌شود.
        result = await self.order_manager.open_real_spot_position(signal)
        fresh = self.storage.get_signal(signal.id)
        if not fresh:
            return

        if not result.opened:
            fresh.execution_mode = config.MODE_NORMAL
            fresh.status = config.STATUS_NORMAL_OPEN
            fresh.raw.update({"real_open_failed": result.raw, "real_open_failed_reason": result.reason})
            self.storage.update_signal(fresh)
            await send_reply(fresh.telegram_message_id, format_real_open_failed_to_normal(fresh, result.reason))
            return

        fresh.status = config.STATUS_REAL_OPEN
        fresh.buy_order_id = result.buy_order_id
        fresh.sell_order_id = result.sell_order_id
        fresh.avg_buy_price = result.avg_buy_price
        fresh.entry_price = result.avg_buy_price or fresh.entry_price
        fresh.target_price = result.target_price or fresh.target_price
        fresh.filled_qty = result.filled_qty
        fresh.buy_fee_usdt = result.buy_fee_usdt
        fresh.raw.update({"real_open": result.raw})
        self.storage.update_signal(fresh)
        await send_reply(fresh.telegram_message_id, format_buy_confirm(fresh))

    async def monitor_normal_signals(self, send_reply: SendReply) -> None:
        async with self._normal_lock:
            signals = self.storage.normal_open_signals()
            if not signals:
                return
            settings = self.storage.settings
            for sig in signals:
                try:
                    price = await asyncio.to_thread(self.okx.get_ticker_price, sig.base_symbol)
                    if price <= 0:
                        continue
                    move = pct_change(sig.entry_price, price)
                    if move + 1e-9 < sig.target_percent:
                        continue

                    gross = sig.amount_usdt * move / 100.0
                    fee = estimate_round_trip_fee(
                        sig.amount_usdt,
                        move,
                        settings.taker_fee_pct,
                        settings.maker_fee_pct,
                    )
                    net = gross - fee
                    closed = self.storage.close_signal(
                        sig.id,
                        close_price=price,
                        move_percent=move,
                        gross_profit_usdt=gross,
                        fee_usdt=fee,
                        net_profit_usdt=net,
                        close_reason="قیمت OKX به درصد حرکت هدف رسید",
                        raw={"okx_close_price": price},
                    )
                    if closed:
                        await send_reply(closed.telegram_message_id, format_normal_result(closed))
                except Exception as exc:
                    logger.warning("مانیتور سیگنال عادی %s ناموفق بود: %s", sig.base_symbol, exc)

    async def monitor_real_orders(self, send_reply: SendReply) -> None:
        async with self._real_lock:
            signals = self.storage.real_open_signals()
            if not signals:
                return
            for sig in signals:
                try:
                    result = await self.order_manager.check_real_close(sig)
                    if not result.closed:
                        continue
                    closed = self.storage.close_signal(
                        sig.id,
                        close_price=result.close_price or 0.0,
                        move_percent=result.move_percent,
                        gross_profit_usdt=result.gross_profit_usdt,
                        fee_usdt=result.fee_usdt,
                        net_profit_usdt=result.net_profit_usdt,
                        close_reason=result.reason,
                        raw={"real_close": result.raw},
                    )
                    if closed:
                        closed.sell_fee_usdt = result.sell_fee_usdt
                        self.storage.update_signal(closed)
                        await send_reply(closed.telegram_message_id, format_real_result(closed))
                except Exception as exc:
                    logger.warning("چک پوزیشن واقعی %s ناموفق بود: %s", sig.base_symbol, exc)

    async def normal_monitor_loop(self, send_reply: SendReply) -> None:
        while True:
            await self.monitor_normal_signals(send_reply)
            await asyncio.sleep(config.NORMAL_MONITOR_INTERVAL_SECONDS)

    async def real_history_loop(self, send_reply: SendReply) -> None:
        while True:
            minutes = max(config.MIN_HISTORY_CHECK_MINUTES, int(self.storage.settings.history_check_minutes))
            await self.monitor_real_orders(send_reply)
            await asyncio.sleep(minutes * 60)
