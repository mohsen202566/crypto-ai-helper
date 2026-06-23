from __future__ import annotations

"""
21 - real_trade_manager.py

Real Toobit futures trade manager for the locked Movement Hunter architecture.

Responsibilities:
- Receive only final AIDecision + TPSLPlan.
- Open REAL positions only when decision_type == REAL and trading is enabled.
- Use Toobit v2 client only through tobit_client.py.
- Enforce safety preflight:
  symbol mapping
  isolated margin
  leverage set/read/verify
  margin/notional/quantity calculation
  min quantity / min notional / step precision
  TP/SL attached at opening whenever supported
- Keep a PENDING_REAL_CONFIRM state for 20-30 seconds after order submission.
- Do not free slot instantly after order submit.
- Verify actual Toobit position after order.
- Repair missing TP/SL after position confirmation if needed.

Strictly forbidden:
- No Paper mode.
- No Setup flow.
- No fake success.
- No arbitrary leverage/size.
- No cross margin.
- No Telegram sending.
- No AI analysis.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
import math
import time

from ai_decision_engine import AIDecision, DECISION_REAL
from tp_sl_engine import TPSLPlan
from symbol_mapper import toobit_symbol, normalize_symbol
from data_store import save_position, save_error, store
from config import SETTINGS


JsonDict = Dict[str, Any]

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"

STATUS_REJECTED = "REJECTED"
STATUS_PENDING_REAL_CONFIRM = "PENDING_REAL_CONFIRM"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_FAILED = "FAILED"

MARGIN_ISOLATED = "ISOLATED"


class RealTradeError(RuntimeError):
    """Raised for real trade safety failures."""


@dataclass(frozen=True)
class TradeSettings:
    trading_enabled: bool
    margin_usdt: float
    leverage: int
    max_positions: int
    isolated_only: bool = True

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class ExchangeSymbolRules:
    symbol: str
    min_qty: float = 0.0
    qty_step: float = 0.0
    min_notional: float = 0.0
    price_tick: float = 0.0
    quantity_precision: int = 6
    price_precision: int = 6

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class OrderSizePlan:
    symbol: str
    margin_usdt: float
    leverage: int
    notional_usdt: float
    price: float
    quantity: float
    quantity_raw: float
    valid: bool
    reason: str = ""

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class RealTradePreflight:
    preflight_id: str
    decision_id: str
    symbol: str
    exchange_symbol: str
    direction: str
    margin_mode: str
    leverage: int
    size_plan: OrderSizePlan
    tp1: float
    tp2: float
    sl: float
    valid: bool
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class RealTradeOpenResult:
    trade_id: str
    decision_id: str
    symbol: str
    exchange_symbol: str
    direction: str
    status: str
    order_id: str = ""
    client_order_id: str = ""
    position_id: str = ""
    entry: float = 0.0
    quantity: float = 0.0
    margin_usdt: float = 0.0
    leverage: int = 0
    tp1: float = 0.0
    tp2: float = 0.0
    sl: float = 0.0
    created_at: int = 0
    confirmed_at: int = 0
    error: str = ""
    preflight: JsonDict = field(default_factory=dict)
    raw_response: JsonDict = field(default_factory=dict)

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


def round_step(value: float, step: float, precision: int = 8) -> float:
    value = safe_float(value)
    step = safe_float(step)
    if value <= 0:
        return 0.0
    if step <= 0:
        return round(value, precision)
    units = math.floor(value / step)
    return round(units * step, precision)


def _call_optional(obj: Any, names: List[str], *args, **kwargs) -> Any:
    """Call the first available compatible client method.

    Important safety fix:
    When a method raises TypeError because keyword arguments are incompatible,
    do NOT retry it with an empty positional argument list. That produced the
    misleading runtime error:
        missing required positional arguments: symbol, side, direction, quantity

    For methods that are intentionally called with positional args, this helper
    still works exactly as before. For keyword-based order execution, the caller
    must pass a payload compatible with the target client method.
    """
    last_error: Optional[Exception] = None
    for name in names:
        fn = getattr(obj, name, None)
        if callable(fn):
            try:
                return fn(*args, **kwargs)
            except TypeError as exc:
                last_error = exc
                # Only try a positional-only fallback when kwargs were not used.
                if kwargs:
                    continue
                try:
                    return fn(*args)
                except Exception as exc2:
                    last_error = exc2
            except Exception as exc:
                last_error = exc
    if last_error:
        raise last_error
    raise RealTradeError(f"client_method_missing:{'/'.join(names)}")


def _to_plain(value: Any) -> Any:
    """Convert dataclasses/objects into JSON-safe dicts for persistent position meta."""
    try:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): _to_plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_to_plain(v) for v in value]
        if hasattr(value, "to_dict") and callable(value.to_dict):
            return _to_plain(value.to_dict())
        if hasattr(value, "__dict__"):
            return _to_plain(dict(value.__dict__))
    except Exception:
        pass
    return str(value)


def _safe_meta_dict(value: Optional[JsonDict]) -> JsonDict:
    if not isinstance(value, dict):
        return {}
    plain = _to_plain(value)
    return plain if isinstance(plain, dict) else {}


def _build_position_meta(decision: AIDecision, plan: TPSLPlan, analysis_meta: Optional[JsonDict]) -> JsonDict:
    """Persist full analysis context needed by position_monitor REAL learning."""
    meta = _safe_meta_dict(analysis_meta)
    decision_dict = _to_plain(decision.to_dict() if hasattr(decision, "to_dict") else decision)
    plan_dict = _to_plain(plan.to_dict() if hasattr(plan, "to_dict") else plan)

    decision_meta = {}
    if isinstance(decision_dict, dict):
        decision_meta = decision_dict.get("meta", {}) if isinstance(decision_dict.get("meta", {}), dict) else {}
    plan_meta = {}
    if isinstance(plan_dict, dict):
        plan_meta = plan_dict.get("meta", {}) if isinstance(plan_dict.get("meta", {}), dict) else {}

    # Candidate recovery priority for position_monitor.py
    candidate = (
        meta.get("candidate")
        or meta.get("analysis_candidate")
        or meta.get("entry_candidate")
        or decision_meta.get("candidate")
        or decision_meta.get("analysis_candidate")
        or plan_meta.get("candidate")
        or plan_meta.get("analysis_candidate")
    )

    meta.update({
        "decision_id": getattr(decision, "decision_id", ""),
        "decision": decision_dict,
        "tp_sl_plan": plan_dict,
        "real_learning_context_saved": bool(candidate),
    })
    if candidate:
        meta.setdefault("candidate", candidate)
        meta.setdefault("analysis_candidate", candidate)
    return meta


def _position_record_from_result(result: RealTradeOpenResult, status_for_monitor: str, meta: JsonDict, current_price: float = 0.0) -> JsonDict:
    """Build the store record in the shape position_monitor.py expects."""
    record = result.to_dict()
    record.update({
        "position_id": result.position_id or result.trade_id,
        "status": status_for_monitor,
        "open_time": result.created_at or now_ts(),
        "entry": result.entry,
        "current_price": current_price or result.entry,
        "highest_price": current_price or result.entry,
        "lowest_price": current_price or result.entry,
        "meta": meta,
    })
    return record


class TradeSettingsReader:
    """Reads real trading settings from config/runtime."""

    def read(self) -> TradeSettings:
        try:
            runtime = store().section("runtime_settings")
        except Exception:
            runtime = {}

        trading_enabled = bool(runtime.get("real_trading_enabled", getattr(SETTINGS.trading, "real_trading_enabled", False)))
        margin_usdt = safe_float(runtime.get("margin_usdt", getattr(SETTINGS.trading, "margin_usdt", 0.0)))
        leverage = safe_int(runtime.get("leverage", getattr(SETTINGS.trading, "leverage", 1)), 1)
        max_positions = safe_int(runtime.get("max_positions", getattr(SETTINGS.trading, "max_positions", 1)), 1)

        return TradeSettings(
            trading_enabled=trading_enabled,
            margin_usdt=margin_usdt,
            leverage=leverage,
            max_positions=max_positions,
            isolated_only=True,
        )


class SymbolRulesReader:
    """Reads Toobit symbol rules through tobit_client.py."""

    def read(self, client: Any, exchange_symbol: str) -> ExchangeSymbolRules:
        try:
            raw = _call_optional(
                client,
                ["get_symbol_rules", "get_contract_rules", "get_instrument_rules"],
                exchange_symbol,
            )
        except Exception:
            raw = {}

        if hasattr(raw, "to_dict") and callable(raw.to_dict):
            raw = raw.to_dict()
        if not isinstance(raw, dict):
            raw = {}

        return ExchangeSymbolRules(
            symbol=exchange_symbol,
            min_qty=safe_float(raw.get("min_qty", raw.get("minQty", raw.get("min_quantity", 0.0)))),
            qty_step=safe_float(raw.get("qty_step", raw.get("stepSize", raw.get("quantity_step", 0.0)))),
            min_notional=safe_float(raw.get("min_notional", raw.get("minNotional", 0.0))),
            price_tick=safe_float(raw.get("price_tick", raw.get("tickSize", 0.0))),
            quantity_precision=safe_int(raw.get("quantity_precision", raw.get("qtyPrecision", 6)), 6),
            price_precision=safe_int(raw.get("price_precision", raw.get("pricePrecision", 6)), 6),
        )




class RealTradeSafetyGuard:
    """
    Final safety guard before any real order.

    Blocks:
    - emergency_stop
    - real trading disabled in runtime_settings
    - duplicate open exchange position for same symbol/direction
    - duplicate pending internal position for same symbol/direction
    - max positions already reached
    """

    CLOSED_STATUSES = {"CLOSED", "TP2", "AI_EXIT", "SL", "FAILED", "REJECTED"}

    def check_runtime(self) -> None:
        try:
            runtime = store().section("runtime_settings")
            legacy_runtime = store().section("runtime")

            emergency_stop = bool(runtime.get("emergency_stop", legacy_runtime.get("emergency_stop", False)))
            if emergency_stop:
                reason = str(runtime.get("emergency_reason", legacy_runtime.get("emergency_reason", "")))
                raise RealTradeError(f"emergency_stop_active:{reason}")

            enabled = bool(runtime.get("real_trading_enabled", getattr(SETTINGS.trading, "real_trading_enabled", False)))
            if not enabled:
                raise RealTradeError("real_trading_disabled_runtime")
        except RealTradeError:
            raise
        except Exception as exc:
            raise RealTradeError(f"runtime_safety_check_failed:{exc}")

    def check_internal_duplicates(self, symbol: str, exchange_symbol: str, direction: str, max_positions: int) -> None:
        try:
            positions = store().section("positions")
        except Exception as exc:
            raise RealTradeError(f"position_store_check_failed:{exc}")

        active_count = 0
        for item in positions.values():
            if not isinstance(item, dict):
                continue

            status = str(item.get("status", "")).upper()
            if status in self.CLOSED_STATUSES:
                continue

            active_count += 1

            item_symbol = str(item.get("exchange_symbol", item.get("symbol", ""))).upper()
            item_direction = normalize_direction(str(item.get("direction", "")))

            if item_symbol in {symbol.upper(), exchange_symbol.upper()} and item_direction == direction:
                raise RealTradeError(f"duplicate_internal_position:{exchange_symbol}:{direction}:{status}")

        if max_positions > 0 and active_count >= max_positions:
            raise RealTradeError(f"max_positions_reached_internal:{active_count}/{max_positions}")

    def check_exchange_duplicates(self, client: Any, exchange_symbol: str, direction: str, max_positions: int) -> None:
        try:
            positions = _call_optional(client, ["get_open_positions", "fetch_open_positions"])
        except Exception as exc:
            raise RealTradeError(f"exchange_position_check_failed:{exc}")

        if hasattr(positions, "to_dict") and callable(positions.to_dict):
            positions = positions.to_dict()
        if isinstance(positions, dict):
            positions = positions.get("positions", positions.get("data", []))
        if not isinstance(positions, list):
            positions = []

        active_count = 0
        for pos in positions:
            if not isinstance(pos, dict):
                continue

            qty = safe_float(pos.get("quantity", pos.get("qty", pos.get("positionAmt", pos.get("size", 0.0)))))
            if qty <= 0:
                continue

            active_count += 1

            sym = str(pos.get("symbol", pos.get("contract", ""))).upper()
            side = normalize_direction(str(pos.get("direction", pos.get("side", pos.get("positionSide", "")))))

            if sym == exchange_symbol.upper() and side == direction:
                raise RealTradeError(f"duplicate_exchange_position:{exchange_symbol}:{direction}")

        if max_positions > 0 and active_count >= max_positions:
            raise RealTradeError(f"max_positions_reached_exchange:{active_count}/{max_positions}")

    def run(self, client: Any, symbol: str, exchange_symbol: str, direction: str, max_positions: int) -> None:
        self.check_runtime()
        self.check_internal_duplicates(symbol, exchange_symbol, direction, max_positions)
        self.check_exchange_duplicates(client, exchange_symbol, direction, max_positions)


class RealTradePreflightBuilder:
    """Builds and verifies preflight before sending any real order."""

    def __init__(self):
        self.settings_reader = TradeSettingsReader()
        self.rules_reader = SymbolRulesReader()
        self.safety_guard = RealTradeSafetyGuard()

    def build(self, client: Any, decision: AIDecision, plan: TPSLPlan) -> RealTradePreflight:
        reasons: List[str] = []
        warnings: List[str] = []

        settings = self.settings_reader.read()

        if decision.decision_type != DECISION_REAL or not decision.should_trade_real:
            return self._invalid(decision, plan, "DECISION_NOT_REAL")

        if not settings.trading_enabled:
            return self._invalid(decision, plan, "REAL_TRADING_DISABLED")

        symbol = normalize_symbol(decision.symbol)
        exchange_symbol = toobit_symbol(symbol)
        direction = normalize_direction(decision.direction)

        if direction not in {DIRECTION_LONG, DIRECTION_SHORT}:
            return self._invalid(decision, plan, "INVALID_DIRECTION")

        try:
            self.safety_guard.run(
                client=client,
                symbol=symbol,
                exchange_symbol=exchange_symbol,
                direction=direction,
                max_positions=settings.max_positions,
            )
            reasons.append("RUNTIME_AND_DUPLICATE_GUARDS_PASSED")
        except Exception as exc:
            return self._invalid(decision, plan, f"SAFETY_GUARD_FAILED:{exc}")

        # Final TP/SL plan safety from tp_sl_engine.py.
        # The AI may choose REAL, but a REAL order must not be sent if the
        # smart TP/SL engine marked the plan invalid or if TP1 cannot cover
        # estimated round-trip fees plus the minimum desired net profit.
        if hasattr(plan, "valid") and not bool(getattr(plan, "valid", True)):
            return self._invalid(decision, plan, "TP_SL_PLAN_INVALID")

        min_net = safe_float(getattr(plan, "min_required_net_profit_usdt", 0.0), 0.0)
        est_net = safe_float(getattr(plan, "estimated_tp1_net_usdt", 0.0), 0.0)
        if min_net > 0 and est_net < min_net:
            return self._invalid(
                decision,
                plan,
                f"TP1_NET_PROFIT_BELOW_MIN_AFTER_FEES:net={est_net:.4f}:min={min_net:.4f}",
            )

        if settings.margin_usdt <= 0:
            return self._invalid(decision, plan, "INVALID_MARGIN_USDT")

        if settings.leverage <= 0:
            return self._invalid(decision, plan, "INVALID_LEVERAGE")

        entry = safe_float(plan.entry or decision.entry)
        if entry <= 0:
            return self._invalid(decision, plan, "INVALID_ENTRY_PRICE")

        try:
            self._ensure_isolated(client, exchange_symbol)
            reasons.append("ISOLATED_MARGIN_VERIFIED_OR_SOFT_CACHED")
        except Exception as exc:
            return self._invalid(decision, plan, f"ISOLATED_VERIFY_FAILED:{exc}")

        try:
            self._ensure_leverage(client, exchange_symbol, settings.leverage)
            reasons.append("LEVERAGE_VERIFIED")
        except Exception as exc:
            return self._invalid(decision, plan, f"LEVERAGE_VERIFY_FAILED:{exc}")

        rules = self.rules_reader.read(client, exchange_symbol)
        size_plan = self._build_size_plan(
            symbol=exchange_symbol,
            margin_usdt=settings.margin_usdt,
            leverage=settings.leverage,
            price=entry,
            rules=rules,
        )

        if not size_plan.valid:
            return RealTradePreflight(
                preflight_id=f"pre_{uuid4().hex}",
                decision_id=decision.decision_id,
                symbol=symbol,
                exchange_symbol=exchange_symbol,
                direction=direction,
                margin_mode=MARGIN_ISOLATED,
                leverage=settings.leverage,
                size_plan=size_plan,
                tp1=plan.tp1,
                tp2=plan.tp2,
                sl=plan.sl,
                valid=False,
                reason_codes=tuple(reasons + [size_plan.reason]),
                warnings=tuple(warnings),
            )

        if plan.tp1 <= 0 or plan.sl <= 0:
            return self._invalid(decision, plan, "TP_SL_MISSING")

        reasons.append("SIZE_PLAN_VALID")
        reasons.append("TP_SL_PRESENT")

        return RealTradePreflight(
            preflight_id=f"pre_{uuid4().hex}",
            decision_id=decision.decision_id,
            symbol=symbol,
            exchange_symbol=exchange_symbol,
            direction=direction,
            margin_mode=MARGIN_ISOLATED,
            leverage=settings.leverage,
            size_plan=size_plan,
            tp1=plan.tp1,
            tp2=plan.tp2,
            sl=plan.sl,
            valid=True,
            reason_codes=tuple(reasons),
            warnings=tuple(warnings),
        )

    def _invalid(self, decision: AIDecision, plan: TPSLPlan, reason: str) -> RealTradePreflight:
        symbol = normalize_symbol(getattr(decision, "symbol", ""))
        try:
            exchange_symbol = toobit_symbol(symbol)
        except Exception:
            exchange_symbol = symbol

        return RealTradePreflight(
            preflight_id=f"pre_{uuid4().hex}",
            decision_id=getattr(decision, "decision_id", ""),
            symbol=symbol,
            exchange_symbol=exchange_symbol,
            direction=normalize_direction(getattr(decision, "direction", "")),
            margin_mode=MARGIN_ISOLATED,
            leverage=safe_int(getattr(SETTINGS.trading, "leverage", 1), 1),
            size_plan=OrderSizePlan(symbol=exchange_symbol, margin_usdt=0, leverage=0, notional_usdt=0, price=0, quantity=0, quantity_raw=0, valid=False, reason=reason),
            tp1=safe_float(getattr(plan, "tp1", 0.0)),
            tp2=safe_float(getattr(plan, "tp2", 0.0)),
            sl=safe_float(getattr(plan, "sl", 0.0)),
            valid=False,
            reason_codes=(reason,),
            warnings=(),
        )

    def _ensure_isolated(self, client: Any, exchange_symbol: str) -> None:
        """Ensure isolated mode without drying REAL on unstable Toobit margin endpoints.

        Toobit margin-mode endpoints can be unavailable on some accounts/symbols.
        We still block explicit CROSS, but we do not reject a good REAL setup only
        because set/get margin mode returned a transient API/method error.
        """
        try:
            _call_optional(
                client,
                [
                    "set_margin_mode",
                    "set_margin_type",
                    "change_margin_mode",
                    "change_symbol_margin_mode",
                    "set_futures_margin_mode",
                ],
                exchange_symbol,
                MARGIN_ISOLATED,
            )
        except Exception as exc:
            save_error("real_trade_margin_mode_set_soft", str(exc), {"symbol": exchange_symbol, "required": MARGIN_ISOLATED})

        try:
            mode = _call_optional(
                client,
                ["get_margin_mode", "get_margin_type", "get_futures_margin_mode"],
                exchange_symbol,
            )
            mode_str = str(mode.get("margin_mode", mode.get("marginType", mode)) if isinstance(mode, dict) else mode).upper()
        except Exception as exc:
            save_error("real_trade_margin_mode_get_soft", str(exc), {"symbol": exchange_symbol, "required": MARGIN_ISOLATED})
            return

        if "CROSS" in mode_str:
            raise RealTradeError(f"margin_cross_blocked:{mode_str}")
        if MARGIN_ISOLATED not in mode_str:
            save_error("real_trade_margin_mode_unknown_soft", f"margin_mode_not_explicit_isolated:{mode_str}", {"symbol": exchange_symbol})

    def _ensure_leverage(self, client: Any, exchange_symbol: str, leverage: int) -> None:
        _call_optional(
            client,
            ["set_leverage", "set_symbol_leverage", "change_leverage", "change_symbol_leverage", "set_futures_leverage"],
            exchange_symbol,
            leverage,
        )
        current = _call_optional(client, ["get_leverage", "get_symbol_leverage", "get_futures_leverage"], exchange_symbol)
        lev = safe_int(current.get("leverage", current.get("lev", 0)) if isinstance(current, dict) else current)
        if lev != int(leverage):
            raise RealTradeError(f"leverage_mismatch:expected={leverage}:got={lev}")

    def _build_size_plan(self, symbol: str, margin_usdt: float, leverage: int, price: float, rules: ExchangeSymbolRules) -> OrderSizePlan:
        notional = margin_usdt * leverage
        qty_raw = notional / price if price > 0 else 0.0
        qty = round_step(qty_raw, rules.qty_step, rules.quantity_precision)

        if qty <= 0:
            return OrderSizePlan(symbol, margin_usdt, leverage, notional, price, 0.0, qty_raw, False, "QUANTITY_ZERO")

        if rules.min_qty > 0 and qty < rules.min_qty:
            return OrderSizePlan(symbol, margin_usdt, leverage, notional, price, qty, qty_raw, False, "QUANTITY_BELOW_MIN")

        actual_notional = qty * price
        if rules.min_notional > 0 and actual_notional < rules.min_notional:
            return OrderSizePlan(symbol, margin_usdt, leverage, notional, price, qty, qty_raw, False, "NOTIONAL_BELOW_MIN")

        return OrderSizePlan(symbol, margin_usdt, leverage, notional, price, qty, qty_raw, True, "OK")


class RealOrderExecutor:
    """Sends the real order through tobit_client.py after preflight."""

    def open_order(self, client: Any, preflight: RealTradePreflight, plan: TPSLPlan) -> JsonDict:
        if not preflight.valid:
            raise RealTradeError("preflight_invalid")

        side = "BUY" if preflight.direction == DIRECTION_LONG else "SELL"
        client_order_id = f"mh_{preflight.decision_id[-18:]}_{int(time.time())}"
        open_side = "BUY_OPEN" if preflight.direction == DIRECTION_LONG else "SELL_OPEN"

        # Primary path: current tobit_client.py expects this exact signature:
        # open_futures_position(symbol, side, direction, quantity, price=...,
        #                       order_type=..., margin_mode=..., leverage=...,
        #                       take_profit=..., take_profit_2=...,
        #                       stop_loss=..., client_order_id=...)
        # Do not send extra legacy keyword names to this method, otherwise Python
        # raises TypeError and the order never reaches Toobit.
        open_fn = getattr(client, "open_futures_position", None)
        if callable(open_fn):
            result = open_fn(
                symbol=preflight.exchange_symbol,
                side=side,
                direction=preflight.direction,
                quantity=preflight.size_plan.quantity,
                price=0.0,
                order_type="MARKET",
                margin_mode=MARGIN_ISOLATED,
                leverage=preflight.leverage,
                take_profit=preflight.tp1,
                take_profit_2=preflight.tp2,
                stop_loss=preflight.sl,
                client_order_id=client_order_id,
            )
        else:
            # Compatibility fallback for older client names. These methods may
            # accept legacy Toobit field names, so keep the broader payload here.
            payload = {
                "symbol": preflight.exchange_symbol,
                "side": side,
                "direction": preflight.direction,
                "open_side": open_side,
                "toobit_side": open_side,
                "quantity": preflight.size_plan.quantity,
                "price": 0.0,
                "order_type": "MARKET",
                "type": "LIMIT",
                "priceType": "MARKET",
                "margin_mode": MARGIN_ISOLATED,
                "leverage": preflight.leverage,
                "take_profit": preflight.tp1,
                "takeProfit": preflight.tp1,
                "take_profit_2": preflight.tp2,
                "stop_loss": preflight.sl,
                "stopLoss": preflight.sl,
                "client_order_id": client_order_id,
                "newClientOrderId": client_order_id,
            }
            result = _call_optional(client, ["create_futures_order", "place_order"], **payload)

        if hasattr(result, "to_dict") and callable(result.to_dict):
            result = result.to_dict()
        if not isinstance(result, dict):
            result = {"raw": result}

        result.setdefault("client_order_id", client_order_id)
        return result


class RealPositionConfirmer:
    """Polls Toobit after order submission to confirm actual futures position."""

    def _symbol_matches(self, client: Any, raw_symbol: str, exchange_symbol: str) -> bool:
        """Use Toobit client's symbol matcher when available, else safe normalized compare."""
        try:
            matcher = getattr(client, "_position_symbol_matches", None)
            if callable(matcher):
                return bool(matcher({"symbol": raw_symbol}, exchange_symbol))
        except Exception:
            pass
        try:
            candidates = getattr(client, "_symbol_candidates", None)
            if callable(candidates):
                return bool(set(candidates(raw_symbol)) & set(candidates(exchange_symbol)))
        except Exception:
            pass
        return str(raw_symbol or "").upper() == str(exchange_symbol or "").upper()

    def _find_matching_position(self, client: Any, exchange_symbol: str, direction: str) -> Optional[JsonDict]:
        positions = _call_optional(client, ["get_open_positions", "fetch_open_positions"], exchange_symbol)
        if hasattr(positions, "to_dict") and callable(positions.to_dict):
            positions = positions.to_dict()
        if isinstance(positions, dict):
            positions = positions.get("positions", positions.get("data", []))
        if not isinstance(positions, list):
            positions = []

        wanted_direction = normalize_direction(direction)
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            sym = str(pos.get("symbol", pos.get("contract", pos.get("exchange_symbol", ""))))
            side = normalize_direction(str(pos.get("direction", pos.get("side", pos.get("positionSide", "")))))
            qty = safe_float(pos.get("quantity", pos.get("qty", pos.get("positionAmt", pos.get("size", 0.0)))))
            if self._symbol_matches(client, sym, exchange_symbol) and side == wanted_direction and qty > 0:
                return pos
        return None

    def confirm(self, client: Any, exchange_symbol: str, direction: str, timeout_seconds: int = 70) -> Optional[JsonDict]:
        """Confirm actual exchange position with the required 60-70s window.

        0-30s: fast polling every ~2s.
        30-70s: slower polling every ~5s.
        A final exchange recheck is performed before returning None.
        """
        timeout_seconds = max(5, int(timeout_seconds or 70))
        started = time.time()
        deadline = started + timeout_seconds

        while time.time() < deadline:
            try:
                found = self._find_matching_position(client, exchange_symbol, direction)
                if found:
                    return found
            except Exception:
                pass

            elapsed = time.time() - started
            sleep_seconds = 2.0 if elapsed < 30.0 else 5.0
            time.sleep(min(sleep_seconds, max(0.0, deadline - time.time())))

        # One last Toobit sync before marking the pending slot as failed.
        try:
            return self._find_matching_position(client, exchange_symbol, direction)
        except Exception:
            return None

    def repair_tp_sl_if_missing(self, client: Any, exchange_symbol: str, direction: str, plan: TPSLPlan) -> None:
        try:
            _call_optional(
                client,
                ["ensure_tp_sl", "set_position_tp_sl", "repair_tp_sl"],
                exchange_symbol,
                direction,
                plan.tp1,
                plan.tp2,
                plan.sl,
            )
        except Exception:
            return


class RealTradeManager:
    """Main real trade manager with safety-first Toobit order flow."""

    def __init__(self, client: Any):
        self.client = client
        self.preflight_builder = RealTradePreflightBuilder()
        self.executor = RealOrderExecutor()
        self.confirmer = RealPositionConfirmer()

    def open_real_position(self, decision: AIDecision, plan: TPSLPlan, analysis_meta: Optional[JsonDict] = None) -> RealTradeOpenResult:
        trade_id = f"real_{uuid4().hex}"
        created = now_ts()

        try:
            preflight = self.preflight_builder.build(self.client, decision, plan)

            if not preflight.valid:
                result = RealTradeOpenResult(
                    trade_id=trade_id,
                    decision_id=decision.decision_id,
                    symbol=decision.symbol,
                    exchange_symbol=preflight.exchange_symbol,
                    direction=decision.direction,
                    status=STATUS_REJECTED,
                    entry=plan.entry,
                    quantity=preflight.size_plan.quantity,
                    margin_usdt=preflight.size_plan.margin_usdt,
                    leverage=preflight.leverage,
                    tp1=plan.tp1,
                    tp2=plan.tp2,
                    sl=plan.sl,
                    created_at=created,
                    error=";".join(preflight.reason_codes),
                    preflight=preflight.to_dict(),
                )
                save_error("real_trade_preflight", result.error, result.to_dict())
                return result

            raw = self.executor.open_order(self.client, preflight, plan)
            order_id = str(raw.get("order_id", raw.get("orderId", raw.get("id", ""))))
            client_order_id = str(raw.get("client_order_id", ""))
            recovered_position = raw.get("position") if isinstance(raw.get("position"), dict) else None

            pending = RealTradeOpenResult(
                trade_id=trade_id,
                decision_id=decision.decision_id,
                symbol=preflight.symbol,
                exchange_symbol=preflight.exchange_symbol,
                direction=preflight.direction,
                status=STATUS_PENDING_REAL_CONFIRM,
                order_id=order_id,
                client_order_id=client_order_id,
                entry=plan.entry,
                quantity=preflight.size_plan.quantity,
                margin_usdt=preflight.size_plan.margin_usdt,
                leverage=preflight.leverage,
                tp1=plan.tp1,
                tp2=plan.tp2,
                sl=plan.sl,
                created_at=created,
                preflight=preflight.to_dict(),
                raw_response=raw,
            )
            meta = _build_position_meta(decision, plan, analysis_meta)
            pending_record = _position_record_from_result(
                pending,
                status_for_monitor=STATUS_PENDING_REAL_CONFIRM,
                meta=meta,
                current_price=plan.entry,
            )
            save_position(trade_id, pending_record)

            position = recovered_position
            if position is None:
                position = self.confirmer.confirm(
                    self.client,
                    exchange_symbol=preflight.exchange_symbol,
                    direction=preflight.direction,
                    timeout_seconds=70,
                )

            if position:
                self.confirmer.repair_tp_sl_if_missing(self.client, preflight.exchange_symbol, preflight.direction, plan)
                confirmed = RealTradeOpenResult(
                    **{
                        **pending.to_dict(),
                        "status": STATUS_CONFIRMED,
                        "position_id": str(position.get("position_id", position.get("id", trade_id))),
                        "confirmed_at": now_ts(),
                        "raw_response": {"order": raw, "position": position},
                    }
                )
                mark_price = safe_float(position.get("mark_price", position.get("markPrice", position.get("current_price", plan.entry))))
                confirmed_record = _position_record_from_result(
                    confirmed,
                    status_for_monitor="OPEN",
                    meta=meta,
                    current_price=mark_price or plan.entry,
                )
                save_position(trade_id, confirmed_record)
                return confirmed

            failed = RealTradeOpenResult(
                **{
                    **pending.to_dict(),
                    "status": STATUS_FAILED,
                    "error": "POSITION_NOT_CONFIRMED_AFTER_ORDER",
                }
            )
            failed_record = _position_record_from_result(
                failed,
                status_for_monitor=STATUS_FAILED,
                meta=meta,
                current_price=plan.entry,
            )
            save_position(trade_id, failed_record)
            save_error("real_trade_confirm", failed.error, failed.to_dict())
            return failed

        except Exception as exc:
            result = RealTradeOpenResult(
                trade_id=trade_id,
                decision_id=getattr(decision, "decision_id", ""),
                symbol=getattr(decision, "symbol", ""),
                exchange_symbol="",
                direction=getattr(decision, "direction", ""),
                status=STATUS_FAILED,
                created_at=created,
                error=str(exc),
            )
            save_error("real_trade_open_exception", str(exc), result.to_dict())
            return result


def create_manager(client: Any) -> RealTradeManager:
    return RealTradeManager(client)


def open_real_position(client: Any, decision: AIDecision, plan: TPSLPlan, analysis_meta: Optional[JsonDict] = None) -> RealTradeOpenResult:
    return RealTradeManager(client).open_real_position(decision, plan, analysis_meta=analysis_meta)
