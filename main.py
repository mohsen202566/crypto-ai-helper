from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from telegram.ext import Application, MessageHandler, filters

from ai_brain import AIBrain, AnalysisInput, SignalDecision
from config import CONTEXT_SYMBOLS, MONITOR_SECONDS, RUN_REPLAY_ON_START, SCANNER_SECONDS, SESSION_CAUTION_MIN_CONFIDENCE, TELEGRAM_BOT_TOKEN, TIMEFRAME_1H, TIMEFRAMES, ensure_runtime_config
from fundamental_guard import FundamentalGuard
from historical_replay import HistoricalReplayEngine
from monitor import SignalMonitor
from okx_data import OkxDataClient
from risk_guard import RiskGuard
from session_guard import GuardDecision, SessionGuard
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


def apply_guards(decision: SignalDecision, guards: list[GuardDecision]) -> SignalDecision:
    active = [g for g in guards if g.level != "OK"]
    if not active:
        return decision
    reason_suffix = " | ".join(g.reason for g in active if g.reason)
    if any(g.hard_block or not g.normal_allowed for g in active):
        return replace(
            decision,
            action="NO_SIGNAL",
            accepted=False,
            real_allowed=False,
            signal_type_hint="none",
            reason=f"{decision.reason} | {reason_suffix}"[:1800],
        )
    penalty = sum(max(0, g.confidence_penalty) for g in active)
    confidence = max(0, int(decision.confidence) - penalty)
    real_allowed = bool(decision.real_allowed and all(g.real_allowed for g in active))
    if any(g.caution for g in active) and confidence < SESSION_CAUTION_MIN_CONFIDENCE:
        return replace(
            decision,
            action="NO_SIGNAL",
            accepted=False,
            real_allowed=False,
            signal_type_hint="none",
            confidence=confidence,
            reason=f"{decision.reason} | GUARD_REJECT_LOW_CONFIDENCE: اعتماد بعد از گارد {confidence}% شد. | {reason_suffix}"[:1800],
        )
    return replace(
        decision,
        real_allowed=real_allowed,
        signal_type_hint="real" if real_allowed else "normal",
        confidence=confidence,
        reason=f"{decision.reason} | {reason_suffix}"[:1800],
    )


async def scanner_loop(okx: OkxDataClient, brain: AIBrain, trade_manager: TradeManager, ui: TelegramBotUI, storage: Storage, session_guard: SessionGuard, risk_guard: RiskGuard, fundamental_guard: FundamentalGuard) -> None:
    while True:
        try:
            fundamental_guard.refresh_if_due()
            await fundamental_guard.send_due_alerts(ui)
            if not storage.auto_signals_enabled():
                await asyncio.sleep(SCANNER_SECONDS)
                continue

            fundamental_decision = fundamental_guard.evaluate()
            session_decision = session_guard.evaluate()
            global_risk_decision = risk_guard.evaluate()
            global_guards = [fundamental_decision, session_decision, global_risk_decision]
            if any(g.hard_block for g in global_guards):
                reason = " | ".join(g.reason for g in global_guards if g.level != "OK")
                storage.record_no_signal("ALL", None, f"GLOBAL_GUARD_BLOCK: {reason}", "")
                await asyncio.sleep(SCANNER_SECONDS)
                continue

            context_cache = await load_context(okx)
            items = []
            for symbol in ACTIVE_SYMBOLS:
                try:
                    if storage.active_symbol_exists(symbol.toobit_symbol):
                        continue
                    decision = await analyze_symbol(okx, brain, symbol, context_cache)
                    direction = decision.direction if decision.direction else None
                    specific_risk = GuardDecision.ok("risk")
                    if direction and global_risk_decision.level == "OK":
                        specific_risk = risk_guard.evaluate(symbol.name, direction)
                    guarded = apply_guards(decision, [fundamental_decision, session_decision, global_risk_decision, specific_risk])
                    if guarded.accepted:
                        items.append((symbol, guarded))
                    else:
                        storage.record_no_signal(symbol.name, guarded.direction, guarded.reason, guarded.features_key)
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
    session_guard = SessionGuard()
    risk_guard = RiskGuard(storage)
    fundamental_guard = FundamentalGuard(storage)

    async def post_init(app: Application) -> None:
        ui.bind_app(app)
        asyncio.create_task(replay_on_start(storage, okx))
        asyncio.create_task(scanner_loop(okx, brain, trade_manager, ui, storage, session_guard, risk_guard, fundamental_guard))
        asyncio.create_task(monitor_loop(monitor, ui))

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT, ui.handle_text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
