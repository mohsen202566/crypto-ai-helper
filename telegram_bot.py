from __future__ import annotations

from telegram import Message
from telegram.ext import ContextTypes

from ai_brain import SignalDecision
from config import BOT_NAME, OWNER_ID, TELEGRAM_CHAT_ID
from storage import Storage, StoredSignal
from trade_manager import CreatedSignal, TradeManager
from utils import money, normalize_digits, parse_float, parse_int, pct, round_trip_fee_usdt, pnl_breakdown_for_exit, net_pnl_for_exit


class TelegramBotUI:
    def __init__(self, storage: Storage, trade_manager: TradeManager) -> None:
        self.storage = storage
        self.trade_manager = trade_manager
        self.app = None

    def bind_app(self, app) -> None:
        self.app = app

    async def handle_text(self, update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or message.text is None:
            return
        if OWNER_ID and update.effective_user and int(update.effective_user.id) != OWNER_ID:
            return
        text = normalize_digits(message.text.strip())
        low = text.lower()
        try:
            if low in {"/start", "start", "پنل", "panel", "status", "ترید"}:
                await message.reply_text(await self.panel_text())
            elif low in {"آمار", "stats"}:
                await message.reply_text(self.stats_text())
            elif low in {"هوش", "هوش مصنوعی", "ai", "AI".lower()}:
                await message.reply_text(self.ai_text())
            elif low in {"ترید روشن", "trade on"}:
                self.storage.set_trade_enabled(True)
                await message.reply_text("ترید واقعی روشن شد.")
            elif low in {"ترید خاموش", "trade off"}:
                self.storage.set_trade_enabled(False)
                await message.reply_text("ترید واقعی خاموش شد. اتوسیگنال طبق تنظیم خودش جداگانه کنترل می‌شود.")
            elif low in {"اتو سیگنال روشن", "اتوسیگنال روشن", "سیگنال روشن", "auto signal on"}:
                self.storage.set_auto_signals_enabled(True)
                await message.reply_text("اتوسیگنال روشن شد؛ ربات اجازه دارد سیگنال Normal/Real جدید صادر کند.")
            elif low in {"اتو سیگنال خاموش", "اتوسیگنال خاموش", "سیگنال خاموش", "auto signal off"}:
                self.storage.set_auto_signals_enabled(False)
                await message.reply_text("اتوسیگنال خاموش شد؛ سیگنال جدید صادر نمی‌شود، ولی پوزیشن‌ها و سیگنال‌های باز همچنان مانیتور می‌شوند.")
            elif low.startswith("ترید دلار") or low.startswith("margin"):
                value = parse_float(text.split()[-1])
                self.storage.set_margin_usdt(value)
                await message.reply_text(f"دلار هر پوزیشن تنظیم شد: {value:.2f} USDT")
            elif low.startswith("ترید لوریج") or low.startswith("leverage"):
                value = parse_int(text.split()[-1])
                self.storage.set_leverage(value)
                await message.reply_text(f"لوریج تنظیم شد: {value}x")
            elif low.startswith("حداکثر پوزیشن") or low.startswith("max"):
                value = parse_int(text.split()[-1])
                self.storage.set_max_positions(value)
                await message.reply_text(f"حداکثر پوزیشن واقعی تنظیم شد: {value}")
            elif low == "حذف آمار تایید":
                self.storage.reset_stats()
                await message.reply_text("آمار و یادگیری پاک شد.")
            elif low == "حذف آمار":
                await message.reply_text("برای تایید بنویس: حذف آمار تایید")
            else:
                await message.reply_text(self.help_text())
        except Exception as exc:
            await message.reply_text(f"خطا: {exc}")

    async def send_guard_alert(self, text: str) -> int | None:
        if self.app is None:
            return None
        msg: Message = await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        return msg.message_id

    async def send_signal(self, *, symbol_name: str, decision: SignalDecision, created: CreatedSignal) -> int | None:
        if self.app is None:
            return None

        direction_icon = self._direction_icon(decision.direction)
        signal_type = created.signal_type.upper()
        margin = self.storage.margin_usdt()
        leverage = self.storage.leverage()
        fee = round_trip_fee_usdt(margin, leverage)
        sl_net = net_pnl_for_exit(margin, leverage, decision.direction or "LONG", decision.entry, decision.sl) if decision.direction else 0.0
        text = (
            f"{direction_icon} سیگنال {signal_type}\n"
            f"━━━━━━━━━━━━━━\n"
            f"ارز: {symbol_name}\n"
            f"جهت: {direction_icon} {decision.direction}\n"
            f"ورود: {decision.entry:.8f}\n\n"
            f"🎯 TP: {decision.tp:.8f} ({pct(decision.tp_distance_pct)})\n"
            f"🛑 SL: {decision.sl:.8f} ({pct(decision.sl_distance_pct)})\n"
            f"RR: {decision.risk_reward:.2f}\n"
            f"کارمزد رفت‌وبرگشت تقریبی: {money(-fee)}\n"
            f"سود خالص احتمالی TP: {money(decision.estimated_net_profit_usdt)}\n"
            f"ضرر خالص احتمالی SL: {money(sl_net)}\n\n"
            f"اعتماد AI: {decision.confidence}% | نمونه بازه: {decision.samples}\n"
            f"وضعیت بازار: {decision.market_state}\n"
            f"تایم‌های بالا: {decision.alignment}\n"
            f"اندیکاتورها:\n{decision.indicator_profile}\n\n"
            f"دلیل:\n{decision.reason[:1200]}"
        )
        msg: Message = await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        self.storage.update_message_id(created.signal_id, msg.message_id)
        return msg.message_id

    async def send_result(self, signal: StoredSignal, status: str, exit_price: float, approx_pnl: float, real_pnl: float | None, result_source: str) -> int | None:
        if self.app is None:
            return None

        status_icon = self._result_icon(status)
        direction_icon = self._direction_icon(signal.direction)
        status_label = status.upper()
        bd = pnl_breakdown_for_exit(signal.margin_usdt, signal.leverage, signal.direction, signal.entry, exit_price)
        text = (
            f"{status_icon} نتیجه سیگنال: {status_label}\n"
            f"━━━━━━━━━━━━━━\n"
            f"ارز: {signal.symbol_name}\n"
            f"نوع: {signal.signal_type} / {result_source}\n"
            f"جهت: {direction_icon} {signal.direction}\n\n"
            f"ورود: {signal.entry:.8f}\n"
            f"خروج: {exit_price:.8f}\n"
            f"TP: {signal.tp:.8f}\n"
            f"SL: {signal.sl:.8f}\n\n"
            f"سود/ضرر خام تقریبی: {money(bd['gross'])}\n"
            f"کارمزد تقریبی: {money(-bd['fee'])}\n"
            f"سود/ضرر خالص تقریبی: {money(approx_pnl)}\n"
            f"سود/ضرر واقعی Toobit: {money(real_pnl)}\n"
            f"MFE: {pct(signal.mfe_pct)} | MAE: {pct(signal.mae_pct)}\n\n"
            f"دلیل استاپ/نتیجه در حافظه AI ثبت شد."
        )
        msg = await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, reply_to_message_id=signal.message_id)
        return msg.message_id

    async def panel_text(self) -> str:
        data = await self.trade_manager.panel_data()
        auto_status = "روشن 🟢" if data.auto_signals_enabled else "خاموش 🔴"
        real_status = "روشن 🟢" if data.trade_enabled else "خاموش ⛔"
        return (
            f"⚙️ پنل ترید {BOT_NAME}\n"
            f"وضعیت اتوسیگنال: {auto_status}\n"
            f"وضعیت ترید واقعی: {real_status}\n"
            f"مبلغ معامله: {data.margin_usdt:.2f} USDT\n"
            f"لوریج: {data.leverage}x\n"
            f"حداکثر پوزیشن واقعی: {data.max_positions}\n"
            f"اسلات واقعی: {data.filled_slots}/{data.max_positions} | خالی {data.empty_slots} | درحال بازشدن {data.pending_slots}\n"
            f"موجودی Toobit: {money(data.wallet_margin_usdt)}\n"
            f"پوزیشن/سفارش صرافی: {data.exchange_open_positions}/{data.exchange_open_orders}\n"
            f"PNL خالص قابل معامله امروز (Real+Normal): {money(float(data.today_stats.get('tradable_pnl', 0)))}\n"
            f"PNL آموزشی Watch امروز: {money(float(data.today_stats.get('watch_pnl', 0)))} | کل آزمایشی: {money(float(data.today_stats.get('pnl', 0)))}\n"
            f"Normal خالص امروز: {money(float(data.today_stats.get('normal_pnl', 0)))} | Real خالص امروز: {money(float(data.today_stats.get('real_pnl', 0)))}\n"
            f"کارمزد تقریبی امروز: {money(-float(data.today_stats.get('fees', 0)))}\n"
            f"TP/SL امروز: {data.today_stats.get('tp', 0)}/{data.today_stats.get('sl', 0)} | WinRate {data.today_stats.get('win_rate', 0):.1f}%\n\n"
            f"نکته: اگر اتوسیگنال خاموش باشد سیگنال جدید صادر نمی‌شود؛ اما سیگنال‌های باز همچنان مانیتور و نتیجه‌شان ثبت می‌شود.\n\n"
            f"دستورات مهم:\n"
            f"ترید | اتو سیگنال روشن | اتو سیگنال خاموش\n"
            f"ترید روشن | ترید خاموش\n"
            f"ترید دلار 10 | ترید لوریج 8 | حداکثر پوزیشن 5\n"
            f"آمار | هوش | حذف آمار"
        )

    def stats_text(self) -> str:
        stats = self.storage.all_stats()
        return (
            f"📊 آمار کل\n"
            f"کل سیگنال‌ها: {stats['total']}\n"
            f"باز: {stats['open']} | بسته: {stats['closed']}\n"
            f"Real: {stats['real']} | Normal: {stats['normal']} | Watch: {stats.get('watch', 0)}\n"
            f"TP: {stats['tp']} | SL: {stats['sl']}\n"
            f"WinRate: {stats['win_rate']:.1f}%\n"
            f"سود/ضرر خالص قابل معامله (Real+Normal): {money(stats.get('tradable_pnl', 0))}\n"
            f"Normal خالص: {money(stats.get('normal_pnl', 0))} | Real خالص: {money(stats.get('real_pnl', 0))}\n"
            f"Watch آموزشی خالص: {money(stats.get('watch_pnl', 0))} | کل آزمایشی: {money(stats['pnl'])}\n"
            f"کارمزد تقریبی کل: {money(-float(stats.get('fees', 0)))}"
        )

    def ai_text(self) -> str:
        data = self.storage.ai_summary()
        best = data.get("best") or {}
        worst = data.get("worst") or {}
        suggestions = data.get("suggestions", [])
        requests = data.get("requests", [])
        sug = "\n".join(f"- {x['message']}" for x in suggestions) or "فعلاً پیشنهادی نیست."
        req = "\n".join(f"- {x['reason']}" for x in requests) or "فعلاً درخواست اندیکاتور نیست."
        risk_profiles = self.storage.top_time_risk_profiles(3)
        risk_text = "\n".join(
            f"- {x.get('session_name','-')} {x.get('hour_bucket','-')} {x.get('weekday','-')} | {x.get('action','-')} | SL {x.get('sl',0)}/{x.get('samples',0)} | {x.get('main_cause','-')}"
            for x in risk_profiles
        ) or "فعلاً ساعت/سشن پرریسک یادگرفته‌شده نیست."
        adaptive_profiles = self.storage.top_adaptive_fix_profiles(4)
        adaptive_text = "\n".join(
            f"- {x.get('scope','-')} {x.get('symbol_name','-')} {x.get('direction','-')} | {x.get('recommended_action','-')} | سطح {x.get('treatment_level',0)} | تست {x.get('tests',0)} موفق {x.get('successes',0)} شکست {x.get('failures',0)} | TP {x.get('tp',0)}/SL {x.get('sl',0)} | {x.get('last_cause','-')}"
            for x in adaptive_profiles
        ) or "فعلاً درمان یادگرفته‌شده فعال نیست."
        loss_cases = self.storage.latest_loss_cases(3)
        loss_text = "\n".join(
            f"- {x.get('symbol_name','-')} {x.get('direction','-')} | {x.get('primary_cause','-')} | {x.get('fix_policy','-')}"
            for x in loss_cases
        ) or "فعلاً پرونده استاپ جدیدی نیست."
        cooldown = self.storage.guard_cooldown()
        cooldown_text = cooldown.get("reason") or "غیرفعال"
        return (
            f"🧠 پنل AI\n"
            f"نمونه‌های یادگیری: {data.get('total_samples', 0)}\n"
            f"اعتماد کلی AI: {data.get('confidence', 0):.1f}%\n"
            f"بهترین: {best.get('symbol_name', '-')} {best.get('direction', '-')} | WR {best.get('win_rate', 0):.1f}% | Net {best.get('net_profit', 0):.4f}\n"
            f"بدترین: {worst.get('symbol_name', '-')} {worst.get('direction', '-')} | WR {worst.get('win_rate', 0):.1f}% | Net {worst.get('net_profit', 0):.4f}\n"
            f"وضعیت ترمز ضرر: {cooldown_text[:300]}\n"
            f"ساعت/سشن‌های پرریسک یادگرفته‌شده:\n{risk_text}\n"
            f"درمان‌های یادگیری فعال:\n{adaptive_text}\n"
            f"آخرین پرونده‌های بازپرس استاپ:\n{loss_text}\n"
            f"پیشنهاد دلار/لوریج:\n{sug}\n"
            f"درخواست اندیکاتور:\n{req}"
        )

    @staticmethod
    def _direction_icon(direction: str | None) -> str:
        if direction == "LONG":
            return "🟢"
        if direction == "SHORT":
            return "🔴"
        return "⚪"

    @staticmethod
    def _result_icon(status: str) -> str:
        if status.upper() == "TP":
            return "✅"
        if status.upper() == "SL":
            return "❌"
        return "⚪"

    @staticmethod
    def help_text() -> str:
        return "دستورات: ترید/پنل، آمار، هوش، اتو سیگنال روشن، اتو سیگنال خاموش، ترید روشن، ترید خاموش، ترید دلار 10، ترید لوریج 8، حداکثر پوزیشن 5، حذف آمار"
