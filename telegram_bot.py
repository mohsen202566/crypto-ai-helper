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
    move = safe_float(cycle.get("entry_score"))
    lines = [
        f"{_side_badge(cycle.get('side'))} — {_coin(cycle.get('symbol'))}",
        "",
        f"شدت حرکت ورود: {move:.2f}%",
        f"نقطهٔ ورود: {_price(entry)}",
        f"لوریج: {safe_int(cycle.get('leverage'))}x  |  {config.MARGIN_MODE}",
        f"مارجین: {_n(plan.get('margin_usdt') or cycle.get('total_margin'))}$"
        f"  |  ارزش پوزیشن: {_n(plan.get('notional_usdt') or cycle.get('total_notional'))}$",
        f"نوع: {_mode_label(cycle.get('mode'))}",
        "",
        f"🛑 حد ضرر ساختاری: {_price(cycle.get('hard_stop_price'))}",
        "📉 خروج: فقط با برگشت ساختاری تأییدشده (بدون حد سود ثابت)",
    ]
    if plan.get("liquidation_price"):
        lines.append(f"⚠️ لیکوئید: {_price(plan.get('liquidation_price'))}")
    if plan.get("risk_usdt"):
        lines += [
            "",
            f"حداکثر ضرر در صورت حد ضرر: {_n(plan.get('risk_usdt'))}$",
            f"(کارمزد رفت‌وبرگشت: {_n(plan.get('cost_usdt'))}$)",
            "سود سقف ندارد — تا وقتی ساختار نزولی معتبر است باز می‌ماند.",
        ]
    reason = str(cycle.get("entry_reason") or "")
    if reason:
        lines += ["", f"🧭 {reason}"]
    return "\n".join(lines)


def _wait_reason(storage: Storage) -> str:
    """آخرین دلیلی که ربات وارد نشده.

    مهم: بین ردیف‌های سلامت، **تازه‌ترین** انتخاب می‌شود، نه اولی که پیدا شد.
    وگرنه یک دلیل قدیمی (مثلاً رد شدن با تنظیمات قبلی) تا ابد نمایش داده
    می‌شود و کاربر فکر می‌کند مشکل هنوز پابرجاست.
    """
    best_detail = ""
    best_ts = -1
    for row in storage.health_rows():
        if str(row.get("component")) not in {"risk", "scan", "universe"}:
            continue
        detail = str(row.get("detail") or "")
        if not detail:
            continue
        ts = safe_int(row.get("ts"))
        if ts > best_ts:
            best_ts, best_detail = ts, detail

    if not best_detail:
        return ""
    age = max(0, int((time.time() * 1000 - best_ts) / 1000)) if best_ts > 0 else 0
    if age < 90:
        return best_detail
    if age < 3600:
        return f"{best_detail}  ({age // 60} دقیقه پیش)"
    return f"{best_detail}  ({age // 3600} ساعت پیش)"


def _size_label(storage: Storage, balance: float = 0.0) -> list[str]:
    """اندازهٔ هر پوزیشن — همیشه با عدد دلاری واقعی و ارزش پوزیشن با لوریج."""
    import risk_engine

    slots = max(1, safe_int(storage.get_setting("max_positions", config.MAX_CONCURRENT_POSITIONS)))
    fixed = safe_float(storage.get_setting("position_size", config.POSITION_SIZE_USDT))
    leverage = max(1, safe_int(storage.get_setting("leverage", config.DEFAULT_LEVERAGE)))
    used = safe_float(storage.open_margin_total())

    if fixed > 0:
        margin = fixed
        source = "ثابت (تنظیم شما)"
    elif balance > 0:
        budget = balance * config.MAX_CAPITAL_ENGAGED_RATE
        margin = budget / slots
        source = (
            f"خودکار — {balance:,.2f}$ × "
            f"{config.MAX_CAPITAL_ENGAGED_RATE * 100:.0f}٪ ÷ {slots} اسلات"
        )
    else:
        return [f"💵 مارجین هر پوزیشن: خودکار (تقسیم بین {slots} اسلات)"]

    lines = [
        f"💵 مارجین هر پوزیشن: {margin:,.2f}$   ({source})",
        f"   ارزش پوزیشن با {leverage}x: {margin * leverage:,.2f}$",
        f"   مجموع در صورت پر شدن {slots} اسلات: {margin * slots:,.2f}$",
    ]

    if balance > 0:
        if fixed > 0:
            free = balance - used
            if free < fixed:
                lines.append(
                    f"   ⛔️ فقط {free:,.2f}$ آزاد است — پوزیشن جدید باز نمی‌شود"
                )
            elif fixed * slots > balance:
                possible = max(1, int(balance // fixed))
                lines.append(
                    f"   ⚠️ موجودی فقط برای {possible} پوزیشن کافی است، نه {slots} تا"
                )
        else:
            free = max(0.0, balance * config.MAX_CAPITAL_ENGAGED_RATE - used)
            if free < margin:
                lines.append(f"   ⏳ فعلاً {free:,.2f}$ آزاد است (بقیه درگیر پوزیشن‌های باز)")
    return lines


def _common_lines(storage: Storage, balance: float = 0.0) -> list[str]:
    return [
        "استراتژی: V3 — شورت بعد از پامپ افراطی",
        f"اسکن: کل بازار ({safe_int(storage.get_setting('tradable_count', 0))} قرارداد)"
        f"  |  اجرا: {config.ENTRY_TIMEFRAME}",
        f"حداکثر پوزیشن هم‌زمان: {safe_int(storage.get_setting('max_positions', config.MAX_CONCURRENT_POSITIONS))}",
        *_size_label(storage, balance),
        f"آستانهٔ کاندید: 24h ≥ {config.WATCHLIST_MIN_GAIN_PCT:.0f}%"
        f"  |  زیر نظر: {safe_int(storage.get_setting('watchlist_size', 0))} نماد",
        f"لوریج: {safe_int(storage.get_setting('leverage', config.DEFAULT_LEVERAGE))}x  |  {config.MARGIN_MODE}",
        f"استراحت بعد از خروج: "
        f"{safe_float(storage.get_setting('cooldown_hours', config.COOLDOWN_HOURS)):.0f} ساعت",
        f"حد ضرر: سقف تأییدشده + {config.STOP_ATR_MULT:.1f}×ATR({config.ATR_PERIOD})"
        "  |  بدون حد سود ثابت",
    ]


def _positions_block(cycles: list[dict[str, Any]]) -> list[str]:
    if not cycles:
        return []
    lines = ["", f"📂 پوزیشن‌های باز ({len(cycles)}):"]
    for c in cycles:
        lines.append(
            f"  {_side_badge(c.get('side'))} {_coin(c.get('symbol'))} | "
            f"ورود {_price(c.get('avg_entry_price'))} | "
            f"پامپ ۲۴س {safe_float(c.get('entry_score')):.1f}% | "
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
    lines += _common_lines(storage, balance)
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
    lines += _common_lines(storage, balance)
    lines += [
        "",
        f"💰 موجودی مجازی: {_n(balance)}$",
        f"سرمایهٔ شروع: {_n(start)}$  |  رشد: {growth:+.1f}%",
        f"📌 سرمایهٔ درگیر: {_n(engaged)}$ ({engaged_pct:.1f}%)",
        "",
        f"پوزیشن‌های باز: {stats['open']}",
        f"کل بسته‌شده: {stats['closed']}  (استاپ دنبال‌کننده {stats['trail']} / حد ضرر {stats['stop']})",
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
        "trail": "📈 استاپ دنبال‌کننده",
        "stop": "🛑 حد ضرر",
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
        f"شدت حرکت ورود بود: {safe_float(cycle.get('entry_score')):.2f}%",
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
        win_rate = (s["wins"] / total_closed * 100.0) if total_closed else 0.0
        return [
            title,
            f"  موجودی: {_n(bal)}$",
            f"  پوزیشن باز: {s['open']}",
            f"  کل بسته‌شده: {total_closed}  (برد: {s['wins']}  |  باخت: {s['losses']})",
            f"  از این میان — استاپ دنبال‌کننده: {s['trail']}  |  حد ضرر: {s['stop']}",
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

        stop = safe_float(c.get("hard_stop_price"))
        best = safe_float(c.get("best_price")) or entry
        stop_gap = abs(price - stop) / price * 100.0 if price > 0 and stop > 0 else 0.0

        lines += [
            f"{_side_badge(c.get('side'))} {_coin(symbol)}  {safe_int(c.get('leverage'))}x",
            f"  ورود {_price(entry)} → حالا {_price(price)}  ({move:+.2f}%)",
            f"  {_pnl(net)}  (بازده مارجین {roi:+.1f}%)",
            f"  🛑 استاپ فعال: {_price(stop)}  ({stop_gap:.1f}% فاصله)  |  بهترین قیمت: {_price(best)}",
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
    """خلاصهٔ یک دوره: چند معامله، روی کدام ارزها، چند تا با استاپ دنبال‌کننده و چند تا حد ضرر."""
    if not cycles:
        return f"{title}\n\nهیچ معامله‌ای بسته نشد."

    trail = [c for c in cycles if str(c.get("exit_reason")) == "trail"]
    sl = [c for c in cycles if str(c.get("exit_reason")) in {"stop", "liquidation"}]
    other = [c for c in cycles if c not in trail and c not in sl]
    net = sum(safe_float(c.get("net_pnl")) for c in cycles)
    fees = sum(safe_float(c.get("fees")) for c in cycles)
    # نرخ برد بر اساس سود/زیان خالص واقعی هر معامله، نه فقط دلیل خروج
    wins = [c for c in cycles if safe_float(c.get("net_pnl")) > 0]
    win_rate = (len(wins) / len(cycles) * 100.0) if cycles else 0.0

    lines = [
        title,
        "",
        f"معاملات: {len(cycles)}  |  نرخ برد: {win_rate:.0f}%  (برد: {len(wins)})",
        f"📈 استاپ دنبال‌کننده: {len(trail)}   🛑 حد ضرر: {len(sl)}" + (f"   ▫️ سایر: {len(other)}" if other else ""),
        f"سود/ضرر خالص: {_pnl(net)}",
        f"کارمزد پرداختی: {_n(fees)}$",
        "",
        "جزئیات:",
    ]
    for c in cycles[-15:]:
        icon = {"trail": "📈", "stop": "🛑", "liquidation": "💥", "timeout": "⏱"}.get(
            str(c.get("exit_reason")), "▫️"
        )
        lines.append(
            f"  {icon} {_coin(c.get('symbol'))} {_side_badge(c.get('side')).split()[1]} "
            f"→ {_pnl(c.get('net_pnl'))}"
        )
    if len(cycles) > 15:
        lines.append(f"  … و {len(cycles) - 15} مورد دیگر")
    return "\n".join(lines)


def why_panel(storage: Storage) -> str:
    """گزارش آخرین مانیتور — چند نماد بررسی شد و چرا وارد نشدیم."""
    report = storage.get_setting("last_monitor_report", None)
    if not isinstance(report, dict) or not report:
        fallback = _wait_reason(storage)
        return f"⏳ {fallback}" if fallback else "هنوز اولین مانیتور انجام نشده."

    ts = safe_int(report.get("ts"))
    age = max(0, int((time.time() * 1000 - ts) / 1000)) if ts else 0
    when = f"{age} ثانیه پیش" if age < 90 else f"{age // 60} دقیقه پیش"

    lines = [
        f"🔍 آخرین مانیتور ({when})",
        "",
        f"نماد زیر نظر (Watchlist): {safe_int(report.get('watchlist'))}",
        f"بررسی‌شده: {safe_int(report.get('checked'))}",
        f"ستاپ معتبر: {safe_int(report.get('setups'))}",
        f"ورود انجام‌شده: {safe_int(report.get('opened'))}",
        f"اسلات خالی: {safe_int(report.get('free_slots'))}",
    ]

    rejects = report.get("rejects") or {}
    if isinstance(rejects, dict) and rejects:
        lines += ["", "دلیل رد شدن:"]
        for code, count in sorted(rejects.items(), key=lambda kv: -kv[1]):
            lines.append(f"  • {_reject_label(code)}: {count}")
    else:
        lines += ["", "هیچ نمادی به مرحلهٔ ارزیابی نرسید."]

    lines += [
        "",
        "این عادی است — بیشتر پامپ‌ها هیچ‌وقت شرایط ورود را کامل نمی‌کنند.",
    ]
    return "\n".join(lines)


_REJECT_LABELS = {
    "cooldown": "در استراحت بعد از معاملهٔ قبلی",
    "no_exhaustion": "نشانهٔ خستگی نیامده (سایهٔ بالا کافی نیست)",
    "no_deceleration": "شتاب صعود هنوز کم نشده",
    "no_structure_break": "کف تأییدشده هنوز نشکسته",
    "cascade_blocked_no_volume": "دادهٔ حجم معتبر نبود (آبشار مجاز نیست)",
    "cascade_too_early_in_bar": "کندل جاری تازه شروع شده",
    "cascade_no_acceleration": "افت شتاب‌گیرنده نیست",
    "cascade_no_range_expansion": "دامنه به اندازهٔ کافی باز نشده",
    "cascade_no_volume_expansion": "حجم به اندازهٔ کافی بالا نرفته",
    "cascade_not_enough_history": "تاریخچهٔ کندل کافی نیست",
    "cascade_invalid_price": "قیمت نامعتبر",
    "spread_too_wide": "اسپرد بالا (هزینهٔ پنهان)",
    "stop_unavailable": "حد ضرر ساختاری قابل محاسبه نبود",
    "not_enough_candles": "کندل کافی موجود نیست",
    "invalid_price": "قیمت نامعتبر",
    "risk_rejected": "ریسک/مارجین اجازه نداد",
}


def _reject_label(code: str) -> str:
    return _REJECT_LABELS.get(str(code), str(code))


def watchlist_panel(storage: Storage) -> str:
    """فهرست نمادهای زیر نظر: تعداد، نام، و درصد پامپ.

    مرتب‌شده بر اساس پامپ فعلی (بیشترین اول) تا سریع ببینی کدام‌ها
    داغ‌ترند. اوج ثبت‌شده هم کنارش می‌آید چون گاهی نماد از سقفش برگشته
    ولی هنوز زیر نظر است — و دقیقاً همان‌جا ممکن است فرصت شورت باشد.
    """
    rows = storage.watchlist_active()
    if not rows:
        return (
            "👀 هیچ نمادی زیر نظر نیست.\n\n"
            f"هیچ ارزی پامپ ۲۴ ساعته ≥ {config.WATCHLIST_MIN_GAIN_PCT:.0f}% ندارد.\n"
            f"اسکن بعدی تا حداکثر {config.WATCHLIST_SCAN_SECONDS / 60:.0f} دقیقهٔ دیگر."
        )

    cooldowns = {str(c.get("symbol")) for c in storage.active_cooldowns()}
    busy = {str(x) for x in storage.open_symbols()}

    ordered = sorted(rows, key=lambda r: safe_float(r.get("last_gain_pct")), reverse=True)

    lines = [f"👀 {len(ordered)} نماد زیر نظر", ""]
    for i, r in enumerate(ordered, 1):
        symbol = str(r.get("symbol"))
        now_pct = safe_float(r.get("last_gain_pct"))
        peak_pct = safe_float(r.get("peak_gain_pct"))
        mark = "🟡" if symbol in busy else ("😴" if symbol in cooldowns else "▫️")
        line = f"{i}. {mark} {_coin(symbol)} — {now_pct:+.1f}%"
        # اوج فقط وقتی نمایش داده می‌شود که واقعاً بالاتر از الان باشد
        if peak_pct - now_pct >= 1.0:
            line += f"  (اوج {peak_pct:+.1f}%)"
        trades = safe_int(r.get("trade_count"))
        if trades:
            line += f"  [{trades} ترید]"
        lines.append(line)

    lines += [
        "",
        f"🟡 پوزیشن باز  |  😴 در استراحت  |  ▫️ فقط زیر نظر",
        f"آستانهٔ ورود به این فهرست: پامپ ۲۴ ساعته ≥ {config.WATCHLIST_MIN_GAIN_PCT:.0f}%",
    ]
    return "\n".join(lines)


def funnel_panel(storage: Storage) -> str:
    """قیف کامل: از کاندید تا معامله — مهم‌ترین گزارش برای تحلیل بعدی."""
    f = storage.watchlist_funnel()
    total = f.get("total_candidates", 0)
    if not total:
        return "هنوز هیچ کاندیدی ثبت نشده."

    with_setup = f.get("candidates_with_setup", 0)
    with_trade = f.get("candidates_with_trade", 0)

    def pct(part: int) -> str:
        return f"{part / total * 100:.0f}%" if total else "-"

    lines = [
        "🔻 قیف V3",
        "",
        f"کاندید (وارد Watchlist): {total}",
        f"  └ حداقل یک ستاپ داشت: {with_setup} ({pct(with_setup)})",
        f"      └ حداقل یک معامله شد: {with_trade} ({pct(with_trade)})",
        "",
        f"مجموع ستاپ‌ها: {f.get('total_setups', 0)}",
        f"مجموع معاملات: {f.get('total_trades', 0)}",
    ]

    rejects = storage.reject_breakdown()
    if rejects:
        lines += ["", "گلوگاه‌ها (دلیل رد شدن ستاپ‌های نزدیک):"]
        for row in rejects[:8]:
            lines.append(f"  • {_reject_label(row.get('reject_code'))}: {row.get('n')}")

    return "\n".join(lines)


def research_panel(storage: Storage, mode: str = "virtual") -> str:
    """گزارش کامل تحقیقاتی — همان چیزی که بعد از چند روز باید بررسی شود."""
    rep = storage.research_report(mode)
    t = rep["trades"]
    f = rep["funnel"]

    if t["total"] == 0:
        total_c = f.get("total_candidates", 0)
        return "\n".join([
            "📊 گزارش تحقیقاتی V3",
            "",
            f"کاندید تا الان: {total_c}",
            f"ستاپ: {f.get('total_setups', 0)}  |  معامله: 0",
            "",
            "هنوز هیچ معاملهٔ بسته‌شده‌ای نیست.",
            "برای دیدن گلوگاه‌ها: «قیف»",
        ])

    pf = t["profit_factor"]
    lines = [
        f"📊 گزارش تحقیقاتی V3 ({_mode_label(mode)})",
        "",
        "── قیف ──",
        f"کاندید: {f.get('total_candidates', 0)}"
        f"  →  ستاپ: {f.get('total_setups', 0)}"
        f"  →  معامله: {t['total']}",
        "",
        "── نتیجه ──",
        f"برد: {t['wins']}  |  باخت: {t['losses']}  |  نرخ برد: {t['win_rate']:.1f}%",
        f"سود ناخالص: {_n(t['gross_profit'])}$  |  ضرر ناخالص: {_n(t['gross_loss'])}$",
        f"کارمزد کل: {_n(t['total_fees'])}$",
        f"سود/ضرر خالص: {_n(t['net_pnl'])}$",
        f"ضریب سود (PF): {pf:.2f}" if pf else "ضریب سود (PF): —",
        f"انتظار هر معامله: {_n(t['expectancy'])}$",
        "",
        "── ریسک ──",
        f"میانگین برد: {_n(t['avg_win'])}$  |  میانگین باخت: {_n(t['avg_loss'])}$",
        f"بزرگ‌ترین برد: {_n(t['largest_win'])}$  |  بزرگ‌ترین باخت: {_n(t['largest_loss'])}$",
        f"حداکثر افت سرمایه: {_n(t['max_drawdown'])}$",
        f"میانگین مدت معامله: {t['avg_duration_min']:.0f} دقیقه",
    ]

    # تفکیک بر اساس نوع ورود — سؤال کلیدی تحقیق
    by_type = rep.get("by_entry_type") or {}
    if by_type:
        lines += ["", "── بر اساس نوع ورود ──"]
        for etype, b in sorted(by_type.items()):
            wr = (b["wins"] / b["n"] * 100.0) if b["n"] else 0.0
            label = {"FAST_CASCADE": "آبشار سریع",
                     "NORMAL_REVERSAL": "برگشت عادی"}.get(etype, etype)
            lines.append(
                f"{label}: {b['n']} معامله | برد {wr:.0f}% | خالص {_n(b['net'])}$"
            )

    by_exit = rep.get("by_exit_reason") or {}
    if by_exit:
        lines += ["", "── دلیل خروج ──"]
        labels = {"HARD_STOP": "حد ضرر", "TECHNICAL_REVERSAL": "برگشت ساختاری",
                  "MAX_HOLD": "پایان مهلت"}
        for reason, n in sorted(by_exit.items(), key=lambda kv: -kv[1]):
            lines.append(f"{labels.get(reason, reason)}: {n}")

    # تمرکز — آیا چند نماد کل نتیجه را می‌سازند؟
    by_symbol = rep.get("by_symbol") or {}
    if len(by_symbol) >= 2:
        ranked = sorted(by_symbol.items(), key=lambda kv: kv[1]["net"], reverse=True)
        total_pos = sum(v["net"] for _, v in ranked if v["net"] > 0)
        lines += ["", "── تمرکز ──", f"نمادهای درگیر: {len(by_symbol)}"]
        if total_pos > 0:
            top1 = max(ranked[0][1]["net"], 0) / total_pos * 100.0
            top3 = sum(max(v["net"], 0) for _, v in ranked[:3]) / total_pos * 100.0
            lines.append(f"سهم بهترین نماد: {top1:.0f}%  |  سه نماد برتر: {top3:.0f}%")
            if top1 > 50:
                lines.append("⚠️ بیش از نیمی از سود از یک نماد — نتیجه متمرکز است.")

    lines += ["", "برای فایل کامل: «خروجی»"]
    return "\n".join(lines)


def help_text() -> str:
    return "\n".join([
        "🤖 ربات V3 — شورت بعد از پامپ افراطی (Paper Test)",
        "",
        "جریان کار:",
        f"  پامپ ۲۴ ساعته ≥ {config.WATCHLIST_MIN_GAIN_PCT:.0f}% → زیر نظر گرفتن",
        "  → نشانهٔ خستگی یا آبشار فروش → شورت",
        "  → حد ضرر ساختاری یا برگشت تأییدشده → خروج → استراحت",
        "",
        "دستورات:",
        "• ترید فعال / ترید خاموش — روشن و خاموش کردن ترید واقعی",
        "• ترید مجازی فعال / ترید مجازی خاموش — روشن و خاموش کردن مجازی",
        "• پنل — پنل ترید واقعی",
        "• ترید مجازی — پنل ترید مجازی",
        "• واچ — نمادهای زیر نظر",
        "• قیف — از چند کاندید، چند ستاپ و چند معامله",
        "• پوزیشن — پوزیشن‌های باز",
        "• پوزیشن ۵ — حداکثر پوزیشن هم‌زمان (۱ تا ۳۰)",
        "• دلار ۱۰ — مارجین هر پوزیشن، ۱ تا ۱۰۰۰ (۰ = خودکار)",
        "• اهرم ۱۰ — لوریج (۱ تا ۱۰۰)",
        "• استراحت ۲ — ساعت استراحت هر نماد بعد از خروج (۱ تا ۱۲)",
        "• سقف ۵۰ — سقف سرمایهٔ درگیر (۰ = کل موجودی)",
        "• زنده — مانیتورینگ لحظه‌ای پوزیشن‌های باز",
        "• امروز — خلاصهٔ معاملات امروز",
        "• گزارش ۱۵ — فاصلهٔ گزارش خودکار به دقیقه (۰ = خاموش)",
        "• چرا — گزارش آخرین مانیتور و دلیل ورود نکردن",
        "• گزارش کامل — آمار کامل تحقیقاتی (برد، PF، تمرکز، تفکیک نوع ورود)",
        "• خروجی — ساخت فایل CSV کامل معاملات برای تحلیل بیرونی",
        "• آمار — آمار واقعی و مجازی",
        "• ریست آمار — پاک کردن تاریخچه",
        "• وضعیت — سلامت سیستم",
        "",
        "⚠️ قوانین استراتژی در طول این تست قفل‌اند و تغییر نمی‌کنند.",
    ])


def symbols_panel(storage: Storage) -> str:
    """V3 لیست ثابت ندارد — کل بازار اسکن می‌شود."""
    count = safe_int(storage.get_setting("tradable_count", 0))
    watch = storage.watchlist_active()
    return "\n".join([
        f"🌐 کل بازار اسکن می‌شود: {count} قرارداد",
        f"👀 الان زیر نظر: {len(watch)} نماد",
        "",
        f"هر نمادی که پامپ ۲۴ ساعته‌اش به {config.WATCHLIST_MIN_GAIN_PCT:.0f}% برسد",
        "خودکار وارد فهرست زیر نظر می‌شود — لیست ثابتی وجود ندارد.",
        "",
        "برای دیدن فهرست: «واچ»",
    ])


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

        if cmd.startswith("استراحت "):
            try:
                value = float(parse_number(cmd.split(" ", 1)[1]))
            except (ValueError, IndexError):
                return "عدد نامعتبر. مثال: استراحت ۲"
            if not config.COOLDOWN_HOURS_MIN <= value <= config.COOLDOWN_HOURS_MAX:
                return (
                    f"عدد باید بین {config.COOLDOWN_HOURS_MIN} تا "
                    f"{config.COOLDOWN_HOURS_MAX} ساعت باشد."
                )
            self.storage.set_setting("cooldown_hours", value)
            return (
                f"✅ استراحت روی {value:.0f} ساعت تنظیم شد — بعد از هر خروج، "
                "همان نماد تا این مدت معامله نمی‌شود، حتی اگر دوباره پامپ کند."
            )

        if cmd in {"واچ", "واچ لیست", "واچ‌لیست", "/watchlist"}:
            return watchlist_panel(self.storage)

        if cmd in {"قیف", "فانل", "/funnel"}:
            return funnel_panel(self.storage)

        if cmd in {"گزارش کامل", "گزارش تحقیق", "تحقیق", "/research"}:
            return research_panel(self.storage, "virtual")

        if cmd in {"خروجی", "اکسپورت", "/export"}:
            import os as _os
            path = _os.path.join(_os.path.dirname(config.RUNTIME_DB) or ".",
                                 "v3_paper_trades.csv")
            try:
                n = self.storage.export_trades_csv(path, "virtual")
            except Exception as exc:
                return f"خروجی گرفته نشد: {exc}"
            if not n:
                return "هنوز معاملهٔ بسته‌شده‌ای برای خروجی نیست."
            return (
                f"✅ {n} معامله در فایل زیر ذخیره شد:\n"
                f"{path}\n\n"
                "شامل: قیمت ورود/خروج، حد ضرر، دلیل خروج، MFE، و همهٔ "
                "اعداد لحظهٔ سیگنال (سایهٔ بالا، شتاب، دامنه، حجم، ATR)."
            )

        if cmd in {"چرا", "دلیل", "/why"}:
            return why_panel(self.storage)

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
            lev = safe_int(self.storage.get_setting("leverage", config.DEFAULT_LEVERAGE))
            real_on = bool(self.storage.get_setting("real_trading_enabled", False))
            if real_on:
                balance, _ = self.storage.cached_balance()
            else:
                balance = safe_float(self.storage.get_setting("virtual_balance", 0.0))

            out = [
                f"✅ هر پوزیشن دقیقاً {value:,.2f}$ مارجین می‌گیرد.",
                "",
                f"با لوریج {lev}x → ارزش هر پوزیشن {value * lev:,.2f}$",
                f"با {slots} پوزیشن هم‌زمان → مجموع {value * slots:,.2f}$ درگیر",
                f"موجودی فعلی: {balance:,.2f}$",
            ]
            if balance > 0 and value > balance:
                out += [
                    "",
                    f"⛔️ این عدد از کل موجودی ({balance:,.2f}$) بیشتر است — "
                    "هیچ پوزیشنی باز نمی‌شود.",
                    f"بیشترین مقدار ممکن: «دلار {balance:,.0f}»",
                ]
            elif balance > 0 and value * slots > balance:
                possible = max(1, int(balance // value))
                out += [
                    "",
                    f"⚠️ موجودی برای {slots} اسلات کافی نیست — عملاً {possible} "
                    f"پوزیشن باز می‌شود.",
                    f"برای اصلاح: «پوزیشن {possible}» یا «دلار {balance / slots:,.0f}»",
                ]
            return "\n".join(out)

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
            size = safe_float(self.storage.get_setting("position_size", config.POSITION_SIZE_USDT))
            note = f"✅ لوریج روی {value}x تنظیم شد — همین عدد برای همهٔ پوزیشن‌ها استفاده می‌شود."
            if size > 0:
                note += f"\nبا مارجین {size:,.2f}$ → ارزش هر پوزیشن {size * value:,.2f}$"
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
