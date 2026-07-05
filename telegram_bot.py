from __future__ import annotations

import re
import time
from typing import Callable

from telegram import Message
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from config import load_settings, set_leverage, set_max_positions, set_trade_amount, set_trade_enabled
from signal_manager import Signal
from stats_manager import StatsManager
from slot_manager import SlotManager

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def normalize_number_text(text: str) -> str:
    return text.translate(PERSIAN_DIGITS).replace("/", ".")


def first_number(text: str) -> float | None:
    text = normalize_number_text(text)
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def direction_fa(direction: str) -> str:
    return "لانگ" if direction == "LONG" else "شورت"


def signal_message(signal: Signal) -> str:
    return (
        "📌 سیگنال جدید\n\n"
        f"نماد: {signal.toobit_symbol}\n"
        f"جهت: {direction_fa(signal.direction)}\n"
        f"ورود: {signal.entry:.8g}\n"
        f"خروج: {signal.tp:.8g}\n"
        f"حد ضرر: {signal.sl:.8g}\n"
        f"درصد حرکت: {signal.estimated_move_percent:.2f}%\n"
        f"سود احتمالی خالص: {signal.estimated_net_profit:.4f} دلار\n"
        f"مدت حدودی باز بودن: {signal.estimated_hold_time}\n"
        f"نوع سیگنال: {signal.signal_type}"
    )


def result_message(signal: Signal, is_tp: bool) -> str:
    title = "✅ تیپی خورد" if is_tp else "❌ استاپ خورد"
    pnl_label = "سود خالص" if is_tp else "ضرر خالص"
    net = signal.net_pnl or 0.0
    return (
        f"{title}\n\n"
        f"نماد: {signal.toobit_symbol}\n"
        f"نوع سیگنال: {signal.signal_type}\n"
        f"جهت: {direction_fa(signal.direction)}\n"
        f"ورود: {signal.entry:.8g}\n"
        f"خروج: {(signal.exit_price or signal.tp):.8g}\n"
        f"{pnl_label}: {net:.4f} دلار\n"
        f"کارمزد: {signal.fee_usdt:.4f} دلار\n"
        f"مدت باز بودن: {format_duration(signal.opened_at, signal.closed_at or time.time())}"
    )


def trade_panel_message(stats_manager: StatsManager, slot_manager: SlotManager) -> str:
    settings = load_settings()
    slot_manager.clear_expired()
    real_open = stats_manager.store.open_real_count()
    reserved = len(slot_manager.reserved_slots())
    used_slots = min(settings.max_positions, real_open + reserved)
    free_slots = max(settings.max_positions - used_slots, 0)
    today_pnl = stats_manager.today_net_pnl()
    trade_status = "فعال" if settings.trade_enabled else "غیرفعال"
    return (
        "📊 پنل ترید\n\n"
        f"ترید: {trade_status}\n"
        f"اسلات‌های پر: {used_slots}\n"
        f"اسلات‌های خالی: {free_slots}\n"
        f"لوریج: {settings.leverage}\n"
        f"دلار هر ترید: {settings.trade_amount_usdt:.2f}\n"
        f"سود یا ضرر امروز: {today_pnl:.4f} دلار"
    )


def format_duration(start: float, end: float) -> str:
    seconds = max(0, int(end - start))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours:
        return f"{hours} ساعت و {minutes} دقیقه"
    return f"{minutes} دقیقه"


class PersianTelegramBot:
    def __init__(self, token: str, stats_manager: StatsManager, slot_manager: SlotManager) -> None:
        self.app = Application.builder().token(token).build()
        self.stats_manager = stats_manager
        self.slot_manager = slot_manager
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

    async def handle_text(self, update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or message.text is None:
            return
        text = message.text.strip()

        if text == "ترید":
            await message.reply_text(trade_panel_message(self.stats_manager, self.slot_manager))
            return

        if text == "ترید روشن":
            set_trade_enabled(True)
            await message.reply_text("ترید روشن شد.")
            return
        if text == "ترید خاموش":
            set_trade_enabled(False)
            await message.reply_text("ترید خاموش شد.")
            return
        if text.startswith("دلار ترید"):
            value = first_number(text)
            if value is None:
                await message.reply_text("مقدار دلار ترید درست نیست.")
                return
            settings = set_trade_amount(value)
            await message.reply_text(f"دلار ترید روی {settings.trade_amount_usdt:.2f} تنظیم شد.")
            return
        if text.startswith("لوریج"):
            value = first_number(text)
            if value is None:
                await message.reply_text("مقدار لوریج درست نیست.")
                return
            settings = set_leverage(int(value))
            await message.reply_text(f"لوریج روی {settings.leverage} تنظیم شد.")
            return
        if text.startswith("حداکثر پوزیشن"):
            value = first_number(text)
            if value is None:
                await message.reply_text("مقدار حداکثر پوزیشن درست نیست.")
                return
            settings = set_max_positions(int(value))
            await message.reply_text(f"حداکثر پوزیشن روی {settings.max_positions} تنظیم شد.")
            return
        if text in {"امار", "آمار"}:
            await message.reply_text(self.stats_manager.render_stats())
            return

    async def send_signal(self, chat_id: str | int, signal: Signal) -> Message:
        return await self.app.bot.send_message(chat_id=chat_id, text=signal_message(signal))

    async def send_result_reply(self, chat_id: str | int, signal: Signal, is_tp: bool) -> Message:
        return await self.app.bot.send_message(
            chat_id=chat_id,
            text=result_message(signal, is_tp),
            reply_to_message_id=signal.telegram_message_id,
        )
