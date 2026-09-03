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


def _age(opened_at: Any) -> str:
    """عمر پوزیشن به زبان ساده."""
    ts = safe_int(opened_at)
    if ts <= 0:
        return "—"
    minutes = max(0, int((time.time() * 1000 - ts) / 60000))
    if minutes < 60:
        return f"{minutes} دقیقه"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ساعت و {minutes % 60} دقیقه"
    return f"{hours // 24} روز و {hours % 24} ساعت"


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
    """آخرین دلیلی که ربات وارد نشده — برای اینکه بدانی منتظر چیست.

    ترتیب اهمیت: اول دلیل رد شدن در محاسبهٔ ریسک، بعد نتیجهٔ اسکن.
    """
    rows = {str(r.get("component")): str(r.get("detail") or "") for r in storage.health_rows()}
    for key in ("risk", "scan", "universe"):
        if rows.get(key):
            return rows[key]
    return ""


def _size_label(storage: Storage) -> str:
    size = safe_float(storage.get_setting("position_size", config.POSITION_SIZE_USDT))
    if size > 0:
        return f"{size:,.2f}$ ثابت"
    return "خودکار (تقسیم سرمایه بین اسلات‌ها)"


def _common_lines(storage: Storage) -> list[str]:
    universe = storage.get_setting("universe", []) or []
    return [
        f"ارزهای تحت اسکن: {len(universe)}",
        f"تایم‌فریم: {config.ENTRY_TIMEFRAME} (روند: {config.TREND_TIMEFRAME})",
        f"حداکثر پوزیشن هم‌زمان: {safe_int(storage.get_setting('max_positions', config.MAX_CONCURRENT_POSITIONS))}",
        f"اندازهٔ هر پوزیشن: {_size_label(storage)}",
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


def _virtual_state(storage: Storage, real_on: bool) -> str:
    if real_on:
        return "⏸ غیرفعال (ترید واقعی روشن است)"
    if not bool(storage.get_setting("virtual_trading_enabled", True)):
        return "⛔️ خاموش — با «ترید مجازی فعال» روشن کنید"
    return "✅ در حال اجرا"


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
        f"وضعیت: {_virtual_state(storage, real_on)}",
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


def live_panel(cycles: list[dict[str, Any]], prices: dict[str, float]) -> str:
    """گزارش لحظه‌ای پوزیشن‌های باز — سود/زیان تحقق‌نیافتهٔ هر کدام."""
    import risk_engine

    if not cycles:
        return "هیچ پوزیشن بازی نیست."

    lines = [f"📡 مانیتورینگ لحظه‌ای — {len(cycles)} پوزیشن باز", ""]
    total_gross = 0.0
    total_margin = 0.0
    for c in cycles:
        symbol = str(c.get("symbol"))
        entry = safe_float(c.get("avg_entry_price"))
        qty = safe_float(c.get("total_quantity"))
        margin = safe_float(c.get("total_margin"))
        price = safe_float(prices.get(symbol))
        if price <= 0 or entry <= 0:
            continue
        gross = risk_engine.unrealized_pnl(
            side=str(c.get("side")), avg_entry=entry, quantity=qty, current_price=price
        )
        net = risk_engine.net_pnl_after_costs(gross, entry * qty)
        total_gross += net
        total_margin += margin

        move = ((price / entry - 1.0) * 100.0) if entry > 0 else 0.0
        if str(c.get("side")).upper() == "SHORT":
            move = -move
        roi = (net / margin * 100.0) if margin > 0 else 0.0

        tp = safe_float(c.get("take_profit_price"))
        sl = safe_float(c.get("hard_stop_price"))
        # چقدر از مسیر تا حد سود طی شده
        span = abs(tp - entry)
        done = abs(price - entry) if (price - entry) * (tp - entry) > 0 else 0.0
        progress = min(100.0, done / span * 100.0) if span > 0 else 0.0

        lines += [
            f"{_side_badge(c.get('side'))} {_coin(symbol)}  {safe_int(c.get('leverage'))}x",
            f"  ورود {_price(entry)} → حالا {_price(price)}  ({move:+.2f}%)",
            f"  {_pnl(net)}  (بازده مارجین {roi:+.1f}%)",
            f"  🎯 {_price(tp)}  🛑 {_price(sl)}  |  {progress:.0f}% تا حد سود",
            f"  ⏱ {_age(c.get('opened_at'))}",
            "",
        ]

    total_roi = (total_gross / total_margin * 100.0) if total_margin > 0 else 0.0
    lines += [
        "──────────",
        f"جمع تحقق‌نیافته: {_pnl(total_gross)}  ({total_roi:+.1f}% مارجین)",
        f"سرمایهٔ درگیر: {_n(total_margin)}$",
    ]
    return "\n".join(lines)


def summary_panel(cycles: list[dict[str, Any]], title: str) -> str:
    """خلاصهٔ یک دوره: چند معامله، روی کدام ارزها، چند تا TP و چند تا SL."""
    if not cycles:
        return f"{title}\n\nهیچ معامله‌ای بسته نشد."

    tp = [c for c in cycles if str(c.get("exit_reason")) == "tp"]
    sl = [c for c in cycles if str(c.get("exit_reason")) in {"stop", "liquidation"}]
    other = [c for c in cycles if c not in tp and c not in sl]
    net = sum(safe_float(c.get("net_pnl")) for c in cycles)
    fees = sum(safe_float(c.get("fees")) for c in cycles)
    win_rate = (len(tp) / len(cycles) * 100.0) if cycles else 0.0

    lines = [
        title,
        "",
        f"معاملات: {len(cycles)}  |  نرخ برد: {win_rate:.0f}%",
        f"🎯 حد سود: {len(tp)}   🛑 حد ضرر: {len(sl)}" + (f"   ↩️ سایر: {len(other)}" if other else ""),
        f"سود/ضرر خالص: {_pnl(net)}",
        f"کارمزد پرداختی: {_n(fees)}$",
        "",
        "جزئیات:",
    ]
    for c in cycles[-15:]:
        icon = {"tp": "🎯", "stop": "🛑", "liquidation": "💥",
                "reversal": "↩️", "timeout": "⏱"}.get(str(c.get("exit_reason")), "▫️")
        lines.append(
            f"  {icon} {_coin(c.get('symbol'))} {_side_badge(c.get('side')).split()[1]} "
            f"→ {_pnl(c.get('net_pnl'))}"
        )
    if len(cycles) > 15:
        lines.append(f"  … و {len(cycles) - 15} مورد دیگر")
    return "\n".join(lines)


def help_text() -> str:
    return "\n".join([
        "🤖 ربات اسکن چندارزی",
        "",
        "دستورات:",
        "• ترید فعال / ترید خاموش — روشن و خاموش کردن ترید واقعی",
        "• ترید مجازی فعال / ترید مجازی خاموش — روشن و خاموش کردن مجازی",
        "• پنل — پنل ترید واقعی",
        "• ترید مجازی — پنل ترید مجازی",
        "• پوزیشن — پوزیشن‌های باز",
        "• پوزیشن ۵ — حداکثر پوزیشن هم‌زمان (۱ تا ۳۰)",
        "• دلار ۱۰ — مارجین هر پوزیشن، ۱ تا ۱۰۰۰ (۰ = خودکار)",
        "• زنده — مانیتورینگ لحظه‌ای پوزیشن‌های باز",
        "• امروز — خلاصهٔ معاملات امروز",
        "• گزارش ۱۵ — فاصلهٔ گزارش خودکار به دقیقه (۰ = خاموش)",
        "• ریست آمار — پاک کردن تاریخچهٔ استراتژی قبلی",
        "• امتیاز ۸۰ — آستانهٔ ورود (۵۵ تا ۹۵؛ بالاتر = محتاط‌تر)",
        "• اهرم ۱۰ — سقف لوریج (۱ تا ۱۰۰)",
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
    def __init__(self, storage: Storage, live_provider: Any = None):
        self.storage = storage
        # موتور این تابع را تزریق می‌کند تا «زنده» بتواند قیمت لحظه‌ای بگیرد.
        self.live_provider = live_provider

    def _queue_live_report(self) -> None:
        """گزارش لحظه‌ای را در پس‌زمینه می‌سازد و به صف پیام‌ها می‌دهد."""
        try:
            self.storage.queue_message(self.live_provider())
        except Exception as exc:
            logger.warning("LIVE_PANEL_FAIL | %s", exc)
            self.storage.queue_message(f"دریافت قیمت لحظه‌ای ناموفق بود: {exc}")

    def handle(self, text: str) -> str:
        cmd = normalize_command(text)

        if cmd in {"/start", "/help", "راهنما", "کمک", "شروع"}:
            return help_text()

        if cmd in {"ترید فعال", "ترید روشن", "/trade_on", "فعال"}:
            self.storage.set_setting("real_trading_enabled", True)
            self.storage.log_event("real_trading_enabled", True)
            return (
                "✅ ترید واقعی فعال شد.\n"
                "پوزیشن‌های جدید با پول واقعی باز می‌شوند.\n"
                "تا وقتی روشن است، مجازی متوقف می‌ماند."
            )

        if cmd in {"ترید مجازی فعال", "ترید مجازی روشن", "مجازی فعال", "مجازی روشن", "/virtual_on"}:
            self.storage.set_setting("virtual_trading_enabled", True)
            return "✅ ترید مجازی روشن شد.\nپوزیشن‌های جدید بدون پول واقعی باز می‌شوند."

        if cmd in {"ترید مجازی خاموش", "ترید مجازی غیرفعال", "مجازی خاموش",
                   "مجازی غیرفعال", "/virtual_off"}:
            self.storage.set_setting("virtual_trading_enabled", False)
            return (
                "⛔️ ترید مجازی خاموش شد.\n"
                "اگر ترید واقعی هم خاموش باشد، ربات فقط اسکن می‌کند و پوزیشن باز نمی‌شود."
            )

        if cmd in {"ترید غیرفعال", "ترید غیر فعال", "ترید خاموش", "/trade_off", "خاموش"}:
            self.storage.set_setting("real_trading_enabled", False)
            self.storage.log_event("real_trading_enabled", False)
            virtual_on = bool(self.storage.get_setting("virtual_trading_enabled", True))
            return (
                "⛔️ ترید واقعی خاموش شد.\n"
                + ("پوزیشن‌های جدید فقط مجازی خواهند بود."
                   if virtual_on else
                   "ترید مجازی هم خاموش است — برای روشن کردن: «ترید مجازی فعال»")
            )

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

        if cmd in {"زنده", "لحظه ای", "لحظه‌ای", "مانیتور", "/live"}:
            cycles = self.storage.open_cycles()
            if not cycles:
                return "هیچ پوزیشن بازی نیست.\n" + (_wait_reason(self.storage) or "")
            if callable(self.live_provider):
                # قیمت زنده گرفتن ممکن است چند ثانیه طول بکشد؛ پاسخ فوری داده
                # می‌شود و گزارش در پس‌زمینه صف می‌شود تا دستورات معطل نمانند.
                threading.Thread(
                    target=self._queue_live_report, name="live-report", daemon=True
                ).start()
                return f"📡 در حال گرفتن قیمت لحظه‌ای {len(cycles)} پوزیشن…"
            return live_panel(cycles, {})

        if cmd in {"امروز", "خلاصه", "خلاصه امروز", "/today"}:
            day_start = int((time.time() - (time.time() % 86400)) * 1000)
            rows = self.storage.closed_since(day_start)
            return summary_panel(rows, "📅 خلاصهٔ امروز")

        if cmd in {"ریست آمار", "ریست امار", "پاک کردن آمار", "/reset_stats"}:
            self.storage.set_setting("pending_reset", True)
            return (
                "⚠️ این کار همهٔ تاریخچهٔ معاملات بسته‌شده را پاک می‌کند و موجودی\n"
                "مجازی را به مقدار شروع برمی‌گرداند. پوزیشن‌های باز دست نمی‌خورند.\n\n"
                "برای تأیید «تأیید ریست» بفرستید."
            )

        if cmd in {"تایید ریست", "تأیید ریست", "/reset_confirm"}:
            if not self.storage.get_setting("pending_reset", False):
                return "درخواست ریستی در انتظار نیست. اول «ریست آمار» بفرستید."
            removed = self.storage.reset_statistics()
            self.storage.set_setting("virtual_balance", config.VIRTUAL_START_CAPITAL_USDT)
            self.storage.set_setting("pending_reset", False)
            return (
                f"✅ {removed} معاملهٔ قدیمی پاک شد.\n"
                f"موجودی مجازی به {config.VIRTUAL_START_CAPITAL_USDT:,.2f}$ برگشت."
            )

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

        if cmd.startswith("دلار ") or cmd.startswith("حجم ") or cmd.startswith("اندازه "):
            try:
                value = float(parse_number(cmd.split(" ", 1)[1]))
            except (ValueError, IndexError):
                return "عدد نامعتبر است. مثال: دلار ۱۰"
            if value < 0:
                return "عدد نمی‌تواند منفی باشد."
            if value == 0:
                self.storage.set_setting("position_size", 0.0)
                return (
                    "✅ اندازهٔ پوزیشن روی خودکار تنظیم شد.\n"
                    "سرمایهٔ مجاز بین اسلات‌ها پخش می‌شود."
                )
            if not config.POSITION_SIZE_MIN <= value <= config.POSITION_SIZE_MAX:
                return (
                    f"عدد باید بین {config.POSITION_SIZE_MIN:.0f} تا "
                    f"{config.POSITION_SIZE_MAX:,.0f} دلار باشد."
                )
            self.storage.set_setting("position_size", value)
            slots = safe_int(self.storage.get_setting("max_positions", config.MAX_CONCURRENT_POSITIONS))
            return (
                f"✅ مارجین هر پوزیشن روی {value:,.2f}$ تنظیم شد.\n"
                f"با {slots} پوزیشن هم‌زمان، حداکثر {value * slots:,.2f}$ درگیر می‌شود.\n"
                "اگر این مقدار از سقف مجاز سرمایه بیشتر باشد، ربات خودش کمترش می‌کند."
            )

        if cmd.startswith("گزارش "):
            try:
                value = int(parse_number(cmd.split(" ", 1)[1]))
            except (ValueError, IndexError):
                return "عدد نامعتبر است. مثال: گزارش ۱۵"
            value = max(config.LIVE_REPORT_MIN, min(value, config.LIVE_REPORT_MAX))
            self.storage.set_setting("live_report_minutes", value)
            if value == 0:
                return "✅ گزارش خودکار خاموش شد. با «زنده» هر وقت خواستی ببین."
            return f"✅ هر {value} دقیقه گزارش لحظه‌ای پوزیشن‌های باز ارسال می‌شود."

        if cmd.startswith("اهرم ") or cmd.startswith("لوریج "):
            try:
                value = int(parse_number(cmd.split(" ", 1)[1]))
            except (ValueError, IndexError):
                return "عدد نامعتبر است. مثال: اهرم ۵"
            value = max(config.LEVERAGE_MIN, min(value, config.LEVERAGE_MAX))
            self.storage.set_setting("leverage", value)
            note = (
                f"✅ سقف لوریج روی {value}x تنظیم شد.\n"
                "ربات کم‌ریسک‌ترین لوریجی را که هنوز بعد از کارمزد صرف کند انتخاب می‌کند."
            )
            if value >= 25:
                note += (
                    f"\n\n⚠️ با {value}x فاصلهٔ لیکوئید حدود {100.0 / value:.1f}٪ است. "
                    "حد ضرر ربات خیلی زودتر فعال می‌شود، ولی یک شمع ناگهانی می‌تواند "
                    "قبل از پر شدن حد ضرر به لیکوئید برسد."
                )
            return note

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
    def __init__(self, storage: Storage, live_provider: Any = None):
        self.storage = storage
        self.router = CommandRouter(storage, live_provider=live_provider)
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
