from __future__ import annotations

"""
Telegram bot interface.

Responsibilities:
- Fast responses for status commands.
- Background tasks for heavy scans/auto signals.
- Preserve commands/options.
- Owner/user access control.
- No heavy scan inside fast commands unless explicitly requested.
"""

import asyncio
import time
from typing import Any, Dict, List, Optional

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
except Exception:  # Allows offline compile/import before requirements are installed.
    Update = Any  # type: ignore
    Application = Any  # type: ignore
    CommandHandler = MessageHandler = None  # type: ignore
    ContextTypes = Any  # type: ignore
    filters = None  # type: ignore

from config import BOT_TOKEN, OWNER_ID, DEFAULT_SYMBOLS, AUTO_SCAN_INTERVAL_SECONDS, AUTO_SIGNAL_ENABLED
from diagnostics import safe, record_error, health_report, tail_log
import users
import coins_fa
import ai_memory
import coin_learning
import coin_risk
import coin_rotation
import sr_learning
import ghost_signals
import slot_manager
import signal_tracker
import real_trade_manager
import real_position_sync
import market_scanner
import scanner
import reply_manager
import recovery_manager
import daily_report
import command_registry
import integration_status


AUTO_TASK_NAME = "auto_scan_loop"
TRACKER_TASK_NAME = "tracker_loop"


def _ts() -> int:
    return int(time.time())


def _uid(update: Update) -> int:
    return int(update.effective_user.id if update.effective_user else 0)


def _text(update: Update) -> str:
    return (update.message.text or "").strip() if update.message else ""


async def _reply(update: Update, text: str) -> None:
    if update.message:
        await update.message.reply_text(text)


def _allowed(update: Update) -> bool:
    return users.is_allowed(_uid(update))


def _owner(update: Update) -> bool:
    return users.is_owner(_uid(update))


async def require_access(update: Update) -> bool:
    if _allowed(update):
        return True
    await _reply(update, "⛔️ دسترسی نداری.")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update):
        return
    await _reply(update, "ربات AI Movement Hunter فعال است.\nدستور: هوش مصنوعی | ترید | وضعیت بازار | بهترین سیگنال")


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, f"ID شما:\n`{_uid(update)}`")


async def adduser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner(update):
        await _reply(update, "فقط مالک می‌تواند کاربر اضافه کند.")
        return
    if not context.args:
        await _reply(update, "مثال: /adduser 123456")
        return
    users.add_user(int(context.args[0]))
    await _reply(update, "✅ کاربر اضافه شد.")


async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner(update):
        await _reply(update, "فقط مالک می‌تواند کاربر حذف کند.")
        return
    if not context.args:
        await _reply(update, "مثال: /removeuser 123456")
        return
    users.remove_user(int(context.args[0]))
    await _reply(update, "✅ کاربر حذف شد.")


async def listusers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner(update):
        await _reply(update, "فقط مالک.")
        return
    await _reply(update, users.list_users_fa())


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update):
        return
    txt = _text(update)
    try:
        await dispatch_text(update, context, txt)
    except Exception as e:
        record_error(e, module="bot", function="handle_message", context={"text": txt})
        await _reply(update, "⚠️ خطای داخلی رخ داد. جزئیات در لاگ ذخیره شد.")


async def dispatch_text(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str) -> None:
    t = txt.strip()
    low = t.lower()

    # Fast status commands
    if t in {"هوش مصنوعی", "وضعیت هوش مصنوعی", "حافظه ربات"}:
        await _reply(update, ai_status_fa())
        return
    if t in {"ترید", "وضعیت ترید"}:
        await _reply(update, real_trade_manager.status_fa())
        return
    if t in {"اسلات‌ها", "اسلات ها"}:
        await _reply(update, slot_manager.summary_fa())
        return
    if t in {"سیگنال‌های مخفی", "سیگنال های مخفی"}:
        await _reply(update, ghost_signals.summary_fa())
        return
    if t in {"آمار", "آمار هوشمند"}:
        await _reply(update, smart_stats_fa())
        return
    if t in {"ریسک کوین‌ها", "ریسک کوین ها"}:
        await _reply(update, coin_risk.summary_fa())
        return
    if t in {"بهترین کوین‌ها", "بهترین کوین ها"}:
        await _reply(update, coin_rotation.summary_fa())
        return
    if t in {"رفتار کوین"}:
        await _reply(update, "مثال: رفتار کوین DOGE")
        return
    if t.startswith("رفتار کوین"):
        sym = coins_fa.normalize_symbol(t.replace("رفتار کوین", "").strip())
        await _reply(update, coin_behavior_fa(sym))
        return
    if t in {"وضعیت بازار", "بررسی بازار"}:
        msg = market_scanner.market_status_fa()
        await _reply(update, msg)
        return

    # Heavy commands: background-ish by sending quick ack, then running.
    if t in {"بهترین سیگنال"}:
        await _reply(update, "در حال بررسی بهترین سیگنال...")
        res = await asyncio.to_thread(scanner.best_signal)
        await _reply(update, res.get("message", "سیگنال مناسبی پیدا نشد."))
        return

    if t in {"بررسی", "اسکن"}:
        await _reply(update, "اسکن بازار شروع شد...")
        res = await asyncio.to_thread(scanner.scan_report_fa)
        await _reply(update, res)
        return

    # Settings commands
    if t in {"ترید روشن", "ترید فعال", "ترید واقعی روشن", "ترید واقعی فعال"}:
        real_trade_manager.set_trade_setting("real_trading_enabled", True)
        real_trade_manager.set_trade_setting("trade_mode", "REAL")
        await _reply(update, "✅ ترید واقعی روشن شد. حالت: REAL")
        return
    if t in {"ترید خاموش", "ترید غیرفعال", "ترید واقعی خاموش", "ترید واقعی غیرفعال"}:
        real_trade_manager.set_trade_setting("real_trading_enabled", False)
        real_trade_manager.set_trade_setting("trade_mode", "PAPER")
        await _reply(update, "✅ ترید واقعی خاموش شد. حالت: PAPER")
        return

    if t == "AI روشن" or t == "هوش مصنوعی روشن":
        real_trade_manager.set_trade_setting("ai_enabled", True)
        await _reply(update, "✅ هوش مصنوعی روشن شد.")
        return
    if t == "AI خاموش" or t == "هوش مصنوعی خاموش":
        real_trade_manager.set_trade_setting("ai_enabled", False)
        await _reply(update, "✅ هوش مصنوعی خاموش شد.")
        return
    if t == "یادگیری روشن":
        real_trade_manager.set_trade_setting("learning_enabled", True)
        await _reply(update, "✅ یادگیری روشن شد.")
        return
    if t == "یادگیری خاموش":
        real_trade_manager.set_trade_setting("learning_enabled", False)
        await _reply(update, "✅ یادگیری خاموش شد.")
        return

    if t.startswith("ترید دلار"):
        val = float(t.replace("ترید دلار", "").strip())
        real_trade_manager.set_trade_setting("position_size_usd", val)
        await _reply(update, f"✅ حجم هر پوزیشن شد {val}$")
        return
    if t.startswith("ترید لوریج"):
        val = int(t.replace("ترید لوریج", "").strip())
        val = max(1, min(50, val))
        real_trade_manager.set_trade_setting("leverage", val)
        await _reply(update, f"✅ لوریج شد {val}x")
        return
    if t.startswith("حداکثر پوزیشن"):
        val = int(t.replace("حداکثر پوزیشن", "").strip())
        val = max(1, min(20, val))
        real_trade_manager.set_trade_setting("max_positions", val)
        await _reply(update, f"✅ حداکثر پوزیشن شد {val}")
        return
    if t.startswith("سرمایه ترید"):
        val = float(t.replace("سرمایه ترید", "").strip())
        real_trade_manager.set_trade_setting("initial_capital", val)
        real_trade_manager.set_trade_setting("balance", val)
        real_trade_manager.set_trade_setting("protected_balance", val)
        await _reply(update, f"✅ سرمایه ترید شد {val}$")
        return
    if t.startswith("قفل ضرر"):
        # Example: قفل ضرر 5 1
        parts = t.split()
        if len(parts) >= 4:
            amount = float(parts[2])
            hours = float(parts[3])
            real_trade_manager.set_trade_setting("daily_loss_lock_amount", amount)
            real_trade_manager.set_trade_setting("daily_lock_hours", hours)
            await _reply(update, f"✅ قفل ضرر: {amount}$ برای {hours} ساعت")
        else:
            await _reply(update, "مثال: قفل ضرر 5 1")
        return

    if t == "توقف اضطراری روشن":
        real_trade_manager.set_trade_setting("emergency_stop", True)
        await _reply(update, "⛔️ توقف اضطراری روشن شد.")
        return
    if t == "توقف اضطراری خاموش":
        real_trade_manager.set_trade_setting("emergency_stop", False)
        await _reply(update, "✅ توقف اضطراری خاموش شد.")
        return

    if t == "گزارش روزانه روشن":
        daily_report.set_enabled(True)
        await _reply(update, "✅ گزارش روزانه روشن شد.")
        return
    if t == "گزارش روزانه خاموش":
        daily_report.set_enabled(False)
        await _reply(update, "✅ گزارش روزانه خاموش شد.")
        return
    if t == "گزارش روزانه":
        await _reply(update, daily_report.build_report_fa())
        return
    if t == "حالت محافظه‌کار":
        real_trade_manager.set_trade_setting("conservative_mode", True)
        await _reply(update, "✅ حالت محافظه‌کار فعال شد.")
        return
    if t == "حالت عادی":
        real_trade_manager.set_trade_setting("conservative_mode", False)
        await _reply(update, "✅ حالت عادی فعال شد.")
        return
    if t in {"بدترین کوین‌ها", "بدترین کوین ها"}:
        rot = coin_rotation.summary()
        worst = rot.get("worst", [])[:8]
        msg = "ضعیف‌ترین‌ها:\n" + ("\n".join(f"{x.get('key')} امتیاز {x.get('score')}" for x in worst) if worst else "داده کافی نیست.")
        await _reply(update, msg)
        return
    if t.startswith("آمار "):
        await _reply(update, smart_stats_fa())
        return
    if t == "حذف آمار":
        await _reply(update, "⚠️ ریست کامل آمار در مرحله نهایی با تایید جداگانه انجام می‌شود تا حافظه یادگیری پاک نشود.")
        return
    if t == "بازیابی":
        await _reply(update, recovery_manager.startup_report_fa())
        return

    if t == "گزارش خطا" and _owner(update):
        await _reply(update, tail_log(20) or "لاگ خالی است.")
        return

    await _reply(update, help_fa())


def ai_status_fa() -> str:
    return integration_status.full_status_fa()


def smart_stats_fa() -> str:
    return "\n\n".join([
        signal_tracker.summary_fa(),
        ai_memory.summary_fa(),
        coin_learning.summary_fa(),
        ghost_signals.summary_fa(),
    ])


def coin_behavior_fa(symbol: str) -> str:
    if not symbol:
        return "نماد را درست وارد کن. مثال: رفتار کوین DOGE"
    long_p = coin_learning.profile(symbol, "LONG").get("profile", {})
    short_p = coin_learning.profile(symbol, "SHORT").get("profile", {})
    return (
        f"🧬 رفتار {coins_fa.display_symbol(symbol)}\n"
        f"LONG: نمونه {long_p.get('samples',0)} | WR {long_p.get('win_rate',0)}% | {long_p.get('personality','UNKNOWN')}\n"
        f"SHORT: نمونه {short_p.get('samples',0)} | WR {short_p.get('win_rate',0)}% | {short_p.get('personality','UNKNOWN')}"
    )


def help_fa() -> str:
    return command_registry.help_fa()



async def send_routed_results(app: Application, routed: List[Dict[str, Any]]) -> None:
    """
    Send only user-visible routed signals and register their Telegram message IDs
    so later TP/SL replies stay attached to the original message.
    Ghosts are tracked silently unless explicitly requested.
    """
    if not OWNER_ID:
        return
    for r in routed or []:
        if not r.get("ok") or not r.get("routed"):
            continue
        typ = str(r.get("type", ""))
        decision = r.get("decision", {}) or {}
        signal_id = r.get("signal_id") or r.get("trade", {}).get("signal_id") or decision.get("record_id")
        if not signal_id:
            continue
        # Keep Ghost silent by default.
        if "GHOST" in typ:
            continue
        if typ == "SETUP":
            text = reply_manager.setup_message_fa(decision)
        else:
            text = reply_manager.active_signal_message_fa(decision)
        msg = await app.bot.send_message(chat_id=OWNER_ID, text=text)
        reply_manager.register_signal_message(
            signal_id=str(signal_id),
            chat_id=OWNER_ID,
            message_id=msg.message_id,
            symbol=decision.get("symbol", ""),
            direction=decision.get("direction", ""),
            signal_type=typ,
        )


async def auto_scan_loop(app: Application) -> None:
    while True:
        try:
            if AUTO_SIGNAL_ENABLED:
                # Heavy work in thread so event loop stays responsive.
                routed = await asyncio.to_thread(scanner.auto_scan_and_route)
                await send_routed_results(app, routed)
        except Exception as e:
            record_error(e, module="bot", function="auto_scan_loop")
        await asyncio.sleep(max(30, int(AUTO_SCAN_INTERVAL_SECONDS)))


async def tracker_loop(app: Application) -> None:
    while True:
        try:
            await asyncio.to_thread(scanner.update_active_from_market)
            routed = await asyncio.to_thread(scanner.process_watching_setups)
            await send_routed_results(app, routed)
            await asyncio.to_thread(real_position_sync.confirm_all_pending_slots)
            await asyncio.to_thread(reply_manager.queue_recent_results_once)
            await asyncio.to_thread(market_scanner.scan_market, DEFAULT_SYMBOLS[:20], None, 120, None, True)
        except Exception as e:
            record_error(e, module="bot", function="tracker_loop")
        await asyncio.sleep(20)


async def post_init(app: Application) -> None:
    recovery_manager.startup_recovery()
    app.create_task(auto_scan_loop(app), name=AUTO_TASK_NAME)
    app.create_task(tracker_loop(app), name=TRACKER_TASK_NAME)
    app.create_task(reply_sender_loop(app), name="reply_sender_loop")
    app.create_task(daily_report_loop(app), name="daily_report_loop")



async def reply_sender_loop(app: Application) -> None:
    while True:
        try:
            rows = reply_manager.pop_pending_replies(20)
            for r in rows:
                await app.bot.send_message(
                    chat_id=r["chat_id"],
                    text=r["text"],
                    reply_to_message_id=r.get("reply_to_message_id"),
                )
        except Exception as e:
            record_error(e, module="bot", function="reply_sender_loop")
        await asyncio.sleep(3)


async def daily_report_loop(app: Application) -> None:
    while True:
        try:
            msg = daily_report.maybe_build_daily_report(force=False)
            if msg and OWNER_ID:
                await app.bot.send_message(chat_id=OWNER_ID, text=msg)
        except Exception as e:
            record_error(e, module="bot", function="daily_report_loop")
        await asyncio.sleep(1800)


def initialize_all() -> None:
    users.initialize()
    ai_memory.initialize_memory_files()
    coin_learning.initialize()
    coin_risk.initialize()
    coin_rotation.initialize()
    sr_learning.initialize()
    ghost_signals.initialize()
    slot_manager.initialize()
    signal_tracker.initialize()
    real_trade_manager.initialize()
    reply_manager.initialize()
    daily_report.initialize()


def build_app() -> Application:
    if Application is Any or Application is None or CommandHandler is None or MessageHandler is None or filters is None:
        raise RuntimeError("python-telegram-bot is not installed. Run: pip install -r requirements.txt")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("adduser", adduser_cmd))
    app.add_handler(CommandHandler("removeuser", removeuser_cmd))
    app.add_handler(CommandHandler("listusers", listusers_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


def main() -> None:
    app = build_app()
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
