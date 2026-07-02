"""قالب پیام‌های فارسی ربات."""
from __future__ import annotations

import config
from models import BotSettings, Signal, TradeStats
from utils import format_float, net_profit_estimate


def fmt_usdt(value: float, digits: int = 4) -> str:
    return f"{format_float(value, digits)} USDT"


def format_trade_panel(settings: BotSettings, stats: TradeStats, balance: dict[str, float] | None, open_real: int, open_normal: int, active_symbols: list[str]) -> str:
    balance_text = "نامشخص"
    if balance is not None:
        balance_text = f"آزاد: {fmt_usdt(balance.get('free', 0), 4)} | کل: {fmt_usdt(balance.get('total', 0), 4)}"
    free_slots = max(0, settings.max_real_positions - open_real)
    return f"""📌 پنل Spot Hunter

وضعیت ترید واقعی: {'روشن ✅' if settings.trading_enabled else 'خاموش ⛔'}
بازار: Spot فقط LONG
موجودی Toobit Spot USDT: {balance_text}

پول هر معامله: {fmt_usdt(settings.trade_amount_usdt, 2)}
درصد حرکت هدف: {format_float(settings.target_percent, 2)}٪
حداکثر پوزیشن واقعی: {settings.max_real_positions}
اسلات واقعی: باز {open_real} | خالی {free_slots}
سیگنال عادی باز: {open_normal}
تعداد ارز فعال: {settings.active_symbol_count}
چک هیستوری واقعی: هر {settings.history_check_minutes} دقیقه

کارمزد Spot:
Maker: {format_float(settings.maker_fee_pct, 4)}٪ | Taker: {format_float(settings.taker_fee_pct, 4)}٪

آمار از شروع ربات:
کل سیگنال‌ها: {stats.total_signals}
بسته‌شده: {stats.closed_total}
سود خام: {fmt_usdt(stats.gross_profit_usdt, 4)}
کارمزد کل: {fmt_usdt(stats.total_fee_usdt, 4)}
سود خالص: {fmt_usdt(stats.net_profit_usdt, 4)}
وین‌ریت تعدادی: {format_float(stats.win_rate_count_pct, 2)}٪
میانگین سود خالص هر معامله: {fmt_usdt(stats.avg_net_per_trade, 4)}

ارزهای فعال:
{', '.join(active_symbols)}"""


def format_signal(signal: Signal, settings: BotSettings | None = None) -> str:
    buy_fee = settings.taker_fee_pct if settings else config.DEFAULT_TAKER_FEE_PCT
    sell_fee = settings.maker_fee_pct if settings else config.DEFAULT_MAKER_FEE_PCT
    estimate = net_profit_estimate(
        signal.amount_usdt,
        signal.target_percent,
        buy_fee,
        sell_fee,
    )
    mode = "پوزیشن واقعی Toobit" if signal.execution_mode == config.MODE_REAL else "سیگنال عادی OKX"
    conf = "\n".join([f"{k}: {v}" for k, v in signal.confirmations.items()])
    return f"""🟢 سیگنال خرید اسپات

نوع: {mode}
ارز: {signal.base_symbol}/USDT
جهت: فقط LONG
امتیاز: {signal.score}/100

ورود: {format_float(signal.entry_price, 8)}
هدف: {format_float(signal.target_price, 8)}
درصد حرکت: +{format_float(signal.target_percent, 2)}٪
پول معامله: {fmt_usdt(signal.amount_usdt, 2)}

سود خام احتمالی: {fmt_usdt(estimate['gross_profit_usdt'], 4)}
کارمزد تقریبی: {fmt_usdt(estimate['fee_usdt'], 4)}
سود خالص تقریبی: {fmt_usdt(estimate['net_profit_usdt'], 4)}

تاییدها:
{conf}

دلیل سیگنال:
{signal.reason}"""


def format_buy_confirm(signal: Signal) -> str:
    return f"""✅ تایید خرید واقعی Toobit

ارز: {signal.base_symbol}/USDT
میانگین خرید: {format_float(signal.avg_buy_price or 0, 8)}
مقدار خرید: {format_float(signal.filled_qty or 0, 8)}
کارمزد خرید: {fmt_usdt(signal.buy_fee_usdt, 6)}

سفارش فروش هدف ثبت شد:
قیمت فروش: {format_float(signal.target_price, 8)}
درصد حرکت هدف: +{format_float(signal.target_percent, 2)}٪
Order ID فروش: {signal.sell_order_id or 'نامشخص'}"""


def format_real_open_failed_to_normal(signal: Signal, reason: str) -> str:
    return f"""⚠️ اجرای واقعی انجام نشد

ارز: {signal.base_symbol}/USDT
علت: {reason}

این سیگنال به حالت عادی تبدیل شد و از این لحظه فقط با OKX دنبال می‌شود."""


def format_normal_result(signal: Signal) -> str:
    return f"""✅ نتیجه سیگنال عادی OKX

ارز: {signal.base_symbol}/USDT
ورود فرضی: {format_float(signal.entry_price, 8)}
خروج فرضی: {format_float(signal.close_price or 0, 8)}
درصد حرکت: +{format_float(signal.move_percent, 2)}٪

پول فرضی: {fmt_usdt(signal.amount_usdt, 2)}
سود خام: {fmt_usdt(signal.gross_profit_usdt, 4)}
کارمزد تقریبی: {fmt_usdt(signal.fee_usdt, 4)}
سود خالص تقریبی: {fmt_usdt(signal.net_profit_usdt, 4)}

دلیل نتیجه:
{signal.close_reason}"""


def format_real_result(signal: Signal) -> str:
    return f"""✅ نتیجه پوزیشن واقعی Toobit Spot

ارز: {signal.base_symbol}/USDT
ورود واقعی: {format_float(signal.avg_buy_price or signal.entry_price, 8)}
خروج واقعی: {format_float(signal.close_price or 0, 8)}
درصد حرکت: +{format_float(signal.move_percent, 2)}٪
مقدار: {format_float(signal.filled_qty or 0, 8)}

سود خام: {fmt_usdt(signal.gross_profit_usdt, 4)}
کارمزد کل: {fmt_usdt(signal.fee_usdt, 4)}
سود خالص: {fmt_usdt(signal.net_profit_usdt, 4)}

دلیل خروج:
{signal.close_reason}"""


def format_stats(stats: TradeStats) -> str:
    return f"""📊 آمار Spot Hunter

کل سیگنال‌ها: {stats.total_signals}
سیگنال عادی: {stats.normal_signals}
پوزیشن واقعی: {stats.real_signals}

بسته‌شده کل: {stats.closed_total}
بسته‌شده عادی: {stats.closed_normal}
بسته‌شده واقعی: {stats.closed_real}

بردها: {stats.wins_count}
ضررها/نتیجه منفی: {stats.losses_count}
وین‌ریت تعدادی: {format_float(stats.win_rate_count_pct, 2)}٪

سود خام کل: {fmt_usdt(stats.gross_profit_usdt, 4)}
کارمزد کل: {fmt_usdt(stats.total_fee_usdt, 4)}
سود خالص کل: {fmt_usdt(stats.net_profit_usdt, 4)}
میانگین سود خالص هر معامله: {fmt_usdt(stats.avg_net_per_trade, 4)}"""


def format_balance(balance: dict[str, float]) -> str:
    return f"""💰 موجودی Toobit Spot USDT

آزاد: {fmt_usdt(balance.get('free', 0), 4)}
درگیر سفارش‌ها: {fmt_usdt(balance.get('locked', 0), 4)}
کل: {fmt_usdt(balance.get('total', 0), 4)}"""


def format_status(settings: BotSettings, okx_ok: bool, toobit_ok: bool, open_real: int, open_normal: int) -> str:
    return f"""🧭 وضعیت ربات

ربات: روشن ✅
OKX: {'وصل ✅' if okx_ok else 'مشکل ❌'}
Toobit: {'وصل ✅' if toobit_ok else 'مشکل/کلید تنظیم نیست ❌'}
ترید واقعی: {'روشن ✅' if settings.trading_enabled else 'خاموش ⛔'}

پوزیشن واقعی باز: {open_real}
سیگنال عادی باز: {open_normal}
چک هیستوری: هر {settings.history_check_minutes} دقیقه"""


def format_positions(title: str, signals: list[Signal]) -> str:
    if not signals:
        return f"{title}\n\nموردی وجود ندارد."
    lines = [title, ""]
    for s in signals[:50]:
        mode = "واقعی" if s.execution_mode == config.MODE_REAL else "عادی"
        lines.append(
            f"{s.base_symbol}/USDT | {mode} | {s.status} | ورود {format_float(s.entry_price, 8)} | هدف {format_float(s.target_price, 8)}"
        )
    return "\n".join(lines)


def format_active_symbols(symbols: list[str], busy: set[str]) -> str:
    lines = ["🪙 ارزهای فعال", ""]
    for sym in symbols:
        state = "درگیر/سیگنال باز" if sym in busy else "آزاد"
        lines.append(f"{sym}/USDT | {state}")
    return "\n".join(lines)


def format_symbol_check(rows: list[dict]) -> str:
    lines = ["🧪 نتیجه چک نمادها", ""]
    ok_all = True
    for row in rows:
        okx_ok = bool(row.get("okx_ok"))
        toobit_ok = bool(row.get("toobit_ok"))
        ok_all = ok_all and okx_ok and toobit_ok
        lines.append(
            f"{row.get('base')}/USDT | OKX: {'✅ ' + str(row.get('okx_symbol')) if okx_ok else '❌ ' + str(row.get('okx_error'))} | "
            f"Toobit: {'✅ ' + str(row.get('toobit_symbol')) if toobit_ok else '❌ ' + str(row.get('toobit_error'))}"
        )
    lines.append("")
    lines.append("نتیجه کلی: همه نمادها قابل استفاده‌اند ✅" if ok_all else "نتیجه کلی: بعضی نمادها مشکل دارند؛ قبل از ترید واقعی اصلاح شوند ❌")
    return "\n".join(lines)


def format_command_error(text: str) -> str:
    return f"❌ {text}"


def format_setting_ok(text: str) -> str:
    return f"✅ {text}"
