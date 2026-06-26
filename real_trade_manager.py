"""
real_trade_manager.py
Level 4 / 1H Smart Scalp Bot

REAL trade orchestration layer.

Locked responsibility:
- Uses state_store.py for slot reservation and active signal records.
- Uses tobit_client.py as the only low-level exchange API layer.
- Opens only one TP and one SL. No multi-target logic.
- Reserves a REAL slot before the exchange request and keeps it during the
  delayed Toobit verification window.
- Confirms the REAL record only when Toobit reports the position exists.
- Releases the reserved slot only when Toobit confirms the position does not exist.
- Does not fetch OKX market data, calculate indicators, make AI decisions,
  calculate TP/SL, or render Telegram text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any, Mapping, Literal

from config import ORDER_VERIFY_DELAY, DEFAULT_LEVERAGE, DEFAULT_TRADE_DOLLAR, MARGIN_MODE
from state_store import SignalRecord, StateStore
from tobit_client import OpenOrderResult, PositionInfo, ToobitClient

try:  # Older tobit_client.py versions may not expose these names yet.
    from tobit_client import get_client as _toobit_get_client
except Exception:  # pragma: no cover
    _toobit_get_client = None  # type: ignore[assignment]

Direction = Literal["LONG", "SHORT"]
ResultStatus = Literal["OK", "FAILED", "RECOVERED"]
REAL_TRADE_MANAGER_VERSION = "simple_state_store_real_v1"
MARGIN_ISOLATED = "isolated"


@dataclass(frozen=True)
class TradeOpenResult:
    status: ResultStatus
    signal_id: str = ""
    symbol: str = ""
    direction: str = ""
    entry: float = 0.0
    tp: float = 0.0
    sl: float = 0.0
    quantity: float = 0.0
    requested_margin_usdt: float = 0.0
    actual_margin_usdt: float = 0.0
    leverage: int = 0
    exchange_order_id: str | None = None
    message: str = ""
    error: str = ""
    recovered: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"OK", "RECOVERED"}


@dataclass(frozen=True)
class TradeCloseResult:
    status: ResultStatus
    signal_id: str = ""
    symbol: str = ""
    direction: str = ""
    close_price: float | None = None
    closed_quantity: float | None = None
    pnl_usdt: float | None = None
    message: str = ""
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_client() -> ToobitClient:
    if callable(_toobit_get_client):
        return _toobit_get_client()  # type: ignore[misc]
    return ToobitClient()


def get_runtime(state: Mapping[str, Any] | Any | None = None) -> dict[str, Any]:
    settings = _settings_from_state(state)
    return {
        "real_trading_enabled": _bool_value(_get(settings, "real_trade_enabled", False)),
        "trade_margin_usdt": _float_value(_get(settings, "trade_dollar_usdt", DEFAULT_TRADE_DOLLAR), DEFAULT_TRADE_DOLLAR),
        "leverage": int(_float_value(_get(settings, "leverage", DEFAULT_LEVERAGE), DEFAULT_LEVERAGE)),
        "min_net_profit_usdt": _float_value(_get(settings, "min_net_profit_usdt", 0.10), 0.10),
        "max_slots": int(_float_value(_get(settings, "max_slots", 1), 1)),
        "margin_mode": str(_get(settings, "margin_mode", MARGIN_MODE)).lower(),
        "verify_delay_seconds": int(ORDER_VERIFY_DELAY),
    }


def estimate_quantity(entry: Any, margin_usdt: Any, leverage: Any) -> float:
    entry_f = _float_value(entry, 0.0)
    margin = _float_value(margin_usdt, 0.0)
    lev = _float_value(leverage, 1.0)
    if entry_f <= 0 or margin <= 0 or lev <= 0:
        return 0.0
    return (margin * lev) / entry_f


def preflight_real_trade(
    decision: Any,
    *,
    client: ToobitClient | None = None,
    store: StateStore | None = None,
    state: Mapping[str, Any] | Any | None = None,
) -> dict[str, Any]:
    """Validate that a REAL order is allowed before reserving a slot."""
    active_store = store or StateStore()
    snapshot = active_store.snapshot()
    runtime = get_runtime(state or snapshot)

    symbol = _symbol(decision)
    direction = _direction(decision)
    entry = _entry(decision)
    tp = _tp(decision)
    sl = _sl(decision)
    mode = str(_get(decision, "mode", "")).upper()
    signal_id = _signal_id(decision, symbol)

    errors: list[str] = []
    warnings: list[str] = []

    if not symbol:
        errors.append("symbol_missing")
    if direction not in {"LONG", "SHORT"}:
        errors.append("invalid_direction")
    if mode not in {"REAL", "TOOBIT"}:
        errors.append("decision_not_real")
    if not runtime["real_trading_enabled"]:
        errors.append("real_trading_disabled")
    if str(runtime["margin_mode"]).lower() != MARGIN_ISOLATED:
        errors.append("cross_margin_blocked")
    if entry <= 0 or tp <= 0 or sl <= 0:
        errors.append("invalid_entry_tp_sl")
    elif direction == "LONG" and not (tp > entry > sl):
        errors.append("invalid_long_tp_sl")
    elif direction == "SHORT" and not (tp < entry < sl):
        errors.append("invalid_short_tp_sl")
    if _float_value(runtime["trade_margin_usdt"], 0.0) <= 0:
        errors.append("invalid_trade_margin_usdt")
    if int(runtime["leverage"]) <= 0:
        errors.append("invalid_leverage")

    if signal_id in snapshot.active_signals or signal_id in snapshot.closed_signals:
        errors.append("duplicate_signal_id")
    if symbol:
        try:
            if active_store.has_active_symbol(symbol):
                errors.append("active_symbol_already_exists")
        except Exception as exc:
            errors.append(f"state_symbol_check_failed:{exc}")
    exchange_position_exists = False
    exchange_open_orders_exist = False
    exchange_open_positions = 0
    exchange_margin_usdt: float | None = None

    c = client
    if c is not None and symbol:
        try:
            positions = list(c.get_open_positions(symbol))
            exchange_position_exists = any(_position_matches(item, symbol, direction) for item in positions)
            if exchange_position_exists:
                errors.append("duplicate_exchange_position")
        except Exception as exc:
            errors.append(f"exchange_position_check_failed:{exc}")

        try:
            orders = list(c.get_open_orders(symbol))
            exchange_open_orders_exist = len(orders) > 0
            if exchange_open_orders_exist:
                errors.append("open_exchange_order_exists")
        except Exception as exc:
            errors.append(f"exchange_open_orders_check_failed:{exc}")

        try:
            all_positions = list(c.get_open_positions())
            exchange_open_positions = len(all_positions)
        except Exception as exc:
            warnings.append(f"exchange_open_positions_count_unreadable:{exc}")

        try:
            exchange_margin_usdt = float(c.get_wallet_margin_usdt())
        except Exception as exc:
            warnings.append(f"wallet_margin_unreadable:{exc}")

        # Keep the slot counter synchronized with Toobit before judging free slots.
        # If this write fails, fail closed rather than opening a duplicate REAL.
        try:
            active_store.sync_toobit_status(
                margin_usdt=exchange_margin_usdt,
                open_positions=exchange_open_positions,
            )
            snapshot = active_store.snapshot()
        except Exception as exc:
            errors.append(f"state_exchange_sync_failed:{exc}")

    if snapshot.free_slots <= 0 and "no_free_real_slot" not in errors:
        errors.append("no_free_real_slot")

    return {
        "status": "OK" if not errors else "FAILED",
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "symbol": symbol,
        "direction": direction,
        "signal_id": signal_id,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "trade_margin_usdt": runtime["trade_margin_usdt"],
        "leverage": runtime["leverage"],
        "margin_mode": runtime["margin_mode"],
        "quantity_estimate": estimate_quantity(entry, runtime["trade_margin_usdt"], runtime["leverage"]),
        "state_free_slots": snapshot.free_slots,
        "state_used_slots": snapshot.used_slots,
        "state_exchange_open_positions": snapshot.toobit_open_positions,
        "exchange_position_exists": exchange_position_exists,
        "exchange_open_orders_exist": exchange_open_orders_exist,
        "exchange_open_positions": exchange_open_positions,
        "exchange_margin_usdt": exchange_margin_usdt,
        "checked_at": time(),
    }


def open_real_trade(
    decision: Any,
    *,
    client: ToobitClient | None = None,
    store: StateStore | None = None,
    state: Mapping[str, Any] | Any | None = None,
) -> TradeOpenResult:
    """Open one REAL trade and synchronize it with StateStore.active_signals.

    Flow:
    1. Verify REAL is allowed.
    2. Reserve one TOOBIT slot in StateStore before the exchange call.
    3. Ask ToobitClient to set isolated margin, set leverage, use configured margin,
       attach TP/SL, and verify the position after the configured delay.
    4. Confirm the StateStore record if the position exists; otherwise release it.
    """
    active_store = store or StateStore()
    c = client or get_client()

    preflight = preflight_real_trade(decision, client=c, store=active_store, state=state)
    if not preflight.get("ok"):
        return TradeOpenResult(
            status="FAILED",
            signal_id=str(preflight.get("signal_id", "")),
            symbol=str(preflight.get("symbol", "")),
            direction=str(preflight.get("direction", "")),
            entry=_float_value(preflight.get("entry"), 0.0),
            tp=_float_value(preflight.get("tp"), 0.0),
            sl=_float_value(preflight.get("sl"), 0.0),
            error=";".join(preflight.get("errors", [])),
            raw={"preflight": preflight},
        )

    record = active_store.register_signal(
        signal_id=str(preflight["signal_id"]),
        symbol=str(preflight["symbol"]),
        direction=preflight["direction"],
        requested_mode="TOOBIT",
        entry=float(preflight["entry"]),
        tp=float(preflight["tp"]),
        sl=float(preflight["sl"]),
    )

    try:
        result = c.open_position_with_tp_sl(
            symbol=record.symbol,
            direction=record.direction,
            margin_usdt=float(preflight["trade_margin_usdt"]),
            leverage=int(preflight["leverage"]),
            tp_price=record.tp,
            sl_price=record.sl,
            price=record.entry,
        )
    except Exception as exc:
        if _looks_uncertain(exc):
            exists = _safe_verify_position(c, record.symbol, record.direction)
            if exists is True:
                active_store.confirm_real_open(record.signal_id)
                _sync_store_from_exchange(active_store, c)
                return TradeOpenResult(
                    status="RECOVERED",
                    signal_id=record.signal_id,
                    symbol=record.symbol,
                    direction=record.direction,
                    entry=record.entry,
                    tp=record.tp,
                    sl=record.sl,
                    leverage=int(preflight["leverage"]),
                    requested_margin_usdt=float(preflight["trade_margin_usdt"]),
                    message="exchange_error_but_position_found_after_verification",
                    recovered=True,
                    raw={"preflight": preflight, "exception": str(exc)},
                )
            return TradeOpenResult(
                status="FAILED",
                signal_id=record.signal_id,
                symbol=record.symbol,
                direction=record.direction,
                entry=record.entry,
                tp=record.tp,
                sl=record.sl,
                error=f"exchange_uncertain_and_position_not_confirmed:{exc}",
                raw={"preflight": preflight, "slot_kept": True},
            )
        active_store.cancel_unconfirmed_real(record.signal_id, reason=str(exc))
        return TradeOpenResult(
            status="FAILED",
            signal_id=record.signal_id,
            symbol=record.symbol,
            direction=record.direction,
            entry=record.entry,
            tp=record.tp,
            sl=record.sl,
            error=f"toobit_open_failed:{exc}",
            raw={"preflight": preflight},
        )

    if bool(getattr(result, "opened", False)):
        active_store.confirm_real_open(record.signal_id)
        _sync_store_from_exchange(active_store, c)
        return _open_result_from_toobit(record, result, status="OK", preflight=preflight)

    reason = str(getattr(result, "reason", "real_position_not_confirmed"))
    if _looks_uncertain(reason):
        exists = _safe_verify_position(c, record.symbol, record.direction)
        if exists is True:
            active_store.confirm_real_open(record.signal_id)
            _sync_store_from_exchange(active_store, c)
            return _open_result_from_toobit(record, result, status="RECOVERED", preflight=preflight, recovered=True)
        return TradeOpenResult(
            status="FAILED",
            signal_id=record.signal_id,
            symbol=record.symbol,
            direction=record.direction,
            entry=record.entry,
            tp=record.tp,
            sl=record.sl,
            error=reason,
            raw={"preflight": preflight, "toobit_result": _object_to_dict(result), "slot_kept": True},
        )

    active_store.cancel_unconfirmed_real(record.signal_id, reason=reason)
    return _open_result_from_toobit(record, result, status="FAILED", preflight=preflight, error=reason)


def exchange_position_checker(record: SignalRecord | Any, *, client: ToobitClient | None = None) -> dict[str, Any]:
    """Adapter for position_monitor.py. Read-only; no order side effects."""
    c = client or get_client()
    symbol = _normalize_symbol(_get(record, "symbol", ""))
    direction = _normalize_direction(_get(record, "direction", ""))
    try:
        positions = list(c.get_open_positions(symbol))
        found = any(_position_matches(item, symbol, direction) for item in positions)
        return {
            "status": "OK",
            "exists": found,
            "open": found,
            "position_exists": found,
            "found": found,
            "symbol": symbol,
            "direction": direction,
            "checked_at": time(),
        }
    except Exception as exc:
        return {
            "status": "FAILED",
            "exists": None,
            "open": None,
            "position_exists": None,
            "found": None,
            "symbol": symbol,
            "direction": direction,
            "error": str(exc),
            "checked_at": time(),
        }


def closed_pnl_reader(record: SignalRecord | Any, *, client: ToobitClient | None = None) -> dict[str, Any]:
    """Adapter for position_monitor.py. Reads closed PnL if the client supports it."""
    c = client or get_client()
    symbol = _normalize_symbol(_get(record, "symbol", ""))
    direction = _normalize_direction(_get(record, "direction", ""))
    for name in ("wait_for_closed_position_pnl", "get_closed_position_pnl"):
        fn = getattr(c, name, None)
        if not callable(fn):
            continue
        try:
            data = fn(symbol, direction)
        except TypeError:
            try:
                data = fn(symbol=symbol, direction=direction)
            except Exception as exc:
                return {"status": "FAILED", "confirmed": False, "pnl_usdt": None, "error": str(exc)}
        except Exception as exc:
            return {"status": "FAILED", "confirmed": False, "pnl_usdt": None, "error": str(exc)}
        if isinstance(data, Mapping):
            out = dict(data)
            out.setdefault("status", "OK" if not out.get("error") else "FAILED")
            return out
    return {"status": "FAILED", "confirmed": False, "pnl_usdt": None, "error": "closed_pnl_reader_unavailable"}


def close_real_position(record: SignalRecord | Any, reason: str = "MANUAL_CLOSE", quantity: Any = 0.0, current_price: Any = 0.0, *, client: ToobitClient | None = None) -> TradeCloseResult:
    c = client or get_client()
    fn = getattr(c, "close_position", None) or getattr(c, "close_futures_position", None)
    if not callable(fn):
        return TradeCloseResult(status="FAILED", signal_id=str(_get(record, "signal_id", "")), symbol=str(_get(record, "symbol", "")), direction=str(_get(record, "direction", "")), error="toobit_close_not_available")
    symbol = _normalize_symbol(_get(record, "symbol", ""))
    direction = _normalize_direction(_get(record, "direction", ""))
    try:
        result = fn(symbol, direction, quantity=quantity, price=current_price, reason=reason)
    except TypeError:
        try:
            result = fn(symbol, direction)
        except Exception as exc:
            return TradeCloseResult(status="FAILED", signal_id=str(_get(record, "signal_id", "")), symbol=symbol, direction=direction, error=str(exc))
    except Exception as exc:
        return TradeCloseResult(status="FAILED", signal_id=str(_get(record, "signal_id", "")), symbol=symbol, direction=direction, error=str(exc))
    return TradeCloseResult(status="OK", signal_id=str(_get(record, "signal_id", "")), symbol=symbol, direction=direction, message="close_requested", raw=_object_to_dict(result))


close_position_executor = close_real_position


def get_real_trade_status(*, client: ToobitClient | None = None, store: StateStore | None = None, include_exchange: bool = True) -> dict[str, Any]:
    active_store = store or StateStore()
    snapshot = active_store.snapshot()
    runtime = get_runtime(snapshot)
    status: dict[str, Any] = {
        "status": "OK",
        "real_trade_manager_version": REAL_TRADE_MANAGER_VERSION,
        "real_trading_enabled": snapshot.settings.real_trade_enabled,
        "runtime": runtime,
        "state_margin_usdt": snapshot.toobit_margin_usdt,
        "state_open_positions": snapshot.toobit_open_positions,
        "active_real_signals": snapshot.reserved_real_slots,
        "pending_real_signals": snapshot.pending_real_slots,
        "free_slots": snapshot.free_slots,
        "active_signals": len(snapshot.active_signals),
        "checked_at": time(),
        "errors": [],
    }
    if not include_exchange:
        return status
    try:
        c = client or get_client()
        margin = c.get_wallet_margin_usdt()
        positions = list(c.get_open_positions())
        active_store.sync_toobit_status(margin_usdt=float(margin), open_positions=len(positions))
        status.update({"toobit_margin_usdt": float(margin), "toobit_open_positions": len(positions), "toobit_connected": True})
    except Exception as exc:
        status["status"] = "FAILED"
        status["toobit_connected"] = False
        status["errors"].append(str(exc))
    return status


def emergency_disable_real_trading(reason: str = "emergency_stop", *, store: StateStore | None = None) -> dict[str, Any]:
    active_store = store or StateStore()
    active_store.set_real_trade_enabled(False)
    return {"status": "OK", "recorded": True, "message": reason}


def validate_real_trade_manager_light() -> dict[str, Any]:
    errors: list[str] = []
    if MARGIN_ISOLATED != "isolated":
        errors.append("margin_constant_invalid")
    return {"status": "OK" if not errors else "FAILED", "valid": not errors, "errors": errors, "version": REAL_TRADE_MANAGER_VERSION}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _open_result_from_toobit(record: SignalRecord, result: OpenOrderResult, *, status: ResultStatus, preflight: Mapping[str, Any], error: str = "", recovered: bool = False) -> TradeOpenResult:
    return TradeOpenResult(
        status=status,
        signal_id=record.signal_id,
        symbol=record.symbol,
        direction=record.direction,
        entry=_float_value(getattr(result, "entry_price", record.entry), record.entry),
        tp=_float_value(getattr(result, "tp_price", record.tp), record.tp),
        sl=_float_value(getattr(result, "sl_price", record.sl), record.sl),
        quantity=_float_value(getattr(result, "quantity", 0.0), 0.0),
        requested_margin_usdt=_float_value(getattr(result, "requested_margin_usdt", preflight.get("trade_margin_usdt")), 0.0),
        actual_margin_usdt=_float_value(getattr(result, "actual_margin_usdt", 0.0), 0.0),
        leverage=int(_float_value(getattr(result, "leverage", preflight.get("leverage", 0)), 0)),
        exchange_order_id=getattr(result, "order_id", None),
        message=str(getattr(result, "reason", "")),
        error=error,
        recovered=recovered,
        raw={"preflight": dict(preflight), "toobit_result": _object_to_dict(result)},
    )


def _safe_verify_position(client: ToobitClient, symbol: str, direction: str) -> bool | None:
    try:
        positions = list(client.get_open_positions(symbol))
        return any(_position_matches(item, symbol, direction) for item in positions)
    except Exception:
        return None


def _position_matches(item: Any, symbol: str, direction: str) -> bool:
    data = _object_to_dict(item)
    row_symbol = _normalize_symbol(data.get("symbol") or data.get("contractCode") or symbol)
    row_direction = _normalize_direction(data.get("side") or data.get("direction") or data.get("positionSide"))
    qty = _float_value(data.get("quantity") or data.get("qty") or data.get("positionAmt") or data.get("size"), 0.0)
    if row_direction not in {"LONG", "SHORT"} and qty != 0:
        row_direction = "LONG" if qty > 0 else "SHORT"
    return row_symbol == _normalize_symbol(symbol) and row_direction == _normalize_direction(direction) and abs(qty) > 0


def _settings_from_state(state: Mapping[str, Any] | Any | None) -> Any:
    if state is None:
        return None
    return _get(state, "settings", state)


def _signal_id(decision: Any, symbol: str) -> str:
    value = str(_get(decision, "signal_id", "")).strip()
    return value or f"REAL_{_normalize_symbol(symbol)}_{int(time() * 1000)}"


def _symbol(decision: Any) -> str:
    return _normalize_symbol(_get(decision, "symbol", ""))


def _direction(decision: Any) -> str:
    return _normalize_direction(_get(decision, "direction", ""))


def _entry(decision: Any) -> float:
    return _float_value(_get(decision, "entry", 0.0), 0.0)


def _tp(decision: Any) -> float:
    direct = _float_value(_get(decision, "tp", 0.0), 0.0)
    if direct > 0:
        return direct
    plan = _get(decision, "tp_sl", None)
    return _float_value(_get(plan, "tp", 0.0), 0.0)


def _sl(decision: Any) -> float:
    direct = _float_value(_get(decision, "sl", 0.0), 0.0)
    if direct > 0:
        return direct
    plan = _get(decision, "tp_sl", None)
    return _float_value(_get(plan, "sl", 0.0), 0.0)


def _normalize_symbol(value: Any) -> str:
    return str(value or "").upper().replace("-", "").replace("_", "").replace("SWAP", "").strip()


def _normalize_direction(value: Any) -> str:
    text = str(value or "").upper().strip()
    if text in {"BUY", "LONG"}:
        return "LONG"
    if text in {"SELL", "SHORT"}:
        return "SHORT"
    return text


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _bool_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled", "active", "فعال"}
    return bool(value)


def _object_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    out: dict[str, Any] = {}
    for name in (
        "symbol", "side", "direction", "quantity", "entry_price", "unrealized_pnl",
        "order_id", "requested_margin_usdt", "actual_margin_usdt", "leverage",
        "tp_price", "sl_price", "opened", "reason", "raw",
    ):
        if hasattr(value, name):
            item = getattr(value, name)
            out[name] = item if not isinstance(item, Mapping) else dict(item)
    return out


def _looks_uncertain(value: Any) -> bool:
    text = str(value or "").lower()
    markers = (
        "timeout", "timed out", "network", "connection", "temporary", "temporarily",
        "rate limit", "429", "500", "502", "503", "504", "unknown", "api",
        "request", "read", "check", "confirm", "unavailable",
    )
    return any(marker in text for marker in markers)


def _sync_store_from_exchange(store: StateStore, client: ToobitClient) -> None:
    try:
        margin = float(client.get_wallet_margin_usdt())
    except Exception:
        margin = None  # keep open-position count synchronized even if wallet read fails
    positions = list(client.get_open_positions())
    store.sync_toobit_status(margin_usdt=margin, open_positions=len(positions))


__all__ = [
    "REAL_TRADE_MANAGER_VERSION",
    "MARGIN_ISOLATED",
    "TradeOpenResult",
    "TradeCloseResult",
    "get_client",
    "get_runtime",
    "estimate_quantity",
    "preflight_real_trade",
    "open_real_trade",
    "exchange_position_checker",
    "closed_pnl_reader",
    "close_real_position",
    "close_position_executor",
    "get_real_trade_status",
    "emergency_disable_real_trading",
    "validate_real_trade_manager_light",
]
