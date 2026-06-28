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

    async def send_ready_alert(self, *, symbol_name: str, direction: str) -> int | None:
        if self.app is None:
            return None
        direction_fa = "لانگ" if direction == "LONG" else "شورت"
        text = f"🟡 آماده ورود\n{symbol_name} {direction_fa}"
        msg = await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        return int(msg.message_id)

    async def send_signal(self, *, symbol_name: str, decision: SignalDecision, created: CreatedSignal) -> int | None:
        if self.app is None or decision.direction is None:
            return None
        color = "🟢" if decision.direction == "LONG" else "🔴"
        direction_fa = "لانگ" if decision.direction == "LONG" else "شورت"
        text = (
            f"{color} سیگنال {direction_fa}\n\n"
            f"ارز: {symbol_name}\n"
            f"نوع: {created.signal_label}\n"
            f"Score: {decision.score}/{decision.threshold}\n"
            f"AI Confidence: {decision.ai_confidence}%\n"
            f"AI Experience: {decision.ai_experience} نمونه\n\n"
            f"Entry: {fmt_price(decision.entry)}\n"
            f"TP: {fmt_price(decision.tp)}\n"
            f"SL: {fmt_price(decision.sl)}\n\n"
            f"Pattern: {decision.candle_pattern}\n"
            f"Entry Stage: {decision.entry_stage_pct:.1f}%\n"
            f"Net Edge: {fmt_pct(decision.net_edge)}\n"
            f"سود تخمینی: {fmt_money(decision.estimated_profit_usdt)} / {decision.estimated_profit_pct:.3f}%\n"
            f"RR: {decision.risk_reward:.2f}\n\n"
            f"امتیازها: جهت {decision.breakdown.score_direction} | پیش‌قدرت {decision.breakdown.score_pre_ignition} | "
            f"کندل {decision.breakdown.score_candle_entry} | AI {decision.breakdown.score_ai_memory} | "
            f"سود/ریسک {decision.breakdown.score_risk_net} | سشن {decision.breakdown.score_session} | OB {decision.breakdown.score_order_block}\n\n"
            f"دلیل: {decision.reason}\n"
            f"وضعیت اجرا: {created.reason}\n\n"
            f"{created.signal_label}"
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
        text = f"{icon} نتیجه {direction_fa}: {result_fa}\nارز: {signal.symbol_name or signal.toobit_symbol}\nنوع: {signal.hunter_type} / {signal.signal_type}\nسود/ضرر تقریبی: {fmt_money(approx_pnl)}"
        if signal.signal_type == "real":
            text += f"\nسود/ضرر واقعی: {fmt_money(real_pnl)}"
        message = await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, reply_to_message_id=signal.message_id)
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
        exch_note = f"\nخطای وضعیت Toobit: {data.exchange_error[:90]}" if data.exchange_error else ""
        long_stats = data.today_stats.get("long", {})
        short_stats = data.today_stats.get("short", {})
        hunter_stats = data.today_stats.get("hunter", {})
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
            f"حداقل سود دلاری: {data.min_profit_usdt:.2f} USDT\n"
            f"حداقل درصد سود: {data.min_profit_pct:.2f}%\n"
            f"اسلات پر/رزرو: {data.filled_slots}\n"
            f"اسلات خالی: {data.empty_slots}\n"
            f"در انتظار تایید 70 ثانیه‌ای: {data.pending_slots}\n"
            f"نماد OKX خطادار: {data.symbol_health.get('okx_disabled', 0)}\n"
            f"نماد Toobit real غیرفعال: {data.symbol_health.get('toobit_real_disabled', 0)}\n\n"
            "📈 امروز:\n"
            f"PnL واقعی Toobit/ربات: {fmt_money(data.today_real_pnl)}\n"
            f"PnL تقریبی عادی: {fmt_money(data.today_approx_pnl)}\n"
            f"لانگ: TP {long_stats.get('tp', 0)} / SL {long_stats.get('sl', 0)} / WR {long_stats.get('win_rate', 0):.1f}%\n"
            f"شورت: TP {short_stats.get('tp', 0)} / SL {short_stats.get('sl', 0)} / WR {short_stats.get('win_rate', 0):.1f}%\n"
            f"شکار: TP {hunter_stats.get('tp', 0)} / SL {hunter_stats.get('sl', 0)} / WR {hunter_stats.get('win_rate', 0):.1f}%"
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
            elif text in {"/پنل", "پنل", "ترید", "/ترید", "وضعیت", "/وضعیت"}:
                await self.send_panel(chat_id)
            elif text.lower() in {"ai", "هوش", "مصنوعی", "هوش مصنوعی"}:
                await self._send_text(chat_id, self.ai_text())
            elif text in {"/ترید_فعال", "ترید فعال", "ترید روشن"}:
                self.storage.set_trade_enabled(True)
                await self._send_text(chat_id, "✅ ترید واقعی فعال شد.")
            elif text in {"/ترید_خاموش", "ترید خاموش", "ترید غیر فعال", "ترید غیرفعال"}:
                self.storage.set_trade_enabled(False)
                await self._send_text(chat_id, "✅ ترید واقعی خاموش شد. سیگنال‌ها عادی ثبت می‌شوند.")
            elif text.startswith("ترید دلار") or text.startswith("/ترید_دلار"):
                value = self._last_number(text)
                self.storage.set_margin_usdt(value)
                await self._send_text(chat_id, f"✅ دلار هر پوزیشن روی {value:.2f} USDT تنظیم شد.")
            elif text.startswith("ترید لوریج") or text.startswith("/ترید_لوریج"):
                value = int(self._last_number(text))
                self.storage.set_leverage(value)
                await self._send_text(chat_id, f"✅ لوریج روی {value}x تنظیم شد.")
            elif text.startswith("حداکثر پوزیشن") or text.startswith("/حداکثر_پوزیشن"):
                value = int(self._last_number(text))
                self.storage.set_max_positions(value)
                await self._send_text(chat_id, f"✅ حداکثر پوزیشن روی {value} تنظیم شد.")
            elif text.startswith("حداقل سود"):
                value = self._last_number(text)
                self.storage.set_min_profit_usdt(value)
                await self._send_text(chat_id, f"✅ حداقل سود دلاری روی {value:.2f} USDT تنظیم شد.")
            elif text.startswith("درصد سود"):
                value = self._last_number(text)
                self.storage.set_min_profit_pct(value)
                await self._send_text(chat_id, f"✅ حداقل درصد سود روی {value:.2f}% تنظیم شد.")
            elif text == "حذف آمار":
                await self._send_text(chat_id, "⚠️ برای صفر کردن آمار بنویس: حذف آمار تایید")
            elif text == "حذف آمار تایید":
                self.storage.reset_stats()
                await self._send_text(chat_id, "✅ آمار و سیگنال‌ها صفر شد.")
            elif text == "ریست یادگیری":
                await self._send_text(chat_id, "⚠️ برای ریست حافظه AI بنویس: ریست یادگیری تایید")
            elif text == "ریست یادگیری تایید":
                self.storage.reset_learning()
                await self._send_text(chat_id, "✅ حافظه AI ریست شد.")
            elif text.startswith("/آمار") or text.startswith("آمار") or text.startswith("امار"):
                parts = text.split()
                days = 7
                if len(parts) > 1:
                    try:
                        days = int(float(parts[-1]))
                    except ValueError:
                        days = 7
                await self._send_text(chat_id, self.stats_text(days))
            else:
                await self._send_text(chat_id, "دستور نامعتبر است. راهنما را بزن.")
        except Exception as exc:
            await self._send_text(chat_id, f"❌ خطا: {exc}")

    def ai_text(self) -> str:
        data = self.storage.ai_panel_stats()
        return (
            "🤖 پنل هوش مصنوعی\n\n"
            f"حافظه فعال: {data['learning_days']} روز\n"
            f"الگوهای ذخیره‌شده: {data['stored_patterns']}\n"
            f"الگوهای فعال 20 روز اخیر: {data['active_patterns']}\n\n"
            f"شکارهای درست: {data['hunter_tp']}\n"
            f"شکارهای SL شده: {data['hunter_sl']}\n\n"
            f"تحلیل‌های درست: {data['analysis_right']}\n"
            f"تحلیل‌های غلط: {data['analysis_wrong']}\n"
            f"میانگین AI Confidence: {data['avg_ai_confidence']:.1f}%\n\n"
            f"بهترین ارز/جهت: {data['best_symbol_side']}\n"
            f"بدترین ارز/جهت: {data['worst_symbol_side']}\n\n"
            f"ساعت‌های خوب: {data['good_sessions']}\n"
            f"ساعت‌های بد فقط عادی: {data['bad_sessions']}\n\n"
            f"بهترین الگوهای RSI/MACD/ADX:\n{data['best_indicator_patterns']}"
        )

    def stats_text(self, days: int) -> str:
        stats = self.storage.stats(days)
        def line(title: str, key: str) -> str:
            item = stats.get(key, {})
            return f"{title}: کل {item.get('total', 0)} | TP {item.get('tp', 0)} | SL {item.get('sl', 0)} | WR {item.get('win_rate', 0):.1f}% | PnL {item.get('pnl', 0):.2f}"
        return "📊 آمار " + str(days) + " روز\n\n" + "\n".join([
            line("همه", "all"), line("عادی", "normal"), line("واقعی", "real"), line("شکار", "hunter"), line("لانگ", "long"), line("شورت", "short"), line("Real Failed", "real_failed"),
        ])

    def help_text(self) -> str:
        return (
            "دستورات:\n"
            "پنل / وضعیت / ترید\n"
            "آمار یا آمار 7\n"
            "هوش مصنوعی / Ai / هوش / مصنوعی\n"
            "ترید فعال / ترید خاموش\n"
            "ترید دلار 20\n"
            "ترید لوریج 10\n"
            "حداکثر پوزیشن 3\n"
            "حداقل سود 1\n"
            "درصد سود 0.10\n"
            "حذف آمار / حذف آمار تایید\n"
            "ریست یادگیری / ریست یادگیری تایید"
        )

    async def _send_text(self, chat_id: int | str, text: str) -> None:
        if self.app is not None:
            await self.app.bot.send_message(chat_id=chat_id, text=text)

    def _last_number(self, text: str) -> float:
        matches = re.findall(r"[-+]?\d+(?:\.\d+)?", text)
        if not matches:
            raise ValueError("عدد پیدا نشد.")
        return float(matches[-1])
