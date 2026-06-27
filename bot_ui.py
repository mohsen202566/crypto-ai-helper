from __future__ import annotations

import re
from typing import Any

from config import OWNER_ID, TELEGRAM_CHAT_ID
from scorer import SignalDecision
from storage import Storage, StoredSignal
from trade_manager import CreatedSignal, PanelData, TradeManager


PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_text(text: str) -> str:
    text = text.strip().translate(PERSIAN_DIGITS)
    text = re.sub(r"\s+", " ", text)
    return text


def fmt_price(value: float | None) -> str:
    if value is None:
        return "نامشخص"
    value = float(value)
    if value >= 100:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


def fmt_money(value: float | None) -> str:
    if value is None:
        return "خطا در خواندن"
    return f"{value:.2f} USDT"


def fmt_pct(value: float) -> str:
    return f"{value * 100:.3f}%"


class BotUI:
    def __init__(self, storage: Storage, trade_manager: TradeManager) -> None:
        self.storage = storage
        self.trade_manager = trade_manager
        self.app: Any | None = None

    def bind_app(self, app: Any) -> None:
        self.app = app

    def _is_owner(self, chat_id: int | str) -> bool:
        allowed = {str(TELEGRAM_CHAT_ID), str(OWNER_ID)}
        return str(chat_id) in allowed

    async def send_signal(self, *, symbol_name: str, decision: SignalDecision, created: CreatedSignal) -> int | None:
        if self.app is None or decision.direction is None:
            return None
        color = "🟢" if decision.direction == "LONG" else "🔴"
        direction_fa = "لانگ" if decision.direction == "LONG" else "شورت"
        if created.signal_type == "real":
            type_fa = "واقعی - در انتظار تایید 70 ثانیه‌ای"
        elif created.signal_type == "normal":
            type_fa = "عادی"
        else:
            type_fa = created.signal_type
        text = (
            f"{color} سیگنال {direction_fa}\n\n"
            f"ارز: {symbol_name}\n"
            f"نوع: {type_fa}\n"
            f"امتیاز: {decision.score}/{decision.threshold}\n\n"
            f"ورود: {fmt_price(decision.entry)}\n"
            f"TP: {fmt_price(decision.tp)}\n"
            f"SL: {fmt_price(decision.sl)}\n\n"
            f"1H: {decision.direction_state_1h} | اعتماد: {decision.direction_confidence_1h}%\n"
            f"4H: {decision.bias_4h}\n"
            f"15m: {decision.setup_15m}\n"
            f"5m: {decision.entry_5m}\n"
            f"Late Entry: {'اوکی' if decision.late_entry_ok else 'رد'}\n"
            f"RR: {decision.risk_reward:.2f}\n"
            f"Net Edge: {fmt_pct(decision.net_edge)}\n\n"
            f"امتیازها: 1H {decision.breakdown.score_1h} | 15m {decision.breakdown.score_15m} | "
            f"5m {decision.breakdown.score_5m} | Late {decision.breakdown.score_late} | "
            f"Risk {decision.breakdown.score_risk} | Market {decision.breakdown.score_market} | 4H {decision.breakdown.score_4h}\n\n"
            f"دلیل: {decision.reason}\n"
            f"وضعیت اجرا: {created.reason}"
        )
        message = await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        self.storage.update_message_id(created.signal_id, int(message.message_id))
        return int(message.message_id)

    async def send_result(self, signal: StoredSignal, status: str, approx_pnl: float, real_pnl: float | None) -> int | None:
        if self.app is None:
            return None
        direction_fa = "لانگ" if signal.direction == "LONG" else "شورت"
        result_fa = "تیپی خورد" if status == "TP" else "استاپ خورد"
        icon = "🟢" if status == "TP" else "🔴"
        text = (
            f"{icon} نتیجه {direction_fa}: {result_fa}\n"
            f"ارز: {signal.symbol_name or signal.toobit_symbol}\n"
            f"نوع: {'واقعی' if signal.signal_type == 'real' else 'عادی'}\n"
            f"سود/ضرر تقریبی: {fmt_money(approx_pnl)}"
        )
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
        status = "فعال ✅" if data.trade_enabled else "خاموش ⛔️"
        wallet_line = fmt_money(data.wallet_margin_usdt)
        if data.wallet_error:
            wallet_line = f"خطا در خواندن ({data.wallet_error[:80]})"
        pos_line = "نامشخص" if data.exchange_open_positions is None else str(data.exchange_open_positions)
        ord_line = "نامشخص" if data.exchange_open_orders is None else str(data.exchange_open_orders)
        if data.exchange_error:
            exch_note = f"\nخطای وضعیت Toobit: {data.exchange_error[:90]}"
        else:
            exch_note = ""
        long_stats = data.today_stats.get("long", {})
        short_stats = data.today_stats.get("short", {})
        return (
            "📌 پنل ترید\n\n"
            f"وضعیت ترید واقعی: {status}\n\n"
            "💰 Toobit:\n"
            f"موجودی قابل استفاده: {wallet_line}\n"
            f"پوزیشن‌های باز واقعی: {pos_line}\n"
            f"سفارش‌های باز واقعی: {ord_line}{exch_note}\n\n"
            "⚙️ تنظیمات ربات:\n"
            f"دلار هر پوزیشن: {data.margin_usdt:.2f} USDT\n"
            f"لوریج: {data.leverage}x\n"
            f"حداکثر پوزیشن: {data.max_positions}\n"
            f"اسلات پر/رزرو: {data.filled_slots}\n"
            f"اسلات خالی: {data.empty_slots}\n"
            f"در انتظار تایید 70 ثانیه‌ای: {data.pending_slots}\n\n"
            "📈 امروز:\n"
            f"PnL واقعی Toobit/ربات: {fmt_money(data.today_real_pnl)}\n"
            f"PnL تقریبی عادی: {fmt_money(data.today_approx_pnl)}\n"
            f"لانگ: TP {long_stats.get('tp', 0)} / SL {long_stats.get('sl', 0)} / WR {long_stats.get('win_rate', 0):.1f}%\n"
            f"شورت: TP {short_stats.get('tp', 0)} / SL {short_stats.get('sl', 0)} / WR {short_stats.get('win_rate', 0):.1f}%"
        )

    async def handle_text(self, update: Any, context: Any) -> None:
        if update.message is None or update.message.text is None:
            return
        chat_id = update.message.chat_id
        text = normalize_text(update.message.text)
        if not self._is_owner(chat_id):
            return
        try:
            if text in {"/start", "start", "راهنما", "/راهنما", "کمک"}:
                await self._send_text(chat_id, self.help_text())
            elif text in {"/پنل", "پنل", "ترید", "/ترید"}:
                await self.send_panel(chat_id)
            elif text in {"وضعیت", "/وضعیت"}:
                await self.send_panel(chat_id)
            elif text in {"/ترید_فعال", "ترید فعال", "ترید روشن"}:
                self.storage.set_trade_enabled(True)
                await self._send_text(chat_id, "✅ ترید واقعی فعال شد. از این لحظه سیگنال‌های معتبر می‌توانند واقعی شوند.")
            elif text in {"/ترید_خاموش", "ترید خاموش", "ترید غیر فعال", "ترید غیرفعال"}:
                self.storage.set_trade_enabled(False)
                await self._send_text(chat_id, "✅ ترید واقعی خاموش شد. سیگنال‌ها عادی ثبت می‌شوند.")
            elif text.startswith("/ترید_دلار") or text.startswith("ترید دلار"):
                value = self._last_number(text)
                self.storage.set_margin_usdt(value)
                await self._send_text(chat_id, f"✅ دلار هر پوزیشن روی {value:.2f} USDT تنظیم شد.")
            elif text.startswith("/ترید_لوریج") or text.startswith("ترید لوریج"):
                value = int(self._last_number(text))
                self.storage.set_leverage(value)
                await self._send_text(chat_id, f"✅ لوریج روی {value}x تنظیم شد.")
            elif text.startswith("/حداکثر_پوزیشن") or text.startswith("حداکثر پوزیشن"):
                value = int(self._last_number(text))
                self.storage.set_max_positions(value)
                await self._send_text(chat_id, f"✅ حداکثر پوزیشن روی {value} تنظیم شد.")
            elif text.startswith("/آمار") or text.startswith("آمار") or text.startswith("امار"):
                parts = text.split()
                days = 7
                if len(parts) > 1:
                    try:
                        days = int(float(parts[-1]))
                    except ValueError:
                        days = 7
                await self._send_text(chat_id, self.stats_text(days))
        except Exception as exc:
            await self._send_text(chat_id, f"خطا: {exc}")

    def _last_number(self, text: str) -> float:
        matches = re.findall(r"[-+]?\d+(?:\.\d+)?", text)
        if not matches:
            raise ValueError("عدد پیدا نشد.")
        return float(matches[-1])

    def stats_text(self, days: int) -> str:
        days = max(1, min(days, 30))
        stats = self.storage.stats(days)
        normal = stats["normal"]
        real = stats["real"]
        long = stats["long"]
        short = stats["short"]
        real_failed = stats["real_failed"]
        return (
            f"📊 آمار {days} روز اخیر\n\n"
            f"🟢 لانگ: سیگنال {long['total']} | TP {long['tp']} | SL {long['sl']} | وین‌ریت {long['win_rate']:.1f}%\n"
            f"🔴 شورت: سیگنال {short['total']} | TP {short['tp']} | SL {short['sl']} | وین‌ریت {short['win_rate']:.1f}%\n\n"
            "📌 عادی:\n"
            f"تعداد: {normal['total']} | TP: {normal['tp']} | SL: {normal['sl']} | باز: {normal['open']}\n"
            f"وین‌ریت: {normal['win_rate']:.1f}% | PnL تقریبی: {fmt_money(normal['pnl'])}\n\n"
            "💰 واقعی:\n"
            f"تعداد: {real['total']} | TP: {real['tp']} | SL: {real['sl']} | باز: {real['open']}\n"
            f"وین‌ریت: {real['win_rate']:.1f}% | PnL واقعی: {fmt_money(real['pnl'])}\n"
            f"ارسال واقعی ناموفق: {real_failed['total']}"
        )

    def help_text(self) -> str:
        return (
            "راهنما:\n"
            "پنل\n"
            "وضعیت\n"
            "آمار\n"
            "آمار 7\n"
            "ترید فعال\n"
            "ترید خاموش\n"
            "ترید دلار 20\n"
            "ترید لوریج 10\n"
            "حداکثر پوزیشن 3"
        )

    async def _send_text(self, chat_id: int | str, text: str) -> None:
        if self.app is not None:
            await self.app.bot.send_message(chat_id=chat_id, text=text)
