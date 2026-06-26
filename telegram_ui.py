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
from typing import Any, Literal

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
    pending_real_slots: int = 0
    active_real_slots: int = 0
    cancelled_signals: int = 0


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
    close_reason: str = ""


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
            f"⏳ در انتظار تایید: {data.pending_real_slots}",
            f"🟢 REAL فعال: {data.active_real_slots}",
            f"📭 اسلات خالی: {data.free_slots}",
            f"🚫 لغوشده‌ها: {data.cancelled_signals}",
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
            f"🎯 TP: {_price(data.tp)}",
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
            *_optional_reason(data.close_reason),
        ]
    )


def render_signal_status(status: str, close_reason: str = "") -> str:
    normalized = str(status or "").upper().strip()
    if normalized == "PENDING_OPEN":
        return "⏳ در انتظار تایید پوزیشن واقعی"
    if normalized == "MONITORING":
        return "🟢 در حال مانیتورینگ"
    if normalized == "CLOSED":
        return "✅ بسته شده"
    if normalized == "CANCELLED":
        reason = str(close_reason or "").strip()
        return "🚫 لغو شد" + (f" — {reason}" if reason else "")
    return "ℹ️ وضعیت نامشخص"


def _optional_reason(reason: str) -> list[str]:
    text = str(reason or "").strip()
    if not text:
        return []
    return [f"📝 دلیل: {text}"]


def render_invalid_value(command_name: str, min_value: float | int, max_value: float | int) -> str:
    return f"❌ مقدار نامعتبر\nحد مجاز: {min_value} تا {max_value}\nدستور: {command_name}"


def _direction_label(direction: Direction) -> str:
    return LONG_LABEL if direction == "LONG" else SHORT_LABEL


def _result_label(result: ResultKind) -> str:
    return "✅ TP خورد" if result == "TP" else "❌ SL خورد"


def _price(value: float) -> str:
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _on_off(value: bool) -> str:
    return "فعال" if value else "غیرفعال"


def build_signal_payload(decision: Any, mode: SignalMode | str = "SIGNAL", result: Any = None) -> str:
    """Build a clean Telegram signal message from StrategyDecision/SignalRecord objects.

    bot.py calls this adapter with raw runtime objects. Keep conversion here so
    bot.py does not leak dataclass repr/JSON fallback into Telegram.
    """
    normalized_mode = _signal_mode(mode)
    symbol = str(_get(decision, "symbol", _get(result, "symbol", "")) or "").upper()
    direction = _direction(_get(decision, "direction", _get(result, "direction", "LONG")))
    tp_sl = _get(decision, "tp_sl", None)
    metadata = _get(decision, "metadata", {}) or {}

    data = SignalMessageData(
        mode=normalized_mode,
        fa_name=_fa_name_from_decision(decision, symbol),
        symbol=symbol,
        direction=direction,
        confidence_pct=_safe_float(_get(decision, "confidence", 0.0)),
        estimated_profit_usdt=_safe_float(
            _get(metadata, "net_profit_usdt", _get(tp_sl, "net_profit_usdt", 0.0))
        ),
        estimated_move_pct=_safe_float(
            _get(tp_sl, "estimated_move_pct", _get(metadata, "estimated_move_pct", 0.0))
        ),
        entry=_safe_float(_get(decision, "entry", _get(result, "entry", 0.0))),
        tp=_safe_float(_get(decision, "tp", _get(result, "tp", 0.0))),
        sl=_safe_float(_get(decision, "sl", _get(result, "sl", 0.0))),
    )
    return render_signal(data)


def build_result_payload(result_payload: Any) -> str:
    """Build a clean Telegram result message from ResultPanelPayload/MonitorResult-like objects."""
    if isinstance(result_payload, ResultMessageData):
        return render_result(result_payload)

    result = _get(result_payload, "result", "TP")
    data = ResultMessageData(
        mode=_signal_mode(_get(result_payload, "mode", "SIGNAL")),
        fa_name=str(_get(result_payload, "fa_name", _get(result_payload, "symbol", "")) or ""),
        symbol=str(_get(result_payload, "symbol", "") or "").upper(),
        direction=_direction(_get(result_payload, "direction", "LONG")),
        result="TP" if str(result).upper() == "TP" else "SL",
        entry=_safe_float(_get(result_payload, "entry", 0.0)),
        exit_price=_safe_float(_get(result_payload, "exit_price", 0.0)),
        pnl_usdt=_safe_float(_get(result_payload, "pnl_usdt", 0.0)),
        move_pct=_safe_float(_get(result_payload, "move_pct", 0.0)),
        duration_minutes=int(_safe_float(_get(result_payload, "duration_minutes", 0.0))),
        close_reason=str(_get(result_payload, "reason", _get(result_payload, "close_reason", "")) or ""),
    )
    return render_result(data)


def _signal_mode(value: Any) -> SignalMode:
    text = str(value or "SIGNAL").upper().strip()
    return "TOOBIT" if text in {"TOOBIT", "REAL"} else "SIGNAL"


def _direction(value: Any) -> Direction:
    text = str(value or "LONG").upper().strip()
    return "SHORT" if text in {"SHORT", "SELL"} else "LONG"


def _fa_name_from_decision(decision: Any, symbol: str) -> str:
    analysis = _get(decision, "analysis", None)
    value = _get(analysis, "fa_name", None) or _get(decision, "fa_name", None)
    return str(value or symbol)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


__all__ = [
    "TradePanelData",
    "StatsPanelData",
    "SignalMessageData",
    "ResultMessageData",
    "render_trade_panel",
    "render_stats_panel",
    "render_signal",
    "render_result",
    "build_signal_payload",
    "build_result_payload",
    "render_signal_status",
    "render_invalid_value",
]
