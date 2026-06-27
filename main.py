from __future__ import annotations

import asyncio
import logging

from telegram.ext import Application, MessageHandler, filters

from ai_controller import AIController, AnalysisInput
from bot_ui import BotUI
from config import (
    MARKET_CONTEXT_SYMBOLS,
    MONITOR_INTERVAL_SECONDS,
    SCAN_INTERVAL_SECONDS,
    TELEGRAM_BOT_TOKEN,
    TIMEFRAME_1H,
    TIMEFRAMES,
    ensure_runtime_config,
)
from monitor import SignalMonitor
from okx_data import OkxDataClient
from storage import Storage
from symbols import SYMBOLS
from toobit_client import get_client
from trade_manager import TradeManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("mtf_ai_bot")


async def scanner_loop(okx: OkxDataClient, controller: AIController, trade_manager: TradeManager, ui: BotUI) -> None:
    market_cache: dict[str, list] = {}
    while True:
        try:
            market_cache = {}
            for inst_id in MARKET_CONTEXT_SYMBOLS:
                try:
                    market_cache[inst_id] = await asyncio.to_thread(okx.get_candles, inst_id, TIMEFRAME_1H)
                except Exception as exc:
                    LOGGER.warning("market context error for %s: %s", inst_id, exc)
            for symbol in SYMBOLS:
                try:
                    candles_by_tf = await asyncio.to_thread(okx.get_multi_timeframe, symbol.okx_inst_id, TIMEFRAMES)
                    decision = controller.analyze(
                        AnalysisInput(
                            symbol_name=symbol.name,
                            candles_by_tf=candles_by_tf,
                            btc_1h=market_cache.get(MARKET_CONTEXT_SYMBOLS[0]),
                            eth_1h=market_cache.get(MARKET_CONTEXT_SYMBOLS[1]),
                        )
                    )
                    if not decision.accepted:
                        continue
                    created = await trade_manager.create_signal(symbol, decision)
                    if created is None:
                        continue
                    await ui.send_signal(symbol_name=symbol.name, decision=decision, created=created)
                except Exception as exc:
                    LOGGER.warning("scan error for %s: %s", symbol.name, exc)
        except Exception as exc:
            LOGGER.warning("scanner loop error: %s", exc)
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


async def monitor_loop(monitor: SignalMonitor, ui: BotUI) -> None:
    while True:
        try:
            await monitor.check_once(ui.send_result)
        except Exception as exc:
            LOGGER.warning("monitor error: %s", exc)
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


def main() -> None:
    ensure_runtime_config()
    storage = Storage()
    okx = OkxDataClient()
    controller = AIController()
    toobit = get_client()
    trade_manager = TradeManager(storage, toobit)
    ui = BotUI(storage, trade_manager)
    monitor = SignalMonitor(storage, okx, toobit)

    async def post_init(app: Application) -> None:
        ui.bind_app(app)
        asyncio.create_task(scanner_loop(okx, controller, trade_manager, ui))
        asyncio.create_task(monitor_loop(monitor, ui))

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT, ui.handle_text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
