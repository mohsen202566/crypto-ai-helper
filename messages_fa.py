"""همه متن‌های فارسی ربات."""
from __future__ import annotations

from typing import Any

import config
from utils import format_num


def side_label(side: str) -> str:
    return "لانگ / خرید" if str(side).upper() == "BUY" else "شورت / فروش"


def signal_message(signal: dict[str, Any]) -> str:
    icon = "🚀" if signal["side"] == "BUY" else "🔻"
    side_fa = side_label(signal["side"])
    mode_fa = signal.get("execution_mode_fa") or ("رئال Toobit" if signal.get("execution_mode") == "REAL" else "عادی / داخلی")
    reasons = "\n".join(f"✅ {r}" for r in signal.get("reasons", [])) or "✅ شرایط اصلی برقرار است"
    warnings = "\n".join(f"⚠️ {w}" for w in signal.get("warnings", []))
    ind = signal.get("indicators", {})
    score_text = signal.get("score_label") or f"{signal.get('score', 0)} از 100"
    text = f"""{icon} سیگنال {side_fa} — {signal['symbol']}

نوع اجرا: {mode_fa}
وضعیت ترید: {signal.get('execution_reason', '-')}

ورود: {format_num(signal['entry'])}
حد سود: {format_num(signal['tp'])}  (+{config.FIXED_TP_PERCENT:.2f}%)
حد ضرر: {format_num(signal['sl'])}  (-{config.FIXED_SL_PERCENT:.2f}%)

دلار ترید: {format_num(signal.get('trade_amount_usdt'), 2)} USDT
لوریج: {signal.get('leverage', '-')}x
جهت بازار: {signal.get('market_state', '-')}
مدت هدف سیگنال: حداکثر {signal.get('max_hold_minutes', 180)} دقیقه

منطق ورود: {score_text}
نوع سیگنال: {signal['signal_type']}
منبع تحلیل: OKX  ({signal['okx_symbol']})
محل اجرای احتمالی: Toobit  ({signal['toobit_symbol']})

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


def _movement_percent(signal: dict[str, Any], price: float) -> float:
    entry = float(signal.get("entry") or 0)
    if entry <= 0:
        return 0.0
    if str(signal.get("side", "")).upper() == "BUY":
        return (float(price) - entry) / entry * 100.0
    return (entry - float(price)) / entry * 100.0


def _signed(value: float, digits: int = 2) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{format_num(value, digits)}"


def _result_message(signal: dict[str, Any], result: str, price: float, pnl: float, mode_fa: str, source_fa: str) -> str:
    side_fa = side_label(signal["side"])
    move = _movement_percent(signal, price)
    if result == "TP":
        return f"""✅ TP خورد — {signal['symbol']}

نوع: {mode_fa}
جهت: {side_fa}
ورود: {format_num(signal['entry'])}
خروج: {format_num(price)}
حرکت: {_signed(move, 2)}%
سود: {_signed(pnl, 4)} USDT

دلیل:
قیمت به حد سود ثابت رسید.
منبع نتیجه: {source_fa}"""

    return f"""❌ استاپ خورد — {signal['symbol']}

نوع: {mode_fa}
جهت: {side_fa}
ورود: {format_num(signal['entry'])}
خروج: {format_num(price)}
حرکت: {_signed(move, 2)}%
ضرر: {_signed(pnl, 4)} USDT

دلیل استاپ:
قیمت به حد ضرر ثابت رسید.
مومنتوم برگشت و حرکت ادامه نداد.
منبع نتیجه: {source_fa}"""


def normal_result_message(signal: dict[str, Any], result: str, price: float, pnl: float = 0.0) -> str:
    return _result_message(signal, result, price, pnl, "عادی / داخلی", "OKX")


def real_result_message(signal: dict[str, Any], result: str, price: float, pnl: float = 0.0) -> str:
    return _result_message(signal, result, price, pnl, "رئال Toobit", "Toobit")


def stats_message(stats: dict[str, Any]) -> str:
    return f"""📊 آمار ربات ورود ۵ دقیقه‌ای با تایید چندتایم‌فریمی

تعداد کل سیگنال‌ها: {int(stats.get('signals_total', 0))}
سیگنال‌های عادی / داخلی: {int(stats.get('normal_signals_total', 0))}
سیگنال‌های رئال Toobit: {int(stats.get('real_signals_total', 0))}

آمار عادی / داخلی:
✅ TP عادی: {int(stats.get('normal_tp', 0))}
❌ SL عادی: {int(stats.get('normal_sl', 0))}
⏳ باز/نامشخص عادی: {int(stats.get('normal_open', 0))}
💰 سود/ضرر عادی: {format_num(stats.get('normal_pnl', 0), 4)} USDT
درصد موفقیت عادی: {format_num(stats.get('normal_winrate', 0), 2)}%

آمار واقعی Toobit:
✅ TP واقعی: {int(stats.get('real_tp', 0))}
❌ SL واقعی: {int(stats.get('real_sl', 0))}
⏳ پوزیشن/معامله باز واقعی: {int(stats.get('real_open', 0))}
⚠️ اجرای ناموفق: {int(stats.get('real_failed', 0))}
💰 سود/ضرر واقعی: {format_num(stats.get('real_pnl', 0), 4)} USDT
درصد موفقیت واقعی: {format_num(stats.get('real_winrate', 0), 2)}%

📌 سود/ضرر کل ثبت‌شده ربات: {format_num(stats.get('total_pnl', 0), 4)} USDT
آخرین حذف آمار: {stats.get('last_reset_utc', '-')}
"""


def panel_message(
    settings: dict[str, Any],
    stats: dict[str, Any],
    balance: dict[str, Any] | None,
    positions: list[dict[str, Any]] | None,
    symbols_count: int,
    toobit_ok: bool = False,
    toobit_error: str | None = None,
    today_pnl: float | None = None,
) -> str:
    balance = balance or {}
    positions = positions or []
    trade_status = "فعال ✅" if settings.get("trade_enabled") else "خاموش ⛔"
    conn = "وصل ✅" if toobit_ok else "قطع/خطا ❌"
    today_txt = f"{format_num(today_pnl, 4)} USDT" if today_pnl is not None else "نامشخص"
    text = f"""🤖 پنل ترید ربات ورود ۵ دقیقه‌ای با تایید چندتایم‌فریمی

وضعیت ترید واقعی: {trade_status}
وضعیت اتصال Toobit: {conn}
منبع تحلیل: OKX
محل اجرا: Toobit
تایم‌فریم ورود: ۵ دقیقه
تایید روند: 1D + 4H + 1H
نمادهای فعال/معتبر: {symbols_count}

تنظیمات ترید:
• دلار هر ترید: {format_num(settings.get('trade_amount_usdt'), 2)} USDT
• لوریج: {int(settings.get('leverage', 0))}x
• حداکثر پوزیشن رئال: {int(settings.get('max_positions', 0))}
• نوع مارجین: {settings.get('margin_type', '-')}

وضعیت مارجین Toobit:
• موجودی: {format_num(balance.get('balance'), 4)} USDT
• مارجین آزاد: {format_num(balance.get('available'), 4)} USDT
• مارجین پوزیشن: {format_num(balance.get('position_margin'), 4)} USDT
• مارجین سفارش: {format_num(balance.get('order_margin'), 4)} USDT
• سود/ضرر شناور: {format_num(balance.get('unrealized_pnl'), 4)} USDT
• سود/ضرر امروز Toobit: {today_txt}

پوزیشن‌های باز Toobit: {len(positions)}

آمار خلاصه:
• سیگنال عادی: {int(stats.get('normal_signals_total', 0))}
• سیگنال رئال: {int(stats.get('real_signals_total', 0))}
• TP/SL عادی: {int(stats.get('normal_tp', 0))} / {int(stats.get('normal_sl', 0))}
• TP/SL واقعی: {int(stats.get('real_tp', 0))} / {int(stats.get('real_sl', 0))}
• سود/ضرر کل: {format_num(stats.get('total_pnl', 0), 4)} USDT
"""
    if toobit_error:
        text += f"\n⚠️ خطای Toobit:\n{toobit_error}\n"
    return text


def help_message() -> str:
    return """📌 دستورات فارسی ربات — بدون اسلش همگی کار می‌کنند

پنل ترید — نمایش پنل کامل ترید و مارجین
آمار — نمایش آمار عادی و رئال
حذف آمار — حذف آمار و سیگنال‌های باز
چک توبیت — بررسی اتصال Toobit
موجودی یا مارجین — نمایش موجودی و مارجین Toobit
پوزیشن — نمایش پوزیشن‌های باز Toobit
سود امروز — نمایش سود/ضرر امروز و آمار سود/ضرر
بازار — نمایش جهت بازار و دلیل سکوت
بازه SOL — نمایش بازه امروز لانگ و شورت همان ارز
ارزها — نمایش ارزهای فعال معتبر OKX/Toobit
مانیتور — نمایش سیگنال‌های باز و وضعیت مانیتور

دلار ترید 10 — تنظیم مقدار هر ترید از ۱ تا ۱۰۰۰۰
لوریج ترید 10 — تنظیم لوریج از ۱ تا ۱۰۰
حداکثر پوزیشن 3 — تنظیم حداکثر پوزیشن رئال از ۱ تا ۱۰۰

ترید فعال یا توبیت روشن — روشن کردن اجرای واقعی روی Toobit
ترید خاموش یا توبیت خاموش — خاموش کردن اجرای واقعی؛ سیگنال عادی ادامه دارد
"""


def positions_message(positions: list[dict[str, Any]]) -> str:
    if not positions:
        return "📌 پوزیشن بازی در Toobit پیدا نشد."
    lines = ["📌 پوزیشن‌های باز Toobit"]
    for p in positions:
        lines.append(
            f"""

نماد: {p.get('symbol', '-')}
جهت: {p.get('side') or p.get('positionSide') or '-'}
ورود: {format_num(p.get('avgPrice') or p.get('entryPrice'))}
قیمت مارک: {format_num(p.get('markPrice') or p.get('lastPrice'))}
حجم: {format_num(p.get('position') or p.get('positionAmt') or p.get('size'))}
مارجین: {format_num(p.get('margin') or p.get('positionMargin'))}
لوریج: {p.get('leverage', '-')}x
سود/ضرر شناور: {format_num(p.get('unrealizedPnL') or p.get('unrealizedPnl') or p.get('unrealizedProfit'), 4)} USDT"""
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
