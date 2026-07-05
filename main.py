from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from config import LOG_DIR, load_settings
from layer1_trend import analyze_trend
from layer2_entry_5m import find_entry
from layer2_power import analyze_power
from okx_client import OKXClient
from signal_manager import Signal, SignalStore
from slot_manager import SlotManager
from stats_manager import StatsManager
from symbols_config import enabled_symbols
from telegram_bot import PersianTelegramBot
from tp_sl_engine import calculate_tp_sl
from trade_executor import TradeExecutor

LOG_FILE = Path(LOG_DIR) / "bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("crypto_ai_helper_1h")


class BotRuntime:
    def __init__(self) -> None:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN تنظیم نشده است.")
        self.settings = load_settings()
        self.okx = OKXClient()
        self.store = SignalStore()
        self.slots = SlotManager(self.store)
        self.executor = TradeExecutor(self.slots)
        self.stats = StatsManager(self.store)
        self.telegram = PersianTelegramBot(token, self.stats)

    async def run(self) -> None:
        await self.telegram.app.initialize()
        await self.telegram.app.start()
        await self.telegram.app.updater.start_polling()
        logger.info("ربات روشن شد.")
        tasks = [
            asyncio.create_task(self.auto_signal_loop(), name="auto_signal_loop"),
            asyncio.create_task(self.result_loop(), name="result_loop"),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await self.telegram.app.updater.stop()
            await self.telegram.app.stop()
            await self.telegram.app.shutdown()

    async def auto_signal_loop(self) -> None:
        """اتو سیگنال همیشه زنده می‌ماند.

        خطای یک نماد، خطای اوکی‌اکس، خطای اندیکاتور، خطای تیپی/استاپ یا خطای توبیت
        نباید کل ربات را بخواباند. هر خطا فقط همان دور/همان نماد را رد می‌کند.
        """
        while True:
            sleep_seconds = 60
            try:
                self.settings = load_settings()
                sleep_seconds = self.settings.scan_interval_seconds
                symbols = enabled_symbols()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("خطا در خواندن تنظیمات یا لیست نمادها؛ ربات ادامه می‌دهد: %s", exc)
                await asyncio.sleep(sleep_seconds)
                continue

            for item in symbols:
                if not self._symbol_enabled(item):
                    continue
                name = str(item.get("name", ""))
                okx_symbol = str(item.get("okx_symbol", ""))
                toobit_symbol = str(item.get("toobit_symbol", ""))
                try:
                    await self.process_symbol(item)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "نماد رد شد و ربات ادامه می‌دهد | نماد: %s | اوکی‌اکس: %s | توبیت: %s | خطا: %s",
                        name,
                        okx_symbol,
                        toobit_symbol,
                        exc,
                    )
                    continue

            await asyncio.sleep(sleep_seconds)

    @staticmethod
    def _symbol_enabled(item: dict[str, Any]) -> bool:
        try:
            return item.get("enabled") is True and bool(item.get("okx_symbol")) and bool(item.get("toobit_symbol"))
        except Exception:
            return False

    async def process_symbol(self, item: dict[str, object]) -> None:
        name = str(item["name"])
        okx_symbol = str(item["okx_symbol"])
        toobit_symbol = str(item["toobit_symbol"]).upper()

        if self.store.has_open_symbol(toobit_symbol):
            return

        candles = await asyncio.to_thread(self._load_candles, okx_symbol)
        trend = analyze_trend(daily=candles["1D"], h4=candles["4H"], h1=candles["1H"])
        if not trend.passed or trend.direction is None:
            return

        power = analyze_power(direction=trend.direction, h1=candles["1H"], m15=candles["15m"])
        if not power.passed:
            return

        entry = find_entry(direction=trend.direction, m5=candles["5m"])
        if not entry.passed:
            return

        tpsl = calculate_tp_sl(
            direction=trend.direction,
            entry=entry.entry_price,
            h1=candles["1H"],
            m15=candles["15m"],
            m5=candles["5m"],
            settings=self.settings,
            strength=power.strength,
        )
        if not tpsl.passed:
            return

        signal_type = "رئال" if self.settings.trade_enabled and self.slots.can_open_real(self.settings.max_positions) else "نرمال"
        signal = Signal.create(
            symbol_name=name,
            okx_symbol=okx_symbol,
            toobit_symbol=toobit_symbol,
            signal_type=signal_type,
            direction=trend.direction,
            entry=tpsl.entry,
            tp=tpsl.tp,
            sl=tpsl.sl,
            estimated_move_percent=tpsl.estimated_move_percent,
            estimated_net_profit=tpsl.estimated_net_profit,
            estimated_hold_time=tpsl.estimated_hold_time,
            rr=tpsl.rr,
            fee_usdt=self.settings.fee_usdt,
        )

        if signal.signal_type == "رئال":
            result = await asyncio.to_thread(
                self.executor.open_real_position,
                signal,
                self.settings.trade_amount_usdt,
                self.settings.leverage,
            )
            if not result.opened:
                logger.warning("پوزیشن رئال باز نشد و نماد رد شد: %s | %s", toobit_symbol, result.reason)
                return

        if not self.settings.telegram_chat_id:
            logger.warning("TELEGRAM_CHAT_ID تنظیم نشده و سیگنال ارسال نشد.")
            return

        sent = await self.telegram.send_signal(self.settings.telegram_chat_id, signal)
        signal.telegram_message_id = sent.message_id
        self.store.add(signal)
        logger.info("سیگنال ارسال شد: %s %s", signal.signal_type, toobit_symbol)

    def _load_candles(self, okx_symbol: str) -> dict[str, list]:
        return {
            "1D": self.okx.get_candles(okx_symbol, "1D", self.settings.candle_limit),
            "4H": self.okx.get_candles(okx_symbol, "4H", self.settings.candle_limit),
            "1H": self.okx.get_candles(okx_symbol, "1H", self.settings.candle_limit),
            "15m": self.okx.get_candles(okx_symbol, "15m", self.settings.candle_limit),
            "5m": self.okx.get_candles(okx_symbol, "5m", self.settings.candle_limit),
        }

    async def result_loop(self) -> None:
        """مانیتور نتیجه‌ها همیشه زنده می‌ماند."""
        while True:
            sleep_seconds = 20
            try:
                self.settings = load_settings()
                sleep_seconds = self.settings.result_interval_seconds

                from result_monitor import ResultMonitor

                monitor = ResultMonitor(self.store, self.okx, self.settings)
                events = await asyncio.to_thread(monitor.check_once)
                for event in events:
                    try:
                        if self.settings.telegram_chat_id and event.signal.telegram_message_id:
                            await self.telegram.send_result_reply(self.settings.telegram_chat_id, event.signal, event.is_tp)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.exception("خطا در ارسال نتیجه؛ مانیتور ادامه می‌دهد: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("خطا در مانیتور نتیجه‌ها؛ ربات ادامه می‌دهد: %s", exc)

            await asyncio.sleep(sleep_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(BotRuntime().run())
    except KeyboardInterrupt:
        pass
