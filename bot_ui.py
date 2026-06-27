from __future__ import annotations

from typing import Any

from config import TELEGRAM_CHAT_ID
from storage import Storage, StoredSignal
from trade_manager import CreatedSignal, PanelData, TradeManager

_DIGIT_TRANS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def fmt_price(value: float) -> str:
    if value >= 100:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


def fmt_money(value: float | None) -> str:
    if value is None:
        return "نامشخص"
    return f"{value:.2f} USDT"


def _normalize_command(text: str) -> str:
    command = text.strip().translate(_DIGIT_TRANS)
    if not command:
        return ""
    command = command.replace("ي", "ی").replace("ك", "ک").replace("‌", " ")
    command = command.replace("ـ", "").replace("_", " ")
    if command.startswith("/"):
        command = command[1:]
    if "@" in command:
        command = command.split("@", 1)[0]
    return " ".join(command.split())


def _last_float(command: str) -> float:
    parts = command.split()
    if not parts:
        raise ValueError("عدد وارد نشده است.")
    return float(parts[-1])


def _last_int(command: str) -> int:
    return int(float(str(_last_float(command))))


class BotUI:
    def __init__(self, storage: Storage, trade_manager: TradeManager) -> None:
        self.storage = storage
        self.trade_manager = trade_manager
        self.app: Any | None = None

    def bind_app(self, app: Any) -> None:
        self.app = app

    async def send_signal(self, *, symbol_name: str, decision, created: CreatedSignal) -> int | None:
        if self.app is None:
            return None
        color = "🟢" if decision.direction == "LONG" else "🔴"
        direction_fa = "لانگ" if decision.direction == "LONG" else "شورت"
        type_fa = "واقعی" if created.signal_type == "real" else "عادی"
        text = (
            f"{color} سیگنال {direction_fa}\n\n"
            f"ارز: {symbol_name}\n"
            f"ورود: {fmt_price(decision.entry)}\n"
            f"تیپی: {fmt_price(decision.tp)}\n"
            f"استاپ: {fmt_price(decision.sl)}\n"
            f"امتیاز: {decision.score}/100\n"
            f"نوع: {type_fa}"
        )
        message = await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        self.storage.update_message_id(created.signal_id, int(message.message_id))
        return int(message.message_id)

    async def send_result(self, signal: StoredSignal, status: str, approx_pnl: float, real_pnl: float | None) -> int | None:
        if self.app is None:
            return None
        if status == "TP":
            text = f"🟢 نتیجه: تیپی خورد\nسود/ضرر تقریبی: {fmt_money(approx_pnl)}"
        else:
            text = f"🔴 نتیجه: استاپ خورد\nسود/ضرر تقریبی: {fmt_money(approx_pnl)}"
        if signal.signal_type == "real":
            text += f"\nسود/ضرر واقعی: {fmt_money(real_pnl)}"
        message = await self.app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            reply_to_message_id=signal.message_id,
        )
        return int(message.message_id)

    async def send_panel(self, chat_id: int | str) -> None:
        data = await self.trade_manager.panel_data()
        await self._send_text(chat_id, self.panel_text(data))

    def panel_text(self, data: PanelData) -> str:
        status = "فعال" if data.trade_enabled else "خاموش"
        return (
            "📌 پنل ترید\n\n"
            f"وضعیت ترید: {status}\n"
            f"مارجین قابل استفاده توبیت: {fmt_money(data.wallet_margin_usdt)}\n"
            f"دلار هر پوزیشن: {data.margin_usdt:.2f} USDT\n"
            f"لوریج: {data.leverage}\n"
            f"حداکثر پوزیشن: {data.max_positions}\n"
            f"اسلات پر: {data.filled_slots}\n"
            f"اسلات خالی: {data.empty_slots}\n"
            f"سود/ضرر امروز واقعی: {fmt_money(data.today_real_pnl)}\n"
            f"سود/ضرر امروز تقریبی: {fmt_money(data.today_approx_pnl)}"
        )

    async def handle_text(self, update: Any, context: Any) -> None:
        if update.message is None or update.message.text is None:
            return
        chat_id = update.message.chat_id
        command = _normalize_command(update.message.text)
        if not command:
            return
        try:
            if command in {"پنل", "ترید", "وضعیت", "سرمایه", "پوزیشنها", "پوزیشن ها"}:
                await self.send_panel(chat_id)
            elif command in {"راهنما", "help", "start", "شروع"}:
                await self._send_text(chat_id, self.help_text())
            elif command in {"ترید فعال", "فعال ترید", "ترید روشن", "روشن ترید", "روشن"}:
                self.storage.set_trade_enabled(True)
                await self._send_text(chat_id, "ترید فعال شد.")
            elif command in {"ترید خاموش", "خاموش ترید", "ترید غیرفعال", "غیرفعال ترید", "خاموش"}:
                self.storage.set_trade_enabled(False)
                await self._send_text(chat_id, "ترید خاموش شد. همه سیگنال‌ها عادی می‌شوند.")
            elif command.startswith("ترید دلار") or command.startswith("تنظیم دلار") or command.startswith("دلار "):
                value = _last_float(command)
                self.storage.set_margin_usdt(value)
                await self._send_text(chat_id, f"دلار هر پوزیشن تنظیم شد: {value:.2f} USDT")
            elif command.startswith("ترید لوریج") or command.startswith("تنظیم لوریج") or command.startswith("لوریج "):
                value = _last_int(command)
                self.storage.set_leverage(value)
                await self._send_text(chat_id, f"لوریج تنظیم شد: {value}")
            elif command.startswith("حداکثر پوزیشن") or command.startswith("ترید اسلات") or command.startswith("اسلات "):
                value = _last_int(command)
                self.storage.set_max_positions(value)
                await self._send_text(chat_id, f"حداکثر پوزیشن تنظیم شد: {value}")
            elif command.startswith("آمار") or command.startswith("امار") or command in {"امروز", "سود", "تاریخچه"}:
                parts = command.split()
                days = int(parts[-1]) if len(parts) > 1 and parts[-1].isdigit() else 7
                await self._send_text(chat_id, self.stats_text(days))
        except Exception as exc:
            await self._send_text(chat_id, f"خطا: {exc}")

    def help_text(self) -> str:
        return (
            "راهنمای دستورها:\n\n"
            "پنل یا ترید\n"
            "آمار یا آمار 7\n"
            "ترید فعال\n"
            "ترید خاموش\n"
            "ترید دلار 10\n"
            "ترید لوریج 5\n"
            "حداکثر پوزیشن 3"
        )

    def stats_text(self, days: int) -> str:
        days = max(1, min(days, 7))
        stats = self.storage.stats(days)
        normal = stats["normal"]
        real = stats["real"]
        return (
            f"📊 آمار {days} روز اخیر\n\n"
            "سیگنال‌های عادی:\n"
            f"تعداد: {normal['total']}\n"
            f"تیپی: {normal['tp']}\n"
            f"استاپ: {normal['sl']}\n"
            f"باز: {normal['open']}\n"
            f"وین‌ریت: {normal['win_rate']:.2f}%\n"
            f"سود/ضرر تقریبی: {fmt_money(normal['pnl'])}\n\n"
            "سیگنال‌های واقعی:\n"
            f"تعداد: {real['total']}\n"
            f"تیپی: {real['tp']}\n"
            f"استاپ: {real['sl']}\n"
            f"باز: {real['open']}\n"
            f"وین‌ریت: {real['win_rate']:.2f}%\n"
            f"سود/ضرر واقعی: {fmt_money(real['pnl'])}"
        )

    async def _send_text(self, chat_id: int | str, text: str) -> None:
        if self.app is not None:
            await self.app.bot.send_message(chat_id=chat_id, text=text)
