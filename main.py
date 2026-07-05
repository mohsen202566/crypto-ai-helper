from __future__ import annotations

import asyncio
import logging

from telegram.ext import Application, MessageHandler, filters

from ai_brain import AIBrain, AnalysisInput
from config import CONTEXT_SYMBOLS, MONITOR_SECONDS, RUN_REPLAY_ON_START, SCANNER_SECONDS, TELEGRAM_BOT_TOKEN, TIMEFRAME_1H, TIMEFRAMES, ensure_runtime_config
from historical_replay import HistoricalReplayEngine
from monitor import SignalMonitor
from okx_data import OkxDataClient
from storage import Storage
from symbols import ACTIVE_SYMBOLS, MarketSymbol
from telegram_bot import TelegramBotUI
from toobit_client import get_client
from trade_manager import TradeManager

LOGGER = logging.getLogger("ai_range_5m_bot")


async def load_context(okx: OkxDataClient) -> dict[str, list]:
    cache: dict[str, list] = {}
    for inst_id in CONTEXT_SYMBOLS:
        try:
            cache[inst_id] = await asyncio.to_thread(okx.get_candles, inst_id, TIMEFRAME_1H)
        except Exception as exc:
            LOGGER.warning("context error %s: %s", inst_id, exc)
    return cache


async def analyze_symbol(okx: OkxDataClient, brain: AIBrain, symbol: MarketSymbol, context_cache: dict[str, list]):
    candles_task = asyncio.to_thread(okx.get_multi_timeframe, symbol.okx_inst_id, TIMEFRAMES)
    price_task = asyncio.to_thread(okx.get_last_price, symbol.okx_inst_id)
    candles_by_tf, live_price = await asyncio.gather(candles_task, price_task)
    return brain.analyze(AnalysisInput(symbol_name=symbol.name, candles_by_tf=candles_by_tf, btc_1h=context_cache.get(CONTEXT_SYMBOLS[0]), eth_1h=context_cache.get(CONTEXT_SYMBOLS[1]), live_price=live_price))


async def scanner_loop(okx: OkxDataClient, brain: AIBrain, trade_manager: TradeManager, ui: TelegramBotUI, storage: Storage) -> None:
    while True:
        try:
            if not storage.auto_signals_enabled():
                await asyncio.sleep(SCANNER_SECONDS)
                continue
            context_cache = await load_context(okx)
            items = []
            for symbol in ACTIVE_SYMBOLS:
                try:
                    if storage.active_symbol_exists(symbol.toobit_symbol):
                        continue
                    decision = await analyze_symbol(okx, brain, symbol, context_cache)
                    if decision.accepted:
                        items.append((symbol, decision))
                    else:
                        storage.record_no_signal(symbol.name, decision.direction, decision.reason, decision.features_key)
                except Exception as exc:
                    LOGGER.warning("scan error %s: %s", symbol.name, exc)
                    storage.record_no_signal(symbol.name, None, f"خطای اسکن: {exc}", "")
            created = await trade_manager.create_signals_batch(items)
            for symbol, decision, created_signal in created:
                await ui.send_signal(symbol_name=symbol.name, decision=decision, created=created_signal)
        except Exception as exc:
            LOGGER.warning("scanner loop error: %s", exc)
        await asyncio.sleep(SCANNER_SECONDS)


async def monitor_loop(monitor: SignalMonitor, ui: TelegramBotUI) -> None:
    while True:
        try:
            await monitor.check_once(ui.send_result)
        except Exception as exc:
            LOGGER.warning("monitor error: %s", exc)
        await asyncio.sleep(MONITOR_SECONDS)


async def replay_on_start(storage: Storage, okx: OkxDataClient) -> None:
    if not RUN_REPLAY_ON_START:
        return
    replay = HistoricalReplayEngine(storage, okx)
    for symbol in ACTIVE_SYMBOLS:
        try:
            result = await asyncio.to_thread(replay.run_symbol, symbol)
            LOGGER.info("replay %s observations=%s missed=%s", result.symbol_name, result.observations, result.missed)
        except Exception as exc:
            LOGGER.warning("replay error %s: %s", symbol.name, exc)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ensure_runtime_config()
    storage = Storage()
    okx = OkxDataClient()
    toobit = get_client()
    brain = AIBrain(storage)
    trade_manager = TradeManager(storage, toobit)
    ui = TelegramBotUI(storage, trade_manager)
    monitor = SignalMonitor(storage, okx, toobit)

    async def post_init(app: Application) -> None:
        ui.bind_app(app)
        asyncio.create_task(replay_on_start(storage, okx))
        asyncio.create_task(scanner_loop(okx, brain, trade_manager, ui, storage))
        asyncio.create_task(monitor_loop(monitor, ui))

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT, ui.handle_text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
