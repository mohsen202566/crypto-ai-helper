from __future__ import annotations

import asyncio
import logging

from telegram.ext import Application, MessageHandler, filters

from ai_controller import AIController, AnalysisInput
from bot_ui import BotUI
from config import FULL_SCAN_SECONDS, MARKET_CONTEXT_SYMBOLS, MONITOR_INTERVAL_SECONDS, TELEGRAM_BOT_TOKEN, TIMEFRAME_1H, TIMEFRAMES, WATCH_SCAN_SECONDS, ensure_runtime_config
from logger_setup import setup_logging
from monitor import SignalMonitor
from okx_data import OkxDataClient
from storage import Storage
from symbol_health import SymbolHealth
from symbols import SYMBOLS, MarketSymbol
from toobit_client import get_client
from trade_manager import TradeManager
from watch_engine import WatchEngine

LOGGER = logging.getLogger("ai_helper_hunter")
SYMBOL_BY_NAME = {symbol.name: symbol for symbol in SYMBOLS}


async def load_market_cache(okx: OkxDataClient) -> dict[str, list]:
    cache: dict[str, list] = {}
    for inst_id in MARKET_CONTEXT_SYMBOLS:
        try:
            cache[inst_id] = await asyncio.to_thread(okx.get_candles, inst_id, TIMEFRAME_1H)
        except Exception as exc:
            LOGGER.warning("market context error for %s: %s", inst_id, exc)
    return cache


async def analyze_symbol(okx: OkxDataClient, controller: AIController, symbol: MarketSymbol, market_cache: dict[str, list], watch_mode: bool = False):
    candles_by_tf = await asyncio.to_thread(okx.get_multi_timeframe, symbol.okx_inst_id, TIMEFRAMES)
    return controller.analyze(AnalysisInput(symbol_name=symbol.name, candles_by_tf=candles_by_tf, btc_1h=market_cache.get(MARKET_CONTEXT_SYMBOLS[0]), eth_1h=market_cache.get(MARKET_CONTEXT_SYMBOLS[1]), watch_mode=watch_mode))


async def scanner_loop(okx: OkxDataClient, controller: AIController, trade_manager: TradeManager, watch_engine: WatchEngine, health: SymbolHealth, ui: BotUI) -> None:
    while True:
        try:
            market_cache = await load_market_cache(okx)
            signal_items = []
            for symbol in SYMBOLS:
                if not health.okx_enabled(symbol.name):
                    continue
                try:
                    decision = await analyze_symbol(okx, controller, symbol, market_cache)
                    health.record_okx_success(symbol.name)
                    if decision.action == "WATCH":
                        watch_engine.register_watch(symbol, decision)
                        if decision.direction and watch_engine.should_send_ready(symbol.name, decision.direction, decision):
                            await ui.send_ready_alert(symbol_name=symbol.name, direction=decision.direction)
                            watch_engine.mark_ready_sent(symbol.name, decision.direction)
                    elif decision.accepted:
                        signal_items.append((symbol, decision))
                    else:
                        health.storage.record_rejection(symbol.name, decision.direction, decision.reject_code, decision.reason, decision.score)
                except Exception as exc:
                    health.record_okx_error(symbol.name, str(exc))
                    LOGGER.warning("scan error for %s: %s", symbol.name, exc)
            created = await trade_manager.create_signals_batch(signal_items)
            for symbol, decision, created_item in created:
                await ui.send_signal(symbol_name=symbol.name, decision=decision, created=created_item)
                if decision.direction:
                    watch_engine.remove_watch(symbol.name, decision.direction)
        except Exception as exc:
            LOGGER.warning("scanner loop error: %s", exc)
        await asyncio.sleep(FULL_SCAN_SECONDS)


async def watch_loop(okx: OkxDataClient, controller: AIController, trade_manager: TradeManager, watch_engine: WatchEngine, health: SymbolHealth, ui: BotUI) -> None:
    while True:
        try:
            market_cache = await load_market_cache(okx)
            signal_items = []
            for watch in watch_engine.active_watches():
                symbol = SYMBOL_BY_NAME.get(str(watch["symbol_name"]))
                if symbol is None or not health.okx_enabled(symbol.name):
                    continue
                try:
                    decision = await analyze_symbol(okx, controller, symbol, market_cache, watch_mode=True)
                    health.record_okx_success(symbol.name)
                    if decision.action == "WATCH":
                        watch_engine.register_watch(symbol, decision)
                        if decision.direction and watch_engine.should_send_ready(symbol.name, decision.direction, decision):
                            await ui.send_ready_alert(symbol_name=symbol.name, direction=decision.direction)
                            watch_engine.mark_ready_sent(symbol.name, decision.direction)
                    elif decision.accepted:
                        signal_items.append((symbol, decision))
                    else:
                        watch_engine.remove_watch(symbol.name, str(watch["direction"]))
                except Exception as exc:
                    health.record_okx_error(symbol.name, str(exc))
                    LOGGER.warning("watch scan error for %s: %s", symbol.name, exc)
            created = await trade_manager.create_signals_batch(signal_items)
            for symbol, decision, created_item in created:
                await ui.send_signal(symbol_name=symbol.name, decision=decision, created=created_item)
                if decision.direction:
                    watch_engine.remove_watch(symbol.name, decision.direction)
        except Exception as exc:
            LOGGER.warning("watch loop error: %s", exc)
        await asyncio.sleep(WATCH_SCAN_SECONDS)


async def monitor_loop(monitor: SignalMonitor, ui: BotUI) -> None:
    while True:
        try:
            await monitor.check_once(ui.send_result)
        except Exception as exc:
            LOGGER.warning("monitor error: %s", exc)
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


def main() -> None:
    setup_logging()
    ensure_runtime_config()
    storage = Storage()
    health = SymbolHealth(storage)
    okx = OkxDataClient()
    controller = AIController(storage)
    toobit = get_client()
    trade_manager = TradeManager(storage, toobit, health)
    watch_engine = WatchEngine(storage)
    ui = BotUI(storage, trade_manager)
    monitor = SignalMonitor(storage, okx, toobit)

    async def post_init(app: Application) -> None:
        ui.bind_app(app)
        asyncio.create_task(scanner_loop(okx, controller, trade_manager, watch_engine, health, ui))
        asyncio.create_task(watch_loop(okx, controller, trade_manager, watch_engine, health, ui))
        asyncio.create_task(monitor_loop(monitor, ui))

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT, ui.handle_text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
