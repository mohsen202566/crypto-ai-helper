from __future__ import annotations

import json
import time
from typing import Any

import config
from monitoring_result_4h import MonitorResult
from storage import StoredSignal
from utils import fmt_money, fmt_price


def onoff(value: bool) -> str:
    return "روشن ✅" if value else "خاموش ⛔"


def render_trade_panel(settings: dict[str, Any], *, active_real: int, free_slots: int, margin_summary: dict[str, Any] | None = None) -> str:
    margin = "نامشخص"
    if margin_summary:
        margin = f"{float(margin_summary.get('available') or margin_summary.get('balance') or 0):.2f} USDT"
    return "\n".join([
        "⚙️ وضعیت ربات",
        "",
        f"💹 ترید واقعی: {onoff(bool(settings['real_trade_enabled']))}",
        f"💰 مارجین توبیت: {margin}",
        f"💲 دلار هر پوزیشن: {float(settings['trade_dollar_usdt']):.2f} USDT",
        f"📈 لوریج: {int(settings['leverage'])}x",
        f"🎯 حداکثر پوزیشن: {int(settings['max_positions'])}",
        f"📌 پوزیشن Real فعال: {active_real}",
        f"🟢 اسلات آزاد: {free_slots}",
        f"💵 سرمایه مجاز ربات: {float(settings['trade_capital_usdt']):.2f} USDT",
        f"🧾 کارمزد رفت‌وبرگشت ثابت: {config.ROUND_TRIP_FEE_USDT:.2f} USDT",
        "",
        "دستورات:",
        "ترید فعال | ترید خاموش",
        "ترید دلار 10 | ترید لوریج 10 | حداکثر پوزیشن 3",
        "آمار | پوزیشن | کوین‌ها | وضعیت",
    ])


def render_signal(signal_id: int, plan: Any, mode: str) -> str:
    d = plan.to_legacy_dict() if hasattr(plan, "to_legacy_dict") else dict(plan)
    title = "🏦 سیگنال توبیت" if mode == "real" else "📊 سیگنال"
    side = "LONG 🟢" if d["direction"] == "LONG" else "SHORT 🔴"
    rr = float(d.get("risk_reward") or 0)
    return "\n".join([
        title,
        f"#{signal_id} | {d['symbol']}",
        f"جهت: {side}",
        f"امتیاز: {float(d['score']):.1f}/100",
        f"قدرت: {d.get('strength', 'معمولی')}",
        f"RR: {rr:g}",
        "",
        f"Entry: {fmt_price(d['entry_price'])}",
        f"TP: {fmt_price(d['tp_price'])}",
        f"SL 1H: {fmt_price(d['sl_price'])}",
        "",
        f"سود خام تقریبی: {float(d.get('estimated_profit_usdt') or 0):.2f} USDT",
        f"سود خالص تقریبی: {float(d.get('estimated_net_profit_usdt') or 0):.2f} USDT",
        f"کارمزد رفت‌وبرگشت: {float(d.get('round_trip_fee_usdt') or config.ROUND_TRIP_FEE_USDT):.2f} USDT",
    ])


def render_result(signal: StoredSignal, result: MonitorResult) -> str:
    title = "✅ TP خورد" if result.status == "TP" else "❌ SL خورد" if result.status == "SL" else "ℹ️ خروج/بسته‌شدن"
    source = "🏦 نتیجه توبیت" if signal.signal_type == "real" else "📊 نتیجه سیگنال"
    return "\n".join([
        source,
        title,
        f"#{signal.id} | {signal.symbol} | {signal.direction}",
        f"Entry: {fmt_price(signal.entry_price)}",
        f"Exit: {fmt_price(result.exit_price)}",
        f"PnL خام: {result.approx_pnl:.2f} USDT",
        f"PnL خالص/واقعی: {result.net_pnl:.2f} USDT",
        f"حرکت: {result.move_pct * 100:.2f}%",
        f"دلیل: {result.reason}",
    ])


def render_stats(stats: dict[str, Any], days: int) -> str:
    normal = stats["normal"]
    real = stats["real"]
    long = stats["long"]
    short = stats["short"]
    failed = stats["real_failed"]
    return "\n".join([
        f"📊 آمار {days} روز اخیر",
        "",
        f"🟢 لانگ: سیگنال {long['total']} | TP {long['tp']} | SL {long['sl']} | وین‌ریت {long['win_rate']:.1f}%",
        f"🔴 شورت: سیگنال {short['total']} | TP {short['tp']} | SL {short['sl']} | وین‌ریت {short['win_rate']:.1f}%",
        "",
        "📌 عادی:",
        f"تعداد: {normal['total']} | TP: {normal['tp']} | SL: {normal['sl']} | باز: {normal['open']}",
        f"وین‌ریت: {normal['win_rate']:.1f}% | PnL خالص تقریبی: {normal['pnl']:.2f} USDT",
        "",
        "💰 واقعی:",
        f"تعداد: {real['total']} | TP: {real['tp']} | SL: {real['sl']} | EXIT: {real['exit']} | باز: {real['open']}",
        f"وین‌ریت: {real['win_rate']:.1f}% | PnL واقعی/خالص: {real['pnl']:.2f} USDT",
        f"ارسال واقعی ناموفق: {failed['total']}",
    ])
