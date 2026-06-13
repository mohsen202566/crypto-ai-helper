# -*- coding: utf-8 -*-
"""
Crypto AI Telegram Bot

نسخه سازگار با:
- AI Classic Direct Analysis
- Auto Signal
- Slot Manager
- Ghost Learning
- Signal Tracker
- Paper Trader
- Persian simple output
"""

import os
import time
import asyncio
import logging
from typing import Dict, List, Optional, Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from analysis import analyze_symbol
from scanner import (
    scan_for_auto_signals,
    get_best_signal,
    get_top_signals,
    scan_market_overview,
)

try:
    from signal_tracker import (
        add_signal_to_tracking,
        check_active_signals,
        format_active_signals,
        format_signal_stats,
        reset_signal_stats,
    )
except Exception:
    add_signal_to_tracking = None
    check_active_signals = None
    format_active_signals = None
    format_signal_stats = None
    reset_signal_stats = None

try:
    from paper_trader import (
        open_paper_position,
        format_paper_stats,
        format_open_positions,
        reset_paper_trades,
    )
except Exception:
    open_paper_position = None
    format_paper_stats = None
    format_open_positions = None
    reset_paper_trades = None

try:
    from ai_memory import format_ai_status
except Exception:
    format_ai_status = None

try:
    from coin_rotation import format_rotation_report
except Exception:
    format_rotation_report = None

try:
    from coin_learning import (
        format_learning_summary,
        format_coin_behavior,
        format_smart_stats,
    )
except Exception:
    format_learning_summary = None
    format_coin_behavior = None
    format_smart_stats = None


# ============================================================
# Config
# ============================================================

try:
    from config import (
        BOT_TOKEN,
        OWNER_ID,
        ALLOWED_USER_IDS,
        AUTO_SIGNAL_ENABLED,
        AUTO_SCAN_INTERVAL_MINUTES,
        AUTO_DIRECT_SCORE_MIN,
    )
except Exception:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)

    allowed_raw = os.getenv("ALLOWED_USER_IDS", "")
    ALLOWED_USER_IDS = [
        int(x.strip())
        for x in allowed_raw.split(",")
        if x.strip().isdigit()
    ]

    AUTO_SIGNAL_ENABLED = os.getenv(
        "AUTO_SIGNAL_ENABLED",
        "true",
    ).lower() == "true"

    AUTO_SCAN_INTERVAL_MINUTES = int(
        os.getenv("AUTO_SCAN_INTERVAL_MINUTES", "5")
    )

    AUTO_DIRECT_SCORE_MIN = int(
        os.getenv("AUTO_DIRECT_SCORE_MIN", "82")
    )


if OWNER_ID and OWNER_ID not in ALLOWED_USER_IDS:
    ALLOWED_USER_IDS.append(OWNER_ID)


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("crypto-ai-bot")


# ============================================================
# Runtime state
# ============================================================

LAST_AUTO_SIGNAL_TIME: Dict[str, int] = {}
AUTO_SIGNAL_COOLDOWN_SECONDS = 60 * 30


# ============================================================
# Access control
# ============================================================

def get_user_id(update: Update) -> int:
    try:
        return int(update.effective_user.id)
    except Exception:
        return 0


def is_allowed(update: Update) -> bool:
    user_id = get_user_id(update)

    if not OWNER_ID:
        return True

    if user_id == OWNER_ID:
        return True

    return user_id in ALLOWED_USER_IDS


async def reject_unauthorized(update: Update):
    if update.message:
        await update.message.reply_text(
            "⛔️ شما اجازه استفاده از این ربات را ندارید."
        )


# ============================================================
# Persian symbol mapping
# ============================================================

PERSIAN_SYMBOLS = {
    "بیتکوین": "BTCUSDT",
    "بیت کوین": "BTCUSDT",
    "btc": "BTCUSDT",
    "اتریوم": "ETHUSDT",
    "اتر": "ETHUSDT",
    "eth": "ETHUSDT",
    "سولانا": "SOLUSDT",
    "سول": "SOLUSDT",
    "sol": "SOLUSDT",
    "دوج": "DOGEUSDT",
    "دوج کوین": "DOGEUSDT",
    "doge": "DOGEUSDT",
    "ریپل": "XRPUSDT",
    "xrp": "XRPUSDT",
    "کاردانو": "ADAUSDT",
    "ada": "ADAUSDT",
    "آواکس": "AVAXUSDT",
    "avax": "AVAXUSDT",
    "بایننس": "BNBUSDT",
    "bnb": "BNBUSDT",
    "تون": "TONUSDT",
    "ton": "TONUSDT",
    "لینک": "LINKUSDT",
    "link": "LINKUSDT",
    "اپتوس": "APTUSDT",
    "apt": "APTUSDT",
    "آربیتروم": "ARBUSDT",
    "arb": "ARBUSDT",
    "پالیگان": "POLUSDT",
    "متیک": "POLUSDT",
    "matic": "POLUSDT",
}


def normalize_symbol_text(text: str) -> Optional[str]:
    text = str(text or "").strip().lower()

    cleaned = (
        text.replace("تحلیل", "")
        .replace("سیگنال", "")
        .replace("بررسی", "")
        .replace("خرید", "")
        .replace("فروش", "")
        .replace("/", "")
        .replace("-", "")
        .strip()
    )

    for key, symbol in PERSIAN_SYMBOLS.items():
        if key in cleaned:
            return symbol

    raw = cleaned.upper().replace(" ", "")

    if raw.endswith("USDT") and len(raw) >= 6:
        return raw

    if raw.isalpha() and 2 <= len(raw) <= 8:
        return f"{raw}USDT"

    return None

# ============================================================
# Formatting helpers
# ============================================================

def fa_direction(direction: str) -> str:
    if direction == "LONG":
        return "لانگ"
    if direction == "SHORT":
        return "شورت"
    return "بدون سیگنال"


def fa_status(result: Dict[str, Any]) -> str:
    if result.get("status") == "ACTIVE":
        return "✅ فعال"
    return "❌ بدون سیگنال"


def format_signal_message(result: Dict[str, Any]) -> str:
    symbol = result.get("symbol", "-")
    direction = result.get("direction", "NO TRADE")

    if direction == "NO TRADE" or result.get("status") != "ACTIVE":
        reasons = result.get("reasons", [])
        reason_text = "\n".join([f"• {x}" for x in reasons[:5]]) if reasons else "شرایط ورود کامل نیست."

        return (
            f"📊 تحلیل {symbol}\n\n"
            f"وضعیت: ❌ بدون سیگنال معتبر\n"
            f"امتیاز: {result.get('score', 0)}\n"
            f"لانگ: {result.get('long_score', 0)} | شورت: {result.get('short_score', 0)}\n\n"
            f"دلایل:\n{reason_text}"
        )

    return (
        f"🚨 سیگنال خودکار\n"
        f"نماد: {symbol}\n"
        f"جهت: {fa_direction(direction)}\n"
        f"وضعیت: ✅ ورود فعال\n\n"
        f"ورود: {result.get('entry')}\n"
        f"حد ضرر: {result.get('stop_loss')}\n"
        f"حد سود ۱: {result.get('tp1')}\n"
        f"حد سود ۲: {result.get('tp2')}\n\n"
        f"امتیاز: {result.get('score')}\n"
        f"ریسک: {result.get('risk_level')}\n"
        f"R/R: {result.get('risk_reward')}\n"
        f"اعتبار: {result.get('validity', '15 تا 45 دقیقه')}"
    )


def format_manual_analysis(result: Dict[str, Any]) -> str:
    symbol = result.get("symbol", "-")
    direction = result.get("direction", "NO TRADE")

    if direction == "NO TRADE" or result.get("status") != "ACTIVE":
        reasons = result.get("reasons", [])
        reason_text = "\n".join([f"• {x}" for x in reasons[:8]]) if reasons else "شرایط ورود کامل نیست."

        return (
            f"📊 تحلیل {symbol}\n\n"
            f"وضعیت: ❌ بدون سیگنال معتبر\n"
            f"امتیاز نهایی: {result.get('score', 0)}\n"
            f"لانگ: {result.get('long_score', 0)} | شورت: {result.get('short_score', 0)}\n"
            f"RSI: {result.get('rsi')}\n"
            f"ADX: {result.get('adx')}\n"
            f"VWAP: {result.get('vwap_status')}\n\n"
            f"دلایل:\n{reason_text}"
        )

    return (
        f"📊 تحلیل {symbol}\n\n"
        f"وضعیت: ✅ سیگنال فعال\n"
        f"جهت: {fa_direction(direction)}\n\n"
        f"ورود: {result.get('entry')}\n"
        f"حد ضرر: {result.get('stop_loss')}\n"
        f"حد سود ۱: {result.get('tp1')}\n"
        f"حد سود ۲: {result.get('tp2')}\n\n"
        f"امتیاز: {result.get('score')}\n"
        f"لانگ: {result.get('long_score')} | شورت: {result.get('short_score')}\n"
        f"ریسک: {result.get('risk_level')}\n"
        f"R/R: {result.get('risk_reward')}\n\n"
        f"RSI: {result.get('rsi')}\n"
        f"ADX: {result.get('adx')}\n"
        f"VWAP: {result.get('vwap_status')}\n"
        f"روند بازار: {result.get('market_regime')}\n\n"
        f"اعتبار: {result.get('validity', '15 تا 45 دقیقه')}"
    )


def format_top_signals(signals: List[Dict[str, Any]]) -> str:
    if not signals:
        return "فعلاً سیگنال مناسبی پیدا نشد."

    lines = ["🏆 بهترین سیگنال‌های فعلی:"]

    for i, sig in enumerate(signals, start=1):
        lines.append(
            f"\n{i}) {sig.get('symbol')} | {fa_direction(sig.get('direction'))}\n"
            f"امتیاز: {sig.get('score')} | ریسک: {sig.get('risk_level')}\n"
            f"ورود: {sig.get('entry')}\n"
            f"SL: {sig.get('stop_loss')} | TP1: {sig.get('tp1')}"
        )

    return "\n".join(lines)


def format_market_overview_text(result: Dict[str, Any]) -> str:

return (
        f"📌 بررسی کلی بازار\n\n"
        f"{result.get('summary')}\n\n"
        f"صعودی: {result.get('bullish_pct')}٪\n"
        f"نزولی: {result.get('bearish_pct')}٪\n"
        f"رنج/نامشخص: {result.get('neutral_pct')}٪\n"
        f"تعداد بررسی‌شده: {result.get('scanned')}"
    )


def attach_signal_metadata(
    signal: Dict[str, Any],
    message_id: int,
    chat_id: int,
    source: str = "auto_signal",
) -> Dict[str, Any]:
    sig = dict(signal)
    sig["telegram_message_id"] = message_id
    sig["chat_id"] = chat_id
    sig["source"] = source
    return sig

# ============================================================
# Commands
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    text = (
        "🤖 Crypto AI Bot\n\n"
        "دستورات:\n"
        "تحلیل بیتکوین\n"
        "تحلیل سولانا\n"
        "سیگنال دوج\n"
        "بهترین سیگنال\n"
        "بررسی بازار\n"
        "آمار\n"
        "پوزیشن‌ها\n"
        "هوش مصنوعی\n"
    )

    await update.message.reply_text(text)


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    await start_command(update, context)


# ============================================================
# Manual Analysis
# ============================================================

async def analyze_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    symbol: str,
):
    try:
        waiting = await update.message.reply_text(
            "⏳ در حال تحلیل..."
        )

        result = analyze_symbol(symbol)

        await waiting.edit_text(
            format_manual_analysis(result)
        )

    except Exception as e:
        await update.message.reply_text(
            f"❌ خطا در تحلیل:\n{str(e)[:200]}"
        )


# ============================================================
# Best Signals
# ============================================================

async def best_signal_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    waiting = await update.message.reply_text(
        "⏳ در حال بررسی بازار..."
    )

    try:
        signals = get_top_signals(limit=5)

        await waiting.edit_text(
            format_top_signals(signals)
        )

    except Exception as e:
        await waiting.edit_text(
            f"❌ خطا:\n{str(e)[:200]}"
        )


# ============================================================
# Market Overview
# ============================================================

async def market_overview_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    waiting = await update.message.reply_text(
        "⏳ در حال بررسی بازار..."
    )

    try:
        overview = scan_market_overview()

        await waiting.edit_text(
            format_market_overview_text(overview)
        )

    except Exception as e:
        await waiting.edit_text(
            f"❌ خطا:\n{str(e)[:200]}"
        )


# ============================================================
# Stats
# ============================================================

async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    text_parts = []

    if format_signal_stats:
        try:
            text_parts.append(
                format_signal_stats()
            )
        except Exception:
            pass

    if format_paper_stats:
        try:
            text_parts.append(
                format_paper_stats()
            )
        except Exception:
            pass

    if not text_parts:
        text_parts.append("آماری موجود نیست.")

    await update.message.reply_text(
        "\n\n".join(text_parts)
    )


# ============================================================
# Active Positions
# ============================================================

async def positions_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    if not format_open_positions:
        await update.message.reply_text(
            "ماژول Paper Trade فعال نیست."
        )
        return

try:
        await update.message.reply_text(
            format_open_positions()
        )
    except Exception as e:
        await update.message.reply_text(
            str(e)[:200]
        )


# ============================================================
# Active Signals
# ============================================================

async def active_signals_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    if not format_active_signals:
        await update.message.reply_text(
            "ماژول Tracker فعال نیست."
        )
        return

    try:
        await update.message.reply_text(
            format_active_signals()
        )
    except Exception as e:
        await update.message.reply_text(
            str(e)[:200]
        )


# ============================================================
# AI Status
# ============================================================

async def ai_status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    text_parts = []

    if format_ai_status:
        try:
            text_parts.append(
                format_ai_status()
            )
        except Exception:
            pass

    if format_learning_summary:
        try:
            text_parts.append(
                format_learning_summary()
            )
        except Exception:
            pass

    if format_rotation_report:
        try:
            text_parts.append(
                format_rotation_report()
            )
        except Exception:
            pass

    if not text_parts:
        text_parts.append(
            "AI Status در دسترس نیست."
        )

    await update.message.reply_text(
        "\n\n".join(text_parts)
    )


# ============================================================
# Reset Stats
# ============================================================

async def reset_stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if get_user_id(update) != OWNER_ID:
        await reject_unauthorized(update)
        return

    try:
        if reset_signal_stats:
            reset_signal_stats()

        if reset_paper_trades:
            reset_paper_trades()

        await update.message.reply_text(
            "✅ آمارها ریست شدند."
        )

    except Exception as e:
        await update.message.reply_text(
            f"❌ خطا:\n{str(e)[:200]}"
        )

# ============================================================
# Register signal after sending Telegram message
# ============================================================

async def register_sent_signal(
    signal: Dict[str, Any],
    sent_message,
    source: str = "auto_signal",
):
    try:
        chat_id = sent_message.chat_id
        message_id = sent_message.message_id

        signal_with_meta = attach_signal_metadata(
            signal=signal,
            message_id=message_id,
            chat_id=chat_id,
            source=source,
        )

        if add_signal_to_tracking:
            try:
                add_signal_to_tracking(
                    signal_with_meta,
                    telegram_message_id=message_id,
                    chat_id=chat_id,
                )
            except TypeError:
                add_signal_to_tracking(signal_with_meta)
            except Exception:
                pass

        if open_paper_position:
            try:
                open_paper_position(
                    signal_with_meta,
                    telegram_message_id=message_id,
                    chat_id=chat_id,
                )
            except Exception:
                pass

    except Exception as e:
        logger.error(f"register_sent_signal error: {e}")


# ============================================================
# Auto Signal Loop
# ============================================================

def auto_signal_key(signal: Dict[str, Any]) -> str:
    return f"{signal.get('symbol')}_{signal.get('direction')}"


def can_send_auto_signal(signal: Dict[str, Any]) -> bool:
    try:
        if signal.get("status") != "ACTIVE":
            return False

        if not signal.get("entry_confirmed", False):
            return False

        if int(signal.get("score", 0) or 0) < int(AUTO_DIRECT_SCORE_MIN):
            return False

        key = auto_signal_key(signal)
        now = int(time.time())
        last = int(LAST_AUTO_SIGNAL_TIME.get(key, 0))

        if now - last < AUTO_SIGNAL_COOLDOWN_SECONDS:
            return False

        return True

    except Exception:
        return False


def mark_auto_signal_sent(signal: Dict[str, Any]):
    key = auto_signal_key(signal)
    LAST_AUTO_SIGNAL_TIME[key] = int(time.time())


async def auto_signal_loop(app: Application):
    if not AUTO_SIGNAL_ENABLED:
        logger.info("Auto signal disabled")
        return

    if not OWNER_ID:
        logger.warning("OWNER_ID not set; auto signal disabled")
        return

    await asyncio.sleep(10)

    while True:
        try:
            scan_result = scan_for_auto_signals(
                max_results=3,
                allow_ghost=True,
            )

            signals = scan_result.get("signals", [])

            for signal in signals:
                if not can_send_auto_signal(signal):
                    continue

                text = format_signal_message(signal)

                sent = await app.bot.send_message(
                    chat_id=OWNER_ID,
                    text=text,
                )

                await register_sent_signal(
                    signal=signal,
                    sent_message=sent,
                    source="auto_signal",
                )

                mark_auto_signal_sent(signal)

                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"auto_signal_loop error: {e}")

        await asyncio.sleep(
            max(60, int(AUTO_SCAN_INTERVAL_MINUTES) * 60)
        )


# ============================================================
# Signal Tracker Loop
# ============================================================

async def signal_tracking_loop(app: Application):
    if not check_active_signals:
        logger.warning("Signal tracker not available")
        return

    await asyncio.sleep(15)

    while True:
        try:
            events = check_active_signals()

            if not events:
                await asyncio.sleep(20)
                continue

if isinstance(events, dict):
                events = [events]

            for event in events:
                if not isinstance(event, dict):
                    continue

                text = event.get("message") or event.get("text")

                if not text:
                    continue

                chat_id = event.get("chat_id") or OWNER_ID
                reply_to_message_id = event.get("reply_to_message_id")

                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_to_message_id=reply_to_message_id,
                    )
                except Exception:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                    )

                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"signal_tracking_loop error: {e}")

        await asyncio.sleep(20)


# ============================================================
# Message Handler
# ============================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update):
        await reject_unauthorized(update)
        return

    text = update.message.text.strip()

    if not text:
        return

    lowered = text.lower()

    if lowered in ["بهترین سیگنال", "بهترین", "top", "best"]:
        await best_signal_command(update, context)
        return

    if lowered in ["بررسی بازار", "بازار", "وضعیت بازار"]:
        await market_overview_command(update, context)
        return

    if lowered in ["آمار", "امار", "stats"]:
        await stats_command(update, context)
        return

    if lowered in ["پوزیشن‌ها", "پوزیشن ها", "positions"]:
        await positions_command(update, context)
        return

    if lowered in ["سیگنال‌های فعال", "سیگنال های فعال", "active signals"]:
        await active_signals_command(update, context)
        return

    if lowered in ["هوش مصنوعی", "ai", "وضعیت ai"]:
        await ai_status_command(update, context)
        return

    if lowered in ["ریست آمار", "reset stats"]:
        await reset_stats_command(update, context)
        return

    symbol = normalize_symbol_text(text)

    if symbol:
        await analyze_request(
            update=update,
            context=context,
            symbol=symbol,
        )
        return

    await update.message.reply_text(
        "متوجه نشدم. مثلا بنویس:\nتحلیل بیتکوین\nبهترین سیگنال\nبررسی بازار"
    )


# ============================================================
# Error handler
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Telegram error",
        exc_info=context.error,
    )


# ============================================================
# Main
# ============================================================

async def post_init(app: Application):
    asyncio.create_task(auto_signal_loop(app))
    asyncio.create_task(signal_tracking_loop(app))


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("positions", positions_command))
    app.add_handler(CommandHandler("active", active_signals_command))
    app.add_handler(CommandHandler("ai", ai_status_command))
    app.add_handler(CommandHandler("resetstats", reset_stats_command))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    app.add_error_handler(error_handler)

    return app

def main():
    app = build_application()

    logger.info("Crypto AI Bot started")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
    )


if name == "main":
    main()
