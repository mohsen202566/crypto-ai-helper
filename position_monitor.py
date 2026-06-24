"""
position_monitor.py
Level 4 / 1H Smart Scalp Bot

Position monitor and lifecycle manager.

Architecture lock:
- Monitors existing REAL/GHOST positions.
- Handles TP1, TP2, SL, AI_EXIT decision flow, runner/profit protection.
- Does not fetch broad market scans or create new signals.
- Does not send Telegram text.
- Does not directly implement Toobit API calls; REAL open/close verification is injected
  through an execution adapter/callback so real_trade_manager.py remains exchange owner.
- Writes position state only through position_manager.py.
- Records outcomes through learning_memory.py.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from constants import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    EVENT_AI_EXIT,
    EVENT_MANUAL_CLOSE,
    EVENT_SL,
    EVENT_TP1,
    EVENT_TP2,
    MODE_GHOST,
    MODE_REAL,
    POSITION_ACTIVE_GHOST,
    POSITION_ACTIVE_REAL,
    POSITION_CLOSED,
    POSITION_CLOSING,
    POSITION_PARTIAL_TP1,
    POSITION_PENDING_REAL_CONFIRM,
    STATUS_FAILED,
    STATUS_OK,
    SYSTEM_VERSION,
)
from models import MonitorEvent, TradeCloseResult, TradeOutcome, TradePosition, to_dict
from position_manager import (
    close_position_record,
    get_active_monitor_positions,
    get_position,
    mark_ai_exit_done,
    mark_closing,
    mark_real_confirmed,
    mark_real_failed,
    mark_sl_hit,
    mark_tp1_partial,
    mark_tp2_hit,
    update_price_extremes,
    upsert_position,
)
from learning_memory import record_outcome
from utils import (
    direction_price_move,
    normalize_direction,
    profit_usdt,
    safe_float,
    safe_int,
    safe_str,
    utc_now_iso,
)


POSITION_MONITOR_VERSION: str = SYSTEM_VERSION


# =============================================================================
# Adapter contracts
# =============================================================================

PriceProvider = Callable[[str], float]
CloseExecutor = Callable[[TradePosition, str, float, float], TradeCloseResult | Mapping[str, Any]]
OpenConfirmChecker = Callable[[TradePosition], Mapping[str, Any] | bool]
ExchangePositionChecker = Callable[[TradePosition], Mapping[str, Any] | bool]
ClosedPnlReader = Callable[[TradePosition], Mapping[str, Any]]


# =============================================================================
# Price / trigger helpers
# =============================================================================

def is_tp_hit(position: TradePosition, current_price: float, tp_number: int = 1) -> bool:
    """Return True if TP1/TP2 is hit."""
    d = normalize_direction(position.direction)
    target = position.tp1 if tp_number == 1 else position.tp2
    target_f = safe_float(target, None)
    if target_f is None or target_f <= 0:
        return False

    if d == DIRECTION_LONG:
        return current_price >= target_f
    if d == DIRECTION_SHORT:
        return current_price <= target_f
    return False


def is_sl_hit(position: TradePosition, current_price: float) -> bool:
    """Return True if SL/protected SL is hit."""
    d = normalize_direction(position.direction)
    sl = safe_float(position.protected_sl, None)
    if sl is None or sl <= 0:
        sl = safe_float(position.sl, 0.0) or 0.0
    if sl <= 0:
        return False

    if d == DIRECTION_LONG:
        return current_price <= sl
    if d == DIRECTION_SHORT:
        return current_price >= sl
    return False


def progress_to_tp1(position: TradePosition, current_price: float) -> float:
    """Return progress from entry to TP1. 1.0 means TP1 reached."""
    d = normalize_direction(position.direction)
    entry = safe_float(position.entry, 0.0) or 0.0
    tp1 = safe_float(position.tp1, 0.0) or 0.0
    price = safe_float(current_price, 0.0) or 0.0

    if entry <= 0 or tp1 <= 0 or price <= 0:
        return 0.0

    if d == DIRECTION_LONG:
        denom = tp1 - entry
        num = price - entry
    elif d == DIRECTION_SHORT:
        denom = entry - tp1
        num = entry - price
    else:
        return 0.0

    if denom <= 0:
        return 0.0
    return max(0.0, num / denom)


def calculate_mfe_mae(position: TradePosition) -> tuple[float, float]:
    """Calculate MFE/MAE percent from stored high/low."""
    entry = safe_float(position.entry, 0.0) or 0.0
    high = safe_float(position.highest_price, entry) or entry
    low = safe_float(position.lowest_price, entry) or entry
    if entry <= 0:
        return 0.0, 0.0

    if position.direction == DIRECTION_LONG:
        mfe = ((high - entry) / entry) * 100.0
        mae = ((entry - low) / entry) * 100.0
    else:
        mfe = ((entry - low) / entry) * 100.0
        mae = ((high - entry) / entry) * 100.0

    return max(0.0, mfe), max(0.0, mae)


def partial_tp1_quantities(position: TradePosition, close_ratio: float = 0.75) -> tuple[float, float]:
    """Return TP1 closed quantity and runner quantity."""
    qty = safe_float(position.quantity, 0.0) or 0.0
    ratio = max(0.0, min(1.0, safe_float(close_ratio, 0.75) or 0.75))
    closed = qty * ratio
    runner = max(0.0, qty - closed)
    return closed, runner


def protected_sl_after_tp1(position: TradePosition, current_price: float) -> float:
    """
    Conservative protected SL after TP1.

    It protects around entry/slightly profitable side instead of over-tightening.
    """
    entry = safe_float(position.entry, 0.0) or 0.0
    tp1 = safe_float(position.tp1, 0.0) or 0.0
    if entry <= 0 or tp1 <= 0:
        return entry

    if position.direction == DIRECTION_LONG:
        return entry + abs(tp1 - entry) * 0.10
    return entry - abs(entry - tp1) * 0.10


# =============================================================================
# Outcome / events
# =============================================================================

def make_outcome(
    position: TradePosition,
    *,
    event: str,
    exit_price: float,
    quantity: Optional[float] = None,
    pnl_usdt: Optional[float] = None,
    pnl_confirmed: bool = False,
    metadata: Optional[Mapping[str, Any]] = None,
) -> TradeOutcome:
    """Build TradeOutcome from position."""
    qty = safe_float(quantity, None)
    if qty is None or qty <= 0:
        qty = safe_float(position.quantity, 0.0) or 0.0

    exit_f = safe_float(exit_price, position.current_price) or position.current_price
    gross = profit_usdt(position.direction, position.entry, exit_f, qty)
    pnl = safe_float(pnl_usdt, gross)
    mfe, mae = calculate_mfe_mae(position)

    entry = safe_float(position.entry, 0.0) or 0.0
    pnl_pct = direction_price_move(position.direction, entry, exit_f) if entry > 0 else 0.0

    return TradeOutcome(
        position_id=position.position_id,
        symbol=position.symbol,
        direction=position.direction,
        event=safe_str(event).upper(),
        mode=position.mode,
        entry=position.entry,
        exit_price=exit_f,
        quantity=qty,
        pnl_usdt=pnl,
        pnl_pct=pnl_pct,
        pnl_confirmed=bool(pnl_confirmed),
        mfe_pct=mfe,
        mae_pct=mae,
        reason_codes=[safe_str(event).upper()],
        metadata={
            **dict(metadata or {}),
            "signal_id": position.signal_id,
            "level": position.level,
            "status": position.status,
        },
        level=position.level,
    )


def make_monitor_event(
    *,
    event: str,
    position: TradePosition,
    status: str = STATUS_OK,
    outcome: Optional[TradeOutcome] = None,
    close_result: Optional[TradeCloseResult] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> MonitorEvent:
    """Build MonitorEvent. bot.py/telegram_ui.py decides message text."""
    return MonitorEvent(
        event=event,
        position_id=position.position_id,
        symbol=position.symbol,
        direction=position.direction,
        mode=position.mode,
        status=status,
        message_key=safe_str(event).lower(),
        reply_to_message_id=position.signal_message_id,
        outcome=outcome,
        close_result=close_result,
        metadata=dict(metadata or {}),
    )


def _record_outcome_safe(outcome: TradeOutcome, extra_metadata: Optional[Mapping[str, Any]] = None) -> bool:
    """Record outcome without crashing monitor loop."""
    result = record_outcome(outcome, metadata=extra_metadata)
    return result.recorded


# =============================================================================
# REAL close / confirm helpers
# =============================================================================

def normalize_close_result(result: TradeCloseResult | Mapping[str, Any] | None, position: TradePosition) -> TradeCloseResult:
    """Normalize close executor result."""
    if isinstance(result, TradeCloseResult):
        return result

    if isinstance(result, Mapping):
        return TradeCloseResult(
            status=safe_str(result.get("status"), STATUS_FAILED),
            position_id=safe_str(result.get("position_id"), position.position_id),
            exchange_order_id=safe_str(result.get("exchange_order_id")),
            symbol=safe_str(result.get("symbol"), position.symbol),
            direction=safe_str(result.get("direction"), position.direction),
            close_price=safe_float(result.get("close_price"), position.current_price) or position.current_price,
            closed_quantity=safe_float(result.get("closed_quantity"), position.quantity) or position.quantity,
            pnl_usdt=safe_float(result.get("pnl_usdt"), None),
            pnl_confirmed=bool(result.get("pnl_confirmed", False)),
            close_confirmed=bool(result.get("close_confirmed", False)),
            message=safe_str(result.get("message")),
            error=safe_str(result.get("error")),
            raw=dict(result.get("raw", {})) if isinstance(result.get("raw", {}), Mapping) else {},
        )

    return TradeCloseResult(
        status=STATUS_FAILED,
        position_id=position.position_id,
        symbol=position.symbol,
        direction=position.direction,
        close_price=position.current_price,
        closed_quantity=position.quantity,
        close_confirmed=False,
        error="missing_close_executor_result",
    )


def close_real_position_verified(
    position: TradePosition,
    *,
    reason: str,
    quantity: float,
    current_price: float,
    close_executor: Optional[CloseExecutor],
) -> TradeCloseResult:
    """
    Request REAL close via injected executor and require close_confirmed=True.

    This preserves the locked rule:
    close request != closed. It is closed only after exchange verification.
    """
    if close_executor is None:
        return TradeCloseResult(
            status=STATUS_FAILED,
            position_id=position.position_id,
            symbol=position.symbol,
            direction=position.direction,
            close_price=current_price,
            closed_quantity=quantity,
            close_confirmed=False,
            error="close_executor_missing",
        )

    mark_closing(position.position_id, reason=reason)
    raw_result = close_executor(position, reason, quantity, current_price)
    result = normalize_close_result(raw_result, position)

    if result.close_confirmed:
        return result

    return TradeCloseResult(
        status=STATUS_FAILED,
        position_id=position.position_id,
        exchange_order_id=result.exchange_order_id,
        symbol=position.symbol,
        direction=position.direction,
        close_price=result.close_price,
        closed_quantity=result.closed_quantity,
        pnl_usdt=result.pnl_usdt,
        pnl_confirmed=result.pnl_confirmed,
        close_confirmed=False,
        message=result.message,
        error=result.error or "close_not_confirmed",
        raw=result.raw,
    )


# =============================================================================
# Pending REAL confirmation
# =============================================================================

def handle_pending_real_confirmation(
    position: TradePosition,
    *,
    open_confirm_checker: Optional[OpenConfirmChecker] = None,
) -> list[MonitorEvent]:
    """Confirm pending REAL open without freeing slot prematurely."""
    if position.status != POSITION_PENDING_REAL_CONFIRM or position.mode != MODE_REAL:
        return []

    if open_confirm_checker is None:
        return []

    result = open_confirm_checker(position)

    confirmed = False
    entry = position.entry
    quantity = position.quantity
    exchange_order_id = position.exchange_order_id
    error = ""

    if isinstance(result, Mapping):
        confirmed = bool(result.get("confirmed", False))
        entry = safe_float(result.get("entry"), position.entry) or position.entry
        quantity = safe_float(result.get("quantity"), position.quantity) or position.quantity
        exchange_order_id = safe_str(result.get("exchange_order_id"), position.exchange_order_id)
        error = safe_str(result.get("error"))
    else:
        confirmed = bool(result)

    if confirmed:
        mark_real_confirmed(position.position_id, entry=entry, quantity=quantity, exchange_order_id=exchange_order_id)
        updated = get_position(position.position_id) or position
        return [
            make_monitor_event(
                event="REAL_OPEN_CONFIRMED",
                position=updated,
                metadata={"entry": entry, "quantity": quantity, "exchange_order_id": exchange_order_id},
            )
        ]

    if error:
        mark_real_failed(position.position_id, reason=error)
        failed = get_position(position.position_id) or position
        return [
            make_monitor_event(
                event="REAL_OPEN_FAILED",
                position=failed,
                status=STATUS_FAILED,
                metadata={"error": error},
            )
        ]

    return []


# =============================================================================
# TP/SL handlers
# =============================================================================

def handle_ghost_close(position: TradePosition, *, event: str, current_price: float, quantity: Optional[float] = None) -> MonitorEvent:
    """Close GHOST position locally and record outcome."""
    qty = safe_float(quantity, None)
    if qty is None or qty <= 0:
        qty = position.quantity

    outcome = make_outcome(position, event=event, exit_price=current_price, quantity=qty, pnl_confirmed=False)
    close_position_record(
        position.position_id,
        close_price=current_price,
        pnl_usdt=outcome.pnl_usdt,
        pnl_confirmed=False,
        close_reason=event,
    )
    _record_outcome_safe(outcome)
    updated = get_position(position.position_id) or position
    return make_monitor_event(event=event, position=updated, outcome=outcome)


def handle_real_close(
    position: TradePosition,
    *,
    event: str,
    current_price: float,
    quantity: Optional[float],
    close_executor: Optional[CloseExecutor],
) -> MonitorEvent:
    """Close REAL position only after executor confirms close."""
    qty = safe_float(quantity, None)
    if qty is None or qty <= 0:
        qty = position.quantity

    close_result = close_real_position_verified(
        position,
        reason=event,
        quantity=qty,
        current_price=current_price,
        close_executor=close_executor,
    )

    if not close_result.close_confirmed:
        updated = get_position(position.position_id) or position
        return make_monitor_event(
            event=f"{event}_CLOSE_NOT_CONFIRMED",
            position=updated,
            status=STATUS_FAILED,
            close_result=close_result,
            metadata={"error": close_result.error},
        )

    outcome = make_outcome(
        position,
        event=event,
        exit_price=close_result.close_price or current_price,
        quantity=close_result.closed_quantity or qty,
        pnl_usdt=close_result.pnl_usdt,
        pnl_confirmed=close_result.pnl_confirmed,
        metadata={"close_confirmed": True, "exchange_order_id": close_result.exchange_order_id},
    )

    close_position_record(
        position.position_id,
        close_price=close_result.close_price or current_price,
        pnl_usdt=outcome.pnl_usdt,
        pnl_confirmed=close_result.pnl_confirmed,
        close_reason=event,
    )
    _record_outcome_safe(outcome)
    updated = get_position(position.position_id) or position
    return make_monitor_event(event=event, position=updated, outcome=outcome, close_result=close_result)


def handle_tp1(
    position: TradePosition,
    *,
    current_price: float,
    close_executor: Optional[CloseExecutor] = None,
) -> MonitorEvent:
    """
    Handle TP1.

    REAL: close about 75%, require verification, keep runner.
    GHOST: mark partial locally and record TP1.
    """
    closed_qty, runner_qty = partial_tp1_quantities(position, close_ratio=0.75)
    protected_sl = protected_sl_after_tp1(position, current_price)

    if position.mode == MODE_REAL:
        close_result = close_real_position_verified(
            position,
            reason=EVENT_TP1,
            quantity=closed_qty,
            current_price=current_price,
            close_executor=close_executor,
        )
        if not close_result.close_confirmed:
            updated = get_position(position.position_id) or position
            return make_monitor_event(
                event="TP1_CLOSE_NOT_CONFIRMED",
                position=updated,
                status=STATUS_FAILED,
                close_result=close_result,
                metadata={"error": close_result.error},
            )

        actual_closed = close_result.closed_quantity or closed_qty
        actual_runner = max(0.0, position.quantity - actual_closed)
        mark_tp1_partial(
            position.position_id,
            closed_quantity=actual_closed,
            runner_quantity=actual_runner,
            protected_sl=protected_sl,
        )
        outcome = make_outcome(
            position,
            event=EVENT_TP1,
            exit_price=close_result.close_price or current_price,
            quantity=actual_closed,
            pnl_usdt=close_result.pnl_usdt,
            pnl_confirmed=close_result.pnl_confirmed,
            metadata={"close_confirmed": True, "runner_quantity": actual_runner, "protected_sl": protected_sl},
        )
        _record_outcome_safe(outcome)
        updated = get_position(position.position_id) or position
        return make_monitor_event(event=EVENT_TP1, position=updated, outcome=outcome, close_result=close_result)

    # GHOST partial TP1.
    mark_tp1_partial(
        position.position_id,
        closed_quantity=closed_qty,
        runner_quantity=runner_qty,
        protected_sl=protected_sl,
    )
    outcome = make_outcome(
        position,
        event=EVENT_TP1,
        exit_price=current_price,
        quantity=closed_qty,
        pnl_confirmed=False,
        metadata={"runner_quantity": runner_qty, "protected_sl": protected_sl},
    )
    _record_outcome_safe(outcome)
    updated = get_position(position.position_id) or position
    return make_monitor_event(event=EVENT_TP1, position=updated, outcome=outcome)


def handle_tp2(position: TradePosition, *, current_price: float, close_executor: Optional[CloseExecutor] = None) -> MonitorEvent:
    """Handle TP2 for remaining runner/full quantity."""
    qty = position.runner_quantity if position.runner_quantity > 0 else position.quantity

    if position.mode == MODE_REAL:
        event = handle_real_close(position, event=EVENT_TP2, current_price=current_price, quantity=qty, close_executor=close_executor)
        if event.status == STATUS_OK:
            mark_tp2_hit(position.position_id)
        return event

    mark_tp2_hit(position.position_id)
    return handle_ghost_close(position, event=EVENT_TP2, current_price=current_price, quantity=qty)


def handle_sl(position: TradePosition, *, current_price: float, close_executor: Optional[CloseExecutor] = None) -> MonitorEvent:
    """Handle SL/protected SL."""
    qty = position.runner_quantity if position.status == POSITION_PARTIAL_TP1 and position.runner_quantity > 0 else position.quantity

    if position.mode == MODE_REAL:
        event = handle_real_close(position, event=EVENT_SL, current_price=current_price, quantity=qty, close_executor=close_executor)
        if event.status == STATUS_OK:
            mark_sl_hit(position.position_id)
        return event

    mark_sl_hit(position.position_id)
    return handle_ghost_close(position, event=EVENT_SL, current_price=current_price, quantity=qty)


def handle_ai_exit(
    position: TradePosition,
    *,
    current_price: float,
    close_executor: Optional[CloseExecutor] = None,
    reason: str = EVENT_AI_EXIT,
) -> MonitorEvent:
    """Handle AI exit. REAL close must be verified."""
    qty = position.runner_quantity if position.status == POSITION_PARTIAL_TP1 and position.runner_quantity > 0 else position.quantity

    if position.mode == MODE_REAL:
        event = handle_real_close(position, event=reason, current_price=current_price, quantity=qty, close_executor=close_executor)
        if event.status == STATUS_OK:
            mark_ai_exit_done(position.position_id)
        return event

    mark_ai_exit_done(position.position_id)
    return handle_ghost_close(position, event=reason, current_price=current_price, quantity=qty)



# =============================================================================
# Exchange-side close recovery
# =============================================================================

def _exchange_position_exists(result: Mapping[str, Any] | bool | None) -> tuple[bool, str, dict[str, Any]]:
    """
    Normalize exchange position checker result.

    Expected truth:
    - True / {"exists": True} / {"open": True}  => position still exists on exchange
    - False / {"exists": False} / {"open": False} => position no longer exists
    """
    if isinstance(result, Mapping):
        exists = bool(
            result.get("exists", result.get("open", result.get("position_exists", result.get("found", False))))
        )
        error = safe_str(result.get("error"))
        return exists, error, dict(result)
    return bool(result), "", {"raw": result}


def _read_closed_pnl(position: TradePosition, closed_pnl_reader: Optional[ClosedPnlReader]) -> dict[str, Any]:
    """Read closed-position history/PnL via injected adapter without crashing monitor loop."""
    if closed_pnl_reader is None:
        return {"status": STATUS_FAILED, "confirmed": False, "pnl_usdt": None, "error": "closed_pnl_reader_missing"}

    try:
        result = closed_pnl_reader(position)
        return dict(result) if isinstance(result, Mapping) else {"status": STATUS_FAILED, "confirmed": False, "pnl_usdt": None, "raw": result}
    except Exception as exc:
        return {"status": STATUS_FAILED, "confirmed": False, "pnl_usdt": None, "error": str(exc)}


def infer_external_close_event(position: TradePosition, pnl_data: Mapping[str, Any], current_price: float) -> str:
    """
    Infer why a REAL position disappeared from exchange.

    Prefer TP/SL price logic when possible; otherwise use realized PnL:
    positive/zero => TP1-style profitable close, negative => SL.
    """
    exit_price = safe_float(
        pnl_data.get("close_price")
        or pnl_data.get("exit_price")
        or pnl_data.get("avg_close_price")
        or (pnl_data.get("row", {}) or {}).get("closePrice") if isinstance(pnl_data.get("row"), Mapping) else None,
        None,
    )
    price = exit_price if exit_price is not None and exit_price > 0 else current_price

    if price and price > 0:
        if is_sl_hit(position, price):
            return EVENT_SL
        if position.tp1_hit or position.status == POSITION_PARTIAL_TP1:
            if is_tp_hit(position, price, tp_number=2):
                return EVENT_TP2
            return EVENT_TP1
        if is_tp_hit(position, price, tp_number=1):
            return EVENT_TP1

    pnl = safe_float(pnl_data.get("pnl_usdt"), None)
    if pnl is not None:
        return EVENT_SL if pnl < 0 else EVENT_TP1

    return EVENT_MANUAL_CLOSE


def handle_real_exchange_disappeared(
    position: TradePosition,
    *,
    current_price: float,
    closed_pnl_reader: Optional[ClosedPnlReader],
) -> MonitorEvent:
    """
    Recover a REAL position that is closed on Toobit but still open internally.

    This handles exchange-side TP/SL, manual app close, or delayed close confirmation.
    It reads closed history/PnL, closes the internal position, records learning/stats,
    and returns an event that bot.py can reply to the original signal.
    """
    pnl_data = _read_closed_pnl(position, closed_pnl_reader)
    event = infer_external_close_event(position, pnl_data, current_price)

    row = pnl_data.get("row", {}) if isinstance(pnl_data.get("row"), Mapping) else {}
    close_price = safe_float(
        pnl_data.get("close_price")
        or pnl_data.get("exit_price")
        or pnl_data.get("avg_close_price")
        or row.get("closePrice")
        or row.get("avgClosePrice")
        or row.get("price")
        or current_price,
        current_price,
    ) or current_price

    pnl_usdt = safe_float(pnl_data.get("pnl_usdt"), None)
    pnl_confirmed = bool(pnl_data.get("confirmed", False))

    outcome = make_outcome(
        position,
        event=event,
        exit_price=close_price,
        quantity=position.runner_quantity if position.runner_quantity > 0 else position.quantity,
        pnl_usdt=pnl_usdt,
        pnl_confirmed=pnl_confirmed,
        metadata={
            "exchange_position_missing": True,
            "closed_history": dict(pnl_data),
            "recovery_reason": "exchange_closed_position_detected",
        },
    )

    close_position_record(
        position.position_id,
        close_price=close_price,
        pnl_usdt=outcome.pnl_usdt,
        pnl_confirmed=pnl_confirmed,
        close_reason=event,
    )
    _record_outcome_safe(outcome, extra_metadata={"exchange_position_missing": True})

    updated = get_position(position.position_id) or position
    close_result = TradeCloseResult(
        status=STATUS_OK,
        position_id=position.position_id,
        symbol=position.symbol,
        direction=position.direction,
        close_price=close_price,
        closed_quantity=outcome.quantity,
        pnl_usdt=outcome.pnl_usdt,
        pnl_confirmed=pnl_confirmed,
        close_confirmed=True,
        message="exchange_position_closed_detected",
        raw={"closed_history": dict(pnl_data)},
    )
    return make_monitor_event(
        event=event,
        position=updated,
        outcome=outcome,
        close_result=close_result,
        metadata={"exchange_position_missing": True, "closed_history_confirmed": pnl_confirmed},
    )


def check_real_exchange_closed(
    position: TradePosition,
    *,
    current_price: float,
    exchange_position_checker: Optional[ExchangePositionChecker],
    closed_pnl_reader: Optional[ClosedPnlReader],
) -> Optional[MonitorEvent]:
    """Return a recovery event if a REAL position is no longer open on exchange."""
    if position.mode != MODE_REAL or position.status == POSITION_PENDING_REAL_CONFIRM:
        return None
    if exchange_position_checker is None:
        return None

    try:
        exists, error, raw = _exchange_position_exists(exchange_position_checker(position))
    except Exception as exc:
        return make_monitor_event(
            event="EXCHANGE_POSITION_CHECK_FAILED",
            position=position,
            status=STATUS_FAILED,
            metadata={"error": str(exc)},
        )

    if error:
        return make_monitor_event(
            event="EXCHANGE_POSITION_CHECK_FAILED",
            position=position,
            status=STATUS_FAILED,
            metadata={"error": error, "raw": raw},
        )

    if exists:
        return None

    return handle_real_exchange_disappeared(
        position,
        current_price=current_price,
        closed_pnl_reader=closed_pnl_reader,
    )


# =============================================================================
# Main monitor function
# =============================================================================

def monitor_position_once(
    position: TradePosition,
    *,
    current_price: float,
    ai_monitor_decision: Optional[Any] = None,
    close_executor: Optional[CloseExecutor] = None,
    open_confirm_checker: Optional[OpenConfirmChecker] = None,
    exchange_position_checker: Optional[ExchangePositionChecker] = None,
    closed_pnl_reader: Optional[ClosedPnlReader] = None,
) -> list[MonitorEvent]:
    """
    Monitor one position once.

    ai_monitor_decision may be MonitorDecision, dict-like, or None.
    """
    events: list[MonitorEvent] = []

    if position.status == POSITION_PENDING_REAL_CONFIRM and position.mode == MODE_REAL:
        return handle_pending_real_confirmation(position, open_confirm_checker=open_confirm_checker)

    if position.status == POSITION_CLOSED:
        return []

    # POSITION_CLOSING can happen after a previous close request. Keep monitoring REAL exchange state
    # so delayed confirmations/TP-SL exchange closes do not leave slots stuck forever.
    if position.status == POSITION_CLOSING and position.mode != MODE_REAL:
        return []

    price = safe_float(current_price, None)
    if price is None or price <= 0:
        return [
            make_monitor_event(
                event="PRICE_UNAVAILABLE",
                position=position,
                status=STATUS_FAILED,
                metadata={"current_price": current_price},
            )
        ]

    update_price_extremes(position.position_id, price)
    position = get_position(position.position_id) or position

    exchange_closed_event = check_real_exchange_closed(
        position,
        current_price=price,
        exchange_position_checker=exchange_position_checker,
        closed_pnl_reader=closed_pnl_reader,
    )
    if exchange_closed_event is not None:
        events.append(exchange_closed_event)
        return events

    # SL has priority for risk accounting.
    if is_sl_hit(position, price):
        events.append(handle_sl(position, current_price=price, close_executor=close_executor))
        return events

    # TP2 for runner/position.
    if position.status == POSITION_PARTIAL_TP1 and is_tp_hit(position, price, tp_number=2):
        events.append(handle_tp2(position, current_price=price, close_executor=close_executor))
        return events

    # TP1 only once.
    if not position.tp1_hit and is_tp_hit(position, price, tp_number=1):
        events.append(handle_tp1(position, current_price=price, close_executor=close_executor))
        return events

    # AI exit: before TP1 must be conservative and should already be filtered by ai_brain,
    # but we enforce the locked 70% progress rule here as a second safety.
    if ai_monitor_decision is not None:
        should_close = bool(getattr(ai_monitor_decision, "should_close", False))
        close_reason = safe_str(getattr(ai_monitor_decision, "close_reason", EVENT_AI_EXIT), EVENT_AI_EXIT)
        progress = progress_to_tp1(position, price)

        if isinstance(ai_monitor_decision, Mapping):
            should_close = bool(ai_monitor_decision.get("should_close", False))
            close_reason = safe_str(ai_monitor_decision.get("close_reason"), EVENT_AI_EXIT)

        if should_close:
            after_tp1 = position.status == POSITION_PARTIAL_TP1 or position.tp1_hit
            if after_tp1 or progress >= 0.70:
                events.append(handle_ai_exit(position, current_price=price, close_executor=close_executor, reason=close_reason or EVENT_AI_EXIT))
                return events
            events.append(
                make_monitor_event(
                    event="AI_EXIT_SKIPPED_EARLY",
                    position=position,
                    metadata={"progress_to_tp1": progress, "required_progress": 0.70},
                )
            )

    return events


def monitor_positions_once(
    *,
    price_provider: PriceProvider,
    ai_decision_provider: Optional[Callable[[TradePosition, float], Any]] = None,
    close_executor: Optional[CloseExecutor] = None,
    open_confirm_checker: Optional[OpenConfirmChecker] = None,
    exchange_position_checker: Optional[ExchangePositionChecker] = None,
    closed_pnl_reader: Optional[ClosedPnlReader] = None,
) -> list[MonitorEvent]:
    """Monitor all active positions once."""
    events: list[MonitorEvent] = []

    for position in get_active_monitor_positions():
        price = safe_float(price_provider(position.symbol), None)
        ai_decision = ai_decision_provider(position, price) if ai_decision_provider and price is not None else None
        events.extend(
            monitor_position_once(
                position,
                current_price=price or 0.0,
                ai_monitor_decision=ai_decision,
                close_executor=close_executor,
                open_confirm_checker=open_confirm_checker,
                exchange_position_checker=exchange_position_checker,
                closed_pnl_reader=closed_pnl_reader,
            )
        )

    return events


# =============================================================================
# Validation
# =============================================================================

def validate_monitor_event(event: MonitorEvent) -> dict[str, Any]:
    """Lightweight validation for monitor event."""
    errors: list[str] = []
    if event.system_version != SYSTEM_VERSION:
        errors.append("INVALID_SYSTEM_VERSION")
    if not event.position_id:
        errors.append("MISSING_POSITION_ID")
    if not event.symbol:
        errors.append("MISSING_SYMBOL")
    if normalize_direction(event.direction) not in {DIRECTION_LONG, DIRECTION_SHORT}:
        errors.append("INVALID_DIRECTION")
    if event.status not in {STATUS_OK, STATUS_FAILED}:
        errors.append("INVALID_STATUS")
    return {
        "valid": not errors,
        "errors": errors,
        "event": event.event,
        "position_id": event.position_id,
        "status": event.status,
    }


__all__ = [
    "POSITION_MONITOR_VERSION",
    "PriceProvider",
    "CloseExecutor",
    "OpenConfirmChecker",
    "ExchangePositionChecker",
    "ClosedPnlReader",
    "is_tp_hit",
    "is_sl_hit",
    "progress_to_tp1",
    "calculate_mfe_mae",
    "partial_tp1_quantities",
    "protected_sl_after_tp1",
    "make_outcome",
    "make_monitor_event",
    "normalize_close_result",
    "close_real_position_verified",
    "handle_pending_real_confirmation",
    "handle_ghost_close",
    "handle_real_close",
    "handle_tp1",
    "handle_tp2",
    "handle_sl",
    "handle_ai_exit",
    "infer_external_close_event",
    "handle_real_exchange_disappeared",
    "check_real_exchange_closed",
    "monitor_position_once",
    "monitor_positions_once",
    "validate_monitor_event",
]
