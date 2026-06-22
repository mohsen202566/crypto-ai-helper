from __future__ import annotations

"""
24 - result_reporter.py

Result reporting formatter for the locked Movement Hunter architecture.

Responsibilities:
- Convert monitor/trade/decision events into short Persian Telegram-ready text.
- Format TP1 / TP2 / AI_EXIT / SL results with:
  green check for profit events
  red cross for SL/loss events
  real Toobit PnL when confirmed
  PnL pending/unavailable warning when not confirmed
- Preserve reply_to_message_id so bot.py can reply to original signal.
- Format REAL order result, GHOST result, AI status and concise errors.
- Keep output simple and decision-focused.

Strictly forbidden:
- No Telegram sending.
- No Toobit calls.
- No AI decision.
- No trading.
- No persistence.
- No Paper mode.
- No Setup flow.

bot.py is responsible for sending messages.
This file only formats report payloads.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import math
import time

from ai_decision_engine import AIDecision, DECISION_REAL, DECISION_GHOST, DECISION_REJECT
from tp_sl_engine import TPSLPlan
from real_trade_manager import RealTradeOpenResult, STATUS_CONFIRMED, STATUS_PENDING_REAL_CONFIRM, STATUS_FAILED, STATUS_REJECTED
from position_monitor import (
    PositionMonitorEvent,
    EVENT_TP1,
    EVENT_TP2,
    EVENT_SL,
    EVENT_AI_EXIT,
    EVENT_PROTECT_SL,
    EVENT_CLOSED_UNKNOWN,
    EVENT_SYNC_OPEN,
    PNL_CONFIRMED,
    PNL_PENDING,
    PNL_UNAVAILABLE,
)
from ghost_manager import GhostMonitorResult
from meta_learning import MetaLearningSummary
from coin_learning import LearningSummary


JsonDict = Dict[str, Any]

REPORT_SIGNAL = "SIGNAL"
REPORT_RESULT = "RESULT"
REPORT_ERROR = "ERROR"
REPORT_STATUS = "STATUS"
REPORT_GHOST = "GHOST"

GREEN_CHECK = "✅"
RED_CROSS = "❌"
WARNING = "⚠️"
INFO = "ℹ️"
ROBOT = "🤖"
MONEY = "💰"
TARGET = "🎯"
SHIELD = "🛡️"


@dataclass(frozen=True)
class ReportPayload:
    report_type: str
    text: str
    reply_to_message_id: int = 0
    should_send: bool = True
    parse_mode: str = ""
    meta: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


def now_ts() -> int:
    return int(time.time())


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def fmt_price(value: Any) -> str:
    v = safe_float(value)
    if v <= 0:
        return "-"
    if v >= 1000:
        return f"{v:.2f}"
    if v >= 100:
        return f"{v:.3f}"
    if v >= 10:
        return f"{v:.4f}"
    if v >= 1:
        return f"{v:.5f}"
    if v >= 0.1:
        return f"{v:.6f}"
    return f"{v:.8f}"


def fmt_usdt(value: Any) -> str:
    v = safe_float(value)
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.4f}$"


def fmt_percent(value: Any) -> str:
    v = safe_float(value)
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.3f}%"


def _short_reasons(reasons: Sequence[str], limit: int = 4) -> str:
    items = [str(r) for r in reasons if r]
    if not items:
        return ""
    return " | ".join(items[:limit])


class SignalReportFormatter:
    """Formats AI final signal text after TP/SL has been calculated."""

    def format_signal(self, decision: AIDecision, plan: TPSLPlan) -> ReportPayload:
        if decision.decision_type == DECISION_REAL:
            title = f"{ROBOT} سیگنال واقعی"
            status = "✅ آماده ورود واقعی"
        elif decision.decision_type == DECISION_GHOST:
            title = f"{ROBOT} سیگنال Ghost"
            status = "👻 فقط یادگیری"
        else:
            title = f"{ROBOT} رد شد"
            status = "❌ بدون ورود"

        direction_icon = "🟢 LONG" if decision.direction == "LONG" else "🔴 SHORT"
        tp2_line = f"\nTP2: {fmt_price(plan.tp2)}" if plan.tp2 and plan.tp2 > 0 else ""

        text = (
            f"{title}\n"
            f"نماد: {decision.symbol}\n"
            f"جهت: {direction_icon}\n"
            f"وضعیت: {status}\n\n"
            f"Entry: {fmt_price(plan.entry)}\n"
            f"TP1: {fmt_price(plan.tp1)}"
            f"{tp2_line}\n"
            f"SL: {fmt_price(plan.sl)}\n\n"
            f"AI: {decision.ai_score:.1f} | Confidence: {decision.confidence_score:.1f} | Risk: {decision.risk_score:.1f}\n"
            f"فاز: {decision.freshness} / {decision.market_state}"
        )

        return ReportPayload(
            report_type=REPORT_SIGNAL,
            text=text,
            should_send=decision.decision_type == DECISION_REAL,
            meta={"decision": decision.to_dict(), "tp_sl_plan": plan.to_dict()},
        )


class TradeOpenReportFormatter:
    """Formats real trade open/preflight results."""

    def format_open_result(self, result: RealTradeOpenResult, reply_to_message_id: int = 0) -> ReportPayload:
        if result.status == STATUS_CONFIRMED:
            icon = GREEN_CHECK
            title = "پوزیشن واقعی تایید شد"
            should_send = True
        elif result.status == STATUS_PENDING_REAL_CONFIRM:
            icon = WARNING
            title = "سفارش ارسال شد؛ منتظر تایید پوزیشن"
            should_send = True
        elif result.status in {STATUS_REJECTED, STATUS_FAILED}:
            icon = RED_CROSS
            title = "پوزیشن واقعی باز نشد"
            should_send = True
        else:
            icon = INFO
            title = f"وضعیت سفارش: {result.status}"
            should_send = True

        error_line = f"\nخطا: {result.error}" if result.error else ""
        pos_line = f"\nPosition ID: {result.position_id}" if result.position_id else ""

        text = (
            f"{icon} {title}\n"
            f"نماد: {result.symbol}\n"
            f"جهت: {result.direction}\n"
            f"مقدار: {result.quantity}\n"
            f"مارجین: {result.margin_usdt}$ | لوریج: {result.leverage}x\n"
            f"Entry: {fmt_price(result.entry)}\n"
            f"TP1: {fmt_price(result.tp1)}"
            f"{f' | TP2: {fmt_price(result.tp2)}' if result.tp2 else ''}\n"
            f"SL: {fmt_price(result.sl)}"
            f"{pos_line}"
            f"{error_line}"
        )

        report_type = REPORT_STATUS if result.status in {STATUS_CONFIRMED, STATUS_PENDING_REAL_CONFIRM} else REPORT_ERROR

        return ReportPayload(
            report_type=report_type,
            text=text,
            reply_to_message_id=reply_to_message_id,
            should_send=should_send,
            meta={"open_result": result.to_dict()},
        )


class ResultReportFormatter:
    """Formats position monitor events."""

    def format_event(self, event: PositionMonitorEvent) -> ReportPayload:
        if not event.should_report:
            return ReportPayload(
                report_type=REPORT_RESULT,
                text="",
                reply_to_message_id=event.reply_to_message_id,
                should_send=False,
                meta={"event": event.to_dict()},
            )

        event_type = event.event_type

        if event_type == EVENT_TP1:
            icon = GREEN_CHECK
            title = "TP1 خورد ✅"
        elif event_type == EVENT_TP2:
            icon = GREEN_CHECK
            title = "TP2 خورد ✅"
        elif event_type == EVENT_AI_EXIT:
            icon = GREEN_CHECK if event.realized_pnl_usdt >= 0 else WARNING
            title = "خروج AI در سود ✅" if event.realized_pnl_usdt >= 0 else "خروج AI ⚠️"
        elif event_type == EVENT_SL:
            icon = RED_CROSS
            title = "Stop Loss خورد ❌"
        elif event_type == EVENT_CLOSED_UNKNOWN:
            icon = WARNING
            title = "پوزیشن بسته شد"
        else:
            icon = INFO
            title = event_type

        pnl_line = self._format_pnl(event)
        protection_line = ""
        if event_type == EVENT_TP1:
            raw = event.raw if isinstance(event.raw, dict) else {}
            protected_sl = raw.get("protected_sl")
            runner_qty = raw.get("runner_quantity")
            if protected_sl:
                protection_line = f"\n{SHIELD} سود محافظت شد | SL محافظ: {fmt_price(protected_sl)}"
                if runner_qty:
                    protection_line += f" | رانر TP2: {runner_qty}"
        reason_line = _short_reasons(event.reason_codes)
        reason_text = f"\nدلیل: {reason_line}" if reason_line else ""

        text = (
            f"{icon} {title}\n"
            f"نماد: {event.symbol}\n"
            f"جهت: {event.direction}\n"
            f"قیمت: {fmt_price(event.price)}\n"
            f"{pnl_line}"
            f"{protection_line}"
            f"{reason_text}"
        )

        return ReportPayload(
            report_type=REPORT_RESULT,
            text=text,
            reply_to_message_id=event.reply_to_message_id,
            should_send=True,
            meta={"event": event.to_dict()},
        )

    def _format_pnl(self, event: PositionMonitorEvent) -> str:
        if event.pnl_status == PNL_CONFIRMED:
            return (
                f"PnL واقعی توبیت: {fmt_usdt(event.realized_pnl_usdt)} "
                f"({fmt_percent(event.realized_pnl_percent)})"
            )

        if event.event_type in {EVENT_TP1}:
            return "PnL: هنوز پوزیشن کامل بسته نشده"

        if event.pnl_status == PNL_UNAVAILABLE:
            return (
                f"PnL: هنوز از توبیت تایید نشد "
                f"(محاسبه تقریبی: {fmt_percent(event.realized_pnl_percent)})"
            )

        return (
            f"PnL: در حال دریافت از توبیت "
            f"(تقریبی: {fmt_percent(event.realized_pnl_percent)})"
        )


class GhostReportFormatter:
    """Formats Ghost monitoring results."""

    def format_ghost_result(self, result: GhostMonitorResult) -> ReportPayload:
        if not result.closed:
            return ReportPayload(
                report_type=REPORT_GHOST,
                text="",
                should_send=False,
                meta={"ghost_result": result.to_dict()},
            )

        if result.result in {"TP1", "TP2", "AI_EXIT"}:
            icon = GREEN_CHECK
            title = f"Ghost {result.result}"
        elif result.result == "SL":
            icon = RED_CROSS
            title = "Ghost SL"
        else:
            icon = INFO
            title = f"Ghost {result.status}"

        # GHOST results are intentionally hidden from Telegram. They are only
        # stored/used for learning by ghost_manager/stats_manager.
        return ReportPayload(
            report_type=REPORT_GHOST,
            text="",
            should_send=False,
            meta={"ghost_result": result.to_dict(), "hidden_from_telegram": True},
        )


class StatusReportFormatter:
    """Formats AI/learning status reports."""

    def format_meta_status(self, meta: MetaLearningSummary) -> ReportPayload:
        best = ", ".join(meta.best_modules[:5]) if meta.best_modules else "-"
        weak = ", ".join(meta.weak_modules[:5]) if meta.weak_modules else "-"

        text = (
            f"{ROBOT} وضعیت یادگیری متا\n"
            f"نمونه‌ها: {meta.sample_count}\n"
            f"لایه‌های قوی: {best}\n"
            f"لایه‌های ضعیف: {weak}"
        )
        return ReportPayload(REPORT_STATUS, text, meta={"meta_learning": meta.to_dict()})

    def format_learning_summary(self, learning: LearningSummary) -> ReportPayload:
        text = (
            f"{ROBOT} خلاصه یادگیری\n"
            f"{learning.coin} {learning.direction}\n"
            f"نمونه: {learning.sample_count} | Real: {learning.real_samples} | Ghost: {learning.ghost_samples}\n"
            f"WR: {learning.win_rate:.1f}% | TP1: {learning.tp1_count} | TP2: {learning.tp2_count} | SL: {learning.sl_count}\n"
            f"وضعیت: {learning.risk_label} / {learning.confidence_hint}"
        )
        return ReportPayload(REPORT_STATUS, text, meta={"learning": learning.to_dict()})


class ErrorReportFormatter:
    """Formats concise errors."""

    def format_error(self, title: str, error: Any, reply_to_message_id: int = 0) -> ReportPayload:
        err = str(error)
        if len(err) > 700:
            err = err[:700] + "..."
        text = f"{RED_CROSS} {title}\n{err}"
        return ReportPayload(
            report_type=REPORT_ERROR,
            text=text,
            reply_to_message_id=reply_to_message_id,
            should_send=True,
            meta={"error": err},
        )


class ResultReporter:
    """Facade used by bot.py."""

    def __init__(self):
        self.signal = SignalReportFormatter()
        self.trade = TradeOpenReportFormatter()
        self.result = ResultReportFormatter()
        self.ghost = GhostReportFormatter()
        self.status = StatusReportFormatter()
        self.error = ErrorReportFormatter()

    def signal_report(self, decision: AIDecision, plan: TPSLPlan) -> ReportPayload:
        return self.signal.format_signal(decision, plan)

    def trade_open_report(self, result: RealTradeOpenResult, reply_to_message_id: int = 0) -> ReportPayload:
        return self.trade.format_open_result(result, reply_to_message_id=reply_to_message_id)

    def position_event_report(self, event: PositionMonitorEvent) -> ReportPayload:
        return self.result.format_event(event)

    def ghost_result_report(self, result: GhostMonitorResult) -> ReportPayload:
        return self.ghost.format_ghost_result(result)

    def meta_status_report(self, meta: MetaLearningSummary) -> ReportPayload:
        return self.status.format_meta_status(meta)

    def learning_summary_report(self, learning: LearningSummary) -> ReportPayload:
        return self.status.format_learning_summary(learning)

    def error_report(self, title: str, error: Any, reply_to_message_id: int = 0) -> ReportPayload:
        return self.error.format_error(title, error, reply_to_message_id=reply_to_message_id)


_default_reporter: Optional[ResultReporter] = None


def reporter() -> ResultReporter:
    global _default_reporter
    if _default_reporter is None:
        _default_reporter = ResultReporter()
    return _default_reporter


def format_signal_report(decision: AIDecision, plan: TPSLPlan) -> ReportPayload:
    return reporter().signal_report(decision, plan)


def format_trade_open_report(result: RealTradeOpenResult, reply_to_message_id: int = 0) -> ReportPayload:
    return reporter().trade_open_report(result, reply_to_message_id=reply_to_message_id)


def format_position_event(event: PositionMonitorEvent) -> ReportPayload:
    return reporter().position_event_report(event)


def format_ghost_result(result: GhostMonitorResult) -> ReportPayload:
    return reporter().ghost_result_report(result)


def format_error_report(title: str, error: Any, reply_to_message_id: int = 0) -> ReportPayload:
    return reporter().error_report(title, error, reply_to_message_id=reply_to_message_id)
