"""اجرای اصلی ربات اسکالپ کلاسیک ۵ دقیقه‌ای.

تحلیل از OKX گرفته می‌شود و اجرای واقعی، در صورت روشن بودن ترید، روی Toobit انجام می‌شود.
"""
from __future__ import annotations

import signal
import threading
import time
from typing import Any

import config
from indicators import calculate_indicators
from messages_fa import normal_result_message, real_result_message, signal_message
from okx_client import OKXClient
from stats_manager import StatsManager
from storage import JSONStorage
from strategy import ClassicScalpingStrategy
from telegram_bot import TelegramBotService
from toobit_client import ToobitClient
from trade_manager import TradeManager
from utils import logger, now_utc_iso, safe_sleep


class FiveMinuteScalperBot:
    def __init__(self):
        self.storage = JSONStorage()
        self.okx = OKXClient()
        self.toobit = ToobitClient()
        self.stats = StatsManager(self.storage)
        self.strategy = ClassicScalpingStrategy()
        self.trade_manager = TradeManager(self.storage, self.stats, self.toobit)
        self.telegram = TelegramBotService(self.storage, self.trade_manager, self.stats)
        self.stop_event = threading.Event()
        self.valid_symbols: dict[str, dict[str, Any]] = {}
        self.last_signal_ts: dict[str, float] = {}
        self.last_error_ts: dict[str, float] = {}

    def validate_symbols(self) -> dict[str, dict[str, Any]]:
        logger.info("شروع اعتبارسنجی نمادها بین OKX و Toobit")
        okx_instruments = None
        toobit_symbols = None

        try:
            okx_instruments = self.okx.get_instruments("SWAP")
            logger.info("تعداد نمادهای OKX دریافت شد: %s", len(okx_instruments))
        except Exception as exc:
            logger.warning("اعتبارسنجی OKX ناموفق بود؛ در زمان دریافت کندل دوباره بررسی می‌شود: %s", exc)

        try:
            toobit_symbols = self.toobit.get_exchange_symbols()
            logger.info("تعداد نمادهای Toobit دریافت شد: %s", len(toobit_symbols))
        except Exception as exc:
            logger.warning("اعتبارسنجی Toobit ناموفق بود؛ در زمان اجرا دوباره بررسی می‌شود: %s", exc)

        valid: dict[str, dict[str, Any]] = {}
        for internal in config.WATCHLIST:
            try:
                okx_symbol = config.SYMBOL_MAP[internal]["okx"]
                toobit_symbol = config.SYMBOL_MAP[internal]["toobit"]
                symbol_info: dict[str, Any] = {}

                if okx_instruments is not None:
                    okx_symbol = self.okx.validate_symbol(internal, okx_instruments)
                if toobit_symbols is not None:
                    toobit_symbol, symbol_info = self.toobit.validate_symbol(internal, toobit_symbols)

                valid[internal] = {
                    "okx_symbol": okx_symbol,
                    "toobit_symbol": toobit_symbol,
                    "toobit_info": symbol_info,
                }
                logger.info("نماد معتبر شد: %s | OKX=%s | Toobit=%s", internal, okx_symbol, toobit_symbol)
            except Exception as exc:
                logger.warning("نماد %s رد شد و ربات ادامه می‌دهد: %s", internal, exc)

        self.valid_symbols = valid
        self.storage.set_validated_symbols(valid)
        if not valid:
            logger.error("هیچ نماد معتبری پیدا نشد. ربات فعال می‌ماند اما تحلیل انجام نمی‌شود.")
        return valid

    def start(self) -> None:
        logger.info("ربات اسکالپ کلاسیک ۵ دقیقه‌ای شروع شد")
        self.validate_symbols()
        self.telegram.start()
        self.telegram.send_message("✅ ربات اسکالپ کلاسیک ۵ دقیقه‌ای روشن شد.\nتحلیل از OKX و اجرای واقعی از Toobit انجام می‌شود.")
        self._install_signal_handlers()
        self.analysis_loop()

    def _install_signal_handlers(self) -> None:
        def handler(_sig: int, _frame: Any) -> None:
            logger.info("درخواست توقف دریافت شد")
            self.stop_event.set()
            self.telegram.stop()

        try:
            signal.signal(signal.SIGINT, handler)
            signal.signal(signal.SIGTERM, handler)
        except Exception:
            pass

    def _symbol_in_cooldown(self, symbol: str) -> bool:
        last = self.last_error_ts.get(symbol, 0)
        return time.time() - last < config.SYMBOL_ERROR_COOLDOWN_SECONDS

    def _mark_symbol_error(self, symbol: str, exc: Exception) -> None:
        self.last_error_ts[symbol] = time.time()
        self.storage.set_symbol_error(symbol, str(exc), time.time())
        logger.warning("خطای نماد %s؛ فقط همین نماد رد شد: %s", symbol, exc)

    def _process_symbol(self, internal: str, mapped: dict[str, Any]) -> float | None:
        if self._symbol_in_cooldown(internal):
            return None
        okx_symbol = mapped["okx_symbol"]
        toobit_symbol = mapped["toobit_symbol"]
        try:
            candles = self.okx.get_candles(okx_symbol)
            indicators = calculate_indicators(candles)
            latest_price = float(indicators["close"])
            signal_data = self.strategy.evaluate(internal, okx_symbol, toobit_symbol, indicators)
            if not signal_data:
                return latest_price

            now_ts = time.time()
            if now_ts - self.last_signal_ts.get(internal, 0) < config.SIGNAL_COOLDOWN_SECONDS:
                return latest_price

            ok, reason = self.trade_manager.can_accept_signal(signal_data)
            if not ok:
                logger.info("سیگنال %s رد شد: %s", internal, reason)
                return latest_price

            # تعیین ریشه‌ای نوع سیگنال قبل از ارسال:
            # اگر ترید روشن، Toobit وصل، و اسلات پوزیشن خالی باشد => رئال Toobit
            # در غیر این صورت => عادی / داخلی
            signal_data = self.trade_manager.decide_execution_mode(signal_data)
            signal_data = self.trade_manager.register_signal(signal_data)
            msg_id = self.telegram.send_message(signal_message(signal_data))
            if msg_id:
                self.storage.update_signal(signal_data["signal_id"], telegram_message_id=msg_id)
                signal_data["telegram_message_id"] = msg_id

            if signal_data.get("execution_mode") == "REAL":
                executed, exec_message, _response = self.trade_manager.try_execute_real(signal_data, mapped.get("toobit_info", {}))
                if not executed:
                    self.telegram.send_message(f"⚠️ اجرای واقعی سیگنال انجام نشد:\n{exec_message}", reply_to_message_id=msg_id)
                else:
                    self.telegram.send_message("✅ سفارش رئال Toobit تایید شد. TP و SL همراه همان سفارش اصلی ثبت شدند.", reply_to_message_id=msg_id)

            self.last_signal_ts[internal] = now_ts
            return latest_price
        except Exception as exc:
            self._mark_symbol_error(internal, exc)
            return None

    def _check_results(self, latest_prices: dict[str, float]) -> None:
        for signal_data, result, price, pnl in self.trade_manager.check_normal_results(latest_prices):
            msg_id = signal_data.get("telegram_message_id")
            self.telegram.send_message(normal_result_message(signal_data, result, price, pnl), reply_to_message_id=msg_id)

        for signal_data, result, price, pnl in self.trade_manager.check_real_results():
            msg_id = signal_data.get("telegram_message_id")
            self.telegram.send_message(real_result_message(signal_data, result, price, pnl), reply_to_message_id=msg_id)

    def analysis_loop(self) -> None:
        while not self.stop_event.is_set():
            if not self.valid_symbols:
                self.validate_symbols()
                safe_sleep(15)
                continue

            latest_prices: dict[str, float] = {}
            for internal, mapped in list(self.valid_symbols.items()):
                if self.stop_event.is_set():
                    break
                price = self._process_symbol(internal, mapped)
                if price is not None:
                    latest_prices[internal] = price
                safe_sleep(0.15)

            try:
                self._check_results(latest_prices)
            except Exception as exc:
                logger.warning("بررسی نتیجه‌ها ناموفق بود، ربات ادامه می‌دهد: %s", exc)

            safe_sleep(config.POLL_INTERVAL_SECONDS)


def main() -> None:
    bot = FiveMinuteScalperBot()
    bot.start()


if __name__ == "__main__":
    main()
