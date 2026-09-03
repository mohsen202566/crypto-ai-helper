"""پنل‌ها و دستورات تلگرام.

چهار پنل:
  • پنل پوزیشن  — هنگام باز شدن هر پوزیشن
  • پنل ترید    — وضعیت کلی و موجودی
  • پنل نتیجه   — با ریپلای روی پیام سیگنال اصلی
  • پنل آمار    — واقعی و مجازی، جدا از هم

دستورات باید فوری پاسخ دهند و هرگز درگیر تحلیل یا درخواست سنگین نشوند؛
همهٔ داده‌ها از دیتابیس محلی خوانده می‌شود، نه از صرافی.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import requests

import config
from storage import Storage
from utils import (
    canonical_base,
    json_loads,
    logger,
    normalize_command,
    parse_number,
    safe_float,
    safe_int,
)

# ----------------------------------------------------------------------
#  کمکی‌های قالب‌بندی
# ----------------------------------------------------------------------

def _n(value: Any, digits: int = 2) -> str:
    return f"{safe_float(value):,.{digits}f}"


def _price(value: Any) -> str:
    v = safe_float(value)
    if v <= 0:
        return "—"
    if v >= 100:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.4f}"
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _pnl(value: Any) -> str:
    v = safe_float(value)
    sign = "+" if v >= 0 else ""
    emoji = "🟢" if v > 0 else ("🔴" if v < 0 else "⚪️")
    return f"{emoji} {sign}{v:,.2f}$"


def _coin(symbol: Any) -> str:
    """نام کوتاه ارز برای نمایش (مثلاً DOGE به‌جای DOGE-SWAP-USDT)."""
    return canonical_base(str(symbol or "")) or str(symbol or "—")


def _side_badge(side: str) -> str:
    return "🟢 لانگ" if str(side).upper() == "LONG" else "🔴 شورت"


def _mode_label(mode: str) -> str:
    return "واقعی" if str(mode) == "real" else "مجازی"


# ----------------------------------------------------------------------
#  پنل‌ها
# ----------------------------------------------------------------------

def position_panel(cycle: dict[str, Any], plan: dict[str, Any] | None = None) -> str:
    """پنل پیام پوزیشن — هنگام باز شدن."""
    plan = plan or json_loads(cycle.get("plan_json"), {}) or {}
    entry = safe_float(cycle.get("avg_entry_price")) or safe_float(plan.get("entry_price"))
    score = safe_float(cycle.get("entry_score"))
    lines = [
        f"{_side_badge(cycle.get('side'))} — {_coin(cycle.get('symbol'))}",
        "",
        f"امتیاز ورود: {score:.0f}/100",
        f"نقطهٔ ورود: {_price(entry)}",
        f"لوریج: {safe_int(cycle.get('leverage'))}x  |  {config.MARGIN_MODE}",
        f"مارجین: {_n(plan.get('margin_usdt') or cycle.get('total_margin'))}$"
        f"  |  ارزش پوزیشن: {_n(plan.get('notional_usdt') or cycle.get('total_notional'))}$",
        f"نوع: {_mode_label(cycle.get('mode'))}",
        "",
        f"🎯 حد سود: {_price(cycle.get('take_profit_price'))}",
        f"🛑 حد ضرر: {_price(cycle.get('hard_stop_price'))}",
    ]
    if plan.get("liquidation_price"):
        lines.append(f"⚠️ لیکوئید: {_price(plan.get('liquidation_price'))}")
    if plan.get("expected_profit_usdt"):
        lines += [
            "",
            f"سود خالص در صورت حد سود: {_n(plan.get('expected_profit_usdt'))}$",
            f"ضرر در صورت حد ضرر: {_n(plan.get('risk_usdt'))}$",
            f"(کارمزد رفت‌وبرگشت: {_n(plan.get('cost_usdt'))}$)",
        ]
    reason = str(cycle.get("entry_reason") or "")
    if reason:
        lines += ["", f"🧭 {reason}"]
    return "\n".join(lines)


def _wait_reason(storage: Storage) -> str:
    """آخرین دلیلی که ربات وارد نشده — برای اینکه بدانی منتظر چیست."""
    waiting = ""
    for row in storage.health_rows():
        if row.get("component") in {"strategy", "risk"}:
            detail = str(row.get("detail") or "")
            if detail:
                waiting = detail
    return waiting


def _common_lines(storage: Storage) -> list[str]:
    universe = storage.get_setting("universe", []) or []
    return [
        f"ارزهای تحت اسکن: {len(universe)}",
        f"تایم‌فریم: {config.ENTRY_TIMEFRAME} (روند: {config.TREND_TIMEFRAME})",
        f"حداکثر پوزیشن هم‌زمان: {safe_int(storage.get_setting('max_positions', config.MAX_CONCURRENT_POSITIONS))}",
        f"آستانهٔ امتیاز: {safe_float(storage.get_setting('score_threshold', config.SCORE_THRESHOLD)):.0f}/100",
        f"لوریج: تا {safe_int(storage.get_setting('leverage', config.DEFAULT_LEVERAGE))}x  |  {config.MARGIN_MODE}",
        f"نسبت سود به ضرر: ۱ به {config.RISK_REWARD_RATIO:.1f}",
    ]


def _positions_block(cycles: list[dict[str, Any]]) -> list[str]:
    if not cycles:
        return []
    lines = ["", f"📂 پوزیشن‌های باز ({len(cycles)}):"]
    for c in cycles:
        lines.append(
            f"  {_side_badge(c.get('side'))} {_coin(c.get('symbol'))} | "
            f"ورود {_price(c.get('avg_entry_price'))} | "
            f"امتیاز {safe_float(c.get('entry_score')):.0f} | "
            f"{safe_int(c.get('leverage'))}x"
        )
    return lines


def real_trade_panel(storage: Storage) -> str:
    """پنل ترید واقعی."""
    real_on = bool(storage.get_setting("real_trading_enabled", False))
    balance, balance_ts = storage.cached_balance()
    stats = storage.stats("real")
    cycles = [c for c in storage.open_cycles() if c.get("mode") == "real"]
    engaged = sum(safe_float(c.get("total_margin")) for c in cycles)
    engaged_pct = (engaged / balance * 100.0) if balance > 0 else 0.0

    age = ""
    if balance_ts:
        seconds = max(0, int((time.time() * 1000 - balance_ts) / 1000))
        age = f" ({seconds}s پیش)"

    lines = [
        "🔵 پنل ترید واقعی",
        "",
        f"ترید واقعی: {'✅ فعال' if real_on else '⛔️ خاموش'}",
    ]
    lines += _common_lines(storage)
    lines += [
        "",
        f"🏦 موجودی توبیت: {_n(balance)}${age}",
        f"📌 سرمایهٔ درگیر: {_n(engaged)}$ ({engaged_pct:.1f}%)",
        f"سقف مجاز درگیری: {config.MAX_CAPITAL_ENGAGED_RATE * 100:.0f}% از موجودی",
        "",
        f"پوزیشن‌های باز: {stats['open']}",
        f"سود/ضرر امروز: {_pnl(stats['pnl_today'])}",
        f"سود/ضرر کل: {_pnl(stats['pnl_total'])}",
    ]
    if balance <= 0:
        lines += ["", "⚠️ موجودی صفر است — تا واریز نکنید ترید واقعی انجام نمی‌شود."]
    lines += _positions_block(cycles)
    if real_on and not cycles:
        reason = _wait_reason(storage)
        if reason:
            lines += ["", f"⏳ {reason}"]
    return "\n".join(lines)


def virtual_trade_panel(storage: Storage) -> str:
    """پنل ترید مجازی."""
    real_on = bool(storage.get_setting("real_trading_enabled", False))
    balance = safe_float(storage.get_setting("virtual_balance", 0.0))
    start = safe_float(config.VIRTUAL_START_CAPITAL_USDT)
    stats = storage.stats("virtual")
    cycles = [c for c in storage.open_cycles() if c.get("mode") == "virtual"]
    engaged = sum(safe_float(c.get("total_margin")) for c in cycles)
    engaged_pct = (engaged / balance * 100.0) if balance > 0 else 0.0
    growth = ((balance / start - 1.0) * 100.0) if start > 0 else 0.0

    lines = [
        "🎮 پنل ترید مجازی",
        "",
        f"وضعیت: {'⏸ غیرفعال (ترید واقعی روشن است)' if real_on else '✅ در حال اجرا'}",
    ]
    lines += _common_lines(storage)
    lines += [
        "",
        f"💰 موجودی مجازی: {_n(balance)}$",
        f"سرمایهٔ شروع: {_n(start)}$  |  رشد: {growth:+.1f}%",
        f"📌 سرمایهٔ درگیر: {_n(engaged)}$ ({engaged_pct:.1f}%)",
        "",
        f"پوزیشن‌های باز: {stats['open']}",
        f"کل بسته‌شده: {stats['closed']}  (حد سود {stats['tp']} / حد ضرر {stats['stop']})",
        f"سود/ضرر امروز: {_pnl(stats['pnl_today'])}",
        f"سود/ضرر کل: {_pnl(stats['pnl_total'])}",
    ]
    lines += _positions_block(cycles)
    if not cycles:
        reason = _wait_reason(storage)
        if reason:
            lines += ["", f"⏳ {reason}"]
    return "\n".join(lines)


def result_panel(cycle: dict[str, Any]) -> str:
    """پنل نتیجه — با ریپلای روی پیام سیگنال اصلی ارسال می‌شود."""
    reason = str(cycle.get("exit_reason") or "")
    label = {
        "tp": "🎯 حد سود",
        "stop": "🛑 حد ضرر",
        "reversal": "↩️ برگشت مومنتوم",
        "timeout": "⏱ پایان مهلت پوزیشن",
        "manual": "✋️ بستن دستی",
        "liquidation": "💥 لیکوئید",
        "failed": "⚠️ سفارش ناموفق",
    }.get(reason, reason)
    net = safe_float(cycle.get("net_pnl"))
    entry = safe_float(cycle.get("avg_entry_price"))
    exit_price = safe_float(cycle.get("exit_price"))
    move = ((exit_price / entry - 1.0) * 100.0) if entry > 0 else 0.0
    if str(cycle.get("side")).upper() == "SHORT":
        move = -move
    return "\n".join([
        f"{_side_badge(cycle.get('side'))} — {_coin(cycle.get('symbol'))}",
        f"نتیجه: {label}",
        f"ورود {_price(entry)} → خروج {_price(exit_price)}  ({move:+.2f}%)",
        f"امتیاز ورود بود: {safe_float(cycle.get('entry_score')):.0f}/100",
        "",
        f"سود/ضرر خالص: {_pnl(net)}",
        f"(ناخالص {_n(cycle.get('gross_pnl'))}$ − کارمزد {_n(cycle.get('fees'))}$)",
        f"نوع: {_mode_label(cycle.get('mode'))}",
    ])


def stats_panel(storage: Storage) -> str:
    """پنل آمار — واقعی و مجازی، جدا."""
    real = storage.stats("real")
    virt = storage.stats("virtual")
    balance, _ = storage.cached_balance()
    virtual_balance = safe_float(storage.get_setting("virtual_balance", 0.0))

    def block(title: str, s: dict[str, Any], bal: float) -> list[str]:
        total_closed = s["closed"]
        win_rate = (s["tp"] / total_closed * 100.0) if total_closed else 0.0
        return [
            title,
            f"  موجودی: {_n(bal)}$",
            f"  پوزیشن باز: {s['open']}",
            f"  کل بسته‌شده: {total_closed}",
            f"  حد سود: {s['tp']}  |  حد ضرر: {s['stop']}",
            f"  نرخ برد: {win_rate:.1f}%",
            f"  سود/ضرر امروز: {_pnl(s['pnl_today'])}",
            f"  سود/ضرر کل: {_pnl(s['pnl_total'])}",
        ]

    lines = ["📊 آمار کل", ""]
    lines += block("🔵 واقعی", real, balance)
    lines.append("")
    lines += block("⚪️ مجازی", virt, virtual_balance)
    return "\n".join(lines)


def help_text() -> str:
    return "\n".join([
        "🤖 ربات اسکن چندارزی",
        "",
        "دستورات:",
        "• ترید فعال / ترید خاموش — روشن و خاموش کردن ترید واقعی",
        "• پنل — پنل ترید واقعی",
        "• ترید مجازی — پنل ترید مجازی",
        "• پوزیشن — پوزیشن‌های باز",
        "• پوزیشن ۵ — حداکثر پوزیشن هم‌زمان (۱ تا ۳۰)",
        "• امتیاز ۸۰ — آستانهٔ ورود (۵۵ تا ۹۵؛ بالاتر = محتاط‌تر)",
        "• اهرم ۵ — سقف لوریج (۱ تا ۱۰)",
        "• سقف ۵۰ — سقف سرمایهٔ درگیر (۰ = کل موجودی)",
        "• ارزها — فهرست ارزهای تحت اسکن",
        "• چرا — دلیل اینکه چرا الان وارد نمی‌شود",
        "• آمار — آمار واقعی و مجازی",
        "• وضعیت — سلامت سیستم",
    ])


def symbols_panel(storage: Storage) -> str:
    universe = storage.get_setting("universe", []) or []
    if not universe:
        return "فهرست ارزها هنوز ساخته نشده — ربات در حال راه‌اندازی است."
    names = [canonical_base(str(s)) for s in universe]
    busy = {canonical_base(str(s)) for s in storage.open_symbols()}
    rows = [
        f"{'🟡' if n in busy else '▫️'} {n}" for n in names
    ]
    lines = [f"🔎 {len(names)} ارز تحت اسکن", ""]
    for i in range(0, len(rows), 3):
        lines.append("  ".join(rows[i:i + 3]))
    lines += ["", "🟡 = پوزیشن باز دارد"]
    return "\n".join(lines)


def health_panel(storage: Storage) -> str:
    rows = storage.health_rows()
    if not rows:
        return "هنوز گزارشی ثبت نشده."
    lines = ["🩺 وضعیت سیستم", ""]
    for r in rows:
        icon = "✅" if r.get("status") == "ok" else "⚠️"
        lines.append(f"{icon} {r.get('component')}: {str(r.get('detail') or '')[:80]}")
    lines.append("")
    lines.append(f"مرحلهٔ راه‌اندازی: {storage.get_setting('startup_phase', '—')}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
#  مسیریاب دستورات
# ----------------------------------------------------------------------

class CommandRouter:
    def __init__(self, storage: Storage):
        self.storage = storage

    def handle(self, text: str) -> str:
        cmd = normalize_command(text)

        if cmd in {"/start", "/help", "راهنما", "کمک", "شروع"}:
            return help_text()

        if cmd in {"ترید فعال", "ترید روشن", "/trade_on", "فعال"}:
            self.storage.set_setting("real_trading_enabled", True)
            self.storage.log_event("real_trading_enabled", True)
            return "✅ ترید واقعی فعال شد.\nچرخه‌های جدید با پول واقعی باز می‌شوند."

        if cmd in {"ترید غیرفعال", "ترید غیر فعال", "ترید خاموش", "/trade_off", "خاموش"}:
            self.storage.set_setting("real_trading_enabled", False)
            self.storage.log_event("real_trading_enabled", False)
            return "⛔️ ترید واقعی خاموش شد.\nچرخه‌های جدید فقط مجازی خواهند بود."

        if cmd in {"پنل", "ترید", "ترید واقعی", "پنل ترید", "/panel", "/trade"}:
            return real_trade_panel(self.storage)

        if cmd in {"ترید مجازی", "مجازی", "پنل مجازی", "/virtual"}:
            return virtual_trade_panel(self.storage)

        if cmd.startswith("امتیاز ") or cmd.startswith("حساسیت "):
            try:
                value = float(parse_number(cmd.split(" ", 1)[1]))
            except (ValueError, IndexError):
                return "عدد نامعتبر. مثال: امتیاز ۸۰"
            if not config.SCORE_THRESHOLD_MIN <= value <= config.SCORE_THRESHOLD_MAX:
                return (
                    f"عدد باید بین {config.SCORE_THRESHOLD_MIN:.0f} تا "
                    f"{config.SCORE_THRESHOLD_MAX:.0f} باشد."
                )
            self.storage.set_setting("score_threshold", value)
            if value >= 85:
                hint = "خیلی محتاط — سیگنال کم ولی باکیفیت‌تر"
            elif value <= 65:
                hint = "حساس — سیگنال زیاد، کارمزد بیشتر"
            else:
                hint = "متعادل"
            return f"✅ آستانهٔ ورود روی {value:.0f}/100 تنظیم شد — {hint}"

        if cmd in {"چرا", "دلیل", "/why"}:
            reason = _wait_reason(self.storage)
            return f"⏳ {reason}" if reason else "دلیلی ثبت نشده — ربات هنوز اولین تحلیل را انجام نداده."

        if cmd in {"آمار", "امار", "آمار کل", "امار کل", "/stats"}:
            return stats_panel(self.storage)

        if cmd in {"پوزیشن", "پوزیشن ها", "پوزیشن‌ها", "/positions"}:
            cycles = self.storage.open_cycles()
            if not cycles:
                return "هیچ پوزیشن بازی وجود ندارد."
            return "\n\n".join(position_panel(c) for c in cycles)

        if cmd in {"ارزها", "ارز ها", "لیست ارز", "نمادها", "/symbols"}:
            return symbols_panel(self.storage)

        if cmd in {"وضعیت", "سلامت", "/health"}:
            return health_panel(self.storage)

        if cmd.startswith("پوزیشن ") and cmd.split(" ", 1)[1].strip().isdigit():
            try:
                value = int(parse_number(cmd.split(" ", 1)[1]))
            except (ValueError, IndexError):
                return "عدد نامعتبر است. مثال: پوزیشن ۵"
            value = max(1, min(value, config.MAX_CONCURRENT_LIMIT))
            self.storage.set_setting("max_positions", value)
            return (
                f"✅ حداکثر پوزیشن هم‌زمان روی {value} تنظیم شد.\n"
                "سرمایه بین همین تعداد اسلات پخش می‌شود — تعداد بیشتر یعنی "
                "پوزیشن‌های کوچک‌تر و پخش‌شده‌تر."
            )

        if cmd.startswith("اهرم ") or cmd.startswith("لوریج "):
            try:
                value = int(parse_number(cmd.split(" ", 1)[1]))
            except (ValueError, IndexError):
                return "عدد نامعتبر است. مثال: اهرم ۵"
            value = max(config.LEVERAGE_MIN, min(value, config.LEVERAGE_MAX))
            self.storage.set_setting("leverage", value)
            return (
                f"✅ سقف لوریج روی {value}x تنظیم شد.\n"
                "ربات کم‌ریسک‌ترین لوریجی را که هنوز بعد از کارمزد صرف کند انتخاب می‌کند."
            )

        if cmd.startswith("سقف "):
            try:
                value = float(parse_number(cmd.split(" ", 1)[1]))
            except (ValueError, IndexError):
                return "عدد نامعتبر است. مثال: سقف ۵۰"
            if value < 0:
                return "عدد نمی‌تواند منفی باشد."
            self.storage.set_setting("capital_cap", value)
            if value == 0:
                return "✅ سقف برداشته شد؛ کل موجودی مبنای محاسبه است."
            return f"✅ سقف سرمایهٔ درگیر روی {value:,.2f}$ تنظیم شد."

        return "دستور شناخته نشد. برای فهرست دستورات «راهنما» بفرستید."


# ----------------------------------------------------------------------
#  کلاینت تلگرام
# ----------------------------------------------------------------------

class TelegramBot:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.router = CommandRouter(storage)
        self.token = config.TELEGRAM_BOT_TOKEN
        self.owner_id = str(config.TELEGRAM_CHAT_ID or "").strip()
        self.session = requests.Session()
        self.offset = 0
        self._stop = threading.Event()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _authorized(self, message: dict[str, Any]) -> bool:
        """فقط صاحب ربات مجاز است دستور بدهد."""
        if not self.owner_id:
            # اگر شناسه تنظیم نشده، اولین کاربر ثبت و قفل می‌شود.
            chat_id = str(message.get("chat", {}).get("id") or "")
            if chat_id:
                self.owner_id = chat_id
                self.storage.set_setting("bound_chat_id", chat_id)
                logger.warning("OWNER_BOUND | chat_id=%s", chat_id[:4] + "***")
                return True
            return False
        sender = str(message.get("from", {}).get("id") or "")
        chat = str(message.get("chat", {}).get("id") or "")
        return self.owner_id in {sender, chat}

    def send_message(self, text: str, reply_to: int | None = None) -> int | None:
        if not self.enabled or not self.owner_id:
            logger.info("TG_SKIP | %s", text[:80])
            return None
        payload: dict[str, Any] = {
            "chat_id": self.owner_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_to:
            payload["reply_to_message_id"] = int(reply_to)
            payload["allow_sending_without_reply"] = True
        try:
            response = self.session.post(
                self._url("sendMessage"), json=payload, timeout=config.REQUEST_TIMEOUT
            )
            data = response.json()
            if data.get("ok"):
                return safe_int(data.get("result", {}).get("message_id"))
            logger.warning("TG_SEND_FAIL | %s", str(data)[:200])
        except Exception as exc:
            logger.warning("TG_SEND_ERROR | %s", exc)
        return None

    def poll_loop(self) -> None:
        """حلقهٔ دریافت دستورات — سبک و مستقل از حلقهٔ تحلیل."""
        if not self.enabled:
            logger.warning("TG_DISABLED | توکن تنظیم نشده است")
            return
        while not self._stop.is_set():
            try:
                response = self.session.get(
                    self._url("getUpdates"),
                    params={
                        "offset": self.offset,
                        "timeout": config.TELEGRAM_POLL_TIMEOUT,
                        "allowed_updates": '["message"]',
                    },
                    timeout=config.TELEGRAM_POLL_TIMEOUT + 10,
                )
                data = response.json()
                for update in data.get("result", []):
                    self.offset = max(self.offset, safe_int(update.get("update_id")) + 1)
                    message = update.get("message") or {}
                    text = str(message.get("text") or "").strip()
                    if not text:
                        continue
                    if not self._authorized(message):
                        logger.warning("TG_UNAUTHORIZED | ignored")
                        continue
                    try:
                        reply = self.router.handle(text)
                    except Exception as exc:
                        logger.exception("CMD_ERROR")
                        reply = f"خطا در اجرای دستور: {exc}"
                    self.send_message(reply, reply_to=safe_int(message.get("message_id")))
            except requests.Timeout:
                continue
            except Exception as exc:
                logger.warning("TG_POLL_ERROR | %s", exc)
                if self._stop.wait(5):
                    return

    def notification_loop(self) -> None:
        """ارسال پیام‌های صف‌شده (سیگنال‌ها، نتایج، هشدارها)."""
        while not self._stop.is_set():
            try:
                for row in self.storage.pending_messages(limit=10):
                    message_id = self.send_message(
                        str(row.get("text")), reply_to=row.get("reply_to")
                    )
                    self.storage.mark_message_sent(safe_int(row.get("id")))
                    cycle_id = row.get("cycle_id")
                    if message_id and cycle_id and not row.get("reply_to"):
                        # پیام سیگنال اصلی؛ ذخیره می‌شود تا نتیجه با ریپلای بیاید.
                        self.storage.set_cycle_message_id(safe_int(cycle_id), message_id)
            except Exception as exc:
                logger.warning("TG_NOTIFY_ERROR | %s", exc)
            if self._stop.wait(2):
                return

    def stop(self) -> None:
        self._stop.set()
        try:
            self.session.close()
        except Exception:
            pass
