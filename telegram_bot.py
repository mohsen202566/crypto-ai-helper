from __future__ import annotations

from telegram import Message, Update
from telegram.ext import ContextTypes

from ai_brain import SignalDecision
from config import BOT_NAME, OWNER_ID, TELEGRAM_CHAT_ID
from storage import Storage, StoredSignal
from trade_manager import CreatedSignal, TradeManager
from utils import duration_text, money, normalize_digits, parse_float, parse_int, pct


class TelegramBotUI:
    def __init__(self, storage: Storage, trade_manager: TradeManager) -> None:
        self.storage = storage
        self.trade_manager = trade_manager
        self.app = None

    def bind_app(self, app) -> None:
        self.app = app

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            elif low in {"هوش", "هوش مصنوعی", "ai"}:
                await message.reply_text(self.ai_text())
            elif low in {"اسکن", "scan"}:
                await message.reply_text(self.scan_text())
            elif low in {"ردها", "دلایل رد", "rejects"}:
                await message.reply_text(self.rejections_text())
            elif low in {"ترید روشن", "trade on"}:
                self.storage.set_trade_enabled(True)
                await message.reply_text("ترید واقعی اسپات روشن شد.")
            elif low in {"ترید خاموش", "trade off"}:
                self.storage.set_trade_enabled(False)
                await message.reply_text("ترید واقعی اسپات خاموش شد؛ سیگنال‌های عادی برای یادگیری ادامه دارند.")
            elif low in {"اتو سیگنال روشن", "اتوسیگنال روشن", "سیگنال روشن", "auto signal on"}:
                self.storage.set_auto_signals_enabled(True)
                await message.reply_text("اتوسیگنال روشن شد.")
            elif low in {"اتو سیگنال خاموش", "اتوسیگنال خاموش", "سیگنال خاموش", "auto signal off"}:
                self.storage.set_auto_signals_enabled(False)
                await message.reply_text("اتوسیگنال خاموش شد؛ پوزیشن‌های باز همچنان مانیتور می‌شوند.")
            elif low.startswith("ترید دلار") or low.startswith("trade dollar"):
                value = parse_float(text)
                self.storage.set_trade_usdt(value)
                await message.reply_text(f"دلار هر پوزیشن اسپات تنظیم شد: {value:.2f} USDT")
            elif low.startswith("حداکثر پوزیشن") or low.startswith("max"):
                value = parse_int(text)
                self.storage.set_max_positions(value)
                await message.reply_text(f"حداکثر پوزیشن همزمان تنظیم شد: {value}")
            elif low == "حذف آمار تایید":
                self.storage.reset_stats()
                await message.reply_text("آمار و یادگیری پاک شد.")
            elif low == "حذف آمار":
                await message.reply_text("برای تایید بنویس: حذف آمار تایید")
            else:
                await message.reply_text(self.help_text())
        except Exception as exc:
            await message.reply_text(f"خطا: {exc}")

    async def send_signal(self, *, decision: SignalDecision, created: CreatedSignal) -> int | None:
        if self.app is None:
            return None
        icon = "🟢"
        text = (
            f"{icon} سیگنال خرید SPOT\n"
            f"━━━━━━━━━━━━━━\n"
            f"ارز: {decision.symbol_name}\n"
            f"نوع: {created.signal_type.upper()}\n"
            f"ورود: {decision.entry:.8f}\n"
            f"هدف فروش: {decision.target:.8f} ({pct(decision.target_distance_pct)})\n"
            f"سود تقریبی بعد کارمزد: {money(decision.estimated_net_profit_usdt)}\n"
            f"زمان احتمالی باز بودن: {duration_text(decision.expected_hold_minutes * 60)}\n"
            f"اعتماد AI: {decision.confidence}% | نمونه بازه: {decision.samples}\n"
            f"بازار: {decision.market_state} | {decision.alignment}\n"
            f"اندیکاتورها: {decision.indicator_profile}\n"
            f"━━━━━━━━━━━━━━\n"
            f"دلیل AI: {decision.reason[:1200]}"
        )
        msg: Message = await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
        self.storage.update_message_id(created.signal_id, msg.message_id)
        return msg.message_id

    async def send_result(self, signal: StoredSignal, status: str, exit_price: float, approx_pnl: float, real_pnl: float | None, result_source: str) -> int | None:
        if self.app is None:
            return None
        icon = "✅" if status == "TARGET" else "❌"
        pnl_text = money(real_pnl if real_pnl is not None else approx_pnl)
        text = (
            f"{icon} نتیجه سیگنال SPOT\n"
            f"━━━━━━━━━━━━━━\n"
            f"ارز: {signal.symbol_name}\n"
            f"نوع: {signal.signal_type.upper()} / {result_source}\n"
            f"ورود: {signal.entry_price:.8f}\n"
            f"خروج/فروش: {exit_price:.8f}\n"
            f"هدف: {signal.target_price:.8f}\n"
            f"سود واقعی/نهایی: {pnl_text}\n"
            f"MFE: {pct(signal.mfe_pct)} | MAE: {pct(signal.mae_pct)}\n"
            f"وضعیت: {'هدف رسید' if status == 'TARGET' else 'ناموفق'}\n"
            f"یادگیری: نتیجه وارد حافظه AI شد."
        )
        msg = await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, reply_to_message_id=signal.message_id)
        return msg.message_id

    async def send_warning(self, signal: StoredSignal, current_price: float, reason: str) -> int | None:
        if self.app is None:
            return None
        distance = (signal.target_price - current_price) / current_price if current_price > 0 else 0.0
        text = (
            f"⚠️ هشدار سیگنال فعال SPOT\n"
            f"━━━━━━━━━━━━━━\n"
            f"ارز: {signal.symbol_name}\n"
            f"نوع: {signal.signal_type.upper()}\n"
            f"ورود: {signal.entry_price:.8f}\n"
            f"قیمت فعلی: {current_price:.8f}\n"
            f"هدف فروش: {signal.target_price:.8f}\n"
            f"فاصله تا هدف: {pct(distance)}\n"
            f"سود احتمالی: {money(signal.estimated_net_profit_usdt)}\n"
            f"دلیل هشدار: {reason}\n"
            f"اقدام: فروش اجباری انجام نمی‌شود؛ هشدار برای اطلاع و یادگیری ثبت شد."
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
            f"وضعیت ترید واقعی اسپات: {real_status}\n"
            f"دلار هر پوزیشن: {data.trade_usdt:.2f} USDT\n"
            f"حداکثر پوزیشن: {data.max_positions}\n"
            f"اسلات: {data.filled_slots}/{data.max_positions} | خالی {data.empty_slots}\n"
            f"موجودی USDT توبیت: {money(data.wallet_usdt)}\n"
            f"سفارش‌های باز توبیت: {data.open_orders if data.open_orders is not None else '-'}\n"
            f"سود امروز: {money(data.today_pnl)}\n"
            f"سود کلی: {money(data.total_pnl)}\n"
            f"اعتماد AI: {data.ai_confidence:.1f}%\n"
            f"TP امروز: {data.today_stats.get('target', 0)} | WinRate {data.today_stats.get('win_rate', 0):.1f}%\n\n"
            f"دستورات: ترید روشن/خاموش | اتو سیگنال روشن/خاموش | ترید دلار 10 | حداکثر پوزیشن 5 | آمار | هوش | اسکن | ردها"
        )

    def stats_text(self) -> str:
        stats = self.storage.all_stats()
        return (
            f"📊 آمار کل\n"
            f"کل سیگنال‌ها: {stats['total']}\n"
            f"باز: {stats['open']} | بسته: {stats['closed']} | ناموفق: {stats['failed']}\n"
            f"Real: {stats['real']} | Normal: {stats['normal']}\n"
            f"هدف‌های رسیده: {stats['target']}\n"
            f"WinRate: {stats['win_rate']:.1f}%\n"
            f"سود/ضرر کل: {money(stats['pnl'])}"
        )

    def ai_text(self) -> str:
        data = self.storage.ai_summary()
        best = data.get("best") or {}
        worst = data.get("worst") or {}
        suggestions = data.get("suggestions", [])
        requests = data.get("requests", [])
        warnings = data.get("warnings", [])
        sug = "\n".join(f"- {x['message']}" for x in suggestions) or "فعلاً پیشنهادی نیست."
        req = "\n".join(f"- {x['reason']}" for x in requests) or "فعلاً درخواست اندیکاتور نیست."
        warn = "\n".join(f"- {x['reason']}" for x in warnings) or "هشدار فعالی نیست."
        return (
            f"🧠 پنل هوش AI\n"
            f"اعتماد کلی AI: {data.get('confidence', 0):.1f}%\n"
            f"نمونه‌های یادگیری: {data.get('total_samples', 0)}\n"
            f"بهترین ارز: {best.get('symbol_name', '-')} | WR {best.get('win_rate', 0):.1f}% | Net {best.get('net_profit', 0):.4f}\n"
            f"بدترین ارز: {worst.get('symbol_name', '-')} | WR {worst.get('win_rate', 0):.1f}% | Net {worst.get('net_profit', 0):.4f}\n"
            f"پیشنهاد دلار/ریسک:\n{sug}\n"
            f"هشدارهای فعال:\n{warn}\n"
            f"درخواست اندیکاتور:\n{req}"
        )

    def scan_text(self) -> str:
        info = self.storage.scan_info()
        return (
            f"📡 وضعیت اسکن\n"
            f"آخرین اسکن: {info.get('time', '-')}\n"
            f"اتوسیگنال: {'روشن' if self.storage.auto_signals_enabled() else 'خاموش'}\n"
            f"بررسی‌شده: {info.get('checked', 0)}\n"
            f"سیگنال ساخته‌شده: {info.get('created', 0)}\n"
            f"رد شده: {info.get('rejected', 0)}\n"
            f"خطا: {info.get('errors', 0)}"
        )

    def rejections_text(self) -> str:
        rows = self.storage.recent_rejections(12)
        if not rows:
            return "ردی ثبت نشده است."
        lines = ["📋 آخرین دلایل رد"]
        for row in rows:
            lines.append(f"{row['symbol_name']}: {row['reason'][:160]}")
        return "\n".join(lines)

    @staticmethod
    def help_text() -> str:
        return "دستورات: ترید، آمار، هوش، اسکن، ردها، اتو سیگنال روشن/خاموش، ترید روشن/خاموش، ترید دلار 10، حداکثر پوزیشن 5، حذف آمار"
