from __future__ import annotations

"""
24 - result_reporter.py

Light Persian formatter for the simplified Level 1 / 5M bot.

Locked goals:
- Short Persian Telegram-ready texts.
- Format:
  AIDecision + TPSLPlan
  RealTradeOpenResult
  PositionMonitorEvent
  GhostMonitorResult
- Preserve reply_to_message_id for TP/SL/AI_EXIT result replies.
- GHOST results stay hidden from Telegram and are only used for learning/stats.
- No Telegram sending.
- No Toobit calls.
- No trading.
- No persistence.
- No AI decision.
- No paper/setup flow.
- No trap/state/confidence/meta/correlation/movement_hunter dependency.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Sequence
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
GHOST = "👻"
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


def obj_value(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def obj_float(obj: Any, key: str, default: float = 0.0) -> float:
    return safe_float(obj_value(obj, key, default), default)


def to_dict(obj: Any) -> JsonDict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        try:
            data = obj.to_dict()
            return dict(data) if isinstance(data, dict) else {}
        except Exception:
            return {}
    try:
        return dict(getattr(obj, "__dict__", {}))
    except Exception:
        return {}


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


def short_reasons(reasons: Sequence[str], limit: int = 3) -> str:
    items = [str(r) for r in reasons if r]
    return " | ".join(items[:limit])


class SignalReportFormatter:
    def format_signal(self, decision: AIDecision, plan: TPSLPlan) -> ReportPayload:
        decision_type = str(obj_value(decision, "decision_type", "")).upper()

        if decision_type == DECISION_REAL:
            title = f"{ROBOT} سیگنال واقعی"
            status = "✅ ورود واقعی"
            should_send = True
        elif decision_type == DECISION_GHOST:
            title = f"{GHOST} سیگنال Ghost"
            status = "👻 فقط یادگیری"
            should_send = False
        else:
            title = f"{RED_CROSS} رد شد"
            status = "بدون ورود"
            should_send = False

        direction = str(obj_value(decision, "direction", "")).upper()
        direction_icon = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"

        tp2 = obj_float(plan, "tp2", 0.0)
        tp2_line = f"\nTP2: {fmt_price(tp2)}" if tp2 > 0 else ""

        pattern_count = safe_int(obj_value(decision, "pattern_count", 0), 0)
        phase = str(obj_value(decision, "predicted_phase", "-") or "-")
        probability = obj_float(decision, "movement_probability", 0.0)

        text = (
            f"{title}\n"
            f"نماد: {obj_value(decision, 'symbol', '-')}\n"
            f"جهت: {direction_icon}\n"
            f"وضعیت: {status}\n\n"
            f"Entry: {fmt_price(obj_value(plan, 'entry', 0.0))}\n"
            f"TP1: {fmt_price(obj_value(plan, 'tp1', 0.0))}"
            f"{tp2_line}\n"
            f"SL: {fmt_price(obj_value(plan, 'sl', 0.0))}\n\n"
            f"AI: {obj_float(decision, 'ai_score', 0.0):.1f} | "
            f"Conf: {obj_float(decision, 'confidence_score', 0.0):.1f} | "
            f"Risk: {obj_float(decision, 'risk_score', 0.0):.1f}\n"
            f"فاز: {phase} | احتمال حرکت: {probability:.1f}% | الگو: {pattern_count}"
        )

        return ReportPayload(
            report_type=REPORT_SIGNAL,
            text=text,
            should_send=should_send,
            meta={"decision": to_dict(decision), "tp_sl_plan": to_dict(plan)},
        )


class TradeOpenReportFormatter:
    def format_open_result(self, result: RealTradeOpenResult, reply_to_message_id: int = 0) -> ReportPayload:
        status = str(obj_value(result, "status", "")).upper()

        if status == STATUS_CONFIRMED:
            icon = GREEN_CHECK
            title = "پوزیشن واقعی تایید شد"
        elif status == STATUS_PENDING_REAL_CONFIRM:
            icon = WARNING
            title = "سفارش ارسال شد؛ منتظر تایید پوزیشن"
        elif status in {STATUS_REJECTED, STATUS_FAILED}:
            icon = RED_CROSS
            title = "پوزیشن واقعی باز نشد"
        else:
            icon = INFO
            title = f"وضعیت سفارش: {status or '-'}"

        error = str(obj_value(result, "error", "") or "")
        error_line = f"\nخطا: {error}" if error else ""
        pos_id = str(obj_value(result, "position_id", "") or "")
        pos_line = f"\nPosition ID: {pos_id}" if pos_id else ""

        tp2 = obj_float(result, "tp2", 0.0)
        tp2_line = f" | TP2: {fmt_price(tp2)}" if tp2 > 0 else ""

        text = (
            f"{icon} {title}\n"
            f"نماد: {obj_value(result, 'symbol', '-')}\n"
            f"جهت: {obj_value(result, 'direction', '-')}\n"
            f"مقدار: {obj_value(result, 'quantity', '-')}\n"
            f"مارجین: {fmt_usdt(obj_value(result, 'margin_usdt', 0.0))} | "
            f"لوریج: {safe_int(obj_value(result, 'leverage', 0), 0)}x\n"
            f"Entry: {fmt_price(obj_value(result, 'entry', 0.0))}\n"
            f"TP1: {fmt_price(obj_value(result, 'tp1', 0.0))}{tp2_line}\n"
            f"SL: {fmt_price(obj_value(result, 'sl', 0.0))}"
            f"{pos_line}"
            f"{error_line}"
        )

        report_type = REPORT_STATUS if status in {STATUS_CONFIRMED, STATUS_PENDING_REAL_CONFIRM} else REPORT_ERROR

        return ReportPayload(
            report_type=report_type,
            text=text,
            reply_to_message_id=safe_int(reply_to_message_id, 0),
            should_send=True,
            meta={"open_result": to_dict(result)},
        )


class ResultReportFormatter:
    def format_event(self, event: PositionMonitorEvent) -> ReportPayload:
        if not bool(obj_value(event, "should_report", True)):
            return ReportPayload(
                report_type=REPORT_RESULT,
                text="",
                reply_to_message_id=safe_int(obj_value(event, "reply_to_message_id", 0), 0),
                should_send=False,
                meta={"event": to_dict(event)},
            )

        event_type = str(obj_value(event, "event_type", "")).upper()

        if event_type == EVENT_TP1:
            icon = GREEN_CHECK
            title = "TP1 خورد"
        elif event_type == EVENT_TP2:
            icon = GREEN_CHECK
            title = "TP2 خورد"
        elif event_type == EVENT_AI_EXIT:
            pnl = obj_float(event, "realized_pnl_usdt", 0.0)
            icon = GREEN_CHECK if pnl >= 0 else WARNING
            title = "خروج AI در سود" if pnl >= 0 else "خروج AI"
        elif event_type == EVENT_SL:
            icon = RED_CROSS
            title = "Stop Loss خورد"
        elif event_type == EVENT_CLOSED_UNKNOWN:
            icon = WARNING
            title = "پوزیشن بسته شد"
        elif event_type == EVENT_PROTECT_SL:
            icon = SHIELD
            title = "SL محافظ فعال شد"
        elif event_type == EVENT_SYNC_OPEN:
            icon = INFO
            title = "پوزیشن Sync شد"
        else:
            icon = INFO
            title = event_type or "رویداد پوزیشن"

        pnl_line = self.format_pnl(event)
        raw = obj_value(event, "raw", {}) if isinstance(obj_value(event, "raw", {}), dict) else {}

        protection_line = ""
        if event_type == EVENT_TP1:
            protected_sl = raw.get("protected_sl")
            runner_qty = raw.get("runner_quantity")
            if protected_sl:
                protection_line = f"\n{SHIELD} SL محافظ: {fmt_price(protected_sl)}"
                if runner_qty:
                    protection_line += f" | رانر TP2: {runner_qty}"

        reason = short_reasons(obj_value(event, "reason_codes", ()) or ())
        reason_line = f"\nدلیل: {reason}" if reason else ""

        text = (
            f"{icon} {title}\n"
            f"نماد: {obj_value(event, 'symbol', '-')}\n"
            f"جهت: {obj_value(event, 'direction', '-')}\n"
            f"قیمت: {fmt_price(obj_value(event, 'price', 0.0))}\n"
            f"{pnl_line}"
            f"{protection_line}"
            f"{reason_line}"
        )

        return ReportPayload(
            report_type=REPORT_RESULT,
            text=text,
            reply_to_message_id=safe_int(obj_value(event, "reply_to_message_id", 0), 0),
            should_send=True,
            meta={"event": to_dict(event)},
        )

    def format_pnl(self, event: PositionMonitorEvent) -> str:
        event_type = str(obj_value(event, "event_type", "")).upper()
        pnl_status = str(obj_value(event, "pnl_status", "")).upper()

        if pnl_status == PNL_CONFIRMED:
            return (
                f"PnL واقعی توبیت: {fmt_usdt(obj_value(event, 'realized_pnl_usdt', 0.0))} "
                f"({fmt_percent(obj_value(event, 'realized_pnl_percent', 0.0))})"
            )

        if event_type == EVENT_TP1:
            return "PnL: TP1 ثبت شد؛ اگر رانر باز باشد PnL نهایی بعداً تایید می‌شود"

        if pnl_status == PNL_UNAVAILABLE:
            return (
                "PnL: هنوز از توبیت تایید نشد "
                f"(تقریبی: {fmt_percent(obj_value(event, 'realized_pnl_percent', 0.0))})"
            )

        return (
            "PnL: در حال دریافت از توبیت "
            f"(تقریبی: {fmt_percent(obj_value(event, 'realized_pnl_percent', 0.0))})"
        )


class GhostReportFormatter:
    def format_ghost_result(self, result: GhostMonitorResult) -> ReportPayload:
        return ReportPayload(
            report_type=REPORT_GHOST,
            text="",
            should_send=False,
            meta={
                "ghost_result": to_dict(result),
                "hidden_from_telegram": True,
            },
        )


class StatusReportFormatter:
    def format_learning_summary(self, learning: LearningSummary) -> ReportPayload:
        text = (
            f"{ROBOT} خلاصه یادگیری\n"
            f"{obj_value(learning, 'coin', '-') or obj_value(learning, 'symbol', '-')} "
            f"{obj_value(learning, 'direction', '-')}\n"
            f"نمونه: {safe_int(obj_value(learning, 'sample_count', 0), 0)} | "
            f"Real: {safe_int(obj_value(learning, 'real_samples', 0), 0)} | "
            f"Ghost: {safe_int(obj_value(learning, 'ghost_samples', 0), 0)}\n"
            f"WR: {obj_float(learning, 'win_rate', obj_float(learning, 'outcome_success_rate', 0.0)):.1f}% | "
            f"TP1: {safe_int(obj_value(learning, 'tp1_count', 0), 0)} | "
            f"TP2: {safe_int(obj_value(learning, 'tp2_count', 0), 0)} | "
            f"SL: {safe_int(obj_value(learning, 'sl_count', 0), 0)}\n"
            f"وضعیت: {obj_value(learning, 'risk_label', '-')} / {obj_value(learning, 'confidence_hint', '-')}"
        )
        return ReportPayload(REPORT_STATUS, text, meta={"learning": to_dict(learning)})


class ErrorReportFormatter:
    def format_error(self, title: str, error: Any, reply_to_message_id: int = 0) -> ReportPayload:
        err = str(error)
        if len(err) > 700:
            err = err[:700] + "..."
        return ReportPayload(
            report_type=REPORT_ERROR,
            text=f"{RED_CROSS} {title}\n{err}",
            reply_to_message_id=safe_int(reply_to_message_id, 0),
            should_send=True,
            meta={"error": err},
        )


class ResultReporter:
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
