"""همه متن‌های فارسی ربات."""
from __future__ import annotations

from typing import Any

from . import config
from .utils import format_num, side_to_persian


def _direction_text(side: str) -> str:
    return "لانگ / خرید 🟢" if str(side).upper() == "BUY" else "شورت / فروش 🔴"


def _mode_text(signal: dict[str, Any]) -> str:
    if signal.get("signal_mode") == "REAL":
        return "رئال Toobit ✅"
    return "عادی / داخلی 📝"


def _pnl_line(pnl: float, pct: float | None = None) -> str:
    sign = "+" if pnl >= 0 else ""
    if pct is None:
        return f"{sign}{format_num(pnl, 4)} USDT"
    pct_sign = "+" if pct >= 0 else ""
    return f"{sign}{format_num(pnl, 4)} USDT  ({pct_sign}{format_num(pct, 4)}%)"


def signal_message(signal: dict[str, Any]) -> str:
    icon = "🚀" if signal["side"] == "BUY" else "🔻"
    reasons = "\n".join(f"✅ {r}" for r in signal.get("reasons", [])) or "✅ شرایط اصلی برقرار است"
    warnings = "\n".join(f"⚠️ {w}" for w in signal.get("warnings", []))
    ind = signal.get("indicators", {})
    text = f"""{icon} سیگنال {_direction_text(signal['side'])} — {signal['symbol']}

نوع ثبت سیگنال: {_mode_text(signal)}
دلیل نوع ثبت: {signal.get('execution_reason', '-')}

ورود: {format_num(signal['entry'])}
حد سود: {format_num(signal['tp'])}  (+{config.FIXED_TP_PERCENT:.2f}%)
حد ضرر: {format_num(signal['sl'])}  (-{config.FIXED_SL_PERCENT:.2f}%)

امتیاز: {signal['score']} از 100
نوع ورود: {signal['signal_type']} اسکالپ ۵ دقیقه‌ای
منبع تحلیل: OKX  ({signal['okx_symbol']})
اجرای رئال: Toobit  ({signal['toobit_symbol']})

تنظیمات معامله در لحظه سیگنال:
• دلار هر ترید: {format_num(signal.get('trade_amount_usdt'), 2)} USDT
• لوریج: {int(signal.get('leverage', 0))}x
• حداکثر پوزیشن رئال: {int(signal.get('max_positions', 0))}

دلایل ورود:
{reasons}"""
    if warnings:
        text += f"\n\nنکات احتیاط:\n{warnings}"
    text += f"""

خلاصه اندیکاتورها:
• RSI: {format_num(ind.get('rsi'), 2)}
• حجم زنده نسبت به میانگین: {format_num(ind.get('volume_multiplier'), 2)}x
• ATR: {format_num(ind.get('atr_percent'), 2)}%
• ADX: {format_num(ind.get('adx'), 2)}

شناسه سیگنال: {signal['signal_id']}"""
    return text


def normal_result_message(signal: dict[str, Any], result: str, price: float, pnl: float = 0.0, pnl_percent: float | None = None) -> str:
    direction = _direction_text(signal["side"])
    if result == "TP":
        return f"""✅ حد سود عادی فعال شد — {signal['symbol']}

نوع سیگنال: عادی / داخلی
جهت معامله: {direction}
ورود: {format_num(signal['entry'])}
قیمت برخورد: {format_num(price)}
نتیجه عادی: TP  (+{config.FIXED_TP_PERCENT:.2f}%)
سود/ضرر محاسبه‌شده: {_pnl_line(pnl, pnl_percent)}

دلیل خروج:
قیمت طبق دیتای OKX به حد سود ثابت سیگنال رسید.

جزئیات:
• حرکت در جهت سیگنال ادامه پیدا کرد
• تارگت ثابت {config.FIXED_TP_PERCENT:.2f}% لمس شد
• نتیجه عادی بر اساس قیمت OKX ثبت شد

منبع بررسی: OKX
شناسه سیگنال: {signal['signal_id']}"""
    return f"""❌ حد ضرر عادی فعال شد — {signal['symbol']}

نوع سیگنال: عادی / داخلی
جهت معامله: {direction}
ورود: {format_num(signal['entry'])}
قیمت برخورد: {format_num(price)}
نتیجه عادی: SL  (-{config.FIXED_SL_PERCENT:.2f}%)
سود/ضرر محاسبه‌شده: {_pnl_line(pnl, pnl_percent)}

دلیل استاپ:
قیمت طبق دیتای OKX به محدوده حد ضرر ثابت سیگنال رسید.

جزئیات:
• قیمت از ناحیه ورود برگشت
• مومنتوم ادامه پیدا نکرد
• سطح SL ثابت سیگنال لمس شد

منبع بررسی: OKX
شناسه سیگنال: {signal['signal_id']}"""


def real_result_message(signal: dict[str, Any], result: str, price: float, pnl: float = 0.0, pnl_percent: float | None = None) -> str:
    direction = _direction_text(signal["side"])
    if result == "TP":
        return f"""✅ حد سود رئال Toobit فعال شد — {signal['symbol']}

نوع سیگنال: رئال Toobit
جهت معامله: {direction}
ورود ثبت‌شده: {format_num(signal['entry'])}
قیمت خروج/برخورد: {format_num(price)}
نتیجه رئال: TP  (+{config.FIXED_TP_PERCENT:.2f}%)
سود/ضرر محاسبه‌شده: {_pnl_line(pnl, pnl_percent)}

دلیل خروج:
پوزیشن رئال/پیگیری‌شده در Toobit به محدوده حد سود ثابت رسید.

جزئیات:
• حرکت طبق جهت سیگنال ادامه پیدا کرد
• قیمت به تارگت {config.FIXED_TP_PERCENT:.2f}% رسید
• نتیجه رئال از مارک‌پرایس/قیمت Toobit پیگیری شد

منبع نتیجه: Toobit
شناسه سیگنال: {signal['signal_id']}"""
    return f"""❌ حد ضرر رئال Toobit فعال شد — {signal['symbol']}

نوع سیگنال: رئال Toobit
جهت معامله: {direction}
ورود ثبت‌شده: {format_num(signal['entry'])}
قیمت خروج/برخورد: {format_num(price)}
نتیجه رئال: SL  (-{config.FIXED_SL_PERCENT:.2f}%)
سود/ضرر محاسبه‌شده: {_pnl_line(pnl, pnl_percent)}

دلیل استاپ:
پوزیشن رئال/پیگیری‌شده در Toobit به محدوده حد ضرر ثابت رسید.

جزئیات:
• قیمت مارک/آخرین قیمت Toobit به محدوده ضرر رسید
• معامله طبق مدیریت ریسک ثابت بسته یا به عنوان SL ثبت شد
• علت خروج، برخورد به SL ثابت سیگنال است

منبع نتیجه: Toobit
شناسه سیگنال: {signal['signal_id']}"""


def stats_message(stats: dict[str, Any]) -> str:
    return f"""📊 آمار ربات اسکالپ ۵ دقیقه‌ای

تعداد کل سیگنال‌ها: {int(stats.get('signals_total', 0))}
سیگنال عادی/داخلی: {int(stats.get('signals_normal', 0))}
سیگنال رئال Toobit: {int(stats.get('signals_real', 0))}

آمار عادی / داخلی:
✅ TP عادی: {int(stats.get('normal_tp', 0))}
❌ SL عادی: {int(stats.get('normal_sl', 0))}
⏳ باز/نامشخص: {int(stats.get('normal_open', 0))}
درصد موفقیت عادی: {format_num(stats.get('normal_winrate', 0), 2)}%
💰 سود/ضرر عادی محاسبه‌شده: {format_num(stats.get('normal_pnl', 0), 4)} USDT

آمار رئال Toobit:
✅ TP رئال: {int(stats.get('real_tp', 0))}
❌ SL رئال: {int(stats.get('real_sl', 0))}
⏳ پوزیشن/معامله رئال باز: {int(stats.get('real_open', 0))}
⚠️ اجرای ناموفق: {int(stats.get('real_failed', 0))}
درصد موفقیت رئال: {format_num(stats.get('real_winrate', 0), 2)}%
💰 سود/ضرر رئال محاسبه‌شده: {format_num(stats.get('real_pnl', 0), 4)} USDT

جمع سود/ضرر کل ربات: {format_num(stats.get('total_pnl', 0), 4)} USDT
آخرین حذف آمار: {stats.get('last_reset_utc', '-')}
"""


def panel_message(
    settings: dict[str, Any],
    stats: dict[str, Any],
    balance: dict[str, Any] | None,
    positions: list[dict[str, Any]] | None,
    symbols_count: int,
    toobit_status: dict[str, Any] | None = None,
    errors: list[str] | None = None,
) -> str:
    balance = balance or {}
    positions = positions or []
    toobit_status = toobit_status or {}
    errors = errors or []
    trade_status = "فعال ✅" if settings.get("trade_enabled") else "خاموش ⛔"
    toobit_conn = "وصل ✅" if toobit_status.get("connected") else "قطع/ناموفق ❌"
    today_pnl = toobit_status.get("today_pnl", 0.0)
    today_pnl_error = toobit_status.get("today_pnl_error")
    err_text = ""
    if errors:
        err_text = "\nهشدارها:\n" + "\n".join(f"⚠️ {e}" for e in errors[:3])
    if today_pnl_error:
        err_text += f"\n⚠️ سود/ضرر امروز Toobit دریافت نشد: {today_pnl_error}"

    return f"""🤖 پنل ترید ربات اسکالپ کلاسیک ۵ دقیقه‌ای

وضعیت ترید رئال: {trade_status}
وضعیت اتصال Toobit: {toobit_conn}
پیام Toobit: {toobit_status.get('message', '-')}
منبع تحلیل: OKX
محل اجرا: Toobit
تایم‌فریم: ۵ دقیقه
نمادهای فعال/معتبر: {symbols_count}

تنظیمات ترید:
• دلار هر ترید: {format_num(settings.get('trade_amount_usdt'), 2)} USDT
• لوریج: {int(settings.get('leverage', 0))}x
• حداکثر پوزیشن رئال: {int(settings.get('max_positions', 0))}
• نوع مارجین: {settings.get('margin_type', '-')}

وضعیت مارجین Toobit:
• موجودی کل: {format_num(balance.get('balance'), 4)} USDT
• مارجین آزاد: {format_num(balance.get('available'), 4)} USDT
• مارجین پوزیشن: {format_num(balance.get('position_margin'), 4)} USDT
• مارجین سفارش: {format_num(balance.get('order_margin'), 4)} USDT
• سود/ضرر شناور: {format_num(balance.get('unrealized_pnl'), 4)} USDT
• سود/ضرر امروز Toobit: {format_num(today_pnl, 4)} USDT

پوزیشن‌های باز Toobit: {len(positions)}
سیگنال‌های کل: {int(stats.get('signals_total', 0))}
سیگنال عادی/رئال: {int(stats.get('signals_normal', 0))} / {int(stats.get('signals_real', 0))}
TP/SL عادی: {int(stats.get('normal_tp', 0))} / {int(stats.get('normal_sl', 0))}
TP/SL رئال: {int(stats.get('real_tp', 0))} / {int(stats.get('real_sl', 0))}
سود/ضرر کل ربات: {format_num(stats.get('total_pnl', 0), 4)} USDT{err_text}
"""


def help_message() -> str:
    return """📌 دستورات ربات — همه بدون اسلش هم کار می‌کنند

پنل ترید — نمایش پنل کامل مدیریت ترید
آمار — نمایش آمار عادی و رئال
حذف آمار — حذف آمار و سیگنال‌های باز
موجودی / مارجین — نمایش موجودی و مارجین Toobit
پوزیشن — نمایش پوزیشن‌های باز Toobit
چک توبیت — بررسی اتصال Toobit
سود امروز — نمایش سود/ضرر امروز Toobit

دلار ترید 10 — تنظیم مقدار هر ترید از ۱ تا ۱۰۰۰۰
لوریج ترید 10 — تنظیم لوریج از ۱ تا ۱۰۰
حداکثر پوزیشن 3 — تنظیم حداکثر پوزیشن رئال از ۱ تا ۱۰۰

ترید فعال — روشن کردن اجرای رئال روی Toobit بعد از چک اتصال
ترید خاموش — خاموش کردن اجرای رئال؛ سیگنال‌ها عادی می‌شوند
توبیت روشن — همان ترید فعال
توبیت خاموش — همان ترید خاموش

نسخه‌های اسلش‌دار مثل /پنل و /آمار هم پشتیبانی می‌شوند.
"""


def toobit_status_message(status: dict[str, Any], ok: bool) -> str:
    if ok:
        bal = status.get("balance") or {}
        return f"""✅ اتصال Toobit برقرار است.

موجودی کل: {format_num(bal.get('balance'), 4)} USDT
مارجین آزاد: {format_num(bal.get('available'), 4)} USDT
سود/ضرر شناور: {format_num(bal.get('unrealized_pnl'), 4)} USDT
"""
    return f"""❌ اتصال Toobit برقرار نیست.

دلیل:
{status.get('message', 'نامشخص')}

ترید رئال روشن نمی‌شود تا اتصال درست شود.
"""


def positions_message(positions: list[dict[str, Any]]) -> str:
    if not positions:
        return "📌 پوزیشن بازی در Toobit پیدا نشد."
    lines = ["📌 پوزیشن‌های باز Toobit"]
    for p in positions:
        side = p.get("side") or p.get("positionSide") or "-"
        lines.append(
            f"""

نماد: {p.get('symbol', '-')}
جهت: {side}
ورود: {format_num(p.get('avgPrice') or p.get('entryPrice'))}
قیمت مارک: {format_num(p.get('markPrice') or p.get('lastPrice'))}
حجم: {format_num(p.get('position') or p.get('positionAmt') or p.get('size') or p.get('quantity'))}
مارجین: {format_num(p.get('margin') or p.get('positionMargin'))}
لوریج: {p.get('leverage', '-')}x
سود/ضرر شناور: {format_num(p.get('unrealizedPnL') or p.get('unRealizedPnl'), 4)} USDT"""
        )
    return "".join(lines)


def balance_message(balance: dict[str, Any]) -> str:
    return f"""💰 موجودی و مارجین Toobit

موجودی کل: {format_num(balance.get('balance'), 4)} USDT
مارجین آزاد: {format_num(balance.get('available'), 4)} USDT
مارجین پوزیشن: {format_num(balance.get('position_margin'), 4)} USDT
مارجین سفارش: {format_num(balance.get('order_margin'), 4)} USDT
سود/ضرر شناور: {format_num(balance.get('unrealized_pnl'), 4)} USDT
کوپن: {format_num(balance.get('coupon'), 4)} USDT
"""
