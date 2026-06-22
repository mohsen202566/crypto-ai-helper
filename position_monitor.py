from __future__ import annotations

"""
23 - position_monitor.py

Continuous real-position monitor for the locked Movement Hunter architecture.

Responsibilities:
- Continuously sync actual Toobit futures positions.
- Keep internal position state aligned with real exchange state.
- Monitor TP1 / TP2 / SL / AI_EXIT events.
- Use exit_engine.py for profit protection / AI close recommendations.
- Ask real_trade_manager/tobit_client to close or repair only through safe client methods.
- Wait/retry real closed-position PnL before confirming result.
- Return structured monitor events for result_reporter.py.

Strictly forbidden:
- No AI entry decision.
- No Telegram sending.
- No Paper mode.
- No Setup flow.
- No fake PnL confirmation.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4
import math
import time

from data_store import save_position, save_error, store
from exit_engine import PositionContext, ExitDecision, evaluate_exit, position_context_from_dict
from analysis_layers import SensorSnapshot
from movement_hunter import MovementHunterResult
from trap_engine import TrapResult
from state_engine import StateResult
from coin_learning import SOURCE_REAL, learn_outcome
from meta_learning import audit_outcome
from movement_memory import record_movement_memory


JsonDict = Dict[str, Any]

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"

POSITION_OPEN = "OPEN"
POSITION_PENDING_REAL_CONFIRM = "PENDING_REAL_CONFIRM"
POSITION_CLOSED = "CLOSED"

EVENT_SYNC_OPEN = "SYNC_OPEN"
EVENT_TP1 = "TP1"
EVENT_TP2 = "TP2"
EVENT_SL = "SL"
EVENT_AI_EXIT = "AI_EXIT"
EVENT_PROTECT_SL = "PROTECT_SL"
EVENT_CLOSED_UNKNOWN = "CLOSED_UNKNOWN"
EVENT_REPAIR_TP_SL = "REPAIR_TP_SL"

PNL_PENDING = "PENDING"
PNL_CONFIRMED = "CONFIRMED"
PNL_UNAVAILABLE = "UNAVAILABLE"

TP1_STRONG_CLOSE_FRACTION = 0.75
TP1_RUNNER_FRACTION = 0.25


@dataclass(frozen=True)
class RealPositionState:
    position_id: str
    symbol: str
    exchange_symbol: str
    direction: str
    entry: float
    quantity: float
    leverage: int
    margin_usdt: float
    tp1: float
    tp2: float
    sl: float
    status: str = POSITION_OPEN
    tp1_hit: bool = False
    tp2_hit: bool = False
    ai_exit_hit: bool = False
    sl_hit: bool = False
    open_time: int = 0
    close_time: int = 0
    current_price: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    unrealized_pnl_usdt: float = 0.0
    realized_pnl_usdt: float = 0.0
    realized_pnl_percent: float = 0.0
    pnl_status: str = PNL_PENDING
    decision_id: str = ""
    signal_message_id: int = 0
    meta: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class PositionMonitorEvent:
    event_id: str
    position_id: str
    symbol: str
    direction: str
    event_type: str
    timestamp: int
    price: float
    realized_pnl_usdt: float = 0.0
    realized_pnl_percent: float = 0.0
    pnl_status: str = PNL_PENDING
    reply_to_message_id: int = 0
    should_report: bool = True
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    raw: JsonDict = field(default_factory=dict)

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


def normalize_direction(direction: str) -> str:
    d = str(direction or "").upper().strip()
    if d in {"LONG", "BUY"}:
        return DIRECTION_LONG
    if d in {"SHORT", "SELL"}:
        return DIRECTION_SHORT
    return d


def pnl_percent(direction: str, entry: float, price: float) -> float:
    entry = safe_float(entry)
    price = safe_float(price)
    if entry <= 0 or price <= 0:
        return 0.0
    if normalize_direction(direction) == DIRECTION_LONG:
        return (price - entry) / entry * 100.0
    return (entry - price) / entry * 100.0


def _call_optional(obj: Any, names: List[str], *args, **kwargs) -> Any:
    last_error: Optional[Exception] = None
    for name in names:
        fn = getattr(obj, name, None)
        if callable(fn):
            try:
                return fn(*args, **kwargs)
            except TypeError:
                try:
                    return fn(*args)
                except Exception as exc:
                    last_error = exc
            except Exception as exc:
                last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError(f"client_method_missing:{'/'.join(names)}")


class PositionStateMapper:
    """Maps exchange/open-position dicts and stored state to RealPositionState."""

    def from_exchange(self, raw: Dict[str, Any], stored: Optional[RealPositionState] = None) -> RealPositionState:
        symbol = str(raw.get("symbol", raw.get("exchange_symbol", stored.exchange_symbol if stored else "")))
        direction = normalize_direction(str(raw.get("direction", raw.get("side", stored.direction if stored else ""))))
        qty = safe_float(raw.get("quantity", raw.get("qty", raw.get("positionAmt", stored.quantity if stored else 0.0))))
        entry = safe_float(raw.get("entry_price", raw.get("entryPrice", raw.get("avgPrice", stored.entry if stored else 0.0))))
        mark = safe_float(raw.get("mark_price", raw.get("markPrice", raw.get("current_price", 0.0))))
        if mark <= 0:
            mark = entry

        position_id = str(raw.get("position_id", raw.get("id", stored.position_id if stored else f"pos_{uuid4().hex}")))

        highest = max(safe_float(stored.highest_price if stored else mark), mark)
        lowest = min(safe_float(stored.lowest_price if stored else mark), mark)

        return RealPositionState(
            position_id=position_id,
            symbol=str(raw.get("base_symbol", stored.symbol if stored else symbol)),
            exchange_symbol=symbol,
            direction=direction,
            entry=entry,
            quantity=abs(qty),
            leverage=safe_int(raw.get("leverage", raw.get("lev", stored.leverage if stored else 0))),
            margin_usdt=safe_float(raw.get("margin_usdt", raw.get("margin", stored.margin_usdt if stored else 0.0))),
            tp1=safe_float(raw.get("tp1", stored.tp1 if stored else 0.0)),
            tp2=safe_float(raw.get("tp2", stored.tp2 if stored else 0.0)),
            sl=safe_float(raw.get("sl", stored.sl if stored else 0.0)),
            status=POSITION_OPEN,
            tp1_hit=bool(stored.tp1_hit if stored else False),
            tp2_hit=bool(stored.tp2_hit if stored else False),
            ai_exit_hit=bool(stored.ai_exit_hit if stored else False),
            sl_hit=bool(stored.sl_hit if stored else False),
            open_time=safe_int(raw.get("open_time", raw.get("openTime", stored.open_time if stored else now_ts()))),
            current_price=mark,
            highest_price=highest,
            lowest_price=lowest,
            unrealized_pnl_usdt=safe_float(raw.get("unrealized_pnl", raw.get("unRealizedProfit", stored.unrealized_pnl_usdt if stored else 0.0))),
            realized_pnl_usdt=safe_float(stored.realized_pnl_usdt if stored else 0.0),
            realized_pnl_percent=safe_float(stored.realized_pnl_percent if stored else 0.0),
            pnl_status=str(stored.pnl_status if stored else PNL_PENDING),
            decision_id=str(raw.get("decision_id", stored.decision_id if stored else "")),
            signal_message_id=safe_int(raw.get("signal_message_id", stored.signal_message_id if stored else 0)),
            meta=dict(stored.meta if stored else {}),
        )

    def from_store(self, data: Any) -> RealPositionState:
        if isinstance(data, RealPositionState):
            return data
        if hasattr(data, "to_dict") and callable(data.to_dict):
            data = data.to_dict()
        if not isinstance(data, dict):
            data = {}

        return RealPositionState(
            position_id=str(data.get("position_id", data.get("trade_id", data.get("id", f"pos_{uuid4().hex}")))),
            symbol=str(data.get("symbol", "")),
            exchange_symbol=str(data.get("exchange_symbol", data.get("symbol", ""))),
            direction=normalize_direction(str(data.get("direction", ""))),
            entry=safe_float(data.get("entry", data.get("entry_price", 0.0))),
            quantity=safe_float(data.get("quantity", 0.0)),
            leverage=safe_int(data.get("leverage", 0)),
            margin_usdt=safe_float(data.get("margin_usdt", 0.0)),
            tp1=safe_float(data.get("tp1", 0.0)),
            tp2=safe_float(data.get("tp2", 0.0)),
            sl=safe_float(data.get("sl", 0.0)),
            status=str(data.get("status", POSITION_OPEN)),
            tp1_hit=bool(data.get("tp1_hit", False)),
            tp2_hit=bool(data.get("tp2_hit", False)),
            ai_exit_hit=bool(data.get("ai_exit_hit", False)),
            sl_hit=bool(data.get("sl_hit", False)),
            open_time=safe_int(data.get("open_time", data.get("created_at", now_ts()))),
            close_time=safe_int(data.get("close_time", 0)),
            current_price=safe_float(data.get("current_price", data.get("entry", 0.0))),
            highest_price=safe_float(data.get("highest_price", data.get("entry", 0.0))),
            lowest_price=safe_float(data.get("lowest_price", data.get("entry", 0.0))),
            unrealized_pnl_usdt=safe_float(data.get("unrealized_pnl_usdt", 0.0)),
            realized_pnl_usdt=safe_float(data.get("realized_pnl_usdt", 0.0)),
            realized_pnl_percent=safe_float(data.get("realized_pnl_percent", 0.0)),
            pnl_status=str(data.get("pnl_status", PNL_PENDING)),
            decision_id=str(data.get("decision_id", "")),
            signal_message_id=safe_int(data.get("signal_message_id", 0)),
            meta=dict(data.get("meta", {}) if isinstance(data.get("meta", {}), dict) else {}),
        )


class PositionEventDetector:
    """Detects TP1/TP2/SL from price and real position state."""

    def detect(self, pos: RealPositionState) -> List[str]:
        events: List[str] = []
        direction = normalize_direction(pos.direction)
        price = safe_float(pos.current_price)

        if price <= 0 or pos.entry <= 0:
            return events

        if direction == DIRECTION_LONG:
            if pos.tp1 > 0 and not pos.tp1_hit and price >= pos.tp1:
                events.append(EVENT_TP1)
            if pos.tp2 > 0 and not pos.tp2_hit and price >= pos.tp2:
                events.append(EVENT_TP2)
            if pos.sl > 0 and not pos.sl_hit and price <= pos.sl:
                events.append(EVENT_SL)
        else:
            if pos.tp1 > 0 and not pos.tp1_hit and price <= pos.tp1:
                events.append(EVENT_TP1)
            if pos.tp2 > 0 and not pos.tp2_hit and price <= pos.tp2:
                events.append(EVENT_TP2)
            if pos.sl > 0 and not pos.sl_hit and price >= pos.sl:
                events.append(EVENT_SL)

        return events


class RealPnLResolver:
    """Waits/retries Toobit closed-position PnL; never treats 0 as confirmed by default."""

    def resolve(self, client: Any, symbol: str, start_time_ms: Optional[int] = None, attempts: int = 10, sleep_seconds: float = 5.0) -> Tuple[float, str, JsonDict]:
        last: JsonDict = {}
        for _ in range(max(1, attempts)):
            try:
                result = _call_optional(
                    client,
                    ["wait_for_closed_position_pnl", "get_closed_position_pnl"],
                    symbol,
                    start_time_ms,
                )
                if hasattr(result, "to_dict") and callable(result.to_dict):
                    result = result.to_dict()
                if not isinstance(result, dict):
                    result = {"raw": result}
                last = result
                rows = result.get("rows", [])
                confirmed = bool(result.get("confirmed", False)) or bool(rows)
                pnl = safe_float(result.get("realized_pnl", result.get("pnl", 0.0)))
                if confirmed:
                    return pnl, PNL_CONFIRMED, result
            except Exception as exc:
                last = {"error": str(exc)}
            time.sleep(sleep_seconds)
        return 0.0, PNL_UNAVAILABLE, last


class PositionMonitor:
    """
    Main real-position monitor.

    bot.py or a scheduler calls:
        sync_once()
        monitor_position()
    and sends returned events to result_reporter.py.
    """

    def __init__(self, client: Any):
        self.client = client
        self.mapper = PositionStateMapper()
        self.detector = PositionEventDetector()
        self.pnl_resolver = RealPnLResolver()

    def load_stored_positions(self) -> List[RealPositionState]:
        records = store().section("positions")
        positions: List[RealPositionState] = []
        for item in records.values():
            try:
                pos = self.mapper.from_store(item)
                if pos.status not in {POSITION_CLOSED, "TP2", "SL", "AI_EXIT"}:
                    positions.append(pos)
            except Exception:
                continue
        return positions

    def fetch_exchange_positions(self) -> List[JsonDict]:
        try:
            positions = _call_optional(self.client, ["get_open_positions", "fetch_open_positions"])
            if hasattr(positions, "to_dict") and callable(positions.to_dict):
                positions = positions.to_dict()
            if isinstance(positions, dict):
                positions = positions.get("positions", positions.get("data", []))
            if not isinstance(positions, list):
                return []
            return [p for p in positions if isinstance(p, dict)]
        except Exception as exc:
            save_error("position_monitor_fetch", str(exc), {})
            return []

    def sync_once(self) -> List[PositionMonitorEvent]:
        events: List[PositionMonitorEvent] = []
        stored = {p.exchange_symbol: p for p in self.load_stored_positions()}
        exchange_positions = self.fetch_exchange_positions()
        seen: set[str] = set()

        for raw in exchange_positions:
            exchange_symbol = str(raw.get("symbol", raw.get("contract", "")))
            old = stored.get(exchange_symbol)
            pos = self.mapper.from_exchange(raw, stored=old)
            seen.add(exchange_symbol)
            save_position(pos.position_id, pos.to_dict())

            if old is None:
                events.append(self._event(pos, EVENT_SYNC_OPEN, pos.current_price, reason_codes=("EXCHANGE_POSITION_IMPORTED",), should_report=False))

        # Anything stored but not on exchange may have closed.
        for exchange_symbol, pos in stored.items():
            if exchange_symbol not in seen and pos.status == POSITION_OPEN:
                closed = RealPositionState(**{**pos.to_dict(), "status": POSITION_CLOSED, "close_time": now_ts()})
                save_position(closed.position_id, closed.to_dict())
                events.append(self._event(closed, EVENT_CLOSED_UNKNOWN, closed.current_price, reason_codes=("POSITION_MISSING_ON_EXCHANGE",), should_report=True))

        return events


    def _tp_mode(self, pos: RealPositionState) -> str:
        try:
            plan = pos.meta.get("tp_sl_plan", {}) if isinstance(pos.meta, dict) else {}
            return str(plan.get("tp_mode", "")).upper()
        except Exception:
            return ""

    def _has_tp2_runner(self, pos: RealPositionState) -> bool:
        return bool(self._tp_mode(pos) == "TP1_TP2" and pos.tp2 > 0 and pos.quantity > 0)

    def _estimate_pnl_usdt(self, pos: RealPositionState, price: float, quantity_fraction: float = 1.0) -> float:
        pct = pnl_percent(pos.direction, pos.entry, price)
        base_margin = safe_float(pos.margin_usdt) * max(0.0, min(1.0, quantity_fraction))
        return base_margin * safe_float(pos.leverage, 1.0) * pct / 100.0

    def _tp1_protected_sl(self, pos: RealPositionState) -> float:
        """
        Protect the TP2 runner after TP1 without choking it.

        Old behavior moved SL exactly to TP1, which can close the runner on a
        normal retest/liquidity wick. This keeps profit protected, but gives the
        remaining 25% runner a little breathing room toward TP2.
        """
        entry = safe_float(pos.entry)
        tp1 = safe_float(pos.tp1)
        tp2 = safe_float(pos.tp2)
        direction = normalize_direction(pos.direction)

        if entry <= 0:
            return 0.0
        if tp1 <= 0:
            return entry

        # If TP2 exists, protect slightly behind TP1 by 15% of TP1→TP2 distance.
        # Never move protection beyond entry in the wrong direction.
        if tp2 > 0 and abs(tp2 - tp1) > 0:
            buffer_dist = abs(tp2 - tp1) * 0.15
            if direction == DIRECTION_LONG:
                return max(entry, tp1 - buffer_dist)
            if direction == DIRECTION_SHORT:
                return min(entry, tp1 + buffer_dist)

        return tp1


    def monitor_position(
        self,
        pos: RealPositionState,
        snapshot: Optional[SensorSnapshot] = None,
        movement: Optional[MovementHunterResult] = None,
        trap: Optional[TrapResult] = None,
        state: Optional[StateResult] = None,
    ) -> List[PositionMonitorEvent]:
        events: List[PositionMonitorEvent] = []
        detected = self.detector.detect(pos)

        updated = pos

        for event_type in detected:
            if event_type == EVENT_TP1 and not updated.tp1_hit:
                # Save-profit plan:
                # - Normal signals: close 100% at TP1.
                # - Strong TP1_TP2 signals: close 75% at TP1, keep 25% runner for TP2.
                if self._has_tp2_runner(updated):
                    original_qty = safe_float(updated.quantity)
                    close_qty = max(0.0, original_qty * TP1_STRONG_CLOSE_FRACTION)
                    runner_qty = max(0.0, original_qty - close_qty)
                    close_raw = self._close_position(updated, quantity=close_qty)
                    protected_sl = self._tp1_protected_sl(updated)

                    meta = dict(updated.meta or {})
                    meta.update({
                        "tp1_profit_locked": True,
                        "tp1_close_fraction": TP1_STRONG_CLOSE_FRACTION,
                        "tp1_closed_quantity": close_qty,
                        "runner_fraction": TP1_RUNNER_FRACTION,
                        "runner_quantity": runner_qty,
                        "protected_sl_after_tp1": protected_sl,
                    })

                    updated = RealPositionState(**{
                        **updated.to_dict(),
                        "tp1_hit": True,
                        "quantity": runner_qty,
                        "sl": protected_sl,
                        "meta": meta,
                    })

                    self._repair_protected_sl(updated, protected_sl)
                    pnl_est = self._estimate_pnl_usdt(updated, updated.current_price, TP1_STRONG_CLOSE_FRACTION)
                    pnl_pct = pnl_percent(updated.direction, updated.entry, updated.current_price)
                    self._learn_real_outcome(updated, EVENT_TP1, updated.current_price, snapshot, movement, trap, state, pnl_est, pnl_pct)
                    events.append(self._event(
                        updated,
                        EVENT_TP1,
                        updated.current_price,
                        pnl_est,
                        pnl_pct,
                        PNL_PENDING,
                        reason_codes=("TP1_PRICE_REACHED", "TP1_PARTIAL_75_PERCENT_CLOSED", "RUNNER_25_PERCENT_TO_TP2", "SL_MOVED_TO_TP1_PROFIT_LOCK"),
                        raw={"partial_close": close_raw, "closed_quantity": close_qty, "runner_quantity": runner_qty, "protected_sl": protected_sl},
                    ))
                else:
                    close_raw = self._close_position(updated, quantity=updated.quantity)
                    closed = RealPositionState(**{**updated.to_dict(), "tp1_hit": True, "status": POSITION_CLOSED, "close_time": now_ts()})
                    pnl, pnl_status, pnl_raw = self._resolve_closed_pnl(closed)
                    if pnl_status != PNL_CONFIRMED:
                        pnl = self._estimate_pnl_usdt(closed, closed.current_price, 1.0)
                    closed = RealPositionState(**{
                        **closed.to_dict(),
                        "realized_pnl_usdt": pnl,
                        "realized_pnl_percent": pnl_percent(closed.direction, closed.entry, closed.current_price),
                        "pnl_status": pnl_status,
                    })
                    self._learn_real_outcome(closed, EVENT_TP1, closed.current_price, snapshot, movement, trap, state, pnl, closed.realized_pnl_percent)
                    save_position(closed.position_id, closed.to_dict())
                    events.append(self._event(
                        closed,
                        EVENT_TP1,
                        closed.current_price,
                        pnl,
                        closed.realized_pnl_percent,
                        pnl_status,
                        reason_codes=("TP1_PRICE_REACHED", "TP1_ONLY_FULL_CLOSE"),
                        raw={"close": close_raw, "pnl": pnl_raw},
                    ))
                    updated = closed
            elif event_type == EVENT_TP2 and not updated.tp2_hit:
                updated = RealPositionState(**{**updated.to_dict(), "tp2_hit": True, "status": POSITION_CLOSED, "close_time": now_ts()})
                pnl, pnl_status, raw = self._resolve_closed_pnl(updated)
                updated = RealPositionState(**{**updated.to_dict(), "realized_pnl_usdt": pnl, "realized_pnl_percent": pnl_percent(updated.direction, updated.entry, updated.current_price), "pnl_status": pnl_status})
                self._learn_real_outcome(updated, EVENT_TP2, updated.current_price, snapshot, movement, trap, state, pnl, updated.realized_pnl_percent)
                events.append(self._event(updated, EVENT_TP2, updated.current_price, pnl, updated.realized_pnl_percent, pnl_status, ("TP2_PRICE_REACHED",), raw=raw))
            elif event_type == EVENT_SL and not updated.sl_hit:
                updated = RealPositionState(**{**updated.to_dict(), "sl_hit": True, "status": POSITION_CLOSED, "close_time": now_ts()})
                pnl, pnl_status, raw = self._resolve_closed_pnl(updated)
                updated = RealPositionState(**{**updated.to_dict(), "realized_pnl_usdt": pnl, "realized_pnl_percent": pnl_percent(updated.direction, updated.entry, updated.current_price), "pnl_status": pnl_status})
                self._learn_real_outcome(updated, EVENT_SL, updated.current_price, snapshot, movement, trap, state, pnl, updated.realized_pnl_percent)
                events.append(self._event(updated, EVENT_SL, updated.current_price, pnl, updated.realized_pnl_percent, pnl_status, ("SL_PRICE_REACHED",), raw=raw))

        # AI exit/profit protection only if all analysis objects are provided.
        if snapshot and movement and trap and state and updated.status == POSITION_OPEN:
            ctx = self._to_exit_context(updated)
            exit_decision = evaluate_exit(ctx=ctx, snapshot=snapshot, movement=movement, trap=trap, state=state)
            if exit_decision.should_move_sl_to_protect and exit_decision.protected_sl > 0:
                self._repair_protected_sl(updated, exit_decision.protected_sl)
                events.append(self._event(updated, EVENT_PROTECT_SL, updated.current_price, reason_codes=exit_decision.reason_codes, warnings=exit_decision.warnings, should_report=False))
            if exit_decision.should_close:
                close_raw = self._close_position(updated)
                closed = RealPositionState(**{**updated.to_dict(), "ai_exit_hit": True, "status": POSITION_CLOSED, "close_time": now_ts()})
                pnl, pnl_status, pnl_raw = self._resolve_closed_pnl(closed)
                closed = RealPositionState(**{**closed.to_dict(), "realized_pnl_usdt": pnl, "realized_pnl_percent": pnl_percent(closed.direction, closed.entry, closed.current_price), "pnl_status": pnl_status})
                self._learn_real_outcome(closed, EVENT_AI_EXIT, closed.current_price, snapshot, movement, trap, state, pnl, closed.realized_pnl_percent)
                save_position(closed.position_id, closed.to_dict())
                events.append(self._event(closed, EVENT_AI_EXIT, closed.current_price, pnl, closed.realized_pnl_percent, pnl_status, exit_decision.reason_codes, exit_decision.warnings, raw={"close": close_raw, "pnl": pnl_raw}))

        save_position(updated.position_id, updated.to_dict())
        return events

    def monitor_all(
        self,
        analysis_provider: Optional[Callable[[RealPositionState], Tuple[SensorSnapshot, MovementHunterResult, TrapResult, StateResult]]] = None,
    ) -> List[PositionMonitorEvent]:
        events = self.sync_once()
        for pos in self.load_stored_positions():
            try:
                if analysis_provider:
                    snapshot, movement, trap, state = analysis_provider(pos)
                    events.extend(self.monitor_position(pos, snapshot=snapshot, movement=movement, trap=trap, state=state))
                else:
                    events.extend(self.monitor_position(pos))
            except Exception as exc:
                save_error("position_monitor_position", str(exc), pos.to_dict())
        return events


    def _coerce_saved_candidate(self, pos: RealPositionState, snapshot: Optional[SensorSnapshot]) -> Any:
        """
        Recover the original AnalysisCandidate for REAL learning.

        Priority:
        1) pos.meta["candidate"] saved at entry time
        2) nested common meta locations
        3) candidate object already embedded in meta

        If the exact candidate is not available, returns None and records an
        error instead of silently skipping learning.
        """
        meta = pos.meta if isinstance(pos.meta, dict) else {}
        candidates = [
            meta.get("candidate"),
            meta.get("analysis_candidate"),
            meta.get("entry_candidate"),
        ]

        decision_meta = meta.get("decision") if isinstance(meta.get("decision"), dict) else {}
        candidates.extend([
            decision_meta.get("candidate"),
            decision_meta.get("analysis_candidate"),
        ])

        tp_plan = meta.get("tp_sl_plan") if isinstance(meta.get("tp_sl_plan"), dict) else {}
        plan_meta = tp_plan.get("meta") if isinstance(tp_plan.get("meta"), dict) else {}
        candidates.extend([
            plan_meta.get("candidate"),
            plan_meta.get("analysis_candidate"),
        ])

        for saved_candidate in candidates:
            if not saved_candidate:
                continue
            try:
                from analysis_engine import AnalysisCandidate
                if isinstance(saved_candidate, AnalysisCandidate):
                    return saved_candidate
                if hasattr(AnalysisCandidate, "from_dict") and callable(getattr(AnalysisCandidate, "from_dict")):
                    return AnalysisCandidate.from_dict(saved_candidate)
                if isinstance(saved_candidate, dict):
                    return AnalysisCandidate(**saved_candidate)
            except Exception as exc:
                try:
                    save_error("position_monitor_candidate_recover", str(exc), {
                        "position_id": pos.position_id,
                        "symbol": pos.symbol,
                        "candidate_keys": list(saved_candidate.keys()) if isinstance(saved_candidate, dict) else str(type(saved_candidate)),
                    })
                except Exception:
                    pass

        try:
            save_error("position_monitor_missing_candidate_for_learning", "candidate_not_found_in_position_meta", {
                "position_id": pos.position_id,
                "symbol": pos.symbol,
                "meta_keys": list(meta.keys()),
                "has_snapshot": snapshot is not None,
            })
        except Exception:
            pass
        return None


    def _learn_real_outcome(
        self,
        pos: RealPositionState,
        event_type: str,
        price: float,
        snapshot: Optional[SensorSnapshot],
        movement: Optional[MovementHunterResult],
        trap: Optional[TrapResult],
        state: Optional[StateResult],
        pnl_usdt: float,
        pnl_percent_value: float,
    ) -> None:
        """
        Feed REAL outcomes back into AI learning.

        This uses the latest provided monitoring context. If full original
        candidate context is not available, it safely skips instead of creating
        fake learning data. bot.py/analysis_provider should pass fresh sensor
        objects for strong learning.
        """
        if snapshot is None or movement is None or trap is None or state is None:
            return

        candidate = self._coerce_saved_candidate(pos, snapshot)

        if candidate is None:
            # Learning requires the original candidate context. Do not create fake
            # learning data, but do record why REAL learning was skipped.
            return

        result = event_type
        try:
            learn_outcome(
                source_type=SOURCE_REAL,
                candidate=candidate,
                result=result,
                movement=movement,
                trap=trap,
                state=state,
                confidence=None,
                entry_price=pos.entry,
                exit_price=price,
                realized_pnl=pnl_usdt,
                realized_pnl_percent=pnl_percent_value,
                mfe_percent=max(0.0, pnl_percent(pos.direction, pos.entry, pos.highest_price if pos.direction == DIRECTION_LONG else pos.lowest_price)),
                mae_percent=max(0.0, -pnl_percent(pos.direction, pos.entry, pos.lowest_price if pos.direction == DIRECTION_LONG else pos.highest_price)),
                holding_seconds=max(0, (pos.close_time or now_ts()) - pos.open_time),
                meta={
                    "source_type": SOURCE_REAL,
                    "position_id": pos.position_id,
                    "decision_id": pos.decision_id,
                    "event_type": event_type,
                    "realized_pnl_usdt": pnl_usdt,
                },
                persist=True,
            )
        except Exception:
            pass

        try:
            audit_outcome(
                source_type=SOURCE_REAL,
                result=result,
                candidate=candidate,
                movement=movement,
                trap=trap,
                state=state,
                confidence=None,
                correlation=None,
                prediction=None,
                persist=True,
            )
        except Exception:
            pass

        try:
            record_movement_memory(
                candidate=candidate,
                after_price=price,
                move_duration_seconds=max(0, (pos.close_time or now_ts()) - pos.open_time),
                movement=movement,
                trap=trap,
                state=state,
                confidence=None,
                mfe_percent=max(0.0, pnl_percent(pos.direction, pos.entry, pos.highest_price if pos.direction == DIRECTION_LONG else pos.lowest_price)),
                mae_percent=max(0.0, -pnl_percent(pos.direction, pos.entry, pos.lowest_price if pos.direction == DIRECTION_LONG else pos.highest_price)),
                meta={
                    "source_type": SOURCE_REAL,
                    "position_id": pos.position_id,
                    "decision_id": pos.decision_id,
                    "result": result,
                },
                persist=True,
            )
        except Exception:
            pass


    def _to_exit_context(self, pos: RealPositionState) -> PositionContext:
        return PositionContext(
            position_id=pos.position_id,
            symbol=pos.symbol,
            direction=pos.direction,
            entry=pos.entry,
            current_price=pos.current_price,
            tp1=pos.tp1,
            tp2=pos.tp2,
            sl=pos.sl,
            tp1_hit=pos.tp1_hit,
            tp2_hit=pos.tp2_hit,
            open_time=pos.open_time,
            last_update=now_ts(),
            highest_price=pos.highest_price,
            lowest_price=pos.lowest_price,
            unrealized_pnl_percent=pnl_percent(pos.direction, pos.entry, pos.current_price),
            unrealized_pnl_usdt=pos.unrealized_pnl_usdt,
        )

    def _repair_protected_sl(self, pos: RealPositionState, protected_sl: float) -> None:
        try:
            _call_optional(self.client, ["set_position_tp_sl", "ensure_tp_sl", "repair_tp_sl"], pos.exchange_symbol, pos.direction, pos.tp1, pos.tp2, protected_sl)
        except Exception as exc:
            save_error("position_monitor_protect_sl", str(exc), {"position": pos.to_dict(), "protected_sl": protected_sl})

    def _close_position(self, pos: RealPositionState, quantity: Optional[float] = None) -> JsonDict:
        try:
            qty = safe_float(quantity, pos.quantity)
            if qty <= 0:
                qty = pos.quantity
            result = _call_optional(self.client, ["close_position"], pos.exchange_symbol, pos.direction, qty)
            if hasattr(result, "to_dict") and callable(result.to_dict):
                result = result.to_dict()
            return result if isinstance(result, dict) else {"raw": result}
        except Exception as exc:
            save_error("position_monitor_ai_close", str(exc), {**pos.to_dict(), "close_quantity": quantity})
            return {"error": str(exc)}

    def _resolve_closed_pnl(self, pos: RealPositionState) -> Tuple[float, str, JsonDict]:
        start_ms = pos.open_time * 1000 if pos.open_time and pos.open_time < 10_000_000_000 else pos.open_time
        return self.pnl_resolver.resolve(
            self.client,
            pos.exchange_symbol,
            start_time_ms=start_ms,
            attempts=10,
            sleep_seconds=5.0,
        )

    def _event(
        self,
        pos: RealPositionState,
        event_type: str,
        price: float,
        pnl_usdt: float = 0.0,
        pnl_percent_value: float = 0.0,
        pnl_status: str = PNL_PENDING,
        reason_codes: Sequence[str] = (),
        warnings: Sequence[str] = (),
        raw: Optional[JsonDict] = None,
        should_report: bool = True,
    ) -> PositionMonitorEvent:
        return PositionMonitorEvent(
            event_id=f"evt_{uuid4().hex}",
            position_id=pos.position_id,
            symbol=pos.symbol,
            direction=pos.direction,
            event_type=event_type,
            timestamp=now_ts(),
            price=safe_float(price),
            realized_pnl_usdt=safe_float(pnl_usdt),
            realized_pnl_percent=safe_float(pnl_percent_value),
            pnl_status=pnl_status,
            reply_to_message_id=pos.signal_message_id,
            should_report=should_report,
            reason_codes=tuple(reason_codes),
            warnings=tuple(warnings),
            raw=dict(raw or {}),
        )


def create_monitor(client: Any) -> PositionMonitor:
    return PositionMonitor(client)


def monitor_all_positions(
    client: Any,
    analysis_provider: Optional[Callable[[RealPositionState], Tuple[SensorSnapshot, MovementHunterResult, TrapResult, StateResult]]] = None,
) -> List[PositionMonitorEvent]:
    return PositionMonitor(client).monitor_all(analysis_provider=analysis_provider)
