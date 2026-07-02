"""ربات تلگرام متنی، بدون دکمه."""
from __future__ import annotations

import asyncio
import re
from typing import Callable

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

import config
from messages_fa import (
    format_active_symbols,
    format_balance,
    format_command_error,
    format_positions,
    format_setting_ok,
    format_stats,
    format_status,
    format_symbol_check,
    format_trade_panel,
)
from okx_client import OkxClient
from scanner import MarketScanner
from storage import JsonStorage
from toobit_client import ToobitClient
from trade_manager import TradeManager
from utils import logger, okx_inst_id, toobit_symbol


class TelegramBot:
    def __init__(self, storage: JsonStorage, okx: OkxClient, toobit: ToobitClient, trade_manager: TradeManager, scanner: MarketScanner):
        if not config.TELEGRAM_BOT_TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN تنظیم نشده است")
        self.storage = storage
        self.okx = okx
        self.toobit = toobit
        self.trade_manager = trade_manager
        self.scanner = scanner
        self.app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        self.app.add_handler(MessageHandler(filters.COMMAND, self.on_text))

    async def send_message(self, text: str) -> int | None:
        if not config.TELEGRAM_CHAT_ID:
            logger.warning("TELEGRAM_CHAT_ID تنظیم نشده است؛ پیام ارسال نشد")
            return None
        msg = await self.app.bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=text)
        return msg.message_id

    async def send_reply(self, reply_to_message_id: int | None, text: str) -> int | None:
        if not config.TELEGRAM_CHAT_ID:
            return None
        msg = await self.app.bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            reply_to_message_id=reply_to_message_id,
            allow_sending_without_reply=True,
        )
        return msg.message_id

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message or not message.text:
            return
        text = self._normalize(message.text)
        try:
            answer = await self.handle_command(text)
        except Exception as exc:
            logger.exception("خطا در پردازش دستور: %s", exc)
            answer = format_command_error(f"خطا در اجرای دستور: {exc}")
        await message.reply_text(answer)

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(text.replace("\u200c", " ").strip().split())

    @staticmethod
    def _number(text: str) -> float | None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", text)
        if not m:
            return None
        return float(m.group(1))

    async def handle_command(self, text: str) -> str:
        lower = text.lower()

        if lower in {"/trade_on", "ترید فعال", "روشن کردن ترید"}:
            self.storage.update_settings(trading_enabled=True)
            return format_setting_ok("ترید واقعی Spot روشن شد.")

        if lower in {"/trade_off", "ترید خاموش", "خاموش کردن ترید"}:
            self.storage.update_settings(trading_enabled=False)
            return format_setting_ok("ترید واقعی Spot خاموش شد. سیگنال‌ها همچنان عادی با OKX دنبال می‌شوند.")

        if lower in {"ترید", "پنل ترید"}:
            balance = None
            if self.toobit.has_credentials:
                try:
                    balance = await asyncio.to_thread(self.toobit.get_spot_usdt_balance)
                except Exception as exc:
                    logger.warning("خواندن موجودی ناموفق بود: %s", exc)
            return format_trade_panel(
                self.storage.settings,
                self.storage.stats,
                balance,
                len(self.storage.real_open_signals()),
                len(self.storage.normal_open_signals()),
                self.scanner.active_symbols(),
            )

        if lower.startswith("ترید دلار") or lower.startswith("تنظیم ترید دلار"):
            value = self._number(lower)
            if value is None or not (config.MIN_TRADE_AMOUNT_USDT <= value <= config.MAX_TRADE_AMOUNT_USDT):
                return format_command_error(f"ترید دلار باید بین {config.MIN_TRADE_AMOUNT_USDT:g} تا {config.MAX_TRADE_AMOUNT_USDT:g} باشد.")
            self.storage.update_settings(trade_amount_usdt=float(value))
            return format_setting_ok(f"پول هر معامله روی {value:g} USDT تنظیم شد.")

        if lower.startswith("حداکثر پوزیشن") or lower.startswith("تنظیم حداکثر پوزیشن"):
            value = self._number(lower)
            if value is None or not (config.MIN_MAX_POSITIONS <= int(value) <= config.MAX_MAX_POSITIONS):
                return format_command_error(f"حداکثر پوزیشن باید بین {config.MIN_MAX_POSITIONS} تا {config.MAX_MAX_POSITIONS} باشد.")
            self.storage.update_settings(max_real_positions=int(value))
            return format_setting_ok(f"حداکثر پوزیشن واقعی روی {int(value)} تنظیم شد.")

        if lower.startswith("درصد حرکت") or lower.startswith("تنظیم درصد حرکت"):
            value = self._number(lower)
            if value is None or not (config.MIN_TARGET_PERCENT <= value <= config.MAX_TARGET_PERCENT):
                return format_command_error(f"درصد حرکت باید بین {config.MIN_TARGET_PERCENT:g} تا {config.MAX_TARGET_PERCENT:g} باشد.")
            self.storage.update_settings(target_percent=float(value))
            return format_setting_ok(f"درصد حرکت هدف روی {value:g}٪ تنظیم شد.")

        if lower.startswith("تعداد ارز فعال") or lower.startswith("تنظیم تعداد ارز فعال"):
            value = self._number(lower)
            if value is None or not (config.MIN_ACTIVE_SYMBOL_COUNT <= int(value) <= config.MAX_ACTIVE_SYMBOL_COUNT):
                return format_command_error(f"تعداد ارز فعال باید بین {config.MIN_ACTIVE_SYMBOL_COUNT} تا {config.MAX_ACTIVE_SYMBOL_COUNT} باشد.")
            self.storage.update_settings(active_symbol_count=int(value))
            return format_setting_ok(f"تعداد ارز فعال روی {int(value)} تنظیم شد.")

        if lower.startswith("چک هیستوری") or lower.startswith("تنظیم چک هیستوری"):
            value = self._number(lower)
            if value is None or not (config.MIN_HISTORY_CHECK_MINUTES <= int(value) <= config.MAX_HISTORY_CHECK_MINUTES):
                return format_command_error(f"چک هیستوری باید بین {config.MIN_HISTORY_CHECK_MINUTES} تا {config.MAX_HISTORY_CHECK_MINUTES} دقیقه باشد.")
            self.storage.update_settings(history_check_minutes=int(value))
            return format_setting_ok(f"چک Order History واقعی هر {int(value)} دقیقه انجام می‌شود.")

        if lower.startswith("کارمزد میکر"):
            value = self._number(lower)
            if value is None or not (config.MIN_FEE_PCT <= value <= config.MAX_FEE_PCT):
                return format_command_error(f"کارمزد میکر باید بین {config.MIN_FEE_PCT:g} تا {config.MAX_FEE_PCT:g} درصد باشد.")
            self.storage.update_settings(maker_fee_pct=float(value))
            return format_setting_ok(f"کارمزد Maker روی {value:g}٪ تنظیم شد.")

        if lower.startswith("کارمزد تیکر"):
            value = self._number(lower)
            if value is None or not (config.MIN_FEE_PCT <= value <= config.MAX_FEE_PCT):
                return format_command_error(f"کارمزد تیکر باید بین {config.MIN_FEE_PCT:g} تا {config.MAX_FEE_PCT:g} درصد باشد.")
            self.storage.update_settings(taker_fee_pct=float(value))
            return format_setting_ok(f"کارمزد Taker روی {value:g}٪ تنظیم شد.")

        if lower in {"آمار", "/stats"}:
            return format_stats(self.storage.stats)

        if lower == "موجودی":
            if not self.toobit.has_credentials:
                return format_command_error("کلید API توبیت تنظیم نشده است.")
            balance = await asyncio.to_thread(self.toobit.get_spot_usdt_balance)
            return format_balance(balance)

        if lower == "وضعیت":
            okx_ok = True
            toobit_ok = self.toobit.has_credentials
            return format_status(self.storage.settings, okx_ok, toobit_ok, len(self.storage.real_open_signals()), len(self.storage.normal_open_signals()))


        if lower in {"چک نمادها", "بررسی نمادها", "چک ارزها"}:
            rows = []
            for base in self.scanner.active_symbols():
                row = {"base": base, "okx_symbol": okx_inst_id(base), "toobit_symbol": toobit_symbol(base)}
                okx_ok, okx_msg = await asyncio.to_thread(self.okx.validate_symbol, base)
                row["okx_ok"] = okx_ok
                if okx_ok:
                    row["okx_symbol"] = okx_msg
                else:
                    row["okx_error"] = okx_msg
                try:
                    tb_symbol, _ = await asyncio.to_thread(self.toobit.validate_spot_symbol, toobit_symbol(base))
                    row["toobit_ok"] = True
                    row["toobit_symbol"] = tb_symbol
                except Exception as exc:
                    row["toobit_ok"] = False
                    row["toobit_error"] = str(exc)
                rows.append(row)
            return format_symbol_check(rows)

        if lower == "ارزهای فعال":
            busy = {s.base_symbol for s in self.storage.open_signals()}
            return format_active_symbols(self.scanner.active_symbols(), busy)

        if lower in {"پوزیشن ها", "پوزیشن های باز"}:
            return format_positions("📌 پوزیشن‌ها و سیگنال‌های باز", self.storage.open_signals())

        if lower == "پوزیشن های بسته":
            return format_positions("✅ پوزیشن‌ها و سیگنال‌های بسته‌شده", self.storage.closed_signals())

        if lower == "سیگنال های عادی":
            return format_positions("📘 سیگنال‌های عادی OKX", self.storage.normal_open_signals())

        if lower == "پوزیشن های واقعی":
            return format_positions("📗 پوزیشن‌های واقعی Toobit", self.storage.real_open_signals())

        if lower == "ریست آمار":
            self.storage.reset_stats()
            return format_setting_ok("آمار صفر شد، ولی سیگنال‌ها و تنظیمات باقی ماندند.")

        if lower == "حذف آمار":
            self.storage.delete_history()
            return format_setting_ok("آمار و تاریخچه سیگنال‌ها حذف شد.")

        return format_command_error("دستور شناخته نشد. برای دیدن پنل بنویس: ترید")

    async def run(self) -> None:
        await self.app.initialize()
        await self.app.start()
        if self.app.updater:
            await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("ربات تلگرام Spot Hunter شروع شد")
        await asyncio.Event().wait()
