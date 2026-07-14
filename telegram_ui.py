"""فقط رندر متن تلگرام؛ بدون I/O و بدون منطق تصمیم‌گیری."""
from __future__ import annotations

from typing import Any

from utils import now_ms, utc_iso


def _n(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "نامشخص"


def _price(value: Any) -> str:
    try:
        x = float(value)
        if x >= 1000:
            return f"{x:,.2f}"
        if x >= 1:
            return f"{x:.5f}".rstrip("0").rstrip(".")
        return f"{x:.10f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "نامشخص"


def _age(updated_at: int | None) -> str:
    if not updated_at:
        return "نامشخص"
    sec = max(0, (now_ms() - int(updated_at)) // 1000)
    if sec < 60:
        return f"{sec} ثانیه"
    return f"{sec // 60} دقیقه"


def _duration(minutes: Any) -> str:
    try:
        value = max(0, int(minutes))
    except (TypeError, ValueError):
        return "نامشخص"
    if value < 60:
        return f"{value} دقیقه"
    if value < 1440:
        hours = value / 60
        return f"{hours:.1f} ساعت" if hours % 1 else f"{int(hours)} ساعت"
    days = value / 1440
    return f"{days:.1f} روز" if days % 1 else f"{int(days)} روز"


def trade_panel(
    settings: dict[str, Any],
    account: dict[str, Any],
    slots: dict[str, int],
    pnl: dict[str, float],
    stage_counts: dict[str, int],
) -> str:
    trade = "🟢 فعال" if settings.get("real_trade_enabled") else "🔴 خاموش"
    connected = "🟢 متصل" if account.get("connected") else "🔴 قطع"
    startup = "✅ آماده" if settings.get("startup_ready") else f"⏳ {settings.get('startup_phase', 'آماده‌سازی')}"
    used_margin = float(account.get("position_margin") or 0) + float(account.get("order_margin") or 0)
    blocked_real = stage_counts.get("REAL_BLOCKED_DIRECTIONS", 0)
    real_ready = max(0, stage_counts.get("REAL_READY", 0) + stage_counts.get("REAL_WATCH", 0) - blocked_real)
    medium = stage_counts.get("MEDIUM", 0)
    initial = stage_counts.get("INITIAL", 0)
    relearn = stage_counts.get("MEDIUM_RELEARN", 0) + blocked_real
    return f"""
📊 پنل ترید
━━━━━━━━━━━━━━━━━━
ترید واقعی: {trade}
ربات: {startup}
Toobit: {connected}
آخرین بروزرسانی Toobit: {_age(account.get('updated_at'))} قبل

💰 موجودی Toobit: {_n(account.get('balance'))} USDT
💵 مارجین آزاد: {_n(account.get('available'))} USDT
📌 مارجین استفاده‌شده: {_n(used_margin)} USDT
📈 سود/ضرر شناور: {_n(account.get('unrealized_pnl'))} USDT

دلار هر پوزیشن: {_n(settings.get('trade_margin_usdt'))} USDT
لوریج: {int(settings.get('leverage', 0))}x
مارجین: Isolated اجباری
حداکثر پوزیشن واقعی: {slots['max']}
اسلات پُر: {slots['used']}
اسلات خالی: {slots['free']}
پوزیشن باز Toobit: {slots.get('toobit_open', account.get('open_positions', 0))}
پوزیشن تأییدشده ربات: {slots['open']}
Pending Open ربات: {slots['pending']}

سود/ضرر امروز: {_n(pnl.get('today'))} USDT
سود/ضرر کل: {_n(pnl.get('total'))} USDT

جهت‌های آماده رئال: {real_ready}
جهت‌های متوسط: {medium}
جهت‌های اولیه: {initial}
جهت‌های در بازآموزی: {relearn}
ارزهای فعال/ذخیره: {settings.get('active_symbols_count', 0)} / {settings.get('reserve_symbols_count', 0)}
━━━━━━━━━━━━━━━━━━
""".strip()


def stats_panel(stats: dict[str, Any], stage_counts: dict[str, int]) -> str:
    blocks = []
    icons = {"REAL": "🔴", "MEDIUM": "🟡", "INITIAL": "⚪"}
    names = {"REAL": "Toobit واقعی", "MEDIUM": "متوسط", "INITIAL": "اولیه"}
    for tier in ("REAL", "MEDIUM", "INITIAL"):
        b = stats.get(tier, {})
        blocks.append(
            f"{icons[tier]} {names[tier]}\n"
            f"کل: {b.get('total', 0)} | فعال: {b.get('active', 0)}\n"
            f"TP: {b.get('tp', 0)} | Stop: {b.get('stop', 0)} | Win: {_n(b.get('win_rate', 0), 1)}%\n"
            f"امروز: {_n(b.get('today_pnl', 0))} | کل: {_n(b.get('net_pnl', 0))} USDT"
        )
    real_pnl = stats.get("real_display_pnl", {})
    return (
        "📊 آمار ربات\n━━━━━━━━━━━━━━━━━━\n"
        + "\n\n".join(blocks)
        + f"\n\n💰 رئال امروز: {_n(real_pnl.get('today'))} USDT"
        + f"\n💎 رئال کل: {_n(real_pnl.get('total'))} USDT"
        + f"\n\nآماده رئال: {max(0, stage_counts.get('REAL_READY', 0) + stage_counts.get('REAL_WATCH', 0) - stage_counts.get('REAL_BLOCKED_DIRECTIONS', 0))} جهت"
        + f"\nبازآموزی/مسدود رئال: {stage_counts.get('MEDIUM_RELEARN', 0) + stage_counts.get('REAL_BLOCKED_DIRECTIONS', 0)} جهت"
        + "\n━━━━━━━━━━━━━━━━━━"
    )


def positions_panel(signals: list[dict[str, Any]], slots: dict[str, int]) -> str:
    if not signals:
        return f"📈 سیگنال یا پوزیشن فعال وجود ندارد.\nاسلات واقعی: {slots['used']} / {slots['max']}"
    lines = [f"📈 فعال‌ها | اسلات واقعی {slots['used']} / {slots['max']}", "━━━━━━━━━━━━━━━━━━"]
    for s in signals:
        icon = {"REAL": "🔴", "MEDIUM": "🟡", "INITIAL": "⚪"}.get(s.get("tier"), "•")
        lines.append(
            f"{icon} #{s.get('id')} {s.get('canonical')} {s.get('side')}\n"
            f"ورود {_price(s.get('entry'))} | TP {_price(s.get('tp'))} | SL {_price(s.get('sl'))}\n"
            f"وضعیت: {s.get('status')} | زمان تخمینی: {s.get('expected_hold_minutes', '?')} دقیقه"
        )
        lines.append("────────────")
    return "\n".join(lines).rstrip("─\n")


def coins_panel(symbols: list[dict[str, Any]]) -> str:
    active = [x for x in symbols if x.get("active")]
    reserve = [x for x in symbols if not x.get("active")]
    active_text = "، ".join(x["canonical"].replace("USDT", "") for x in active)
    return f"""
🪙 وضعیت ارزها
━━━━━━━━━━━━━━━━━━
فعال: {len(active)}
{active_text}

ذخیره آماده/درحال پروفایل: {len(reserve)}
کل Registry: {len(symbols)}
همه نمادها در OKX، Bybit و Toobit اعتبارسنجی می‌شوند.
━━━━━━━━━━━━━━━━━━
""".strip()


def health_panel(health: list[dict[str, Any]], settings: dict[str, Any], account: dict[str, Any]) -> str:
    lines = ["🩺 سلامت ربات", "━━━━━━━━━━━━━━━━━━"]
    lines.append(f"Startup: {'READY' if settings.get('startup_ready') else settings.get('startup_phase', 'BOOT')}")
    lines.append(f"Toobit cache: {'OK' if account.get('connected') else 'ERROR'} | age {_age(account.get('updated_at'))}")
    for item in health:
        icon = "✅" if item.get("level") == "ok" else "⚠️" if item.get("level") == "warning" else "❌"
        lines.append(f"{icon} {item.get('component')}: {item.get('message')}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def signal_message(signal: dict[str, Any]) -> str:
    icon = {"REAL": "🔴", "MEDIUM": "🟡", "INITIAL": "⚪"}.get(signal.get("tier"), "📡")
    title = {"REAL": "سیگنال Toobit", "MEDIUM": "سیگنال متوسط", "INITIAL": "سیگنال اولیه"}.get(signal.get("tier"), "سیگنال")
    d = signal.get("decision") or {}
    reasons = d.get("reasons") or []
    risks = d.get("risks") or []
    reason_text = "\n".join(f"• {x}" for x in reasons[:4]) or "• شواهد نرم ترکیبی"
    risk_text = "\n".join(f"• {x}" for x in risks[:3]) or "• ریسک ویژه ثبت نشد"
    status = "\nوضعیت اجرا: PENDING_OPEN؛ تأیید Toobit بعد از ۷۰ ثانیه" if signal.get("tier") == "REAL" else ""
    return f"""
{icon} {title} #{signal.get('id')}

{signal.get('canonical')} | {signal.get('side')}
ورود: {_price(signal.get('entry'))}
TP: {_price(signal.get('tp'))}
SL: {_price(signal.get('sl'))}
RR: {_n(signal.get('rr'))}

مارجین: {_n(signal.get('margin_usdt'))} USDT
لوریج: {signal.get('leverage')}x
سود خالص پیش‌بینی‌شده: {_n(signal.get('expected_net_profit'))} USDT
تایم معامله: {signal.get('trade_timeframe', d.get('trade_timeframe', '-'))}
تایم ورود: {signal.get('entry_timeframe', d.get('entry_timeframe', '-'))}
زمان تخمینی: حدود {_duration(signal.get('expected_hold_minutes'))}
مدل ورود: {d.get('entry_type', '-')}
رفتار: {d.get('behavior', '-')}
قدرت: {_n(d.get('strength_score'), 1)}
اطمینان جهت: {_n(d.get('direction_score'), 1)}
کیفیت ورود: {_n(d.get('entry_quality'), 1)}
منبع داده: {signal.get('data_source')}{status}

دلایل:
{reason_text}
ریسک‌ها:
{risk_text}
""".strip()


def position_open_message(signal: dict[str, Any]) -> str:
    return f"""
🟢 پوزیشن Toobit تأیید شد #{signal.get('id')}
{signal.get('canonical')} | {signal.get('side')} | {signal.get('trade_timeframe', '-')}
قیمت ورود واقعی: {_price(signal.get('actual_entry') or signal.get('entry'))}
TP: {_price(signal.get('tp'))} | SL: {_price(signal.get('sl'))}
اسلات تا بسته‌شدن پوزیشن پُر می‌ماند.
""".strip()


def result_message(signal: dict[str, Any]) -> str:
    result = signal.get("result") or signal.get("status")
    icon = {"TP": "✅", "STOP": "❌", "FAILED_OPEN": "⚠️", "MANUAL_CLOSE": "ℹ️", "CANCELLED": "⛔"}.get(result, "ℹ️")
    diagnosis = signal.get("stop_diagnosis") or {}
    diag_text = ""
    if diagnosis:
        top = sorted(diagnosis.items(), key=lambda x: x[1], reverse=True)[:4]
        diag_text = "\n\nبررسی علت Stop:\n" + "\n".join(f"• {k}: {_n(v, 1)}%" for k, v in top)
    return f"""
{icon} نتیجه #{signal.get('id')}
{signal.get('canonical')} | {signal.get('side')} | {signal.get('tier')} | {signal.get('trade_timeframe', '-')}
نتیجه: {result}
قیمت پایان: {_price(signal.get('close_price'))}
سود/ضرر خالص: {_n(signal.get('net_pnl'))} USDT
مدت: {max(0, int((int(signal.get('closed_at') or now_ms()) - int(signal.get('created_at') or now_ms())) / 60000))} دقیقه
یادگیری {signal.get('canonical')}-{signal.get('side')}: {float(signal.get('learning_effect_percent') or 0):+.2f}%
اطمینان یادگیری: {signal.get('learning_confidence', 'در انتظار نمونه بیشتر')}{diag_text}
""".strip()


def failed_open_message(signal: dict[str, Any]) -> str:
    return f"⚠️ پوزیشن #{signal.get('id')} برای {signal.get('canonical')} بعد از ۷۰ ثانیه در Toobit پیدا نشد؛ اسلات آزاد شد."


def ready_message(active_count: int) -> str:
    return f"""
✅ ربات آماده شروع است

پروفایل‌های فعال: {active_count} / {active_count}
ترید واقعی: خاموش
سیگنال‌های اولیه و متوسط: فعال
یادگیری، تست و سناریوسازی آماده است.
""".strip()


def startup_message(phase: str) -> str:
    return f"⏳ آماده‌سازی ربات\n{phase}\nتا تکمیل پروفایل‌های فعال هیچ سیگنالی صادر نمی‌شود."


def help_text() -> str:
    return """
📌 دستورات فارسی

ترید | پنل | وضعیت
آمار
پوزیشن
کوین‌ها
سلامت

ترید فعال
ترید خاموش
ترید دلار 5        (۱ تا ۱۰۰۰۰)
ترید لوریج 10     (۱ تا ۱۰۰)
حداکثر پوزیشن 3  (۱ تا ۲۰۰)

ریست سود
ریست سود کل

لاگ رد فعال
لاگ رد خاموش
""".strip()
