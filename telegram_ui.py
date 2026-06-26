"""
Telegram UI renderer for Crypto AI Helper bot.

Locked responsibility:
- Formats panels and messages only.
- No analysis, no Toobit API, no OKX API, no order execution, no learning.

Design lock:
- Small, simple, strong.
- One responsibility only.
- Shows trade panel, stats panel, signal messages, and instant TP/SL results.
- TP results must be visually green/confirmed; SL results must be visually red/failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from config import (
    LONG_LABEL,
    SHORT_LABEL,
    TITLE_NORMAL_RESULT,
    TITLE_NORMAL_SIGNAL,
    TITLE_STATS_PANEL,
    TITLE_TOOBIT_RESULT,
    TITLE_TOOBIT_SIGNAL,
    TITLE_TRADE_PANEL,
)

Direction = Literal["LONG", "SHORT"]
SignalMode = Literal["TOOBIT", "SIGNAL"]
ResultKind = Literal["TP", "SL"]


@dataclass(frozen=True)
class TradePanelData:
    auto_signal_enabled: bool
    real_trade_enabled: bool
    toobit_margin_usdt: float | None
    trade_capital_usdt: float
    trade_dollar_usdt: float
    leverage: int
    min_net_profit_usdt: float
    max_slots: int
    open_positions: int
    free_slots: int


@dataclass(frozen=True)
class StatsPanelData:
    real_signals: int
    real_monitoring: int
    real_tp: int
    real_sl: int
    real_win_rate: float
    real_pnl_usdt: float
    signal_only_total: int
    signal_only_tp: int
    signal_only_sl: int
    signal_only_win_rate: float


@dataclass(frozen=True)
class SignalMessageData:
    mode: SignalMode
    fa_name: str
    symbol: str
    direction: Direction
    confidence_pct: float
    estimated_profit_usdt: float
    estimated_move_pct: float
    entry: float
    tp: float
    sl: float


@dataclass(frozen=True)
class ResultMessageData:
    mode: SignalMode
    fa_name: str
    symbol: str
    direction: Direction
    result: ResultKind
    entry: float
    exit_price: float
    pnl_usdt: float
    move_pct: float
    duration_minutes: int


def render_trade_panel(data: TradePanelData) -> str:
    margin = "نامشخص" if data.toobit_margin_usdt is None else f"{data.toobit_margin_usdt:.2f} USDT"
    return "\n".join(
        [
            TITLE_TRADE_PANEL,
            "",
            f"🤖 اتو سیگنال: {_on_off(data.auto_signal_enabled)}",
            f"💹 ترید: {_on_off(data.real_trade_enabled)}",
            "",
            f"💰 مارجین توبیت: {margin}",
            f"💵 سرمایه مجاز ربات: {data.trade_capital_usdt:.2f} USDT",
            f"💲 دلار هر پوزیشن: {data.trade_dollar_usdt:.2f} USDT",
            f"📈 لوریج: {data.leverage}x",
            "🛡️ حالت مارجین: Isolated",
            f"💸 حداقل سود خالص: {data.min_net_profit_usdt:.2f} USDT",
            "",
            f"📦 حداکثر اسلات: {data.max_slots}",
            f"📂 پوزیشن‌های باز: {data.open_positions}",
            f"📭 اسلات خالی: {data.free_slots}",
        ]
    )


def render_stats_panel(data: StatsPanelData) -> str:
    return "\n".join(
        [
            TITLE_STATS_PANEL,
            "",
            "🏦 معاملات توبیت",
            f"📨 سیگنال‌های صادر شده: {data.real_signals}",
            f"⏳ در حال مانیتورینگ: {data.real_monitoring}",
            f"✅ TP: {data.real_tp}",
            f"❌ SL: {data.real_sl}",
            f"🏆 Win Rate: {data.real_win_rate:.2f}%",
            f"💰 سود/ضرر تا امروز: {data.real_pnl_usdt:+.2f} USDT",
            "",
            "📊 سیگنال‌های غیرواقعی",
            f"📨 سیگنال‌ها: {data.signal_only_total}",
            f"✅ TP: {data.signal_only_tp}",
            f"❌ SL: {data.signal_only_sl}",
            f"🏆 Win Rate: {data.signal_only_win_rate:.2f}%",
        ]
    )


def render_signal(data: SignalMessageData) -> str:
    title = TITLE_TOOBIT_SIGNAL if data.mode == "TOOBIT" else TITLE_NORMAL_SIGNAL
    move_icon = "📈" if data.direction == "LONG" else "📉"
    return "\n".join(
        [
            title,
            "",
            f"🪙 {data.fa_name} | {data.symbol}",
            _direction_label(data.direction),
            "",
            f"📊 اعتبار: {data.confidence_pct:.1f}%",
            f"💰 سود تخمینی: {data.estimated_profit_usdt:+.2f} USDT",
            f"{move_icon} حرکت تخمینی: {data.estimated_move_pct:+.2f}%",
            "",
            f"💵 ورود: {_price(data.entry)}",
            f"🎯 TP1: {_price(data.tp)}",
            f"🛑 SL: {_price(data.sl)}",
        ]
    )


def render_result(data: ResultMessageData) -> str:
    title = TITLE_TOOBIT_RESULT if data.mode == "TOOBIT" else TITLE_NORMAL_RESULT
    result_label = _result_label(data.result)
    pnl_icon = "🟢" if data.pnl_usdt >= 0 else "🔴"
    return "\n".join(
        [
            title,
            "",
            f"🪙 {data.fa_name} | {data.symbol}",
            _direction_label(data.direction),
            "",
            result_label,
            "",
            f"💵 ورود: {_price(data.entry)}",
            f"🚪 خروج: {_price(data.exit_price)}",
            "",
            f"💰 سود/ضرر: {data.pnl_usdt:+.2f} USDT {pnl_icon}",
            f"📊 حرکت: {data.move_pct:+.2f}%",
            f"⏱️ زمان: {data.duration_minutes} دقیقه",
        ]
    )


def render_invalid_value(command_name: str, min_value: float | int, max_value: float | int) -> str:
    return f"❌ مقدار نامعتبر\nحد مجاز: {min_value} تا {max_value}\nدستور: {command_name}"


def _direction_label(direction: Direction) -> str:
    return LONG_LABEL if direction == "LONG" else SHORT_LABEL


def _result_label(result: ResultKind) -> str:
    return "✅ TP1 خورد" if result == "TP" else "❌ SL خورد"


def _price(value: float) -> str:
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _on_off(value: bool) -> str:
    return "فعال" if value else "غیرفعال"


__all__ = [
    "TradePanelData",
    "StatsPanelData",
    "SignalMessageData",
    "ResultMessageData",
    "render_trade_panel",
    "render_stats_panel",
    "render_signal",
    "render_result",
    "render_invalid_value",
]
