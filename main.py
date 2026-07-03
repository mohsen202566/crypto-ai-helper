from __future__ import annotations

import asyncio

from telegram.ext import ApplicationBuilder, MessageHandler, filters

import config
from ai_brain import AIBrain
from historical_replay import HistoricalReplayEngine
from monitor import SignalMonitor
from okx_data import OkxDataClient
from storage import Storage
from symbols import ACTIVE_SYMBOLS, CONTEXT_SYMBOLS
from telegram_bot import TelegramBotUI
from toobit_client import ToobitClient
from trade_manager import TradeManager
from utils import logger, now_utc


async def scanner_loop(storage: Storage, okx: OkxDataClient, ai: AIBrain, trade_manager: TradeManager, ui: TelegramBotUI) -> None:
    while True:
        checked = created = rejected = errors = 0
        try:
            btc_tfs = okx.get_multi_timeframe(CONTEXT_SYMBOLS[0].okx_inst_id)
            eth_tfs = okx.get_multi_timeframe(CONTEXT_SYMBOLS[1].okx_inst_id)
        except Exception as exc:
            logger.warning("context error: %s", exc)
            btc_tfs = eth_tfs = None
        if storage.auto_signals_enabled():
            for symbol in ACTIVE_SYMBOLS:
                checked += 1
                try:
                    candles = okx.get_multi_timeframe(symbol.okx_inst_id)
                    decision = ai.analyze(symbol=symbol, candles_by_tf=candles, btc_tfs=btc_tfs, eth_tfs=eth_tfs, trade_usdt=storage.trade_usdt())
                    result = await trade_manager.create_signal(symbol, decision)
                    if result:
                        _, created_signal = result
                        await ui.send_signal(decision=decision, created=created_signal)
                        created += 1
                    else:
                        rejected += 1
                except Exception as exc:
                    errors += 1
                    storage.record_no_signal(symbol.name, f"خطای اسکن: {exc}")
                    logger.warning("scan error %s: %s", symbol.name, exc)
        storage.set_scan_info({"time": now_utc().isoformat(timespec="seconds"), "checked": checked, "created": created, "rejected": rejected, "errors": errors})
        await asyncio.sleep(config.SCANNER_SECONDS)


async def monitor_loop(monitor: SignalMonitor) -> None:
    while True:
        await monitor.run_once()
        await asyncio.sleep(config.MONITOR_SECONDS)


async def replay_loop(replay: HistoricalReplayEngine) -> None:
    if config.RUN_REPLAY_ON_START:
        await replay.run_daily_replay()
    while True:
        await asyncio.sleep(config.REPLAY_REFRESH_HOURS * 3600)
        await replay.run_daily_replay()


async def post_init(app) -> None:
    storage: Storage = app.bot_data["storage"]
    okx: OkxDataClient = app.bot_data["okx"]
    toobit: ToobitClient = app.bot_data["toobit"]
    ui: TelegramBotUI = app.bot_data["ui"]
    trade_manager: TradeManager = app.bot_data["trade_manager"]
    ai = AIBrain(storage)
    monitor = SignalMonitor(storage, okx, toobit, ui)
    replay = HistoricalReplayEngine(storage, okx)
    app.create_task(scanner_loop(storage, okx, ai, trade_manager, ui))
    app.create_task(monitor_loop(monitor))
    app.create_task(replay_loop(replay))


def main() -> None:
    config.ensure_runtime_config()
    storage = Storage()
    okx = OkxDataClient()
    toobit = ToobitClient()
    trade_manager = TradeManager(storage, toobit)
    ui = TelegramBotUI(storage, trade_manager)
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    ui.bind_app(app)
    app.bot_data.update({"storage": storage, "okx": okx, "toobit": toobit, "ui": ui, "trade_manager": trade_manager})
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ui.handle_text))
    app.add_handler(MessageHandler(filters.COMMAND, ui.handle_text))
    logger.info("%s started", config.BOT_NAME)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
