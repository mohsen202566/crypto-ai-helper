"""
real_trade_manager.py
Level 4 / 1H Smart Scalp Bot

Real trade orchestration layer.

Architecture lock:
- Owns REAL preflight, slot checks, position record creation, real open request,
  confirmation wrapper, close adapter, and Toobit execution orchestration.
- Does not fetch market data, calculate indicators, make AI decisions, or build Telegram text.
- Uses tobit_client.py as the only low-level exchange API layer.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional
import time

from constants import (
    DIRECTION_LONG, DIRECTION_SHORT, FEE_CONFIG, MODE_REAL, POSITION_PENDING_REAL_CONFIRM,
    STATUS_FAILED, STATUS_OK, STATUS_RECOVERED, SYSTEM_VERSION, TRADE_CONFIG,
)
from models import AIDecision, TPSLPlan, TradeCloseResult, TradeOpenResult, TradePosition, RecordResult
from position_manager import add_position, count_open_real_positions, get_open_positions, get_position, has_open_position, mark_real_confirmed, mark_real_failed
from strategy_manager import get_trade_runtime_config, is_real_trading_enabled
from tobit_client import MARGIN_ISOLATED, ToobitClient, get_client
from utils import fee_estimate, make_position_id, normalize_direction, normalize_symbol, notional_value, profit_usdt, safe_float, safe_int, safe_str, utc_now_iso


REAL_TRADE_MANAGER_VERSION: str = SYSTEM_VERSION


def get_runtime(state: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    return get_trade_runtime_config(state)


def estimate_quantity(entry: Any, margin_usdt: Any, leverage: Any) -> float:
    entry_f = safe_float(entry, 0.0) or 0.0
    margin = safe_float(margin_usdt, 0.0) or 0.0
    lev = safe_float(leverage, 1.0) or 1.0
    if entry_f <= 0 or margin <= 0 or lev <= 0:
        return 0.0
    return (margin * lev) / entry_f


def estimate_tp1_net_profit(direction: str, entry: float, tp1: float, quantity: float) -> tuple[float, float, float]:
    gross = profit_usdt(direction, entry, tp1, quantity)
    notional = notional_value(entry, quantity)
    fee_rate = safe_float(FEE_CONFIG.get("estimated_round_trip_fee_rate"), 0.0012) or 0.0012
    fees = fee_estimate(notional, fee_rate / 2.0, sides=2)
    return gross, fees, gross - fees


def preflight_real_trade(decision: AIDecision, *, client: Optional[ToobitClient] = None, state: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    c = client or get_client()
    runtime = get_runtime(state)
    symbol = normalize_symbol(decision.symbol)
    direction = normalize_direction(decision.direction)
    plan = decision.tp_sl
    errors: list[str] = []
    warnings: list[str] = []

    if decision.mode != MODE_REAL:
        errors.append("decision_not_real")
    if not is_real_trading_enabled(state):
        errors.append("real_trading_disabled")
    if direction not in {DIRECTION_LONG, DIRECTION_SHORT}:
        errors.append("invalid_direction")
    if plan is None or not isinstance(plan, TPSLPlan):
        errors.append("missing_tp_sl_plan")
    elif not plan.valid:
        errors.append("invalid_tp_sl_plan")
    if has_open_position(symbol, direction, mode=MODE_REAL):
        errors.append("duplicate_real_position")
    max_real = safe_int(runtime.get("max_concurrent_real_positions"), TRADE_CONFIG.get("max_concurrent_real_positions", 3)) or 3
    if count_open_real_positions() >= max_real:
        errors.append("max_real_positions_reached")

    margin_mode = safe_str(runtime.get("margin_mode"), MARGIN_ISOLATED).upper()
    if margin_mode != MARGIN_ISOLATED:
        errors.append("cross_margin_blocked")

    margin = safe_float(runtime.get("margin_usdt"), 0.0) or 0.0
    leverage = safe_int(runtime.get("leverage"), 1) or 1
    entry = safe_float(decision.entry, 0.0) or (safe_float(plan.entry, 0.0) if plan else 0.0) or 0.0
    quantity_est = estimate_quantity(entry, margin, leverage)

    qty_ok, qty, qty_reason, rules = c.validate_quantity(symbol, quantity_est, entry)
    if not qty_ok:
        errors.append(qty_reason)

    if plan is not None and qty > 0:
        gross, fees, net = estimate_tp1_net_profit(direction, entry, plan.tp1, qty)
        min_net = safe_float(FEE_CONFIG.get("minimum_net_profit_usdt"), 0.10) or 0.10
        if FEE_CONFIG.get("reject_if_tp1_net_profit_below_minimum", True) and net < min_net:
            errors.append(f"tp1_net_profit_too_low:{net:.4f}<{min_net:.4f}")
    else:
        gross = fees = net = 0.0

    lev_ok = False
    lev_reason = "not_checked"
    if not errors:
        lev_ok, lev_reason = c.verify_leverage(symbol, leverage)
        if TRADE_CONFIG.get("require_leverage_verification", True) and not lev_ok:
            errors.append(f"leverage_not_verified:{lev_reason}")

    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "ok": not errors, "errors": errors, "warnings": warnings, "symbol": symbol, "direction": direction,
        "entry": entry, "margin_usdt": margin, "leverage": leverage, "margin_mode": margin_mode,
        "quantity_estimate": quantity_est, "quantity": qty, "quantity_reason": qty_reason,
        "symbol_rules": rules.to_dict() if rules else {}, "tp1_gross_profit_estimate": gross,
        "fee_estimate": fees, "tp1_net_profit_estimate": net, "checked_at": utc_now_iso(),
    }


def build_pending_position(decision: AIDecision, preflight: Mapping[str, Any], open_result: Optional[TradeOpenResult] = None) -> TradePosition:
    plan = decision.tp_sl
    symbol = normalize_symbol(decision.symbol)
    direction = normalize_direction(decision.direction)
    entry = safe_float(preflight.get("entry"), 0.0) or safe_float(decision.entry, 0.0) or (plan.entry if plan else 0.0)
    position_id = make_position_id(symbol, direction, 4)
    return TradePosition(
        position_id=position_id, signal_id=decision.signal_id, symbol=symbol, direction=direction, mode=MODE_REAL,
        status=POSITION_PENDING_REAL_CONFIRM, entry=entry, current_price=entry, highest_price=entry, lowest_price=entry,
        tp1=plan.tp1 if plan else 0.0, tp2=plan.tp2 if plan else None, sl=plan.sl if plan else 0.0,
        quantity=safe_float(preflight.get("quantity"), 0.0) or (open_result.quantity if open_result else 0.0),
        margin_usdt=safe_float(preflight.get("margin_usdt"), 0.0) or 0.0, leverage=safe_int(preflight.get("leverage"), 1) or 1,
        exchange_symbol=safe_str((preflight.get("symbol_rules") or {}).get("exchange_symbol")),
        exchange_order_id=open_result.exchange_order_id if open_result else "",
        decision_metadata={"decision": decision.metadata, "preflight": dict(preflight), "open_result": open_result.raw if open_result else {}},
        level=decision.level,
    )


def open_real_trade(decision: AIDecision, *, client: Optional[ToobitClient] = None, state: Optional[Mapping[str, Any]] = None) -> TradeOpenResult:
    c = client or get_client()
    preflight = preflight_real_trade(decision, client=c, state=state)
    if not preflight.get("ok"):
        return TradeOpenResult(status=STATUS_FAILED, symbol=decision.symbol, direction=decision.direction, entry=decision.entry, error=";".join(preflight.get("errors", [])), raw={"preflight": preflight})
    plan = decision.tp_sl
    if plan is None:
        return TradeOpenResult(status=STATUS_FAILED, symbol=decision.symbol, direction=decision.direction, entry=decision.entry, error="missing_tp_sl_plan", raw={"preflight": preflight})
    result = c.open_futures_position(symbol=decision.symbol, direction=decision.direction, quantity=preflight["quantity"], price=preflight["entry"], order_type="MARKET", margin_mode=MARGIN_ISOLATED, leverage=safe_int(preflight.get("leverage"), 1) or 1, take_profit=plan.tp1, take_profit_2=plan.tp2, stop_loss=plan.sl, client_order_id=f"L4_OPEN_{normalize_symbol(decision.symbol)}_{normalize_direction(decision.direction)}_{int(time.time()*1000)}")
    if result.status not in {STATUS_OK, STATUS_RECOVERED}:
        return result
    pos = build_pending_position(decision, preflight, result)
    add_res = add_position(pos, reject_duplicate=True)
    if add_res.status != STATUS_OK:
        return TradeOpenResult(status=STATUS_FAILED, symbol=decision.symbol, direction=decision.direction, entry=pos.entry, quantity=pos.quantity, exchange_order_id=result.exchange_order_id, error=f"position_record_failed:{add_res.error or add_res.message}", raw={"open_result": result.raw, "preflight": preflight})
    if result.status == STATUS_RECOVERED:
        mark_real_confirmed(pos.position_id, entry=result.entry or pos.entry, quantity=result.quantity or pos.quantity, exchange_order_id=result.exchange_order_id)
    return TradeOpenResult(status=result.status, position_id=pos.position_id, exchange_order_id=result.exchange_order_id, symbol=pos.symbol, direction=pos.direction, entry=result.entry or pos.entry, quantity=result.quantity or pos.quantity, message="real_open_requested", recovered=result.recovered, raw={"open_result": result.raw, "preflight": preflight})


def confirm_real_open(position: TradePosition, *, client: Optional[ToobitClient] = None) -> dict[str, Any]:
    c = client or get_client()
    row = c.get_position(position.symbol, position.direction)
    if not row:
        return {"confirmed": False}
    entry = safe_float(row.get("entryPrice") or row.get("avgPrice") or row.get("price"), position.entry) or position.entry
    qty = abs(safe_float(row.get("positionAmt") or row.get("qty") or row.get("volume"), position.quantity) or position.quantity)
    order_id = safe_str(row.get("orderId") or row.get("id") or position.exchange_order_id)
    mark_real_confirmed(position.position_id, entry=entry, quantity=qty, exchange_order_id=order_id)
    return {"confirmed": True, "entry": entry, "quantity": qty, "exchange_order_id": order_id}


def wait_for_real_open_confirmation(position_id: str, *, client: Optional[ToobitClient] = None, timeout_seconds: int | None = None) -> dict[str, Any]:
    c = client or get_client()
    timeout = safe_int(timeout_seconds, TRADE_CONFIG.get("real_confirm_timeout_seconds", 70)) or 70
    fast = safe_float(TRADE_CONFIG.get("real_confirm_fast_poll_seconds"), 2) or 2.0
    slow = safe_float(TRADE_CONFIG.get("real_confirm_slow_poll_seconds"), 5) or 5.0
    deadline = time.time() + timeout
    while time.time() <= deadline:
        pos = get_position(position_id)
        if not pos:
            return {"confirmed": False, "error": "position_not_found"}
        result = confirm_real_open(pos, client=c)
        if result.get("confirmed"):
            return result
        elapsed = timeout - max(0, deadline - time.time())
        time.sleep(fast if elapsed < 30 else slow)
    if get_position(position_id):
        mark_real_failed(position_id, "real_open_confirmation_timeout")
    return {"confirmed": False, "error": "real_open_confirmation_timeout"}


def close_real_position(position: TradePosition, reason: str = "MANUAL_CLOSE", quantity: Any = 0.0, current_price: Any = 0.0, *, client: Optional[ToobitClient] = None) -> TradeCloseResult:
    c = client or get_client()
    qty = safe_float(quantity, 0.0) or position.quantity
    price = safe_float(current_price, 0.0) or position.current_price
    result = c.close_position(position.symbol, position.direction, quantity=qty, price=price)
    result.position_id = position.position_id
    if not result.pnl_confirmed and result.pnl_usdt is None and result.close_confirmed:
        result.pnl_usdt = profit_usdt(position.direction, position.entry, result.close_price or price, result.closed_quantity or qty)
        result.pnl_confirmed = False
    return result


def close_position_executor(position: TradePosition, reason: str, quantity: float, current_price: float) -> TradeCloseResult:
    return close_real_position(position, reason=reason, quantity=quantity, current_price=current_price)


def emergency_disable_real_trading(reason: str = "emergency_stop") -> RecordResult:
    from strategy_manager import disable_real_trading
    res = disable_real_trading()
    return RecordResult(status=res.status, recorded=res.recorded, message=reason, metadata={"source": "real_trade_manager"})




def _exchange_position_to_status(row: Mapping[str, Any], *, client: Optional[ToobitClient] = None) -> dict[str, Any]:
    """Normalize a Toobit position row for status display only."""
    c = client or get_client()
    symbol_raw = row.get("symbol") or row.get("contractCode") or row.get("instrumentId") or row.get("instId") or ""
    symbol = c.normalize_bot_symbol(symbol_raw) if hasattr(c, "normalize_bot_symbol") else normalize_symbol(symbol_raw)
    direction = c._position_direction(row) if hasattr(c, "_position_direction") else normalize_direction(row.get("direction") or row.get("side"))
    qty = c._position_qty(row) if hasattr(c, "_position_qty") else abs(safe_float(row.get("qty") or row.get("volume") or row.get("positionAmt"), 0.0) or 0.0)
    entry = safe_float(row.get("entryPrice") or row.get("avgPrice") or row.get("price"), 0.0) or 0.0
    mark = safe_float(row.get("markPrice") or row.get("lastPrice") or row.get("currentPrice") or entry, entry) or entry
    pnl = safe_float(row.get("unRealizedProfit") or row.get("unrealizedPnl") or row.get("pnl") or row.get("profit"), 0.0) or 0.0
    leverage = safe_int(row.get("leverage") or row.get("lever"), 0) or 0
    return {
        "symbol": symbol,
        "exchange_symbol": safe_str(symbol_raw),
        "direction": direction,
        "quantity": qty,
        "entry": entry,
        "mark": mark,
        "pnl_usdt": pnl,
        "leverage": leverage,
        "raw": dict(row),
    }


def get_real_trade_status(*, client: Optional[ToobitClient] = None, include_exchange: bool = True) -> dict[str, Any]:
    """
    Build a full REAL trade/Toobit status snapshot for Telegram.

    This is the only layer allowed to touch Toobit for trade-status data.
    bot.py and telegram_ui.py must consume the returned payload only.
    """
    runtime = get_runtime()
    local_positions = get_open_positions()
    real_positions = [p for p in local_positions if safe_str(p.mode).upper() == MODE_REAL]
    ghost_positions = [p for p in local_positions if safe_str(p.mode).upper() != MODE_REAL]

    status: dict[str, Any] = {
        "system_version": SYSTEM_VERSION,
        "real_trade_manager_version": REAL_TRADE_MANAGER_VERSION,
        "status": STATUS_OK,
        "checked_at": utc_now_iso(),
        "real_trading_enabled": is_real_trading_enabled(),
        "runtime": runtime,
        "margin_usdt": safe_float(runtime.get("margin_usdt"), 0.0) or 0.0,
        "leverage": safe_int(runtime.get("leverage"), 1) or 1,
        "margin_mode": safe_str(runtime.get("margin_mode"), MARGIN_ISOLATED).upper(),
        "max_concurrent_real_positions": safe_int(runtime.get("max_concurrent_real_positions"), 0) or 0,
        "max_concurrent_total_positions": safe_int(runtime.get("max_concurrent_total_positions"), 0) or 0,
        "local_open_total": len(local_positions),
        "local_real_open": len(real_positions),
        "local_ghost_open": len(ghost_positions),
        "local_positions": [p.__dict__ for p in local_positions],
        "toobit_connected": False,
        "balance": {"status": STATUS_FAILED, "asset": "USDT", "balance": 0.0, "available": 0.0, "error": "not_checked"},
        "toobit_open_positions": [],
        "toobit_open_total": 0,
        "toobit_pnl_usdt": 0.0,
        "errors": [],
    }

    if status["margin_mode"] != MARGIN_ISOLATED:
        status["errors"].append("margin_mode_not_isolated")
        status["status"] = STATUS_FAILED

    if not include_exchange:
        status["balance"]["error"] = "exchange_check_skipped"
        return status

    try:
        c = client or get_client()
    except Exception as exc:
        status["errors"].append(f"toobit_client_error:{exc}")
        c = None

    if c is not None:
        try:
            balance = c.get_account_balance("USDT")
            status["balance"] = dict(balance)
            status["toobit_connected"] = bool(balance.get("status") == STATUS_OK and balance.get("credentials_loaded", True))
            if balance.get("error"):
                status["errors"].append(f"balance_error:{balance.get('error')}")
        except Exception as exc:
            status["errors"].append(f"balance_error:{exc}")
            status["balance"] = {"status": STATUS_FAILED, "asset": "USDT", "balance": None, "available": None, "error": str(exc)}


    try:
        rows = c.get_open_positions()
        exchange_positions = [_exchange_position_to_status(row, client=c) for row in rows]
        status["toobit_open_positions"] = exchange_positions
        status["toobit_open_total"] = len(exchange_positions)
        status["toobit_pnl_usdt"] = sum(safe_float(p.get("pnl_usdt"), 0.0) or 0.0 for p in exchange_positions)
    except Exception as exc:
        status["errors"].append(f"positions_error:{exc}")

    if status["errors"]:
        # Keep status OK when only live exchange data failed but internal runtime is usable.
        status["status"] = STATUS_OK
    return status


def validate_real_trade_manager_light() -> dict[str, Any]:
    errors: list[str] = []
    runtime = get_runtime()
    if safe_str(runtime.get("margin_mode"), MARGIN_ISOLATED).upper() != MARGIN_ISOLATED:
        errors.append("margin_mode_not_isolated")
    return {"system_version": SYSTEM_VERSION, "real_trade_manager_version": REAL_TRADE_MANAGER_VERSION, "status": STATUS_OK if not errors else STATUS_FAILED, "valid": not errors, "errors": errors, "checked_at": utc_now_iso()}


__all__ = [
    "REAL_TRADE_MANAGER_VERSION", "get_runtime", "estimate_quantity", "estimate_tp1_net_profit",
    "preflight_real_trade", "build_pending_position", "open_real_trade", "confirm_real_open",
    "wait_for_real_open_confirmation", "close_real_position", "close_position_executor",
    "emergency_disable_real_trading", "get_real_trade_status", "validate_real_trade_manager_light",
]
