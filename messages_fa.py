"""همه متن‌های فارسی ربات."""
from __future__ import annotations

from typing import Any

from . import config
from .utils import format_num, side_to_persian


def signal_message(signal: dict[str, Any]) -> str:
    icon = "🚀" if signal["side"] == "BUY" else "🔻"
    side_fa = side_to_persian(signal["side"])
    reasons = "\n".join(f"✅ {r}" for r in signal.get("reasons", [])) or "✅ شرایط اصلی برقرار است"
    warnings = "\n".join(f"⚠️ {w}" for w in signal.get("warnings", []))
    ind = signal.get("indicators", {})
    text = f"""{icon} سیگنال {side_fa} — {signal['symbol']}

ورود: {format_num(signal['entry'])}
حد سود: {format_num(signal['tp'])}  (+{config.FIXED_TP_PERCENT:.2f}%)
حد ضرر: {format_num(signal['sl'])}  (-{config.FIXED_SL_PERCENT:.2f}%)

امتیاز: {signal['score']} از 100
نوع سیگنال: {signal['signal_type']} اسکالپ ۵ دقیقه‌ای
منبع تحلیل: OKX  ({signal['okx_symbol']})
اجرای واقعی: Toobit  ({signal['toobit_symbol']})

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


def normal_result_message(signal: dict[str, Any], result: str, price: float) -> str:
    side_fa = side_to_persian(signal["side"])
    if result == "TP":
        return f"""✅ حد سود عادی فعال شد — {signal['symbol']}

نوع سیگنال: {side_fa}
ورود: {format_num(signal['entry'])}
قیمت برخورد: {format_num(price)}
نتیجه عادی: +{config.FIXED_TP_PERCENT:.2f}%

دلیل خروج:
قیمت طبق دیتای OKX به حد سود ثابت سیگنال رسید.

جزئیات:
• حرکت در جهت سیگنال ادامه پیدا کرد
• تارگت ثابت {config.FIXED_TP_PERCENT:.2f}% لمس شد
• نتیجه عادی بر اساس قیمت OKX ثبت شد

منبع بررسی: OKX
شناسه سیگنال: {signal['signal_id']}"""
    return f"""❌ حد ضرر عادی فعال شد — {signal['symbol']}

نوع سیگنال: {side_fa}
ورود: {format_num(signal['entry'])}
قیمت برخورد: {format_num(price)}
نتیجه عادی: -{config.FIXED_SL_PERCENT:.2f}%

دلیل استاپ:
قیمت طبق دیتای OKX به محدوده حد ضرر ثابت سیگنال رسید.

جزئیات:
• قیمت از ناحیه ورود برگشت
• مومنتوم ادامه پیدا نکرد
• سطح SL ثابت سیگنال لمس شد

منبع بررسی: OKX
شناسه سیگنال: {signal['signal_id']}"""


def real_result_message(signal: dict[str, Any], result: str, price: float, pnl: float = 0.0) -> str:
    side_fa = side_to_persian(signal["side"])
    pnl_text = f"{format_num(pnl, 4)} USDT"
    if result == "TP":
        return f"""✅ حد سود واقعی فعال شد — {signal['symbol']}

نوع سیگنال: {side_fa}
ورود واقعی/ثبت‌شده: {format_num(signal['entry'])}
قیمت خروج/برخورد: {format_num(price)}
نتیجه خام: +{config.FIXED_TP_PERCENT:.2f}%
سود/ضرر تقریبی: {pnl_text}

دلیل خروج:
پوزیشن واقعی/پیگیری‌شده در Toobit به محدوده حد سود ثابت رسید.

جزئیات:
• حرکت طبق جهت سیگنال ادامه پیدا کرد
• قیمت به تارگت {config.FIXED_TP_PERCENT:.2f}% رسید
• نتیجه واقعی از سمت Toobit/مارک‌پرایس پیگیری شد

منبع نتیجه: Toobit
شناسه سیگنال: {signal['signal_id']}"""
    return f"""❌ حد ضرر واقعی فعال شد — {signal['symbol']}

نوع سیگنال: {side_fa}
ورود واقعی/ثبت‌شده: {format_num(signal['entry'])}
قیمت خروج/برخورد: {format_num(price)}
نتیجه خام: -{config.FIXED_SL_PERCENT:.2f}%
سود/ضرر تقریبی: {pnl_text}

دلیل استاپ:
پوزیشن واقعی/پیگیری‌شده در Toobit به محدوده حد ضرر ثابت رسید.

جزئیات:
• قیمت مارک/آخرین قیمت توبیت به محدوده ضرر رسید
• معامله طبق مدیریت ریسک ثابت بسته یا قابل ثبت به عنوان SL شد
• علت خروج، برخورد به SL ثابت سیگنال است

منبع نتیجه: Toobit
شناسه سیگنال: {signal['signal_id']}"""


def stats_message(stats: dict[str, Any]) -> str:
    return f"""📊 آمار ربات اسکالپ ۵ دقیقه‌ای

تعداد کل سیگنال‌ها: {int(stats.get('signals_total', 0))}

آمار عادی / داخلی:
✅ TP عادی: {int(stats.get('normal_tp', 0))}
❌ SL عادی: {int(stats.get('normal_sl', 0))}
⏳ باز/نامشخص: {int(stats.get('normal_open', 0))}
درصد موفقیت عادی: {format_num(stats.get('normal_winrate', 0), 2)}%

آمار واقعی Toobit:
✅ TP واقعی: {int(stats.get('real_tp', 0))}
❌ SL واقعی: {int(stats.get('real_sl', 0))}
⏳ پوزیشن/معامله باز: {int(stats.get('real_open', 0))}
⚠️ اجرای ناموفق: {int(stats.get('real_failed', 0))}
💰 سود/ضرر خالص تقریبی: {format_num(stats.get('real_pnl', 0), 4)} USDT
درصد موفقیت واقعی: {format_num(stats.get('real_winrate', 0), 2)}%

آخرین حذف آمار: {stats.get('last_reset_utc', '-')}
"""


def panel_message(settings: dict[str, Any], stats: dict[str, Any], balance: dict[str, Any] | None, positions: list[dict[str, Any]] | None, symbols_count: int) -> str:
    balance = balance or {}
    positions = positions or []
    trade_status = "فعال ✅" if settings.get("trade_enabled") else "خاموش ⛔"
    return f"""🤖 پنل ربات اسکالپ کلاسیک ۵ دقیقه‌ای

وضعیت ترید واقعی: {trade_status}
منبع تحلیل: OKX
محل اجرا: Toobit
تایم‌فریم: ۵ دقیقه
نمادهای فعال/معتبر: {symbols_count}

تنظیمات ترید:
• دلار هر ترید: {format_num(settings.get('trade_amount_usdt'), 2)} USDT
• لوریج: {int(settings.get('leverage', 0))}x
• حداکثر پوزیشن: {int(settings.get('max_positions', 0))}
• نوع مارجین: {settings.get('margin_type', '-')}

وضعیت مارجین Toobit:
• موجودی: {format_num(balance.get('balance'), 4)} USDT
• مارجین آزاد: {format_num(balance.get('available'), 4)} USDT
• مارجین پوزیشن: {format_num(balance.get('position_margin'), 4)} USDT
• مارجین سفارش: {format_num(balance.get('order_margin'), 4)} USDT
• سود/ضرر شناور: {format_num(balance.get('unrealized_pnl'), 4)} USDT

پوزیشن‌های باز Toobit: {len(positions)}
سیگنال‌های کل: {int(stats.get('signals_total', 0))}
TP/SL عادی: {int(stats.get('normal_tp', 0))} / {int(stats.get('normal_sl', 0))}
TP/SL واقعی: {int(stats.get('real_tp', 0))} / {int(stats.get('real_sl', 0))}
"""


def help_message() -> str:
    return """📌 دستورات فارسی ربات

/پنل — نمایش پنل کامل
/آمار — نمایش آمار عادی و واقعی
/حذف_آمار — حذف آمار و سیگنال‌های باز
/موجودی — نمایش موجودی و مارجین Toobit
/پوزیشن — نمایش پوزیشن‌های باز Toobit

/دلار_ترید 10 — تنظیم مقدار هر ترید از ۱ تا ۱۰۰۰۰
/لوریج_ترید 10 — تنظیم لوریج از ۱ تا ۱۰۰
/حداکثر_پوزیشن 3 — تنظیم حداکثر پوزیشن از ۱ تا ۱۰۰

/ترید_فعال — روشن کردن اجرای واقعی روی Toobit
/ترید_خاموش — خاموش کردن اجرای واقعی؛ تحلیل و سیگنال ادامه دارد
"""


def positions_message(positions: list[dict[str, Any]]) -> str:
    if not positions:
        return "📌 پوزیشن بازی در Toobit پیدا نشد."
    lines = ["📌 پوزیشن‌های باز Toobit"]
    for p in positions:
        lines.append(
            f"""

نماد: {p.get('symbol', '-')}
جهت: {p.get('side', '-')}
ورود: {format_num(p.get('avgPrice'))}
قیمت مارک: {format_num(p.get('markPrice') or p.get('lastPrice'))}
حجم: {format_num(p.get('position'))}
مارجین: {format_num(p.get('margin'))}
لوریج: {p.get('leverage', '-')}x
سود/ضرر شناور: {format_num(p.get('unrealizedPnL'), 4)} USDT"""
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
