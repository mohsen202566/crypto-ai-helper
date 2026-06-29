from __future__ import annotations

import re
from typing import Any

from config import BOT_NAME, OWNER_ID, TELEGRAM_CHAT_ID
from scorer import SignalDecision
from storage import Storage, StoredSignal
from symbols import ALTERNATIVE_SYMBOLS, MAIN_SYMBOLS
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
    return f"{value:.4f} USDT"


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
        # Watch/ready alerts are internal only.
        # Telegram receives only final signals, TP/SL results, and command reports.
        return None


    async def send_signal(self, *, symbol_name: str, decision: SignalDecision, created: CreatedSignal) -> int | None:
        if self.app is None or decision.direction is None:
            return None
        color = "🟢" if decision.direction == "LONG" else "🔴"
        direction_fa = "لانگ" if decision.direction == "LONG" else "شورت"
        text = (
            f"{color} سیگنال ۱ ساعته {direction_fa}\n\n"
            f"ارز: {symbol_name}\n"
            f"نوع: {created.signal_label}\n"
            f"امتیاز: {decision.score}/{decision.threshold} | Real {decision.real_threshold}\n"
            f"کیفیت AI: {decision.entry_quality}\n"
            f"دقت ورود AI: {decision.entry_precision_pct:.1f}%\n"
            f"اعتماد AI: {decision.ai_confidence}% / تجربه: {decision.ai_experience}\n"
            f"اثر AI: {decision.ai_effect} / adj {decision.ai_adjustment}\n"
            f"Threshold AI: {decision.threshold_source}\n"
            f"Market Mode: {decision.market_mode}\n\n"
            f"Entry: {fmt_price(decision.entry)}\n"
            f"TP: {fmt_price(decision.tp)}\n"
            f"SL: {fmt_price(decision.sl)}\n"
            f"RR: {decision.risk_reward:.2f}\n\n"
            f"سود خالص تخمینی: {fmt_money(decision.estimated_net_profit_usdt)}\n"
            f"Net Edge: {fmt_pct(decision.net_edge)}\n"
            f"وضعیت اجرا: {created.reason}\n\n"
            f"امتیازها: جهت {decision.breakdown.score_direction} | شروع {decision.breakdown.score_pre_ignition} | کندل {decision.breakdown.score_candle_entry} | دقت ورود {decision.breakdown.score_entry_precision} | AI {decision.breakdown.score_ai_memory} | TP/SL {decision.breakdown.score_tp_sl} | بازار {decision.breakdown.score_market_mode} | زمان {decision.breakdown.score_session} | سود {decision.breakdown.score_net_sync}\n\n"
            f"دلیل: {decision.reason}\n"
            f"پروفایل: {decision.indicator_profile[:650]}"
        )
        msg = await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        self.storage.update_message_id(created.signal_id, int(msg.message_id))
        return int(msg.message_id)

    async def send_result(self, signal: StoredSignal, status: str, approx_pnl: float, real_pnl: float | None, result_source: str | None = None) -> int | None:
        if self.app is None:
            return None
        direction_fa = "لانگ" if signal.direction == "LONG" else "شورت"
        result_fa = "تیپی خورد" if status == "TP" else "استاپ خورد" if status == "SL" else "خروج هوشمند AI"
        icon = "🟢" if status == "TP" else "🔴" if status == "SL" else "🟡"
        source_fa = {"toobit_real": "واقعی توبیت", "normal_on_real": "عادی روی سیگنال واقعی", "normal": "عادی ربات", "ghost_or_failed": "Ghost/Failed"}.get(result_source or signal.result_source or "", "عادی ربات")
        text = f"{icon} نتیجه {direction_fa}: {result_fa}\nارز: {signal.symbol_name or signal.toobit_symbol}\nنوع نتیجه: {source_fa}\nنوع سیگنال: {signal.hunter_type} / {signal.signal_type}\nکیفیت AI: {signal.entry_quality or '-'}\nسود/ضرر تقریبی: {fmt_money(approx_pnl)}"
        if signal.signal_type == "real":
            text += f"\nسود/ضرر واقعی Toobit: {fmt_money(real_pnl)}"
        msg = await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, reply_to_message_id=signal.message_id)
        return int(msg.message_id)

    async def send_panel(self, chat_id: int | str) -> None:
        data = await self.trade_manager.panel_data()
        await self._send_text(chat_id, self.panel_text(data))

    def panel_text(self, data: PanelData) -> str:
        status = "فعال ✅" if data.trade_enabled else "خاموش ⛔️"
        real_tpsl = data.today_stats.get("toobit_real_tp_sl", {})
        normal_tpsl = data.today_stats.get("normal_tp_sl", {})
        return (
            f"📌 پنل {BOT_NAME}\n\n"
            f"ترید واقعی: {status}\n"
            "بازار واقعی: Toobit Futures | دیتا: OKX\n"
            "تحلیل: 1H | ورود: 30m/15m | مانیتور TP/SL: 10 ثانیه\n\n"
            f"موجودی قابل استفاده: {fmt_money(data.wallet_margin_usdt)}\n"
            f"پوزیشن‌های باز واقعی: {'نامشخص' if data.exchange_open_positions is None else data.exchange_open_positions}\n"
            f"سفارش‌های باز واقعی: {'نامشخص' if data.exchange_open_orders is None else data.exchange_open_orders}\n\n"
            f"دلار هر پوزیشن: {data.margin_usdt:.2f} USDT\n"
            f"لوریج: {data.leverage}x\n"
            f"حداکثر پوزیشن: {data.max_positions}\n"
            f"اسلات پر/رزرو: {data.filled_slots} / در انتظار: {data.pending_slots} / خالی: {data.empty_slots}\n"
            f"نماد OKX خطادار: {data.symbol_health.get('okx_disabled', 0)}\n"
            f"نماد Toobit real غیرفعال: {data.symbol_health.get('toobit_real_disabled', 0)}\n\n"
            f"PnL واقعی Toobit/ربات: {fmt_money(data.today_real_pnl)}\n"
            f"PnL تقریبی عادی: {fmt_money(data.today_approx_pnl)}\n"
            f"TP/SL واقعی Toobit: TP {real_tpsl.get('tp', 0)} / SL {real_tpsl.get('sl', 0)} / WR {real_tpsl.get('win_rate', 0):.1f}%\n"
            f"TP/SL عادی ربات: TP {normal_tpsl.get('tp', 0)} / SL {normal_tpsl.get('sl', 0)} / WR {normal_tpsl.get('win_rate', 0):.1f}%"
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
            elif text.lower() in {"ai", "هوش", "مصنوعی", "هوش مصنوعی", "یادگیری", "پیشنهاد"}:
                await self._send_text(chat_id, self.ai_text())
            elif text in {"ارزها", "/ارزها"}:
                await self._send_text(chat_id, self.symbols_text())
            elif text in {"شکار", "زنده", "/شکار", "/زنده"}:
                await self._send_text(chat_id, self.hunt_text())
            elif text in {"ردها", "/ردها"}:
                await self._send_text(chat_id, self.rejections_text())
            elif text in {"سیگنالها", "سیگنال‌ها", "/سیگنالها", "/سیگنال‌ها"}:
                await self._send_text(chat_id, self.recent_signals_text())
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
                days = int(parts[-1]) if len(parts) > 1 and parts[-1].isdigit() else 7
                await self._send_text(chat_id, self.stats_text(days))
            else:
                await self._send_text(chat_id, "دستور نامشخص است. برای راهنما بنویس: راهنما")
        except Exception as exc:
            await self._send_text(chat_id, f"خطا: {exc}")

    def ai_text(self) -> str:
        data = self.storage.ai_panel_stats()
        return (
            "🧠 گزارش هوش مصنوعی\n\n"
            f"بازه یادگیری: {data['learning_days']} روز\n"
            f"الگوهای فعال/ثبت‌شده: {data['active_patterns']} / {data['stored_patterns']}\n"
            f"درست/غلط: {data['analysis_right']} / {data['analysis_wrong']}\n"
            f"میانگین اعتماد AI: {data['avg_ai_confidence']:.1f}%\n"
            f"بهترین ارز/جهت: {data['best_symbol_side']}\n"
            f"ضعیف‌ترین ارز/جهت: {data['worst_symbol_side']}\n\n"
            f"الگوهای قوی:\n{data['patterns']}\n\n"
            f"بازه‌های اندیکاتوری:\n{data['best_indicator_ranges']}\n\n"
            f"پیشنهادها:\n{data['suggestions']}"
        )

    def stats_text(self, days: int) -> str:
        data = self.storage.stats(days)
        all_stats = data.get("all", {})
        real = data.get("real", {})
        normal = data.get("normal", {})
        return (
            f"📊 آمار {days} روز\n\n"
            f"کل: TP {all_stats.get('tp', 0)} / SL {all_stats.get('sl', 0)} / WR {all_stats.get('win_rate', 0):.1f}% / PnL {fmt_money(all_stats.get('pnl', 0))}\n"
            f"واقعی: TP {real.get('tp', 0)} / SL {real.get('sl', 0)} / WR {real.get('win_rate', 0):.1f}% / PnL {fmt_money(real.get('pnl', 0))}\n"
            f"عادی: TP {normal.get('tp', 0)} / SL {normal.get('sl', 0)} / WR {normal.get('win_rate', 0):.1f}% / PnL {fmt_money(normal.get('pnl', 0))}"
        )

    def symbols_text(self) -> str:
        main = "، ".join(s.name for s in MAIN_SYMBOLS)
        alt = "، ".join(s.name for s in ALTERNATIVE_SYMBOLS)
        return f"🪙 ارزهای اصلی فعال:\n{main}\n\nجایگزین‌های AI بعد از یادگیری کافی:\n{alt}"

    def hunt_text(self) -> str:
        rows = self.storage.active_hunt_status(20)
        if not rows:
            return "🎯 وضعیت زنده شکار\n\nفعلاً Watch فعال ثبت نشده است."
        lines = ["🎯 وضعیت زنده شکار 1H\n"]
        for row in rows[:20]:
            lines.append(
                f"{row.get('symbol_name')} {row.get('direction') or '-'} | "
                f"score {row.get('score')} | AI {row.get('ai_confidence')}% | "
                f"updated {str(row.get('updated_at'))[:19]}"
            )
        return "\n".join(lines)

    def rejections_text(self) -> str:
        rows = self.storage.recent_rejections(20)
        if not rows:
            return "🧾 ردهای اخیر\n\nرد تحلیلی/hard فعلاً ثبت نشده است."
        lines = ["🧾 ردهای اخیر\n"]
        for row in rows[:20]:
            lines.append(
                f"#{row.get('id')} {str(row.get('created_at'))[:19]} | "
                f"{row.get('symbol_name')} {row.get('direction') or '-'} | "
                f"score {row.get('score')} | {row.get('code') or '-'} | {row.get('reason') or '-'}"
            )
        return "\n".join(lines)

    def recent_signals_text(self) -> str:
        rows = self.storage.recent_signals(20)
        if not rows:
            return "📡 سیگنال‌های اخیر\n\nفعلاً سیگنالی ثبت نشده است."
        lines = ["📡 سیگنال‌های اخیر\n"]
        for row in rows[:20]:
            lines.append(
                f"#{row.get('id')} {str(row.get('created_at'))[:19]} | "
                f"{row.get('symbol_name')} {row.get('direction')} | "
                f"score {row.get('score')}/{row.get('threshold')} R{row.get('real_threshold')} | "
                f"{row.get('signal_type')}/{row.get('real_status')} | {row.get('status')} | "
                f"{row.get('entry_quality') or '-'} | msg {row.get('message_id') or '-'}"
            )
        return "\n".join(lines)

    def help_text(self) -> str:
        return "پنل\nآمار\nآمار 7\nهوش\nیادگیری\nپیشنهاد\nارزها\nشکار\nزنده\nردها\nسیگنال‌ها\nترید فعال\nترید خاموش\nترید دلار 20\nترید لوریج 10\nحداکثر پوزیشن 3\nحذف آمار\nریست یادگیری"

    async def _send_text(self, chat_id: int | str, text: str) -> None:
        if self.app is None:
            return
        await self.app.bot.send_message(chat_id=chat_id, text=text)

    @staticmethod
    def _last_number(text: str) -> float:
        matches = re.findall(r"\d+(?:\.\d+)?", text)
        if not matches:
            raise ValueError("عدد پیدا نشد.")
        return float(matches[-1])
