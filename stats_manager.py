from __future__ import annotations

"""
25 - stats_manager.py

Statistics manager for the locked Movement Hunter architecture.

Responsibilities:
- Track REAL and GHOST outcomes separately and together.
- Track TP1 / TP2 / AI_EXIT / SL / rejected / opened / closed counts.
- Track win rate by:
  symbol
  direction
  source type
  market state
  freshness
  predicted phase
  trap level
- Track real Toobit PnL when confirmed.
- Provide short Persian reports for bot.py commands:
  آمار
  آمار 7 روز
  آمار کل
  آمار هوشمند
  حذف آمار
- Keep reports short to avoid Telegram delivery failure.

Strictly forbidden:
- No trading.
- No Toobit calls.
- No AI decision.
- No Telegram sending.
- No Paper mode.
- No Setup flow.

This file only stores, aggregates and formats statistics.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import uuid4
import math
import time

from data_store import save_stat_event, store
from position_monitor import PositionMonitorEvent, EVENT_TP1, EVENT_TP2, EVENT_AI_EXIT, EVENT_SL
from ghost_manager import GhostMonitorResult
from ai_decision_engine import AIDecision, DECISION_REAL, DECISION_GHOST, DECISION_REJECT


JsonDict = Dict[str, Any]

SOURCE_REAL = "REAL"
SOURCE_GHOST = "GHOST"
SOURCE_REJECT = "REJECT"

RESULT_TP1 = "TP1"
RESULT_TP2 = "TP2"
RESULT_AI_EXIT = "AI_EXIT"
RESULT_SL = "SL"
RESULT_REJECT = "REJECT"
RESULT_OPEN = "OPEN"
RESULT_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class StatEvent:
    stat_id: str
    timestamp: int
    source_type: str
    symbol: str
    direction: str
    result: str
    decision_id: str = ""
    position_id: str = ""
    ghost_id: str = ""
    market_state: str = "UNKNOWN"
    freshness: str = "UNKNOWN"
    predicted_phase: str = "UNKNOWN"
    trap_level: str = "UNKNOWN"
    ai_score: float = 0.0
    confidence_score: float = 0.0
    risk_score: float = 0.0
    realized_pnl_usdt: float = 0.0
    realized_pnl_percent: float = 0.0
    pnl_confirmed: bool = False
    meta: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class StatsSummary:
    source_type: str
    period_label: str
    total_events: int
    opened_count: int
    closed_count: int
    tp1_count: int
    tp2_count: int
    ai_exit_count: int
    sl_count: int
    reject_count: int
    win_rate: float
    confirmed_pnl_usdt: float
    avg_pnl_percent: float
    best_symbol: str = "-"
    worst_symbol: str = "-"
    long_win_rate: float = 0.0
    short_win_rate: float = 0.0
    notes: Tuple[str, ...] = field(default_factory=tuple)

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


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except Exception:
        return low


def normalize_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().replace("-", "").replace("/", "").replace("_", "").strip()
    if s and not s.endswith("USDT") and len(s) <= 14:
        s += "USDT"
    return s


def normalize_direction(direction: str) -> str:
    d = str(direction or "").upper().strip()
    if d in {"LONG", "BUY"}:
        return "LONG"
    if d in {"SHORT", "SELL"}:
        return "SHORT"
    return d or "UNKNOWN"


def is_win(result: str) -> bool:
    """WinRate is intentionally based only on TP1 vs SL.

    TP2 and AI_EXIT are valuable outcome/profit-protection metrics, but they
    must not inflate win rate because they can occur after TP1 on the same
    position.
    """
    return str(result).upper() == RESULT_TP1


def is_loss(result: str) -> bool:
    return str(result).upper() == RESULT_SL


def is_winrate_result(result: str) -> bool:
    """Only results that participate in WR denominator."""
    return str(result).upper() in {RESULT_TP1, RESULT_SL}


def is_closed_result(result: str) -> bool:
    """All final/monitorable result events stored for statistics.

    This remains broader than win-rate results so TP2 and AI_EXIT still show in
    reports and learning metadata, without affecting WR.
    """
    return str(result).upper() in {RESULT_TP1, RESULT_TP2, RESULT_AI_EXIT, RESULT_SL}


def result_key(event: "StatEvent") -> str:
    """Stable id used to avoid double-counting the same close event."""
    if event.ghost_id and is_closed_result(event.result):
        return f"GHOST:{event.ghost_id}:{event.result}"
    if event.position_id and is_closed_result(event.result):
        return f"REAL:{event.position_id}:{event.result}"
    if event.decision_id and event.result in {RESULT_OPEN, RESULT_REJECT}:
        return f"DECISION:{event.decision_id}:{event.result}"
    return event.stat_id


class StatEventBuilder:
    """Builds StatEvent records from decisions and monitor results."""

    def from_decision(self, decision: AIDecision) -> StatEvent:
        if decision.decision_type == DECISION_REAL:
            source = SOURCE_REAL
            result = RESULT_OPEN
        elif decision.decision_type == DECISION_GHOST:
            source = SOURCE_GHOST
            result = RESULT_OPEN
        else:
            source = SOURCE_REJECT
            result = RESULT_REJECT

        return StatEvent(
            stat_id=f"stat_{uuid4().hex}",
            timestamp=getattr(decision, "timestamp", 0) or now_ts(),
            source_type=source,
            symbol=normalize_symbol(getattr(decision, "symbol", "")),
            direction=normalize_direction(getattr(decision, "direction", "")),
            result=result,
            decision_id=str(getattr(decision, "decision_id", "")),
            market_state=str(
                getattr(
                    decision,
                    "market_state",
                    decision.meta.get("market_state", "UNKNOWN")
                    if isinstance(getattr(decision, "meta", {}), dict)
                    else "UNKNOWN",
                )
            ),
            freshness=str(
                getattr(
                    decision,
                    "freshness",
                    decision.meta.get("freshness", "UNKNOWN")
                    if isinstance(getattr(decision, "meta", {}), dict)
                    else "UNKNOWN",
                )
            ),
            predicted_phase=str(getattr(decision, "predicted_phase", "UNKNOWN")),
            ai_score=safe_float(getattr(decision, "ai_score", 0.0)),
            confidence_score=safe_float(getattr(decision, "confidence_score", 0.0)),
            risk_score=safe_float(getattr(decision, "risk_score", 0.0)),
            meta={"decision_type": getattr(decision, "decision_type", "UNKNOWN")},
        )

    def from_position_event(self, event: PositionMonitorEvent) -> StatEvent:
        result = RESULT_UNKNOWN
        if event.event_type == EVENT_TP1:
            result = RESULT_TP1
        elif event.event_type == EVENT_TP2:
            result = RESULT_TP2
        elif event.event_type == EVENT_AI_EXIT:
            result = RESULT_AI_EXIT
        elif event.event_type == EVENT_SL:
            result = RESULT_SL

        return StatEvent(
            stat_id=f"stat_{uuid4().hex}",
            timestamp=event.timestamp or now_ts(),
            source_type=SOURCE_REAL,
            symbol=normalize_symbol(event.symbol),
            direction=normalize_direction(event.direction),
            result=result,
            position_id=event.position_id,
            realized_pnl_usdt=safe_float(event.realized_pnl_usdt),
            realized_pnl_percent=safe_float(event.realized_pnl_percent),
            pnl_confirmed=str(event.pnl_status).upper() == "CONFIRMED",
            meta={"event": event.to_dict()},
        )

    def from_ghost_result(self, result: GhostMonitorResult) -> StatEvent:
        result_dict = result.to_dict() if hasattr(result, "to_dict") and callable(result.to_dict) else {}
        outcome = str(getattr(result, "result", "") or result_dict.get("result") or RESULT_UNKNOWN).upper()
        direction = normalize_direction(getattr(result, "direction", "") or result_dict.get("direction", ""))
        symbol = normalize_symbol(getattr(result, "symbol", "") or result_dict.get("symbol", ""))
        ghost_id = str(getattr(result, "ghost_id", "") or result_dict.get("ghost_id", ""))

        if outcome in {RESULT_TP1, RESULT_TP2, RESULT_AI_EXIT}:
            pnl_percent = safe_float(getattr(result, "mfe_percent", result_dict.get("mfe_percent", 0.0)))
        elif outcome == RESULT_SL:
            pnl_percent = -abs(safe_float(getattr(result, "mae_percent", result_dict.get("mae_percent", 0.0))))
        else:
            pnl_percent = 0.0

        return StatEvent(
            stat_id=f"stat_{uuid4().hex}",
            timestamp=safe_int(result_dict.get("closed_at", result_dict.get("timestamp", now_ts())), now_ts()),
            source_type=SOURCE_GHOST,
            symbol=symbol,
            direction=direction,
            result=outcome,
            ghost_id=ghost_id,
            market_state=str(result_dict.get("market_state", result_dict.get("state", "UNKNOWN"))),
            freshness=str(result_dict.get("freshness", "UNKNOWN")),
            predicted_phase=str(result_dict.get("predicted_phase", result_dict.get("phase", "UNKNOWN"))),
            trap_level=str(result_dict.get("trap_level", "UNKNOWN")),
            realized_pnl_percent=pnl_percent,
            pnl_confirmed=False,
            meta={"ghost_result": result_dict},
        )


class StatsStore:
    """Persistence adapter around data_store.py."""

    def save(self, event: StatEvent) -> None:
        # Avoid double-counting if a monitor loop reports the same TP/SL more than once.
        key = result_key(event)
        try:
            section = store().section("stats")
            for existing in section.values():
                try:
                    old_event = self._coerce(existing)
                    if result_key(old_event) == key:
                        return
                except Exception:
                    continue
        except Exception:
            pass
        save_stat_event(event.stat_id, event.to_dict())

    def load_all(self) -> List[StatEvent]:
        records = []
        try:
            values = store().section("stats").values()
        except Exception:
            values = []
        for item in values:
            try:
                records.append(self._coerce(item))
            except Exception:
                continue
        return records

    def clear(self) -> int:
        try:
            section = store().section("stats")
            count = len(section)
            section.clear()
            store().save()
            return count
        except Exception:
            return 0

    def _coerce(self, item: Any) -> StatEvent:
        if isinstance(item, StatEvent):
            return item
        if hasattr(item, "to_dict") and callable(item.to_dict):
            item = item.to_dict()
        if not isinstance(item, dict):
            item = {}

        meta = dict(item.get("meta", {}) if isinstance(item.get("meta", {}), dict) else {})
        ghost_meta = meta.get("ghost_result", {}) if isinstance(meta.get("ghost_result", {}), dict) else {}
        event_meta = meta.get("event", {}) if isinstance(meta.get("event", {}), dict) else {}

        symbol = item.get("symbol") or ghost_meta.get("symbol") or event_meta.get("symbol") or ""
        direction = item.get("direction") or ghost_meta.get("direction") or event_meta.get("direction") or ""
        result = item.get("result") or ghost_meta.get("result") or event_meta.get("event_type") or RESULT_UNKNOWN

        return StatEvent(
            stat_id=str(item.get("stat_id", item.get("id", f"stat_{uuid4().hex}"))),
            timestamp=safe_int(item.get("timestamp", ghost_meta.get("closed_at", now_ts()))),
            source_type=str(item.get("source_type", SOURCE_REAL)).upper(),
            symbol=normalize_symbol(symbol),
            direction=normalize_direction(direction),
            result=str(result).upper(),
            decision_id=str(item.get("decision_id", ghost_meta.get("decision_id", ""))),
            position_id=str(item.get("position_id", event_meta.get("position_id", ""))),
            ghost_id=str(item.get("ghost_id", ghost_meta.get("ghost_id", ""))),
            market_state=str(item.get("market_state", ghost_meta.get("market_state", "UNKNOWN"))),
            freshness=str(item.get("freshness", ghost_meta.get("freshness", "UNKNOWN"))),
            predicted_phase=str(item.get("predicted_phase", ghost_meta.get("predicted_phase", "UNKNOWN"))),
            trap_level=str(item.get("trap_level", ghost_meta.get("trap_level", "UNKNOWN"))),
            ai_score=safe_float(item.get("ai_score", ghost_meta.get("ai_score", 0.0))),
            confidence_score=safe_float(item.get("confidence_score", ghost_meta.get("confidence_score", 0.0))),
            risk_score=safe_float(item.get("risk_score", ghost_meta.get("risk_score", 0.0))),
            realized_pnl_usdt=safe_float(item.get("realized_pnl_usdt")),
            realized_pnl_percent=safe_float(item.get("realized_pnl_percent")),
            pnl_confirmed=bool(item.get("pnl_confirmed", False)),
            meta=meta,
        )


class StatsAggregator:
    """Aggregates events into short summaries."""

    def filter_period(self, events: Sequence[StatEvent], days: Optional[int] = None) -> Tuple[List[StatEvent], str]:
        if days is None or days <= 0:
            return list(events), "کل"
        since = now_ts() - days * 86400
        return [e for e in events if e.timestamp >= since], f"{days} روز"

    def summarize(self, events: Sequence[StatEvent], source_type: str = "ALL", period_label: str = "کل") -> StatsSummary:
        source = str(source_type or "ALL").upper()
        filtered = [e for e in events if source == "ALL" or e.source_type == source]

        total = len(filtered)
        opened = sum(1 for e in filtered if e.result == RESULT_OPEN)
        tp1 = sum(1 for e in filtered if e.result == RESULT_TP1)
        tp2 = sum(1 for e in filtered if e.result == RESULT_TP2)
        ai_exit = sum(1 for e in filtered if e.result == RESULT_AI_EXIT)
        sl = sum(1 for e in filtered if e.result == RESULT_SL)
        reject = sum(1 for e in filtered if e.result == RESULT_REJECT)
        # WinRate must be based only on TP1 vs SL.
        # TP2 and AI_EXIT are tracked separately and must not double-count wins.
        wins = tp1
        losses = sl
        closed = wins + losses
        wr = wins / closed * 100.0 if closed else 0.0

        confirmed_pnl = sum(e.realized_pnl_usdt for e in filtered if e.pnl_confirmed)
        pnl_events = [e.realized_pnl_percent for e in filtered if is_closed_result(e.result)]
        avg_pnl = sum(pnl_events) / len(pnl_events) if pnl_events else 0.0

        best_symbol, worst_symbol = self._best_worst_symbol(filtered)
        long_wr = self._direction_wr(filtered, "LONG")
        short_wr = self._direction_wr(filtered, "SHORT")

        notes: List[str] = []
        if closed == 0:
            notes.append("هنوز معامله بسته‌شده کافی نیست")
        if source == SOURCE_REAL and confirmed_pnl == 0:
            notes.append("PnL تاییدشده توبیت هنوز کم/صفر است")

        return StatsSummary(
            source_type=source,
            period_label=period_label,
            total_events=total,
            opened_count=opened,
            closed_count=closed,
            tp1_count=tp1,
            tp2_count=tp2,
            ai_exit_count=ai_exit,
            sl_count=sl,
            reject_count=reject,
            win_rate=clamp(wr),
            confirmed_pnl_usdt=confirmed_pnl,
            avg_pnl_percent=avg_pnl,
            best_symbol=best_symbol,
            worst_symbol=worst_symbol,
            long_win_rate=long_wr,
            short_win_rate=short_wr,
            notes=tuple(notes),
        )

    def _direction_wr(self, events: Sequence[StatEvent], direction: str) -> float:
        items = [e for e in events if e.direction == direction and is_winrate_result(e.result)]
        if not items:
            return 0.0
        wins = sum(1 for e in items if is_win(e.result))
        return wins / len(items) * 100.0

    def _best_worst_symbol(self, events: Sequence[StatEvent]) -> Tuple[str, str]:
        by_symbol: Dict[str, List[StatEvent]] = {}
        for e in events:
            if is_winrate_result(e.result):
                by_symbol.setdefault(e.symbol, []).append(e)

        scored: List[Tuple[str, float, int]] = []
        for symbol, items in by_symbol.items():
            if len(items) < 2:
                continue
            wins = sum(1 for e in items if is_win(e.result))
            wr = wins / len(items) * 100.0
            scored.append((symbol, wr, len(items)))

        if not scored:
            return "-", "-"

        scored.sort(key=lambda x: (x[1], x[2]), reverse=True)
        best = scored[0][0]
        worst = sorted(scored, key=lambda x: (x[1], -x[2]))[0][0]
        return best, worst


class StatsFormatter:
    """Formats short Persian reports."""

    def format_summary(self, summary: StatsSummary) -> str:
        label = "همه" if summary.source_type == "ALL" else summary.source_type
        notes = ""
        if summary.notes:
            notes = "\n" + "\n".join(f"• {n}" for n in summary.notes[:2])

        return (
            f"📊 آمار {label} - {summary.period_label}\n"
            f"کل رویدادها: {summary.total_events}\n"
            f"بازشده: {summary.opened_count} | بسته‌شده: {summary.closed_count} | Reject: {summary.reject_count}\n"
            f"✅ TP1: {summary.tp1_count} | 🎯 TP2: {summary.tp2_count} | 💰 AI Exit: {summary.ai_exit_count} | ❌ SL: {summary.sl_count}\n"
            f"WinRate: {summary.win_rate:.1f}%\n"
            f"Long WR: {summary.long_win_rate:.1f}% | Short WR: {summary.short_win_rate:.1f}%\n"
            f"PnL تاییدشده: {summary.confirmed_pnl_usdt:+.4f}$ | Avg: {summary.avg_pnl_percent:+.3f}%\n"
            f"بهترین: {summary.best_symbol} | ضعیف‌ترین: {summary.worst_symbol}"
            f"{notes}"
        )

    def format_combined(self, real: StatsSummary, ghost: StatsSummary, all_summary: StatsSummary) -> str:
        return (
            f"📊 آمار ربات - {all_summary.period_label}\n\n"
            f"REAL: WR {real.win_rate:.1f}% | TP:{real.tp1_count + real.tp2_count + real.ai_exit_count} SL:{real.sl_count} | PnL {real.confirmed_pnl_usdt:+.4f}$\n"
            f"GHOST: WR {ghost.win_rate:.1f}% | TP:{ghost.tp1_count + ghost.tp2_count + ghost.ai_exit_count} SL:{ghost.sl_count}\n"
            f"کل: WR {all_summary.win_rate:.1f}% | Reject:{all_summary.reject_count}\n"
            f"Long WR: {all_summary.long_win_rate:.1f}% | Short WR: {all_summary.short_win_rate:.1f}%\n"
            f"بهترین: {all_summary.best_symbol} | ضعیف‌ترین: {all_summary.worst_symbol}"
        )


class StatsManager:
    """Main statistics manager used by bot.py."""

    def __init__(self):
        self.builder = StatEventBuilder()
        self.store = StatsStore()
        self.aggregator = StatsAggregator()
        self.formatter = StatsFormatter()

    def record_decision(self, decision: AIDecision) -> StatEvent:
        event = self.builder.from_decision(decision)
        self.store.save(event)
        return event

    def record_position_event(self, event: PositionMonitorEvent) -> StatEvent:
        stat = self.builder.from_position_event(event)
        if stat.result != RESULT_UNKNOWN:
            self.store.save(stat)
        return stat

    def record_ghost_result(self, result: GhostMonitorResult) -> StatEvent:
        stat = self.builder.from_ghost_result(result)
        if stat.result != RESULT_UNKNOWN:
            self.store.save(stat)
        return stat

    def summary(self, days: Optional[int] = None, source_type: str = "ALL") -> StatsSummary:
        events, label = self.aggregator.filter_period(self.store.load_all(), days=days)
        return self.aggregator.summarize(events, source_type=source_type, period_label=label)

    def report(self, days: Optional[int] = None) -> str:
        events, label = self.aggregator.filter_period(self.store.load_all(), days=days)
        real = self.aggregator.summarize(events, SOURCE_REAL, label)
        ghost = self.aggregator.summarize(events, SOURCE_GHOST, label)
        all_summary = self.aggregator.summarize(events, "ALL", label)
        return self.formatter.format_combined(real, ghost, all_summary)

    def detailed_report(self, days: Optional[int] = None, source_type: str = "ALL") -> str:
        return self.formatter.format_summary(self.summary(days=days, source_type=source_type))

    def clear_stats(self) -> str:
        count = self.store.clear()
        return f"✅ آمار پاک شد\nتعداد رکورد حذف‌شده: {count}"


_default_manager: Optional[StatsManager] = None


def manager() -> StatsManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = StatsManager()
    return _default_manager


def record_decision(decision: AIDecision) -> StatEvent:
    return manager().record_decision(decision)


def record_position_event(event: PositionMonitorEvent) -> StatEvent:
    return manager().record_position_event(event)


def record_ghost_result(result: GhostMonitorResult) -> StatEvent:
    return manager().record_ghost_result(result)


def stats_report(days: Optional[int] = None) -> str:
    return manager().report(days=days)


def detailed_stats_report(days: Optional[int] = None, source_type: str = "ALL") -> str:
    return manager().detailed_report(days=days, source_type=source_type)


def clear_stats() -> str:
    return manager().clear_stats()
