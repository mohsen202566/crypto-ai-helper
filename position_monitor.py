from __future__ import annotations

"""
23 - position_monitor.py

Light real-position monitor for the simplified Level 1 / 5M crypto futures bot.

Locked goals:
- Monitor real Toobit positions.
- Detect TP1 / TP2 / SL.
- Use exit_engine.py for simple smart exit.
- AI exit before TP1 is already locked in exit_engine.py:
  only after 70% path to TP1.
- Learn REAL outcomes through coin_learning.py and movement_memory.py.
- Return events for result_reporter.py; no Telegram sending here.
- Preserve reply_to_message_id for result replies.
- No AI entry decision.
- No paper/setup flow.
- No fake confirmed PnL.
- No trap/state/confidence/meta/correlation/movement_hunter dependency.

This file monitors and returns structured events only.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import uuid4
import math
import time

from data_store import save_position, save_error, store
from exit_engine import PositionContext, ExitDecision, evaluate_exit
from analysis_layers import SensorSnapshot
from analysis_engine import AnalysisCandidate
from coin_learning import SOURCE_REAL, learn_outcome
from movement_memory import record_movement_memory


JsonDict = Dict[str, Any]

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"

POSITION_OPEN = "OPEN"
POSITION_CLOSED = "CLOSED"

EVENT_SYNC_OPEN = "SYNC_OPEN"
EVENT_TP1 = "TP1"
EVENT_TP2 = "TP2"
EVENT_SL = "SL"
EVENT_AI_EXIT = "AI_EXIT"
EVENT_PROTECT_SL = "PROTECT_SL"
EVENT_CLOSED_UNKNOWN = "CLOSED_UNKNOWN"

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
        if value is None:
            return default
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


def normalize_symbol_key(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    if not raw:
        return ""
    raw = raw.replace("/", "").replace("_", "-")
    if raw.endswith("-SWAP-USDT"):
        return raw.replace("-SWAP-USDT", "USDT").replace("-", "")
    if raw.endswith("-SWAP-USDC"):
        return raw.replace("-SWAP-USDC", "USDC").replace("-", "")
    return raw.replace("-", "").replace("SWAP", "")


def symbol_match(a: str, b: str) -> bool:
    ka = normalize_symbol_key(a)
    kb = normalize_symbol_key(b)
    return bool(ka and kb and ka == kb)


def position_symbol_keys(pos: Any) -> set[str]:
    values: List[Any] = []
    if isinstance(pos, RealPositionState):
        values = [pos.symbol, pos.exchange_symbol]
    elif isinstance(pos, dict):
        values = [
            pos.get("symbol"),
            pos.get("exchange_symbol"),
            pos.get("base_symbol"),
            pos.get("contract"),
            pos.get("contractCode"),
            pos.get("instId"),
        ]

    keys: set[str] = set()
    for value in values:
        raw = str(value or "").upper().strip()
        key = normalize_symbol_key(raw)
        if raw:
            keys.add(raw)
        if key:
            keys.add(key)
    return keys


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
    def from_exchange(self, raw: Dict[str, Any], stored: Optional[RealPositionState] = None) -> RealPositionState:
        symbol = str(raw.get("symbol", raw.get("exchange_symbol", stored.exchange_symbol if stored else "")))
        direction = normalize_direction(str(raw.get("direction", raw.get("side", stored.direction if stored else ""))))
        qty = safe_float(raw.get("quantity", raw.get("qty", raw.get("positionAmt", stored.quantity if stored else 0.0))))
        entry = safe_float(raw.get("entry_price", raw.get("entryPrice", raw.get("avgPrice", stored.entry if stored else 0.0))))
        mark = safe_float(raw.get("mark_price", raw.get("markPrice", raw.get("current_price", stored.current_price if stored else 0.0))))
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
            meta=dict(data.get("meta", {}) if isinstance(data.get("meta", {}) , dict) else {}),
        )


class PositionEventDetector:
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
        elif direction == DIRECTION_SHORT:
            if pos.tp1 > 0 and not pos.tp1_hit and price <= pos.tp1:
                events.append(EVENT_TP1)
            if pos.tp2 > 0 and not pos.tp2_hit and price <= pos.tp2:
                events.append(EVENT_TP2)
            if pos.sl > 0 and not pos.sl_hit and price >= pos.sl:
                events.append(EVENT_SL)

        return events


class RealPnLResolver:
    def resolve(self, client: Any, symbol: str, start_time_ms: Optional[int] = None, attempts: int = 2, sleep_seconds: float = 0.25) -> Tuple[float, str, JsonDict]:
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
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        return 0.0, PNL_UNAVAILABLE, last


class PositionMonitor:
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
                if pos.status != POSITION_CLOSED:
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
        stored_positions = self.load_stored_positions()
        stored_by_key: Dict[str, RealPositionState] = {}

        for pos in stored_positions:
            for key in position_symbol_keys(pos):
                stored_by_key.setdefault(key, pos)

        exchange_positions = self.fetch_exchange_positions()
        seen_keys: set[str] = set()
        seen_position_ids: set[str] = set()

        for raw in exchange_positions:
            raw_keys = position_symbol_keys(raw)
            exchange_symbol = str(raw.get("symbol", raw.get("contract", raw.get("contractCode", raw.get("instId", "")))))
            if exchange_symbol:
                raw_keys.add(exchange_symbol.upper().strip())
                raw_keys.add(normalize_symbol_key(exchange_symbol))

            old: Optional[RealPositionState] = None
            for key in raw_keys:
                if key in stored_by_key:
                    old = stored_by_key[key]
                    break

            pos = self.mapper.from_exchange(raw, stored=old)

            if old is not None:
                pos = RealPositionState(**{
                    **pos.to_dict(),
                    "position_id": old.position_id,
                    "symbol": old.symbol or pos.symbol,
                    "exchange_symbol": pos.exchange_symbol or old.exchange_symbol,
                    "tp1": old.tp1 if old.tp1 > 0 else pos.tp1,
                    "tp2": old.tp2 if old.tp2 > 0 else pos.tp2,
                    "sl": old.sl if old.sl > 0 else pos.sl,
                    "signal_message_id": old.signal_message_id or pos.signal_message_id,
                    "decision_id": old.decision_id or pos.decision_id,
                    "meta": old.meta if old.meta else pos.meta,
                    "tp1_hit": old.tp1_hit,
                    "tp2_hit": old.tp2_hit,
                    "ai_exit_hit": old.ai_exit_hit,
                    "sl_hit": old.sl_hit,
                })

            for key in position_symbol_keys(pos) | raw_keys:
                if key:
                    seen_keys.add(key)
            seen_position_ids.add(pos.position_id)

            save_position(pos.position_id, pos.to_dict())

            if old is None:
                events.append(self._event(pos, EVENT_SYNC_OPEN, pos.current_price, reason_codes=("EXCHANGE_POSITION_IMPORTED",), should_report=False))

        for pos in stored_positions:
            matched_by_symbol = bool(position_symbol_keys(pos) & seen_keys)
            matched_by_id = bool(pos.position_id and pos.position_id in seen_position_ids)
            if not matched_by_symbol and not matched_by_id and pos.status == POSITION_OPEN:
                closed = RealPositionState(**{**pos.to_dict(), "status": POSITION_CLOSED, "close_time": now_ts()})
                pnl, pnl_status, pnl_raw = self._resolve_closed_pnl(closed)
                closed = RealPositionState(**{
                    **closed.to_dict(),
                    "realized_pnl_usdt": pnl,
                    "realized_pnl_percent": pnl_percent(closed.direction, closed.entry, closed.current_price),
                    "pnl_status": pnl_status,
                })
                save_position(closed.position_id, closed.to_dict())
                events.append(self._event(
                    closed,
                    EVENT_CLOSED_UNKNOWN,
                    closed.current_price,
                    pnl,
                    closed.realized_pnl_percent,
                    pnl_status,
                    reason_codes=("POSITION_MISSING_ON_EXCHANGE", "SYMBOL_MATCH_NORMALIZED"),
                    raw={"pnl": pnl_raw},
                    should_report=True,
                ))

        return events

    def monitor_position(
        self,
        pos: RealPositionState,
        snapshot: Optional[SensorSnapshot] = None,
    ) -> List[PositionMonitorEvent]:
        events: List[PositionMonitorEvent] = []
        detected = self.detector.detect(pos)
        updated = pos

        for event_type in detected:
            if event_type == EVENT_TP1 and not updated.tp1_hit:
                events.extend(self._handle_tp1(updated, snapshot))
                updated = self.mapper.from_store(events[-1].raw.get("position", updated.to_dict())) if events and events[-1].raw.get("position") else updated
                if events and events[-1].event_type == EVENT_TP1 and events[-1].should_report:
                    return events

            elif event_type == EVENT_TP2 and not updated.tp2_hit:
                closed = RealPositionState(**{**updated.to_dict(), "tp2_hit": True, "status": POSITION_CLOSED, "close_time": now_ts()})
                pnl, pnl_status, raw = self._resolve_closed_pnl(closed)
                if pnl_status != PNL_CONFIRMED:
                    pnl = self._estimate_pnl_usdt(closed, closed.current_price, 1.0)
                closed = RealPositionState(**{
                    **closed.to_dict(),
                    "realized_pnl_usdt": pnl,
                    "realized_pnl_percent": pnl_percent(closed.direction, closed.entry, closed.current_price),
                    "pnl_status": pnl_status,
                })
                self._learn_real_outcome(closed, EVENT_TP2, closed.current_price, snapshot, pnl, closed.realized_pnl_percent)
                save_position(closed.position_id, closed.to_dict())
                events.append(self._event(closed, EVENT_TP2, closed.current_price, pnl, closed.realized_pnl_percent, pnl_status, ("TP2_PRICE_REACHED",), raw={"pnl": raw, "position": closed.to_dict()}))
                return events

            elif event_type == EVENT_SL and not updated.sl_hit:
                closed = RealPositionState(**{**updated.to_dict(), "sl_hit": True, "status": POSITION_CLOSED, "close_time": now_ts()})
                pnl, pnl_status, raw = self._resolve_closed_pnl(closed)
                if pnl_status != PNL_CONFIRMED:
                    pnl = self._estimate_pnl_usdt(closed, closed.current_price, 1.0)
                closed = RealPositionState(**{
                    **closed.to_dict(),
                    "realized_pnl_usdt": pnl,
                    "realized_pnl_percent": pnl_percent(closed.direction, closed.entry, closed.current_price),
                    "pnl_status": pnl_status,
                })
                self._learn_real_outcome(closed, EVENT_SL, closed.current_price, snapshot, pnl, closed.realized_pnl_percent)
                save_position(closed.position_id, closed.to_dict())
                events.append(self._event(closed, EVENT_SL, closed.current_price, pnl, closed.realized_pnl_percent, pnl_status, ("SL_PRICE_REACHED",), raw={"pnl": raw, "position": closed.to_dict()}))
                return events

        if snapshot and updated.status == POSITION_OPEN:
            ctx = self._to_exit_context(updated)
            exit_decision = evaluate_exit(ctx=ctx, snapshot=snapshot)

            if exit_decision.should_move_sl_to_protect and exit_decision.protected_sl > 0:
                protected = RealPositionState(**{**updated.to_dict(), "sl": exit_decision.protected_sl})
                self._repair_protected_sl(protected, exit_decision.protected_sl)
                save_position(protected.position_id, protected.to_dict())
                events.append(self._event(
                    protected,
                    EVENT_PROTECT_SL,
                    protected.current_price,
                    reason_codes=exit_decision.reason_codes,
                    warnings=exit_decision.warnings,
                    raw={"position": protected.to_dict(), "exit_decision": exit_decision.to_dict()},
                    should_report=False,
                ))
                updated = protected

            if exit_decision.should_close:
                close_raw = self._close_position_verified(updated)
                if not self._close_order_ok(close_raw) or close_raw.get("closed_confirmed") is False:
                    save_error("position_monitor_ai_exit_close_failed", str(close_raw), updated.to_dict())
                    events.append(self._event(
                        updated,
                        EVENT_AI_EXIT,
                        updated.current_price,
                        reason_codes=exit_decision.reason_codes,
                        warnings=tuple(list(exit_decision.warnings) + ["AI_EXIT_NOT_MARKED_BECAUSE_CLOSE_NOT_CONFIRMED"]),
                        raw={"close": close_raw, "exit_decision": exit_decision.to_dict()},
                        should_report=False,
                    ))
                    save_position(updated.position_id, updated.to_dict())
                    return events

                closed = RealPositionState(**{**updated.to_dict(), "ai_exit_hit": True, "status": POSITION_CLOSED, "close_time": now_ts()})
                pnl, pnl_status, pnl_raw = self._resolve_closed_pnl(closed)
                if pnl_status != PNL_CONFIRMED:
                    pnl = self._estimate_pnl_usdt(closed, closed.current_price, 1.0)
                closed = RealPositionState(**{
                    **closed.to_dict(),
                    "realized_pnl_usdt": pnl,
                    "realized_pnl_percent": pnl_percent(closed.direction, closed.entry, closed.current_price),
                    "pnl_status": pnl_status,
                })
                self._learn_real_outcome(closed, EVENT_AI_EXIT, closed.current_price, snapshot, pnl, closed.realized_pnl_percent)
                save_position(closed.position_id, closed.to_dict())
                events.append(self._event(
                    closed,
                    EVENT_AI_EXIT,
                    closed.current_price,
                    pnl,
                    closed.realized_pnl_percent,
                    pnl_status,
                    reason_codes=tuple(list(exit_decision.reason_codes) + ["AI_EXIT_CONFIRMED_BY_EXIT_ENGINE"]),
                    warnings=exit_decision.warnings,
                    raw={"close": close_raw, "pnl": pnl_raw, "exit_decision": exit_decision.to_dict(), "position": closed.to_dict()},
                ))
                return events

        save_position(updated.position_id, updated.to_dict())
        return events

    def monitor_all(
        self,
        analysis_provider: Optional[Callable[[RealPositionState], SensorSnapshot]] = None,
    ) -> List[PositionMonitorEvent]:
        events = self.sync_once()
        for pos in self.load_stored_positions():
            try:
                snapshot = analysis_provider(pos) if analysis_provider else None
                events.extend(self.monitor_position(pos, snapshot=snapshot))
            except Exception as exc:
                save_error("position_monitor_position", str(exc), pos.to_dict())
        return events

    def _handle_tp1(self, pos: RealPositionState, snapshot: Optional[SensorSnapshot]) -> List[PositionMonitorEvent]:
        events: List[PositionMonitorEvent] = []

        if self._has_tp2_runner(pos):
            original_qty = safe_float(pos.quantity)
            close_qty = max(0.0, original_qty * TP1_STRONG_CLOSE_FRACTION)
            runner_qty = max(0.0, original_qty - close_qty)
            close_raw = self._close_position_verified(pos, quantity=close_qty)

            if not self._close_order_ok(close_raw):
                save_error("position_monitor_tp1_partial_close_failed", str(close_raw), pos.to_dict())
                events.append(self._event(
                    pos,
                    EVENT_TP1,
                    pos.current_price,
                    reason_codes=("TP1_PRICE_REACHED", "TP1_PARTIAL_CLOSE_FAILED"),
                    warnings=("TP1_NOT_MARKED_BECAUSE_CLOSE_FAILED",),
                    raw={"partial_close": close_raw, "position": pos.to_dict()},
                    should_report=False,
                ))
                return events

            protected_sl = self._tp1_protected_sl(pos)
            meta = dict(pos.meta or {})
            meta.update({
                "tp1_profit_locked": True,
                "tp1_close_fraction": TP1_STRONG_CLOSE_FRACTION,
                "tp1_closed_quantity": close_qty,
                "runner_fraction": TP1_RUNNER_FRACTION,
                "runner_quantity": runner_qty,
                "protected_sl_after_tp1": protected_sl,
            })

            updated = RealPositionState(**{
                **pos.to_dict(),
                "tp1_hit": True,
                "quantity": runner_qty,
                "sl": protected_sl,
                "meta": meta,
            })
            self._repair_protected_sl(updated, protected_sl)
            pnl_est = self._estimate_pnl_usdt(pos, pos.current_price, TP1_STRONG_CLOSE_FRACTION)
            pnl_pct = pnl_percent(pos.direction, pos.entry, pos.current_price)
            self._learn_real_outcome(updated, EVENT_TP1, updated.current_price, snapshot, pnl_est, pnl_pct)
            save_position(updated.position_id, updated.to_dict())
            events.append(self._event(
                updated,
                EVENT_TP1,
                updated.current_price,
                pnl_est,
                pnl_pct,
                PNL_PENDING,
                reason_codes=("TP1_PRICE_REACHED", "TP1_PARTIAL_75_PERCENT_CLOSED", "RUNNER_25_PERCENT_TO_TP2", "PROTECTED_SL_AFTER_TP1"),
                raw={"partial_close": close_raw, "closed_quantity": close_qty, "runner_quantity": runner_qty, "protected_sl": protected_sl, "position": updated.to_dict()},
            ))
            return events

        close_raw = self._close_position_verified(pos, quantity=pos.quantity)
        if not self._close_order_ok(close_raw):
            save_error("position_monitor_tp1_full_close_failed", str(close_raw), pos.to_dict())
            events.append(self._event(
                pos,
                EVENT_TP1,
                pos.current_price,
                reason_codes=("TP1_PRICE_REACHED", "TP1_FULL_CLOSE_FAILED"),
                warnings=("TP1_NOT_MARKED_BECAUSE_CLOSE_FAILED",),
                raw={"close": close_raw, "position": pos.to_dict()},
                should_report=False,
            ))
            return events

        closed = RealPositionState(**{**pos.to_dict(), "tp1_hit": True, "status": POSITION_CLOSED, "close_time": now_ts()})
        pnl, pnl_status, pnl_raw = self._resolve_closed_pnl(closed)
        if pnl_status != PNL_CONFIRMED:
            pnl = self._estimate_pnl_usdt(closed, closed.current_price, 1.0)
        closed = RealPositionState(**{
            **closed.to_dict(),
            "realized_pnl_usdt": pnl,
            "realized_pnl_percent": pnl_percent(closed.direction, closed.entry, closed.current_price),
            "pnl_status": pnl_status,
        })
        self._learn_real_outcome(closed, EVENT_TP1, closed.current_price, snapshot, pnl, closed.realized_pnl_percent)
        save_position(closed.position_id, closed.to_dict())
        events.append(self._event(
            closed,
            EVENT_TP1,
            closed.current_price,
            pnl,
            closed.realized_pnl_percent,
            pnl_status,
            reason_codes=("TP1_PRICE_REACHED", "TP1_ONLY_FULL_CLOSE"),
            raw={"close": close_raw, "pnl": pnl_raw, "position": closed.to_dict()},
        ))
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
        return base_margin * max(1.0, safe_float(pos.leverage, 1.0)) * pct / 100.0

    def _tp1_protected_sl(self, pos: RealPositionState) -> float:
        entry = safe_float(pos.entry)
        tp1 = safe_float(pos.tp1)
        tp2 = safe_float(pos.tp2)
        direction = normalize_direction(pos.direction)

        if entry <= 0:
            return 0.0
        if tp1 <= 0:
            return entry

        if tp2 > 0 and abs(tp2 - tp1) > 0:
            buffer_dist = abs(tp2 - tp1) * 0.15
            if direction == DIRECTION_LONG:
                return max(entry, tp1 - buffer_dist)
            if direction == DIRECTION_SHORT:
                return min(entry, tp1 + buffer_dist)
        return tp1

    def _close_order_ok(self, raw: JsonDict) -> bool:
        if not isinstance(raw, dict):
            return True
        if raw.get("error"):
            return False
        if raw.get("ok") is False or raw.get("success") is False:
            return False
        status = str(raw.get("status", "")).upper()
        if status in {"REJECTED", "FAILED", "ERROR"}:
            return False
        return True

    def _is_position_still_open(self, pos: RealPositionState) -> bool:
        try:
            try:
                positions = _call_optional(self.client, ["get_open_positions", "fetch_open_positions"], pos.exchange_symbol)
            except TypeError:
                positions = _call_optional(self.client, ["get_open_positions", "fetch_open_positions"])
            if hasattr(positions, "to_dict") and callable(positions.to_dict):
                positions = positions.to_dict()
            if isinstance(positions, dict):
                positions = positions.get("positions", positions.get("data", []))
            if not isinstance(positions, list):
                return True

            target_direction = normalize_direction(pos.direction)
            for item in positions:
                if not isinstance(item, dict):
                    continue
                qty = safe_float(item.get("quantity", item.get("qty", item.get("positionAmt", item.get("size", 0.0)))))
                if qty <= 0:
                    continue
                item_symbol = str(item.get("symbol", item.get("exchange_symbol", item.get("contract", item.get("contractCode", item.get("instId", ""))))))
                item_dir = normalize_direction(str(item.get("direction", item.get("side", item.get("positionSide", item.get("holdSide", ""))))))
                if item_dir not in {DIRECTION_LONG, DIRECTION_SHORT}:
                    raw_side = str(item).upper()
                    if "SHORT" in raw_side or "SELL" in raw_side:
                        item_dir = DIRECTION_SHORT
                    elif "LONG" in raw_side or "BUY" in raw_side:
                        item_dir = DIRECTION_LONG

                if (symbol_match(item_symbol, pos.exchange_symbol) or symbol_match(item_symbol, pos.symbol)) and item_dir == target_direction:
                    return True
        except Exception as exc:
            save_error("position_monitor_close_verify_failed", str(exc), pos.to_dict())
            return True
        return False

    def _close_position_verified(self, pos: RealPositionState, quantity: Optional[float] = None, attempts: int = 5) -> JsonDict:
        qty = safe_float(quantity, pos.quantity)
        if qty <= 0:
            qty = safe_float(pos.quantity)
        full_close = qty <= 0 or pos.quantity <= 0 or qty >= pos.quantity * 0.999
        last: JsonDict = {}

        for attempt in range(1, max(1, attempts) + 1):
            raw = self._close_position(pos, quantity=qty)
            last = raw if isinstance(raw, dict) else {"raw": raw}
            last["close_attempt"] = attempt
            last["requested_close_quantity"] = qty
            last["full_close_requested"] = full_close

            if not self._close_order_ok(last):
                last["closed_confirmed"] = False
                last["verification_reason"] = "close_order_api_rejected_or_failed"
                time.sleep(1.2)
                continue

            if not full_close:
                last["verified"] = False
                last["closed_confirmed"] = None
                last["verification_reason"] = "partial_close_no_full_position_disappearance_expected"
                return last

            time.sleep(1.5)
            if not self._is_position_still_open(pos):
                last["verified"] = True
                last["closed_confirmed"] = True
                last["verification_reason"] = "position_disappeared_from_open_positions"
                return last

            last["verified"] = False
            last["closed_confirmed"] = False
            last["verification_reason"] = "position_still_open_after_close_order"
            time.sleep(1.5)

        last.setdefault("closed_confirmed", False)
        last.setdefault("verified", False)
        return last

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
            attempts=2,
            sleep_seconds=0.25,
        )

    def _coerce_saved_candidate(self, pos: RealPositionState) -> Optional[AnalysisCandidate]:
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

        for saved_candidate in candidates:
            if not saved_candidate:
                continue
            try:
                if isinstance(saved_candidate, AnalysisCandidate):
                    return saved_candidate
                if hasattr(AnalysisCandidate, "from_dict") and callable(getattr(AnalysisCandidate, "from_dict")):
                    return AnalysisCandidate.from_dict(saved_candidate)
                if isinstance(saved_candidate, dict):
                    from analysis_layers import SensorSnapshot
                    from analysis_engine import SensorDirectionHint, SensorMomentumState

                    sensor_data = saved_candidate.get("sensor_snapshot", {}) or {}
                    direction_data = saved_candidate.get("sensor_direction", {}) or {}
                    momentum_data = saved_candidate.get("momentum_state", {}) or {}

                    sensor_snapshot = SensorSnapshot(**{
                        k: v for k, v in sensor_data.items()
                        if k in getattr(SensorSnapshot, "__dataclass_fields__", {})
                    })
                    sensor_direction = SensorDirectionHint(**{
                        k: v for k, v in direction_data.items()
                        if k in getattr(SensorDirectionHint, "__dataclass_fields__", {})
                    })
                    momentum_state = SensorMomentumState(**{
                        k: v for k, v in momentum_data.items()
                        if k in getattr(SensorMomentumState, "__dataclass_fields__", {})
                    })

                    return AnalysisCandidate(
                        candidate_id=str(saved_candidate.get("candidate_id", f"cand_recovered_{uuid4().hex}")),
                        symbol=str(saved_candidate.get("symbol", pos.symbol)),
                        timeframe=str(saved_candidate.get("timeframe", "5m")),
                        timestamp=safe_int(saved_candidate.get("timestamp", pos.open_time)),
                        direction_hint=normalize_direction(saved_candidate.get("direction_hint", pos.direction)),
                        bias=str(saved_candidate.get("bias", "")),
                        sensor_direction=sensor_direction,
                        momentum_state=momentum_state,
                        sensor_snapshot=sensor_snapshot,
                        market_mode=dict(saved_candidate.get("market_mode", {}) if isinstance(saved_candidate.get("market_mode", {}), dict) else {}),
                        reason_codes=tuple(saved_candidate.get("reason_codes", ()) or ()),
                        warnings=tuple(saved_candidate.get("warnings", ()) or ()),
                        valid=bool(saved_candidate.get("valid", True)),
                    )
            except Exception as exc:
                save_error("position_monitor_candidate_recover", str(exc), {"position_id": pos.position_id, "symbol": pos.symbol})

        save_error("position_monitor_missing_candidate_for_learning", "candidate_not_found_in_position_meta", {
            "position_id": pos.position_id,
            "symbol": pos.symbol,
            "meta_keys": list(meta.keys()),
        })
        return None

    def _learn_real_outcome(
        self,
        pos: RealPositionState,
        event_type: str,
        price: float,
        snapshot: Optional[SensorSnapshot],
        pnl_usdt: float,
        pnl_percent_value: float,
    ) -> None:
        candidate = self._coerce_saved_candidate(pos)
        if candidate is None:
            return

        mfe = max(0.0, pnl_percent(pos.direction, pos.entry, pos.highest_price if pos.direction == DIRECTION_LONG else pos.lowest_price))
        mae = max(0.0, -pnl_percent(pos.direction, pos.entry, pos.lowest_price if pos.direction == DIRECTION_LONG else pos.highest_price))
        holding_seconds = max(0, (pos.close_time or now_ts()) - pos.open_time)

        try:
            learn_outcome(
                source_type=SOURCE_REAL,
                candidate=candidate,
                result=event_type,
                entry_price=pos.entry,
                exit_price=price,
                realized_pnl=pnl_usdt,
                realized_pnl_percent=pnl_percent_value,
                mfe_percent=mfe,
                mae_percent=mae,
                holding_seconds=holding_seconds,
                meta={
                    "source_type": SOURCE_REAL,
                    "position_id": pos.position_id,
                    "decision_id": pos.decision_id,
                    "event_type": event_type,
                    "realized_pnl_usdt": pnl_usdt,
                },
                persist=True,
            )
        except Exception as exc:
            save_error("position_monitor_coin_learning", str(exc), pos.to_dict())

        try:
            record_movement_memory(
                candidate=candidate,
                exit_price=price,
                duration_seconds=holding_seconds,
                outcome=event_type,
                mfe_percent=mfe,
                mae_percent=mae,
                meta={
                    "source_type": SOURCE_REAL,
                    "position_id": pos.position_id,
                    "decision_id": pos.decision_id,
                    "result": event_type,
                },
                persist=True,
            )
        except Exception as exc:
            save_error("position_monitor_movement_memory", str(exc), pos.to_dict())

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
    analysis_provider: Optional[Callable[[RealPositionState], SensorSnapshot]] = None,
) -> List[PositionMonitorEvent]:
    return PositionMonitor(client).monitor_all(analysis_provider=analysis_provider)
