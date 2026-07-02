"""اسکنر بازار برای پیدا کردن سیگنال‌های اسپات لانگ."""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import config
from okx_client import OkxClient
from storage import JsonStorage
from strategy import SpotLongStrategy
from trade_manager import TradeManager
from utils import logger

SendMessage = Callable[[str], Awaitable[int | None]]
SendReply = Callable[[int | None, str], Awaitable[int | None]]


class MarketScanner:
    def __init__(self, storage: JsonStorage, okx: OkxClient, strategy: SpotLongStrategy, trade_manager: TradeManager):
        self.storage = storage
        self.okx = okx
        self.strategy = strategy
        self.trade_manager = trade_manager
        self._running = True

    def active_symbols(self) -> list[str]:
        count = max(config.MIN_ACTIVE_SYMBOL_COUNT, min(int(self.storage.settings.active_symbol_count), config.MAX_ACTIVE_SYMBOL_COUNT))
        return config.SAFE_SYMBOLS[:count]

    async def scan_once(self, send_message: SendMessage, send_reply: SendReply) -> None:
        self.strategy.update_settings(self.storage.settings)
        for base in self.active_symbols():
            if self.storage.has_active_symbol(base):
                continue
            try:
                pack = await asyncio.to_thread(self.okx.get_market_pack, base)
                signal = self.strategy.evaluate(pack)
                if signal is None:
                    continue
                await self.trade_manager.handle_new_signal(signal, send_message, send_reply)
            except Exception as exc:
                logger.warning("اسکن %s ناموفق بود: %s", base, exc)
            await asyncio.sleep(0.2)

    async def loop(self, send_message: SendMessage, send_reply: SendReply) -> None:
        while self._running:
            await self.scan_once(send_message, send_reply)
            await asyncio.sleep(config.SCAN_INTERVAL_SECONDS)
