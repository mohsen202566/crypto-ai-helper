"""نقطه شروع Crypto AI Helper Spot Hunter."""
from __future__ import annotations

import asyncio
import signal

import config
from okx_client import OkxClient
from order_manager import OrderManager
from scanner import MarketScanner
from storage import JsonStorage
from strategy import SpotLongStrategy
from telegram_bot import TelegramBot
from toobit_client import ToobitClient
from trade_manager import TradeManager
from utils import logger, single_instance_lock


async def async_main() -> None:
    storage = JsonStorage()
    okx = OkxClient()
    toobit = ToobitClient()
    strategy = SpotLongStrategy(storage.settings)
    order_manager = OrderManager(toobit, storage.settings)
    trade_manager = TradeManager(storage, okx, order_manager)
    scanner = MarketScanner(storage, okx, strategy, trade_manager)
    telegram = TelegramBot(storage, okx, toobit, trade_manager, scanner)

    tasks: list[asyncio.Task] = []

    async def start_background() -> None:
        tasks.append(asyncio.create_task(scanner.loop(telegram.send_message, telegram.send_reply), name="scanner"))
        tasks.append(asyncio.create_task(trade_manager.normal_monitor_loop(telegram.send_reply), name="normal_monitor"))
        tasks.append(asyncio.create_task(trade_manager.real_history_loop(telegram.send_reply), name="real_history"))

    await start_background()
    try:
        await telegram.run()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    with single_instance_lock(config.LOCK_FILE):
        logger.info("شروع Crypto AI Helper Spot Hunter")
        try:
            asyncio.run(async_main())
        except KeyboardInterrupt:
            logger.info("ربات با KeyboardInterrupt متوقف شد")


if __name__ == "__main__":
    main()
