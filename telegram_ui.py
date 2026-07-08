from __future__ import annotations

import json
import time
from typing import Any

import config
from storage import StoredSignal


def _yn(v: bool) -> str:
    return "روشن ✅" if v else "خاموش ⛔"


def _fmt(x: float, n: int = 4) -> str:
    try:
        return f"{float(x):.{n}f}"
    except Exception:
        return str(x)


def render_trade_panel(settings: dict[str, Any], stats: dict[str, Any], runtime: dict[str, str] | None = None, balance: dict[str, float] | None = None) -> str:
    runtime = runtime or {}
    balance = balance or {}
    last_scan = runtime.get("last_scan_summary", "")
    scan_line = "نامشخص"
    if last_scan:
        try:
            s = json.loads(last_scan)
            scan_line = f"اسکن‌شده {s.get('scanned',0)}/{s.get('total',0)} | سیگنال {s.get('signals',0)} | رد {s.get('rejected',0)} | خطا {s.get('errors',0)}"
        except Exception:
            scan_line = last_scan[:120]
    bal_line = "نامشخص"
    if balance:
        bal_line = f"USDT کل: {_fmt(balance.get('total',0),2)} | آزاد: {_fmt(balance.get('available',0),2)}"
    return (
        "📊 <b>پنل ترید ICE-5M</b>\n\n"
        f"اتو سیگنال: <b>{_yn(bool(settings['auto_signal_enabled']))}</b>\n"
        f"ترید واقعی Toobit: <b>{_yn(bool(settings['real_trade_enabled']))}</b>\n"
        f"دیتا/مانیتور: <b>OKX</b>\n"
        f"اجرای واقعی: <b>Toobit</b>\n\n"
        f"موجودی فیوچرز: {bal_line}\n"
        f"مبلغ هر معامله: <b>{_fmt(settings['trade_dollar_usdt'],2)} USDT</b>\n"
        f"لوریج: <b>{settings['leverage']}x</b>\n"
        f"حداکثر پوزیشن همزمان: <b>{settings['max_positions']}</b>\n"
        f"اسلات واقعی باز: <b>{stats.get('real_open',0)}/{settings['max_positions']}</b>\n"
        f"حداقل سود خالص: <b>{_fmt(settings['min_net_profit_usdt'],3)} USDT</b>\n"
        f"RR: <b>{config.ICE_RR:.2f}</b> | فقط یک TP | RR زیر 1 ممنوع\n\n"
        f"آخرین اسکن: {scan_line}\n\n"
        "⌨️ <b>دستورات</b>\n"
        "ترید\n"
        "ترید روشن / ترید فعال\n"
        "ترید خاموش\n"
        "اتو سیگنال روشن\n"
        "اتو سیگنال خاموش\n"
        "ترید دلار 10\n"
        "ترید لوریج 8\n"
        "حداکثر پوزیشن 3\n"
        "حداقل سود 0.02\n"
        "آمار\n"
        "هوش\n"
        "حذف آمار تایید\n"
    )


def render_signal(signal_id: int, plan, signal_type: str) -> str:
    t = "REAL / واقعی ✅" if signal_type == "real" else "NORMAL / سیگنال معمولی 🟡"
    d = "لانگ 🟢" if plan.direction == "LONG" else "شورت 🔴"
    reasons = "\n".join(f"• {r}" for r in list(plan.reasons)[:8])
    return (
        f"🚀 <b>سیگنال ICE-5M #{signal_id}</b>\n"
        f"نوع: <b>{t}</b>\n"
        f"ارز: <b>{plan.symbol}</b>\n"
        f"جهت: <b>{d}</b>\n"
        f"امتیاز: <b>{plan.score:.1f}</b> | قدرت: {plan.strength}\n\n"
        f"Entry: <code>{_fmt(plan.entry_price,6)}</code>\n"
        f"TP: <code>{_fmt(plan.tp_price,6)}</code>\n"
        f"SL: <code>{_fmt(plan.sl_price,6)}</code>\n"
        f"RR: <b>{plan.risk_reward:.2f}</b> | یک TP\n"
        f"سود خالص تخمینی: <b>{plan.estimated_net_profit_usdt:.4f} USDT</b>\n\n"
        "🧠 دلایل:\n" + reasons
    )


def render_result(signal: StoredSignal, result) -> str:
    emoji = "✅" if result.result == "TP" else "❌" if result.result == "SL" else "⚠️"
    return (
        f"{emoji} <b>نتیجه سیگنال #{signal.id}</b>\n"
        f"ارز: <b>{signal.symbol}</b> | {signal.direction}\n"
        f"نتیجه: <b>{result.result}</b>\n"
        f"قیمت نتیجه: <code>{_fmt(result.price,6)}</code>\n"
        f"PnL تخمینی: <b>{result.pnl_usdt:.4f} USDT</b>\n"
        f"زمان باز بودن: {int(result.age_seconds)} ثانیه\n"
        f"دلیل: {result.reason}"
    )


def render_stats(stats: dict[str, Any]) -> str:
    rejects = stats.get("last_rejects") or []
    reject_lines = "\n".join(f"• {r.get('symbol')}: {r.get('reason')}" for r in rejects[:6]) or "ندارد"
    return (
        "📈 <b>آمار ربات ICE-5M</b>\n\n"
        f"پوزیشن/سیگنال باز: <b>{stats.get('open',0)}</b>\n"
        f"باز واقعی: <b>{stats.get('real_open',0)}</b> | معمولی: <b>{stats.get('normal_open',0)}</b>\n"
        f"بسته‌شده: <b>{stats.get('closed',0)}</b>\n"
        f"TP: <b>{stats.get('wins',0)}</b>\n"
        f"SL: <b>{stats.get('losses',0)}</b>\n"
        f"Soft Exit: <b>{stats.get('soft',0)}</b>\n"
        f"Winrate: <b>{stats.get('winrate',0):.1f}%</b>\n"
        f"PnL تخمینی: <b>{stats.get('pnl',0):.4f} USDT</b>\n\n"
        "آخرین ردها:\n" + reject_lines
    )


def render_brain(settings: dict[str, Any], runtime: dict[str, str]) -> str:
    return (
        "🧠 <b>هوش / وضعیت تحلیل ICE</b>\n\n"
        "منطق فعال: Imbalance + Compression + Explosion\n"
        "ورود: اولین انفجار 1M بعد از فشردگی 5M، بدون پولبک اجباری\n"
        "فیلترها: اسپرد، عمق، دلتا، CVD، حجم، ضد دیرورود، سود خالص\n"
        "خروج: فقط یک TP و یک SL + Soft Exit مانیتوری\n\n"
        f"آخرین دلیل بلاک واقعی: {runtime.get('last_real_block_reason','ندارد')}\n"
        f"آخرین خطای واقعی: {runtime.get('last_real_failed','ندارد')}\n"
        f"آخرین بلاک سیگنال: {runtime.get('last_signal_block_reason','ندارد')}"
    )
