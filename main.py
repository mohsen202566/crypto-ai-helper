from __future__ import annotations

import asyncio
import logging

from telegram.ext import Application, MessageHandler, filters

from bot_ui import BotUI
from config import SCAN_INTERVAL_SECONDS, MONITOR_INTERVAL_SECONDS, SYMBOLS, TELEGRAM_BOT_TOKEN, ensure_runtime_config
from indicators import calculate_indicators
from monitor import SignalMonitor
from okx_data import OkxDataClient
from scorer import TechnicalScorer
from storage import Storage
from toobit_client import get_client
from trade_manager import TradeManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("scalp5")


async def scanner_loop(okx: OkxDataClient, scorer: TechnicalScorer, trade_manager: TradeManager, ui: BotUI) -> None:
    while True:
        for symbol in SYMBOLS:
            try:
                candles = await asyncio.to_thread(okx.get_candles, symbol.okx_inst_id)
                indicator = calculate_indicators(candles)
                entry = candles[-1].close
                decision = scorer.score(indicator, entry)
                if not decision.accepted:
                    continue
                created = await trade_manager.create_signal(symbol, decision)
                await ui.send_signal(symbol_name=symbol.name, decision=decision, created=created)
            except Exception as exc:
                LOGGER.warning("scan error for %s: %s", symbol.name, exc)
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
    scorer = TechnicalScorer()
    toobit = get_client()
    trade_manager = TradeManager(storage, toobit)
    ui = BotUI(storage, trade_manager)
    monitor = SignalMonitor(storage, okx, toobit)

    async def post_init(app: Application) -> None:
        ui.bind_app(app)
        asyncio.create_task(scanner_loop(okx, scorer, trade_manager, ui))
        asyncio.create_task(monitor_loop(monitor, ui))

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT, ui.handle_text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
