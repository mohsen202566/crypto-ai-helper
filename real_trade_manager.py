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
import threading
from datetime import datetime, timezone

from constants import (
    DIRECTION_LONG, DIRECTION_SHORT, FEE_CONFIG, MODE_REAL, POSITION_PENDING_REAL_CONFIRM,
    STATUS_FAILED, STATUS_OK, STATUS_RECOVERED, SYSTEM_VERSION, TRADE_CONFIG,
)
from models import AIDecision, TPSLPlan, TradeCloseResult, TradeOpenResult, TradePosition, RecordResult
from position_manager import add_position, count_open_real_positions, get_open_positions, get_position, has_open_position, mark_real_confirmed, mark_real_failed
try:
    from position_manager import reconcile_real_positions_with_exchange
except Exception:  # Backward-compatible when position_manager has not been updated yet.
    reconcile_real_positions_with_exchange = None
from strategy_manager import get_trade_runtime_config, is_real_trading_enabled
try:
    from tobit_client import MARGIN_ISOLATED, ToobitClient, get_client
except Exception:
    from tobit_client import ToobitClient
    MARGIN_ISOLATED = "ISOLATED"

    def get_client() -> ToobitClient:
        return ToobitClient()
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


def _seconds_since_iso(value: Any) -> float:
    """Return age in seconds for an ISO timestamp; fail safe to a large age."""
    raw = safe_str(value)
    if not raw:
        return 999999.0
    try:
        clean = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return 999999.0


def _real_confirm_grace_seconds() -> int:
    """How long a pending REAL must keep its slot before being allowed to fail."""
    return max(1, safe_int(TRADE_CONFIG.get("real_confirm_timeout_seconds"), 70) or 70)




def _client_call(fn: Any, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """Safely call optional Toobit client compatibility helpers."""
    if not callable(fn):
        return default
    for call in (
        lambda: fn(*args, **kwargs),
        lambda: fn(*args),
        lambda: fn(**kwargs),
        lambda: fn(),
    ):
        try:
            return call()
        except TypeError:
            continue
        except Exception:
            return default
    return default


def _object_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raw = getattr(value, "raw", None)
    if isinstance(raw, Mapping):
        base = dict(raw)
    else:
        base = {}
    for name in ("symbol", "side", "direction", "quantity", "entry_price", "unrealized_pnl", "order_id"):
        if hasattr(value, name):
            base[name] = getattr(value, name)
    return base


def _position_matches_direction(row: Any, direction: str) -> bool:
    wanted = normalize_direction(direction)
    side = normalize_direction(
        getattr(row, "side", "")
        or (row.get("side") if isinstance(row, Mapping) else "")
        or (row.get("positionSide") if isinstance(row, Mapping) else "")
        or (row.get("direction") if isinstance(row, Mapping) else "")
    )
    if side in {DIRECTION_LONG, DIRECTION_SHORT}:
        return side == wanted
    qty = safe_float(
        getattr(row, "quantity", None)
        or (row.get("positionAmt") if isinstance(row, Mapping) else None)
        or (row.get("positionAmount") if isinstance(row, Mapping) else None)
        or (row.get("size") if isinstance(row, Mapping) else None)
        or (row.get("qty") if isinstance(row, Mapping) else None),
        0.0,
    ) or 0.0
    return qty > 0 and wanted in {DIRECTION_LONG, DIRECTION_SHORT}


def _exchange_position_exists(client: ToobitClient, symbol: str, direction: str) -> tuple[bool, dict[str, Any]]:
    """Hard safety guard: never open a duplicate REAL when Toobit already has it."""
    symbol = normalize_symbol(symbol)
    direction = normalize_direction(direction)
    try:
        rows = []
        if hasattr(client, "get_open_positions"):
            rows = client.get_open_positions(symbol)
        else:
            raise RuntimeError("toobit_get_open_positions_missing")
        if not isinstance(rows, list):
            rows = [rows] if rows else []
        for row in rows:
            row_map = _object_to_dict(row)
            row_symbol = normalize_symbol(row_map.get("symbol") or row_map.get("contractCode") or symbol)
            if row_symbol == symbol and _position_matches_direction(row, direction):
                return True, row_map
    except Exception as exc:
        # Fail closed in preflight: caller receives this error row and blocks if needed.
        return False, {"error": f"exchange_position_check_failed:{exc}"}
    return False, {}

def _exchange_position_check_error(row: Any) -> str:
    """Return exchange-read error text when a position check failed.

    A failed Toobit read must never be treated as "position does not exist".
    Otherwise the bot could release a REAL slot or mark a valid pending/open
    position as failed only because the exchange API was temporarily unreadable.
    """
    if isinstance(row, Mapping) and row.get("error"):
        return safe_str(row.get("error"))
    return ""


def _looks_like_exchange_read_error(text: Any) -> bool:
    """True when a Toobit/open result is uncertain, not a confirmed no-open.

    In these cases the REAL slot must stay reserved because the exchange may
    have accepted the order while the API read/confirmation failed.
    """
    t = safe_str(text).lower()
    if not t:
        return False
    markers = (
        "check_failed", "read_failed", "unreadable", "timeout", "timed out",
        "network", "connection", "temporary", "temporarily", "rate limit",
        "429", "5xx", "502", "503", "504", "position_check",
        "exchange_position_check_failed", "exchange_open_positions_check_failed",
        "api", "request", "unknown",
    )
    return any(m in t for m in markers)


def _toobit_open_uncertain(result: Any = None, exc: Any = None) -> bool:
    """Return True when an open attempt may have reached Toobit.

    Invalid input/risk rejections are safe to fail. Exchange/API/confirmation
    errors are not safe to fail because the position may already exist.
    """
    texts = []
    if exc is not None:
        texts.append(exc)
    if result is not None:
        for name in ("reason", "error", "message", "status"):
            value = getattr(result, name, "")
            if value:
                texts.append(value)
        raw = getattr(result, "raw", None)
        if raw:
            texts.append(raw)
    return any(_looks_like_exchange_read_error(t) for t in texts)


def _exchange_open_positions_snapshot(client: ToobitClient) -> tuple[list[dict[str, Any]], str]:
    """Read Toobit open positions as the exchange source of truth for slots."""
    try:
        rows = client.get_open_positions()
        if not isinstance(rows, list):
            return [], "exchange_open_positions_invalid_response"
        return [_object_to_dict(r) for r in rows], ""
    except Exception as exc:
        return [], f"exchange_open_positions_check_failed:{exc}"


def _normalize_exchange_order_symbol(client: ToobitClient, row: Mapping[str, Any]) -> str:
    raw = safe_str(
        row.get("symbol")
        or row.get("contractCode")
        or row.get("instrumentId")
        or row.get("instId")
        or row.get("exchange_symbol")
    )
    if not raw:
        return ""
    try:
        if hasattr(client, "normalize_bot_symbol"):
            return normalize_symbol(client.normalize_bot_symbol(raw))
    except Exception:
        pass
    return normalize_symbol(raw)


def _order_is_reduce_or_tpsl(row: Mapping[str, Any]) -> bool:
    """Best-effort classifier for stale TP/SL/reduce orders on Toobit."""
    text = safe_str(row).upper()
    side = safe_str(row.get("side") or row.get("orderSide") or row.get("positionSide")).upper()
    order_type = safe_str(row.get("type") or row.get("orderType") or row.get("priceType")).upper()
    return (
        "TAKE" in text
        or "PROFIT" in text
        or "STOP" in text
        or "LOSS" in text
        or "TP" in text
        or "SL" in text
        or "CLOSE" in side
        or "TRIGGER" in order_type
    )


def _get_open_orders_for_symbol(client: ToobitClient, symbol: str) -> list[dict[str, Any]]:
    """Read open orders for the symbol.

    Locked safety rule:
    - If open-order reading is unavailable or fails, raise and let preflight
      fail closed. A failed read must never be treated as "no open orders".
    """
    target = normalize_symbol(symbol)
    last_error = ""
    for name in ("get_open_orders", "get_current_orders", "get_active_orders", "open_orders"):
        fn = getattr(client, name, None)
        if not callable(fn):
            continue
        try:
            rows = fn(target)
        except TypeError:
            try:
                rows = fn(symbol=target)
            except TypeError:
                try:
                    rows = fn()
                except Exception as exc:
                    last_error = f"{name}_failed:{exc}"
                    continue
            except Exception as exc:
                last_error = f"{name}_failed:{exc}"
                continue
        except Exception as exc:
            last_error = f"{name}_failed:{exc}"
            continue

        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise RuntimeError(f"{name}_invalid_response")

        out: list[dict[str, Any]] = []
        for row in rows:
            row_map = _object_to_dict(row)
            if not row_map:
                continue
            row_symbol = _normalize_exchange_order_symbol(client, row_map)
            if row_symbol and row_symbol != target:
                continue
            out.append(row_map)
        return out

    if last_error:
        raise RuntimeError(f"exchange_open_orders_check_failed:{last_error}")
    raise RuntimeError("exchange_open_orders_reader_missing")


def _exchange_open_order_guard(client: ToobitClient, symbol: str) -> tuple[bool, list[dict[str, Any]]]:
    """Block new REAL when any open order already exists for the symbol.

    Return shape:
    - (True, rows)  => block because open order exists.
    - (False, [])   => no open order.
    - (False, [{"error": "..."}]) => read failed; caller must fail closed.
    """
    for name in ("has_open_order_for_symbol", "has_open_orders_for_symbol", "has_open_orders"):
        fn = getattr(client, name, None)
        if not callable(fn):
            continue
        try:
            value = fn(symbol)
        except TypeError:
            try:
                value = fn(symbol=symbol)
            except Exception as exc:
                return False, [{"error": f"exchange_open_orders_check_failed:{name}:{exc}"}]
        except Exception as exc:
            return False, [{"error": f"exchange_open_orders_check_failed:{name}:{exc}"}]

        if isinstance(value, Mapping):
            return bool(value.get("has_open_orders") or value.get("exists") or value.get("ok") is False), list(value.get("orders") or [])
        if value is not None:
            return bool(value), []

    try:
        orders = _get_open_orders_for_symbol(client, symbol)
    except Exception as exc:
        return False, [{"error": str(exc)}]
    return bool(orders), orders

def _same_tp_sl_payload(position: TradePosition, plan: TPSLPlan) -> dict[str, Any]:
    """Keep one immutable TP/SL payload for exchange repair; never recalculate later."""
    return {
        "symbol": normalize_symbol(position.symbol),
        "direction": normalize_direction(position.direction),
        "take_profit": safe_float(plan.tp1, 0.0) or 0.0,
        "take_profit_2": None,  # TP2 is internal/runner logic, not a separate Toobit order.
        "stop_loss": safe_float(plan.sl, 0.0) or 0.0,
    }


def _tp_sl_orders_present(client: ToobitClient, symbol: str) -> tuple[bool, dict[str, Any]]:
    """Best-effort check that at least one TP and one SL/reduce order exist for this symbol."""
    try:
        orders = _get_open_orders_for_symbol(client, symbol)
    except Exception as exc:
        return False, {"orders": [], "error": f"tp_sl_orders_read_failed:{exc}"}
    if not orders:
        return False, {"orders": [], "reason": "no_tp_sl_orders_found"}
    text_items = [safe_str(row).upper() for row in orders]
    has_tp = any("TAKE" in t or "PROFIT" in t or "TP" in t for t in text_items)
    has_sl = any("STOP" in t or "LOSS" in t or "SL" in t for t in text_items)
    # Some Toobit TP/SL attached at open may not be returned as normal open orders.
    # If we can read orders and only see generic reduce/close trigger rows, count them as present conservatively.
    reduce_count = sum(1 for row in orders if _order_is_reduce_or_tpsl(row))
    present = (has_tp and has_sl) or reduce_count >= 2
    return present, {"orders": orders, "has_tp": has_tp, "has_sl": has_sl, "reduce_count": reduce_count}


def verify_or_repair_same_tp_sl_after_delay(position: TradePosition, plan: TPSLPlan, *, client: Optional[ToobitClient] = None, delay_seconds: int = 70) -> dict[str, Any]:
    """
    After Toobit has had time to materialize attached TP/SL, verify them.

    Rule locked by user:
    - TP/SL must be sent with the OPEN order first.
    - Do NOT create separate TP/SL immediately after open.
    - After ~70 seconds, if the REAL position exists but TP/SL is missing,
      repair using the exact same TP1/SL from the original signal, never new values.
    """
    c = client or get_client()
    wait = max(0, safe_int(delay_seconds, 70) or 70)
    if wait:
        time.sleep(wait)

    exists, row = _exchange_position_exists(c, position.symbol, position.direction)
    check_error = _exchange_position_check_error(row)
    if check_error:
        return {
            "status": STATUS_FAILED,
            "ok": False,
            "action": "skip_exchange_position_unreadable",
            "position_id": position.position_id,
            "error": check_error,
            "slot_safe": True,
            "exchange_position": row,
        }
    if not exists:
        return {"status": STATUS_OK, "ok": True, "action": "skip_position_not_open", "position_id": position.position_id, "exchange_position": row}

    present, detail = _tp_sl_orders_present(c, position.symbol)
    if present:
        return {"status": STATUS_OK, "ok": True, "action": "tp_sl_present", "position_id": position.position_id, "detail": detail}
    if isinstance(detail, Mapping) and detail.get("error"):
        return {
            "status": STATUS_FAILED,
            "ok": False,
            "action": "tp_sl_unreadable_no_repair",
            "position_id": position.position_id,
            "error": safe_str(detail.get("error")),
            "detail": dict(detail),
        }

    payload = _same_tp_sl_payload(position, plan)
    fn = getattr(c, "ensure_tp_sl", None) or getattr(c, "set_position_tp_sl", None)
    if not callable(fn):
        return {"status": STATUS_FAILED, "ok": False, "action": "repair_unavailable", "position_id": position.position_id, "payload": payload, "detail": detail}

    repaired = _client_call(
        fn,
        payload["symbol"],
        payload["direction"],
        take_profit=payload["take_profit"],
        stop_loss=payload["stop_loss"],
        take_profit_2=None,
        default={"status": STATUS_FAILED, "ok": False, "error": "ensure_tp_sl_call_failed"},
    )
    if not isinstance(repaired, Mapping):
        repaired = {"status": STATUS_OK if repaired else STATUS_FAILED, "ok": bool(repaired)}
    return {
        "status": STATUS_OK if bool(repaired.get("ok", repaired.get("status") == STATUS_OK)) else STATUS_FAILED,
        "ok": bool(repaired.get("ok", repaired.get("status") == STATUS_OK)),
        "action": "repaired_same_tp_sl",
        "position_id": position.position_id,
        "payload": payload,
        "repair_result": dict(repaired),
        "detail": detail,
    }


def _schedule_same_tp_sl_verification(position: TradePosition, plan: TPSLPlan, *, delay_seconds: int = 70) -> None:
    """Non-blocking post-open TP/SL verification so Telegram/manual commands stay responsive."""
    def _runner() -> None:
        try:
            verify_or_repair_same_tp_sl_after_delay(position, plan, delay_seconds=0)
        except Exception:
            # Safety verifier must never crash the bot process.
            pass

    timer = threading.Timer(max(0, safe_int(delay_seconds, 70) or 70), _runner)
    timer.daemon = True
    timer.start()



def _mapping_from_obj(value: Any) -> dict[str, Any]:
    """Best-effort mapping extraction without depending on model internals."""
    if isinstance(value, Mapping):
        return dict(value)
    raw = getattr(value, "raw", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    meta = getattr(value, "metadata", None)
    if isinstance(meta, Mapping):
        return dict(meta)
    return {}


def _first_hunter_value(key: str, *sources: Any) -> Any:
    """Return first non-None hunter/selector/timing value from mixed sources."""
    for source in sources:
        if source is None:
            continue
        if isinstance(source, Mapping) and key in source:
            value = source.get(key)
            if value is not None:
                return value
        if hasattr(source, key):
            value = getattr(source, key)
            if value is not None:
                return value
        mapped = _mapping_from_obj(source)
        if key in mapped and mapped.get(key) is not None:
            return mapped.get(key)
        for nested_key in ("hunter_features", "start_evidence_profile", "selector", "timing", "entry_quality", "movement_state"):
            nested = mapped.get(nested_key)
            if isinstance(nested, Mapping) and key in nested and nested.get(key) is not None:
                return nested.get(key)
    return None


def _bool_feature(value: Any) -> Optional[bool]:
    """Stable optional bool parser. None means the feature was not provided."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


def extract_decision_hunter_features(decision: AIDecision) -> dict[str, Any]:
    """Extract final safety features used before sending a REAL order.

    This is not a signal engine. It is the last REAL-order guard so a stale
    candidate cannot bypass the hunter/selector stack and open after the move
    is already late, exhausted, or chasing.
    """
    metadata = _mapping_from_obj(getattr(decision, "metadata", {}))
    plan = getattr(decision, "tp_sl", None)
    plan_raw = _mapping_from_obj(plan)
    plan_hunter = plan_raw.get("hunter_features") if isinstance(plan_raw.get("hunter_features"), Mapping) else {}
    sources = (metadata, plan_hunter, plan_raw, decision)

    alias_map = {
        "start_score": ("start_score", "start_evidence_score", "hunter_start_score", "entry_start_score"),
        "fresh_momentum_score": ("fresh_momentum_score", "fresh_score", "freshness_score"),
        "chase_risk_score": ("chase_risk_score", "chase_risk", "anti_chase_risk"),
        "late_risk_score": ("late_risk_score", "late_risk", "late_entry_risk"),
        "move_age_score": ("move_age_score", "move_age", "move_age_risk"),
        "exhaustion_score": ("exhaustion_score", "exhaustion_risk_score", "exhaustion_risk"),
        "start_pressure_score": ("start_pressure_score", "start_pressure"),
        "selector_rank_score": ("selector_rank_score", "rank_score", "selection_score"),
        "start_signal_count": ("start_signal_count", "start_signals", "start_evidence_count"),
        "structure_start_active": ("structure_start_active", "structure_start"),
        "momentum_start_active": ("momentum_start_active", "momentum_start"),
        "liquidity_start_active": ("liquidity_start_active", "liquidity_start"),
        "fresh_context_active": ("fresh_context_active", "context_start_active", "fresh_context"),
        "selector_selected_for_real": ("selector_selected_for_real", "selected_for_real", "selector_real_selected"),
        "movement_state": ("movement_state", "state", "move_state"),
        "market_regime": ("market_regime", "market_mode", "regime"),
    }

    features: dict[str, Any] = {}
    for canonical, aliases in alias_map.items():
        for alias in aliases:
            value = _first_hunter_value(alias, *sources)
            if value is not None:
                features[canonical] = value
                break
    return features


def real_hunter_safety_guard(decision: AIDecision) -> tuple[list[str], list[str], dict[str, Any]]:
    """Return REAL-blocking hunter errors, warnings, and extracted features."""
    features = extract_decision_hunter_features(decision)
    errors: list[str] = []
    warnings: list[str] = []

    start = safe_float(features.get("start_score"), None)
    fresh = safe_float(features.get("fresh_momentum_score"), None)
    chase = safe_float(features.get("chase_risk_score"), None)
    late = safe_float(features.get("late_risk_score"), None)
    age = safe_float(features.get("move_age_score"), None)
    exhaustion = safe_float(features.get("exhaustion_score"), None)
    selected_for_real = _bool_feature(features.get("selector_selected_for_real"))

    min_start = safe_float(TRADE_CONFIG.get("real_hunter_min_start_score"), 50.0) or 50.0
    max_chase = safe_float(TRADE_CONFIG.get("real_hunter_max_chase_risk"), 70.0) or 70.0
    max_late = safe_float(TRADE_CONFIG.get("real_hunter_max_late_risk"), 65.0) or 65.0
    max_age = safe_float(TRADE_CONFIG.get("real_hunter_max_move_age"), 70.0) or 70.0
    max_exhaustion = safe_float(TRADE_CONFIG.get("real_hunter_max_exhaustion"), 75.0) or 75.0
    min_fresh_when_weak_start = safe_float(TRADE_CONFIG.get("real_hunter_min_fresh_when_weak_start"), 45.0) or 45.0
    require_hunter = bool(TRADE_CONFIG.get("require_hunter_preflight_guard", True))

    if not features:
        if require_hunter:
            errors.append("hunter_features_missing_for_real")
        else:
            warnings.append("hunter_features_missing")
        return errors, warnings, features

    if selected_for_real is False:
        errors.append("selector_not_selected_for_real")
    if chase is not None and chase >= max_chase:
        errors.append(f"hunter_chase_risk_high:{chase:.1f}>={max_chase:.1f}")
    if late is not None and late >= max_late:
        errors.append(f"hunter_late_risk_high:{late:.1f}>={max_late:.1f}")
    if age is not None and age >= max_age:
        errors.append(f"hunter_move_too_old:{age:.1f}>={max_age:.1f}")
    if exhaustion is not None and exhaustion >= max_exhaustion:
        errors.append(f"hunter_exhaustion_high:{exhaustion:.1f}>={max_exhaustion:.1f}")
    if start is not None and start < min_start and (fresh is None or fresh < min_fresh_when_weak_start):
        errors.append(f"hunter_start_too_weak:{start:.1f}<{min_start:.1f}")

    start_flags = [
        _bool_feature(features.get("structure_start_active")),
        _bool_feature(features.get("momentum_start_active")),
        _bool_feature(features.get("liquidity_start_active")),
        _bool_feature(features.get("fresh_context_active")),
    ]
    if require_hunter and start is None and not any(v is True for v in start_flags):
        errors.append("hunter_start_evidence_missing_for_real")

    return errors, warnings, features


def build_ai_decision_snapshot(decision: AIDecision, preflight: Optional[Mapping[str, Any]] = None, open_result: Optional[TradeOpenResult] = None) -> dict[str, Any]:
    """Store a compact immutable snapshot for later learning/debugging."""
    metadata = _mapping_from_obj(getattr(decision, "metadata", {}))
    plan = getattr(decision, "tp_sl", None)
    plan_raw = _mapping_from_obj(plan)
    hunter = extract_decision_hunter_features(decision)
    component_scores = metadata.get("component_scores") if isinstance(metadata.get("component_scores"), Mapping) else {}
    return {
        "system_version": SYSTEM_VERSION,
        "signal_id": safe_str(getattr(decision, "signal_id", "")),
        "symbol": normalize_symbol(getattr(decision, "symbol", "")),
        "direction": normalize_direction(getattr(decision, "direction", "")),
        "mode": safe_str(getattr(decision, "mode", "")).upper(),
        "level": safe_int(getattr(decision, "level", 4), 4) or 4,
        "score": safe_float(getattr(decision, "score", None), None),
        "confidence": safe_float(getattr(decision, "confidence", None), None),
        "entry": safe_float(getattr(decision, "entry", None), None),
        "reason_codes": list(getattr(decision, "reason_codes", []) or []),
        "reject_reason": safe_str(getattr(decision, "reject_reason", "")),
        "hunter_features": hunter,
        "component_scores": dict(component_scores),
        "selector_rank_score": hunter.get("selector_rank_score"),
        "movement_state": hunter.get("movement_state") or metadata.get("movement_state"),
        "market_regime": hunter.get("market_regime") or metadata.get("market_regime") or metadata.get("market_mode"),
        "tp_sl": {
            "entry": safe_float(getattr(plan, "entry", 0.0), 0.0) if plan else 0.0,
            "tp1": safe_float(getattr(plan, "tp1", 0.0), 0.0) if plan else 0.0,
            "tp2": safe_float(getattr(plan, "tp2", 0.0), 0.0) if plan else None,
            "sl": safe_float(getattr(plan, "sl", 0.0), 0.0) if plan else 0.0,
            "rr": safe_float(getattr(plan, "rr", 0.0), 0.0) if plan else 0.0,
            "valid": bool(getattr(plan, "valid", False)) if plan else False,
            "reason_codes": list(getattr(plan, "reason_codes", []) or []) if plan else [],
            "raw": dict(plan_raw),
        },
        "preflight": dict(preflight or {}),
        "open_result": dict(getattr(open_result, "raw", {}) or {}) if open_result else {},
        "created_at": utc_now_iso(),
    }

def preflight_real_trade(decision: AIDecision, *, client: Optional[ToobitClient] = None, state: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    c = client or get_client()
    runtime = get_runtime(state)
    symbol = normalize_symbol(decision.symbol)
    direction = normalize_direction(decision.direction)
    plan = decision.tp_sl
    errors: list[str] = []
    warnings: list[str] = []

    hunter_errors, hunter_warnings, hunter_features = real_hunter_safety_guard(decision)
    errors.extend(hunter_errors)
    warnings.extend(hunter_warnings)

    if safe_str(getattr(decision, "mode", "")).upper() != MODE_REAL:
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

    # HARD exchange-side duplicate guard.
    # Internal positions.json can lag during API delays; Toobit is source of truth before opening REAL.
    exchange_position_exists, exchange_position_row = _exchange_position_exists(c, symbol, direction)
    if exchange_position_exists:
        errors.append("duplicate_exchange_position")
    elif isinstance(exchange_position_row, Mapping) and exchange_position_row.get("error"):
        errors.append(safe_str(exchange_position_row.get("error")))

    exchange_orders_exist, exchange_orders = _exchange_open_order_guard(c, symbol)
    exchange_orders_error = ""
    if exchange_orders and isinstance(exchange_orders[0], Mapping) and exchange_orders[0].get("error"):
        exchange_orders_error = safe_str(exchange_orders[0].get("error"))
        errors.append(exchange_orders_error)
    elif exchange_orders_exist:
        errors.append("stale_or_open_exchange_orders_exist")

    max_real = safe_int(runtime.get("max_concurrent_real_positions"), TRADE_CONFIG.get("max_concurrent_real_positions", 3)) or 3

    local_real_open = count_open_real_positions()
    if local_real_open >= max_real:
        errors.append(f"max_real_positions_reached:{local_real_open}>={max_real}")

    exchange_open_positions, exchange_open_error = _exchange_open_positions_snapshot(c)
    exchange_open_total = len(exchange_open_positions)
    if exchange_open_error:
        # Safety first: if we cannot read Toobit open positions, do not open a new REAL.
        errors.append(exchange_open_error)
    elif exchange_open_total >= max_real:
        errors.append(f"max_exchange_real_positions_reached:{exchange_open_total}>={max_real}")

    margin_mode = safe_str(runtime.get("margin_mode"), MARGIN_ISOLATED).upper()
    if margin_mode != MARGIN_ISOLATED:
        errors.append("cross_margin_blocked")

    margin = safe_float(runtime.get("margin_usdt"), 0.0) or 0.0
    leverage = safe_int(runtime.get("leverage"), 1) or 1
    entry = safe_float(decision.entry, 0.0) or (safe_float(plan.entry, 0.0) if plan else 0.0) or 0.0
    quantity_est = estimate_quantity(entry, margin, leverage)

    qty = 0.0
    qty_reason = "not_checked"
    rules = None
    if entry <= 0:
        errors.append("invalid_entry_price")
    elif margin <= 0:
        errors.append("invalid_margin_usdt")
    elif leverage <= 0:
        errors.append("invalid_leverage")
    else:
        try:
            rules = c.get_symbol_rules(symbol) if hasattr(c, "get_symbol_rules") else None
            qty = c.quantity_from_margin(symbol=symbol, margin_usdt=margin, leverage=leverage, price=entry)
            qty_reason = "ok"
        except Exception as exc:
            qty_reason = f"quantity_invalid:{exc}"
            errors.append(qty_reason)

    if plan is not None and qty > 0:
        gross, fees, net = estimate_tp1_net_profit(direction, entry, plan.tp1, qty)
        min_net = safe_float(FEE_CONFIG.get("minimum_net_profit_usdt"), 0.10) or 0.10
        if FEE_CONFIG.get("reject_if_tp1_net_profit_below_minimum", True) and net < min_net:
            errors.append(f"tp1_net_profit_too_low:{net:.4f}<{min_net:.4f}")
    else:
        gross = fees = net = 0.0

    # Isolated margin and leverage are verified inside tobit_client.open_position_with_tp_sl
    # immediately before the real order. Preflight must not silently set exchange state.

    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "ok": not errors, "errors": errors, "warnings": warnings, "symbol": symbol, "direction": direction,
        "exchange_position_exists": exchange_position_exists,
        "exchange_position": exchange_position_row if exchange_position_exists else {},
        "exchange_open_orders_exist": exchange_orders_exist,
        "exchange_open_orders_error": exchange_orders_error,
        "exchange_open_orders": exchange_orders[:5] if (exchange_orders_exist or exchange_orders_error) else [],
        "exchange_open_total": exchange_open_total,
        "exchange_open_positions": exchange_open_positions[:10],
        "exchange_open_error": exchange_open_error,
        "local_real_open": local_real_open,
        "max_real_positions": max_real,
        "entry": entry, "margin_usdt": margin, "leverage": leverage, "margin_mode": margin_mode,
        "quantity_estimate": quantity_est, "quantity": qty, "quantity_reason": qty_reason,
        "symbol_rules": (rules.to_dict() if hasattr(rules, "to_dict") else _object_to_dict(rules)) if rules else {}, "tp1_gross_profit_estimate": gross,
        "fee_estimate": fees, "tp1_net_profit_estimate": net,
        "hunter_features": dict(hunter_features),
        "ai_decision_snapshot": build_ai_decision_snapshot(decision, preflight={
            "hunter_errors": list(hunter_errors),
            "hunter_warnings": list(hunter_warnings),
            "hunter_features": dict(hunter_features),
        }),
        "checked_at": utc_now_iso(),
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
        decision_metadata={
            "decision": _mapping_from_obj(getattr(decision, "metadata", {})),
            "ai_snapshot": build_ai_decision_snapshot(decision, preflight=preflight, open_result=open_result),
            "hunter_features": dict(preflight.get("hunter_features", {})) if isinstance(preflight.get("hunter_features"), Mapping) else {},
            "preflight": dict(preflight),
            "open_result": open_result.raw if open_result else {},
        },
        level=decision.level,
    )


def _trade_open_result_from_toobit(result: Any, *, position_id: str = "", error: str = "", preflight: Optional[Mapping[str, Any]] = None) -> TradeOpenResult:
    opened = bool(getattr(result, "opened", False))
    status = STATUS_OK if opened else STATUS_FAILED
    return TradeOpenResult(
        status=status,
        position_id=position_id,
        exchange_order_id=safe_str(getattr(result, "order_id", "")),
        symbol=normalize_symbol(getattr(result, "symbol", "")),
        direction=normalize_direction(getattr(result, "direction", "")),
        entry=safe_float(getattr(result, "entry_price", 0.0), 0.0) or 0.0,
        quantity=safe_float(getattr(result, "quantity", 0.0), 0.0) or 0.0,
        message=safe_str(getattr(result, "reason", "")) or "real_open_checked_after_70s",
        error=error if not opened else "",
        recovered=False,
        raw={"toobit_result": _object_to_dict(result), "preflight": dict(preflight or {})},
    )


def _hold_pending_after_uncertain_open(position: TradePosition, reason: str, preflight: Mapping[str, Any], *, client: ToobitClient, plan: Optional[TPSLPlan] = None) -> TradeOpenResult:
    """Keep local REAL slot when Toobit open result is uncertain.

    The manager must not mark the slot failed on API/confirmation uncertainty;
    the 70-second exchange check may later confirm the position.
    """
    confirm = wait_for_real_open_confirmation(position.position_id, client=client, timeout_seconds=_real_confirm_grace_seconds())
    if confirm.get("confirmed"):
        if plan is not None:
            confirmed_pos = get_position(position.position_id) or position
            _schedule_same_tp_sl_verification(confirmed_pos, plan, delay_seconds=_real_confirm_grace_seconds())
        return TradeOpenResult(
            status=STATUS_OK,
            position_id=position.position_id,
            exchange_order_id=safe_str(confirm.get("exchange_order_id") or position.exchange_order_id),
            symbol=position.symbol,
            direction=position.direction,
            entry=safe_float(confirm.get("entry"), position.entry) or position.entry,
            quantity=safe_float(confirm.get("quantity"), position.quantity) or position.quantity,
            message="real_open_recovered_after_70s",
            recovered=True,
            raw={"preflight": dict(preflight), "confirm": dict(confirm), "uncertain_reason": reason},
        )
    return TradeOpenResult(
        status=STATUS_FAILED,
        position_id=position.position_id,
        symbol=position.symbol,
        direction=position.direction,
        entry=position.entry,
        quantity=position.quantity,
        error=reason,
        raw={"preflight": dict(preflight), "confirm": dict(confirm), "slot_held": bool(confirm.get("slot_held")), "uncertain_open": True},
    )


def open_real_trade(decision: AIDecision, *, client: Optional[ToobitClient] = None, state: Optional[Mapping[str, Any]] = None) -> TradeOpenResult:
    """Open one REAL trade through the locked Toobit client.

    Locked flow:
    1) preflight blocks Trade OFF, duplicates, slots, open orders, bad TP/SL/margin.
    2) reserve a local PENDING_REAL_CONFIRM slot before the exchange call.
    3) call tobit_client.open_position_with_tp_sl with margin, leverage and TP/SL.
       The client itself ensures isolated mode, leverage, quantity rounding, TP/SL rounding,
       actual-margin validation, and the 70-second post-order position check.
    4) keep the slot if Toobit confirms the position; otherwise mark it failed.
    """
    c = client or get_client()
    preflight = preflight_real_trade(decision, client=c, state=state)
    if not preflight.get("ok"):
        return TradeOpenResult(
            status=STATUS_FAILED,
            symbol=decision.symbol,
            direction=decision.direction,
            entry=decision.entry,
            error=";".join(preflight.get("errors", [])),
            raw={"preflight": preflight},
        )

    plan = decision.tp_sl
    if plan is None:
        return TradeOpenResult(status=STATUS_FAILED, symbol=decision.symbol, direction=decision.direction, entry=decision.entry, error="missing_tp_sl_plan", raw={"preflight": preflight})

    # Reserve the REAL slot before the blocking 70-second Toobit verification.
    pending_pos = build_pending_position(decision, preflight, None)
    add_res = add_position(pending_pos, reject_duplicate=True)
    if add_res.status != STATUS_OK:
        return TradeOpenResult(
            status=STATUS_FAILED,
            symbol=decision.symbol,
            direction=decision.direction,
            entry=pending_pos.entry,
            quantity=pending_pos.quantity,
            error=f"position_record_failed:{getattr(add_res, 'error', '') or getattr(add_res, 'message', '')}",
            raw={"preflight": preflight},
        )

    try:
        result = c.open_position_with_tp_sl(
            symbol=preflight["symbol"],
            direction=preflight["direction"],
            margin_usdt=safe_float(preflight.get("margin_usdt"), 0.0) or 0.0,
            leverage=safe_int(preflight.get("leverage"), 1) or 1,
            tp_price=safe_float(plan.tp1, 0.0) or 0.0,
            sl_price=safe_float(plan.sl, 0.0) or 0.0,
            price=safe_float(preflight.get("entry"), 0.0) or None,
        )
    except Exception as exc:
        reason = f"toobit_open_exception:{exc}"
        if _toobit_open_uncertain(exc=exc):
            return _hold_pending_after_uncertain_open(pending_pos, reason, preflight, client=c, plan=plan)
        mark_real_failed(pending_pos.position_id, reason)
        return TradeOpenResult(
            status=STATUS_FAILED,
            position_id=pending_pos.position_id,
            symbol=pending_pos.symbol,
            direction=pending_pos.direction,
            entry=pending_pos.entry,
            quantity=pending_pos.quantity,
            error=reason,
            raw={"preflight": preflight},
        )

    if not bool(getattr(result, "opened", False)):
        reason = safe_str(getattr(result, "reason", "real_open_not_confirmed_after_70s"))
        if _toobit_open_uncertain(result=result):
            return _hold_pending_after_uncertain_open(pending_pos, reason, preflight, client=c, plan=plan)
        mark_real_failed(pending_pos.position_id, reason)
        return _trade_open_result_from_toobit(result, position_id=pending_pos.position_id, error=reason, preflight=preflight)

    mark_real_confirmed(
        pending_pos.position_id,
        entry=safe_float(getattr(result, "entry_price", pending_pos.entry), pending_pos.entry) or pending_pos.entry,
        quantity=safe_float(getattr(result, "quantity", pending_pos.quantity), pending_pos.quantity) or pending_pos.quantity,
        exchange_order_id=safe_str(getattr(result, "order_id", pending_pos.exchange_order_id)),
    )

    confirmed_pos = get_position(pending_pos.position_id) or pending_pos
    _schedule_same_tp_sl_verification(confirmed_pos, plan, delay_seconds=_real_confirm_grace_seconds())

    return TradeOpenResult(
        status=STATUS_OK,
        position_id=pending_pos.position_id,
        exchange_order_id=safe_str(getattr(result, "order_id", "")),
        symbol=pending_pos.symbol,
        direction=pending_pos.direction,
        entry=safe_float(getattr(result, "entry_price", pending_pos.entry), pending_pos.entry) or pending_pos.entry,
        quantity=safe_float(getattr(result, "quantity", pending_pos.quantity), pending_pos.quantity) or pending_pos.quantity,
        message="real_open_confirmed_after_70s",
        recovered=False,
        raw={
            "toobit_result": _object_to_dict(result),
            "preflight": preflight,
            "ai_snapshot": build_ai_decision_snapshot(decision, preflight=preflight),
        },
    )


def confirm_real_open(position: TradePosition, *, client: Optional[ToobitClient] = None) -> dict[str, Any]:
    """Confirm pending REAL only after the locked 70-second grace window."""
    age_seconds = _seconds_since_iso(getattr(position, "opened_at", ""))
    grace = _real_confirm_grace_seconds()
    if age_seconds < grace:
        return {
            "confirmed": False,
            "pending": True,
            "age_seconds": age_seconds,
            "grace_seconds": grace,
            "slot_held": True,
            "not_checked_before_grace": True,
        }

    c = client or get_client()
    exists, row = _exchange_position_exists(c, position.symbol, position.direction)
    if not exists:
        check_error = _exchange_position_check_error(row)
        if check_error:
            return {
                "confirmed": False,
                "pending": True,
                "age_seconds": age_seconds,
                "grace_seconds": grace,
                "error": check_error,
                "slot_held": True,
            }
        return {
            "confirmed": False,
            "pending": False,
            "age_seconds": age_seconds,
            "grace_seconds": grace,
            "error": "real_open_not_found_after_grace",
            "slot_held": False,
        }

    entry = safe_float(row.get("entry_price") or row.get("entryPrice") or row.get("avgPrice") or row.get("price"), position.entry) or position.entry
    qty = abs(safe_float(row.get("quantity") or row.get("positionAmt") or row.get("qty") or row.get("volume"), position.quantity) or position.quantity)
    order_id = safe_str(row.get("order_id") or row.get("orderId") or row.get("id") or position.exchange_order_id)
    mark_real_confirmed(position.position_id, entry=entry, quantity=qty, exchange_order_id=order_id)
    return {"confirmed": True, "entry": entry, "quantity": qty, "exchange_order_id": order_id}

def wait_for_real_open_confirmation(position_id: str, *, client: Optional[ToobitClient] = None, timeout_seconds: int | None = None) -> dict[str, Any]:
    """Wait once, then check Toobit after the locked 70-second window.

    No fast polling before 70 seconds; the REAL slot stays reserved while waiting.
    """
    timeout = safe_int(timeout_seconds, TRADE_CONFIG.get("real_confirm_timeout_seconds", 70)) or 70
    pos = get_position(position_id)
    if not pos:
        return {"confirmed": False, "error": "position_not_found"}

    age_seconds = _seconds_since_iso(getattr(pos, "opened_at", ""))
    wait_left = max(0.0, float(timeout) - age_seconds)
    if wait_left > 0:
        time.sleep(wait_left)

    pos = get_position(position_id)
    if not pos:
        return {"confirmed": False, "error": "position_not_found"}

    result = confirm_real_open(pos, client=client)
    if result.get("confirmed"):
        return result
    if result.get("error") and result.get("slot_held"):
        return {**result, "confirmed": False, "pending": True, "slot_held": True}
    mark_real_failed(position_id, result.get("error") or "real_open_confirmation_timeout")
    return {"confirmed": False, "error": result.get("error") or "real_open_confirmation_timeout"}

def close_real_position(position: TradePosition, reason: str = "MANUAL_CLOSE", quantity: Any = 0.0, current_price: Any = 0.0, *, client: Optional[ToobitClient] = None) -> TradeCloseResult:
    c = client or get_client()
    qty = safe_float(quantity, 0.0) or position.quantity
    price = safe_float(current_price, 0.0) or position.current_price
    fn = getattr(c, "close_position", None) or getattr(c, "close_futures_position", None)
    if not callable(fn):
        return TradeCloseResult(status=STATUS_FAILED, position_id=position.position_id, symbol=position.symbol, direction=position.direction, error="toobit_close_not_available")
    result = _client_call(fn, position.symbol, position.direction, quantity=qty, price=price, default=None)
    if result is None:
        return TradeCloseResult(status=STATUS_FAILED, position_id=position.position_id, symbol=position.symbol, direction=position.direction, error="toobit_close_failed")
    result.position_id = position.position_id
    if not getattr(result, "pnl_confirmed", False) and getattr(result, "pnl_usdt", None) is None and getattr(result, "close_confirmed", False):
        result.pnl_usdt = profit_usdt(position.direction, position.entry, getattr(result, "close_price", None) or price, getattr(result, "closed_quantity", None) or qty)
        result.pnl_confirmed = False
    return result


def close_position_executor(position: TradePosition, reason: str, quantity: float, current_price: float) -> TradeCloseResult:
    return close_real_position(position, reason=reason, quantity=quantity, current_price=current_price)


def exchange_position_checker(position: TradePosition, *, client: Optional[ToobitClient] = None) -> dict[str, Any]:
    """Adapter for position_monitor: check whether a REAL position still exists on Toobit.

    This does not close or modify anything. It only reads Toobit state so
    position_monitor can detect exchange-side TP/SL/manual closes.
    """
    try:
        c = client or get_client()
        exists, row = _exchange_position_exists(c, position.symbol, position.direction)
        check_error = _exchange_position_check_error(row)
        if check_error:
            return {
                "status": STATUS_FAILED,
                "exists": None,
                "open": None,
                "position_exists": None,
                "found": None,
                "error": check_error,
                "symbol": normalize_symbol(position.symbol),
                "direction": normalize_direction(position.direction),
                "row": dict(row) if isinstance(row, Mapping) else row,
                "checked_at": utc_now_iso(),
            }
        return {
            "status": STATUS_OK,
            "exists": exists,
            "open": exists,
            "position_exists": exists,
            "found": exists,
            "symbol": normalize_symbol(position.symbol),
            "direction": normalize_direction(position.direction),
            "row": dict(row) if isinstance(row, Mapping) else row,
            "checked_at": utc_now_iso(),
        }
    except Exception as exc:
        return {
            "status": STATUS_FAILED,
            "exists": None,
            "open": None,
            "position_exists": None,
            "found": None,
            "error": str(exc),
            "symbol": normalize_symbol(getattr(position, "symbol", "")),
            "direction": normalize_direction(getattr(position, "direction", "")),
            "checked_at": utc_now_iso(),
        }


def closed_pnl_reader(position: TradePosition, *, client: Optional[ToobitClient] = None) -> dict[str, Any]:
    """Adapter for position_monitor: read closed-position PnL after Toobit TP/SL/manual close.

    It prefers Toobit closed-history helpers when available. The wait is kept
    short so monitor loop and Telegram responses do not hang.
    """
    try:
        c = client or get_client()
        symbol = normalize_symbol(position.symbol)
        direction = normalize_direction(position.direction)

        fn_wait = getattr(c, "wait_for_closed_position_pnl", None)
        if callable(fn_wait):
            data = _client_call(
                fn_wait,
                symbol,
                direction,
                timeout_seconds=safe_int(TRADE_CONFIG.get("closed_pnl_wait_seconds"), 8) or 8,
                poll_seconds=safe_int(TRADE_CONFIG.get("closed_pnl_poll_seconds"), 2) or 2,
                default=None,
            )
            if isinstance(data, Mapping) and (data.get("confirmed") or data.get("pnl_usdt") is not None):
                out = dict(data)
                out.setdefault("status", STATUS_OK)
                return out

        fn_once = getattr(c, "get_closed_position_pnl", None)
        if callable(fn_once):
            data = _client_call(fn_once, symbol, direction, default=None)
            if isinstance(data, Mapping):
                out = dict(data)
                out.setdefault("status", STATUS_OK if not out.get("error") else STATUS_FAILED)
                return out

        return {
            "status": STATUS_FAILED,
            "confirmed": False,
            "pnl_usdt": None,
            "error": "closed_pnl_reader_unavailable",
            "symbol": symbol,
            "direction": direction,
        }
    except Exception as exc:
        return {
            "status": STATUS_FAILED,
            "confirmed": False,
            "pnl_usdt": None,
            "error": str(exc),
            "symbol": normalize_symbol(getattr(position, "symbol", "")),
            "direction": normalize_direction(getattr(position, "direction", "")),
        }


def emergency_disable_real_trading(reason: str = "emergency_stop") -> RecordResult:
    from strategy_manager import disable_real_trading
    res = disable_real_trading()
    return RecordResult(status=res.status, recorded=res.recorded, message=reason, metadata={"source": "real_trade_manager"})




def _exchange_position_to_status(row: Any, *, client: Optional[ToobitClient] = None) -> dict[str, Any]:
    """Normalize a Toobit position row/dataclass for status display only."""
    row_map = _object_to_dict(row)
    c = client or get_client()
    symbol_raw = row_map.get("symbol") or row_map.get("contractCode") or row_map.get("instrumentId") or row_map.get("instId") or ""
    symbol = c.normalize_bot_symbol(symbol_raw) if hasattr(c, "normalize_bot_symbol") else normalize_symbol(symbol_raw)
    direction = normalize_direction(row_map.get("direction") or row_map.get("side"))
    qty = abs(safe_float(row_map.get("quantity") or row_map.get("qty") or row_map.get("volume") or row_map.get("positionAmt"), 0.0) or 0.0)
    entry = safe_float(row_map.get("entry_price") or row_map.get("entryPrice") or row_map.get("avgPrice") or row_map.get("price"), 0.0) or 0.0
    mark = safe_float(row_map.get("markPrice") or row_map.get("lastPrice") or row_map.get("currentPrice") or entry, entry) or entry
    pnl = safe_float(row_map.get("unrealized_pnl") or row_map.get("unRealizedProfit") or row_map.get("unrealizedPnl") or row_map.get("pnl") or row_map.get("profit"), 0.0) or 0.0
    leverage = safe_int(row_map.get("leverage") or row_map.get("lever"), 0) or 0
    return {"symbol": symbol, "exchange_symbol": safe_str(symbol_raw), "direction": direction, "quantity": qty, "entry": entry, "mark": mark, "pnl_usdt": pnl, "leverage": leverage, "raw": row_map}


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
        "effective_real_open": len(real_positions),
        "available_real_slots": max(0, (safe_int(runtime.get("max_concurrent_real_positions"), 0) or 0) - len(real_positions)),
        "real_slots_over_limit": False,
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
            if hasattr(c, "get_account_balance"):
                balance = c.get_account_balance("USDT")
                status["balance"] = dict(balance)
            else:
                available = c.get_wallet_margin_usdt()
                status["balance"] = {"status": STATUS_OK, "asset": "USDT", "balance": available, "available": available}
            balance = status["balance"]
            status["toobit_connected"] = bool(balance.get("status") == STATUS_OK and balance.get("available") is not None)
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
        # Effective REAL slot usage is exchange-first. Toobit can contain REAL
        # positions that are missing from positions.json; those must still count
        # against the configured REAL max to prevent over-opening.
        max_real_slots = safe_int(status.get("max_concurrent_real_positions"), 0) or 0
        status["effective_real_open"] = max(status.get("local_real_open", 0), status.get("toobit_open_total", 0))
        status["available_real_slots"] = max(0, max_real_slots - safe_int(status.get("effective_real_open"), 0)) if max_real_slots > 0 else 0
        status["real_slots_over_limit"] = bool(max_real_slots > 0 and safe_int(status.get("effective_real_open"), 0) > max_real_slots)

        # Keep local REAL slots aligned with the exchange before showing the panel.
        # If Toobit no longer has a REAL position but positions.json still marks it open,
        # the stale local record is closed so max-real slots are freed immediately.
        if reconcile_real_positions_with_exchange is not None:
            reconcile_result = reconcile_real_positions_with_exchange(
                exchange_positions,
                close_reason="trade_status_exchange_reconcile",
            )
            status["reconcile"] = reconcile_result
            if safe_int(reconcile_result.get("closed_count"), 0) > 0:
                refreshed_local_positions = get_open_positions()
                refreshed_real_positions = [p for p in refreshed_local_positions if safe_str(p.mode).upper() == MODE_REAL]
                refreshed_ghost_positions = [p for p in refreshed_local_positions if safe_str(p.mode).upper() != MODE_REAL]
                status["local_open_total"] = len(refreshed_local_positions)
                status["local_real_open"] = len(refreshed_real_positions)
                status["local_ghost_open"] = len(refreshed_ghost_positions)
                status["local_positions"] = [p.__dict__ for p in refreshed_local_positions]
                max_real_slots = safe_int(status.get("max_concurrent_real_positions"), 0) or 0
                status["effective_real_open"] = max(status.get("local_real_open", 0), status.get("toobit_open_total", 0))
                status["available_real_slots"] = max(0, max_real_slots - safe_int(status.get("effective_real_open"), 0)) if max_real_slots > 0 else 0
                status["real_slots_over_limit"] = bool(max_real_slots > 0 and safe_int(status.get("effective_real_open"), 0) > max_real_slots)
        else:
            status["reconcile"] = {
                "status": STATUS_FAILED,
                "changed": False,
                "closed_count": 0,
                "error": "position_manager_reconcile_missing",
            }
    except Exception as exc:
        status["errors"].append(f"positions_error:{exc}")

    if status["errors"]:
        # Hard config/exchange errors must be visible to the panel.
        # Do not hide margin/Toobit failures as OK; the UI can still render the payload.
        status["status"] = STATUS_FAILED
    return status


def validate_real_trade_manager_light() -> dict[str, Any]:
    errors: list[str] = []
    runtime = get_runtime()
    if safe_str(runtime.get("margin_mode"), MARGIN_ISOLATED).upper() != MARGIN_ISOLATED:
        errors.append("margin_mode_not_isolated")
    return {"system_version": SYSTEM_VERSION, "real_trade_manager_version": REAL_TRADE_MANAGER_VERSION, "status": STATUS_OK if not errors else STATUS_FAILED, "valid": not errors, "errors": errors, "checked_at": utc_now_iso()}


__all__ = [
    "REAL_TRADE_MANAGER_VERSION", "get_runtime", "estimate_quantity", "estimate_tp1_net_profit",
    "extract_decision_hunter_features", "real_hunter_safety_guard", "build_ai_decision_snapshot",
    "preflight_real_trade", "build_pending_position", "open_real_trade", "confirm_real_open",
    "wait_for_real_open_confirmation", "close_real_position", "close_position_executor",
    "exchange_position_checker", "closed_pnl_reader",
    "emergency_disable_real_trading", "verify_or_repair_same_tp_sl_after_delay", "get_real_trade_status", "validate_real_trade_manager_light",
]
