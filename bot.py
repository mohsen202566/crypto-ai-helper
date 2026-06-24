"""
bot.py
Level 4 / 1H Smart Scalp Bot

Main orchestration layer with RealTrade/Toobit integration and Telegram runtime.

Architecture lock:
- Owns Telegram-style command execution orchestration.
- Uses command_router.py to parse commands.
- Uses telegram_ui.py to build Persian texts.
- Can call analysis engines for manual analysis/scan.
- Can show status, stats, positions, and strategy settings.
- Does not directly call Toobit low-level APIs.
- Real execution is delegated only to real_trade_manager.py.
- real_trade_manager.py delegates low-level exchange calls only to tobit_client.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Mapping, Optional

from constants import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    LEVEL_4_SYMBOLS,
    MODE_GHOST,
    MODE_REAL,
    MODE_REJECT,
    POSITION_ACTIVE_GHOST,
    OKX_CANDLE_LIMIT_DEFAULT,
    PRIMARY_TIMEFRAME,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_RECOVERED,
    STRATEGY_LEVEL,
    STRATEGY_CODE,
    SYSTEM_VERSION,
)
from command_router import CommandRoute, parse_command, validate_route
from telegram_ui import (
    render_ai_decision,
    render_error,
    render_help,
    render_ok,
    render_positions_list,
    render_stats_snapshot,
    render_strategy_status,
    render_trade_runtime,
    render_ai_status,
    render_reset_stats_result,
    render_unknown_command,
    validate_rendered_text,
)
import strategy_manager
from position_manager import add_position, get_open_positions, has_open_position, update_position
from signal_manager import (
    mark_ghost_opened,
    mark_real_open_failed,
    mark_real_open_requested,
    mark_rejected,
    record_signal,
)
from stats_engine import build_stats_snapshot
from models import AIDecision, Candle, MarketSnapshot, MonitorEvent, TradeCloseResult, TradePosition
from market_data import fetch_market_snapshot, make_offline_snapshot
from technical_sensors import build_sensor_snapshot
from structure_engine import build_structure_snapshot
from momentum_engine import build_momentum_snapshot
from liquidity_engine import build_liquidity_snapshot
from market_context import build_market_context_from_snapshots
from reversal_engine import build_reversal_snapshot
from timing_engine import build_timing_snapshot
from tp_sl_engine import build_tp_sl_plan
from ai_brain import build_ai_decision, validate_ai_decision, evaluate_open_position
from real_trade_manager import (
    close_real_position,
    close_position_executor,
    confirm_real_open,
    open_real_trade,
    preflight_real_trade,
    validate_real_trade_manager_light,
    get_real_trade_status,
)
from learning_memory import get_learning_summary, reset_learning_memory
from position_monitor import monitor_positions_once, progress_to_tp1
from utils import normalize_direction, normalize_symbol, safe_float, safe_int, safe_str, utc_now_iso


BOT_VERSION: str = SYSTEM_VERSION
LOGGER_NAME = "level4_bot"
logger = logging.getLogger(LOGGER_NAME)


# =============================================================================
# Response helpers
# =============================================================================

def make_bot_response(
    *,
    text: str,
    status: str = STATUS_OK,
    action: str = "",
    data: Optional[Mapping[str, Any]] = None,
    reply_to_message_id: Optional[int] = None,
) -> dict[str, Any]:
    return {
        "system_version": SYSTEM_VERSION,
        "created_at": utc_now_iso(),
        "status": status,
        "action": action,
        "text": safe_str(text),
        "data": dict(data or {}),
        "reply_to_message_id": reply_to_message_id,
    }


def validate_bot_response(response: Mapping[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if safe_str(response.get("system_version")) != SYSTEM_VERSION:
        errors.append("INVALID_SYSTEM_VERSION")
    if safe_str(response.get("status")) not in {STATUS_OK, STATUS_FAILED}:
        errors.append("INVALID_STATUS")
    if not safe_str(response.get("text")):
        errors.append("EMPTY_TEXT")
    text_validation = validate_rendered_text(safe_str(response.get("text")))
    if not text_validation.get("valid"):
        errors.extend(text_validation.get("errors", []))
    return {
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "action": response.get("action"),
    }


# =============================================================================
# Safe adapters for strategy_manager versions
# =============================================================================

def _call_first(names: list[str], *args: Any, **kwargs: Any) -> Any:
    for name in names:
        fn = getattr(strategy_manager, name, None)
        if callable(fn):
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
                    logger.exception("strategy_manager adapter failed: %s", name)
                    break
    return None


def _result_ok(result: Any) -> bool:
    if isinstance(result, Mapping):
        status = safe_str(result.get("status")).upper()
        return status == STATUS_OK or bool(result.get("ok", False)) or bool(result.get("success", False)) or bool(result.get("recorded", False))
    if result is None:
        return True
    return bool(result)


def _get_trade_runtime() -> dict[str, Any]:
    result = _call_first(["get_trade_runtime_config", "get_runtime_config", "get_trade_settings", "get_settings"])
    return dict(result) if isinstance(result, Mapping) else {}


def _set_strategy_level(level: int) -> bool:
    """Set the single active strategy level for new decisions.

    Level 4 is fully implemented in this repository. Other levels can be selected
    in state so old/open positions keep their original level, but this Level 4
    process will refuse new analysis/trades unless Level 4 is active.
    """
    level = safe_int(level, STRATEGY_LEVEL) or STRATEGY_LEVEL
    if not (1 <= level <= 9):
        return False

    if level == STRATEGY_LEVEL:
        result = _call_first(["set_level4_active"])
        if result is not None:
            return _result_ok(result)

    try:
        state = strategy_manager.load_strategy_state()
        state["active_level"] = level
        state["active_strategy"] = STRATEGY_CODE if level == STRATEGY_LEVEL else f"LEVEL_{level}"
        return bool(strategy_manager.save_strategy_state(state))
    except Exception:
        logger.exception("failed to set strategy level")
        return False


def _list_strategy_levels() -> list[dict[str, Any]]:
    try:
        state = strategy_manager.load_strategy_state()
        active = safe_int(state.get("active_level"), STRATEGY_LEVEL) or STRATEGY_LEVEL
    except Exception:
        active = STRATEGY_LEVEL
    levels: list[dict[str, Any]] = []
    for level in range(1, 10):
        levels.append({
            "level": level,
            "name": "Level 4 / 1H Smart Scalp" if level == STRATEGY_LEVEL else f"Level {level}",
            "active": level == active,
            "implemented": level == STRATEGY_LEVEL,
            "new_signals_allowed": level == active == STRATEGY_LEVEL,
        })
    return levels


def _render_strategy_list() -> str:
    lines = ["📚 لیست استراتژی‌ها"]
    for item in _list_strategy_levels():
        active = "✅ فعال" if item.get("active") else "▫️ غیرفعال"
        implemented = "آماده اجرا" if item.get("implemented") else "غیرفعال در این نسخه"
        lines.append(f"Level {item['level']}: {item['name']} | {active} | {implemented}")
    lines.append("")
    lines.append("قانون: فقط Level انتخاب‌شده برای تصمیم‌های جدید فعال است؛ این فایل فقط منطق اجرایی Level 4 را دارد.")
    return "\n".join(lines)


def _update_runtime(**kwargs: Any) -> bool:
    """Persist real trading runtime settings; not a display-only update."""
    try:
        if "margin_usdt" in kwargs:
            fn = getattr(strategy_manager, "set_margin_usdt", None)
            if callable(fn):
                return _result_ok(fn(kwargs["margin_usdt"]))
            state = strategy_manager.load_strategy_state()
            state["margin_usdt"] = safe_float(kwargs["margin_usdt"], state.get("margin_usdt"))
            return bool(strategy_manager.save_strategy_state(state))

        if "min_margin_usdt" in kwargs:
            fn = getattr(strategy_manager, "set_min_margin_usdt", None)
            if callable(fn):
                return _result_ok(fn(kwargs["min_margin_usdt"]))
            state = strategy_manager.load_strategy_state()
            value = safe_float(kwargs["min_margin_usdt"], None)
            if value is None:
                return False
            current_max = safe_float(state.get("max_margin_usdt", state.get("margin_usdt", value)), value) or value
            if value < 1 or value > 1000 or value > current_max:
                return False
            state["min_margin_usdt"] = value
            state["margin_usdt"] = max(value, min(current_max, safe_float(state.get("margin_usdt", value), value) or value))
            return bool(strategy_manager.save_strategy_state(state))

        if "max_margin_usdt" in kwargs:
            fn = getattr(strategy_manager, "set_max_margin_usdt", None)
            if callable(fn):
                return _result_ok(fn(kwargs["max_margin_usdt"]))
            state = strategy_manager.load_strategy_state()
            value = safe_float(kwargs["max_margin_usdt"], None)
            if value is None:
                return False
            current_min = safe_float(state.get("min_margin_usdt", state.get("margin_usdt", value)), value) or value
            if value < 1 or value > 1000 or value < current_min:
                return False
            state["max_margin_usdt"] = value
            state["margin_usdt"] = max(current_min, min(value, safe_float(state.get("margin_usdt", current_min), current_min) or current_min))
            return bool(strategy_manager.save_strategy_state(state))

        if "leverage" in kwargs:
            fn = getattr(strategy_manager, "set_leverage", None)
            if callable(fn):
                return _result_ok(fn(kwargs["leverage"]))
            state = strategy_manager.load_strategy_state()
            state["leverage"] = safe_int(kwargs["leverage"], state.get("leverage"))
            return bool(strategy_manager.save_strategy_state(state))

        if "max_positions" in kwargs or "max_concurrent_real_positions" in kwargs or "max_concurrent_total_positions" in kwargs:
            value = kwargs.get("max_positions", kwargs.get("max_concurrent_real_positions", kwargs.get("max_concurrent_total_positions")))
            count = safe_int(value, None)
            if count is None or count <= 0:
                return False
            state = strategy_manager.load_strategy_state()
            state["max_concurrent_real_positions"] = count
            state["max_concurrent_total_positions"] = max(count, safe_int(state.get("max_concurrent_total_positions"), count) or count)
            return bool(strategy_manager.save_strategy_state(state))

        if "real_trading_enabled" in kwargs:
            return _result_ok(strategy_manager.set_real_trading(bool(kwargs["real_trading_enabled"])))
    except Exception:
        logger.exception("failed to update runtime")
        return False
    return False


def _reset_trade_runtime() -> bool:
    try:
        result = strategy_manager.reset_strategy_state()
        return _result_ok(result)
    except Exception:
        logger.exception("failed to reset trade runtime")
        return False


def _enable_trade() -> bool:
    return _result_ok(_call_first(["enable_real_trading", "enable_trade", "set_trade_enabled"], True))


def _disable_trade() -> bool:
    return _result_ok(_call_first(["disable_real_trading", "disable_trade", "set_trade_enabled"], False))


def _real_trading_enabled() -> bool:
    fn = getattr(strategy_manager, "is_real_trading_enabled", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            logger.exception("is_real_trading_enabled failed")
    state = _call_first(["load_strategy_state", "get_strategy_state"])
    if isinstance(state, Mapping):
        return bool(state.get("real_trading_enabled", state.get("trade_enabled", False)))
    return False


# =============================================================================
# Market provider adapter
# =============================================================================

class OKXMarketProvider:
    """Live OKX provider used by Telegram commands."""

    def get_candles(self, symbol: str, timeframe: str = PRIMARY_TIMEFRAME, limit: int = OKX_CANDLE_LIMIT_DEFAULT) -> list[Candle]:
        result = fetch_market_snapshot(normalize_symbol(symbol), timeframe=timeframe, limit=limit)
        snapshot = getattr(result, "snapshot", None)
        if snapshot is not None and getattr(snapshot, "candles", None):
            return list(snapshot.candles)
        return []


def provider_get_candles(provider: Any, symbol: str, *, timeframe: str = PRIMARY_TIMEFRAME, limit: int = OKX_CANDLE_LIMIT_DEFAULT) -> list[Candle]:
    raw: Any = None

    if isinstance(provider, Mapping):
        raw = provider.get(normalize_symbol(symbol)) or provider.get(symbol)
    else:
        for name in ("get_candles", "fetch_candles", "candles"):
            fn = getattr(provider, name, None)
            if callable(fn):
                try:
                    raw = fn(symbol, timeframe=timeframe, limit=limit)
                    break
                except TypeError:
                    try:
                        raw = fn(symbol, timeframe, limit)
                        break
                    except TypeError:
                        try:
                            raw = fn(symbol)
                            break
                        except Exception:
                            raw = None
                            break
                    except Exception:
                        raw = None
                        break
                except Exception:
                    raw = None
                    break
        if raw is None and callable(provider):
            try:
                raw = provider(symbol, timeframe, limit)
            except TypeError:
                try:
                    raw = provider(symbol)
                except Exception:
                    raw = None
            except Exception:
                raw = None

    if raw is None:
        return []

    candles: list[Candle] = []
    for item in list(raw)[-limit:]:
        if isinstance(item, Candle):
            candles.append(item)
        elif isinstance(item, Mapping):
            candles.append(
                Candle(
                    timestamp=item.get("timestamp", item.get("time", 0)),
                    open=item.get("open", item.get("o", 0.0)),
                    high=item.get("high", item.get("h", 0.0)),
                    low=item.get("low", item.get("l", 0.0)),
                    close=item.get("close", item.get("c", 0.0)),
                    volume=item.get("volume", item.get("v", 0.0)),
                    timeframe=timeframe,
                )
            )
    return candles


def build_snapshots_from_provider(provider: Any, symbols: list[str], *, timeframe: str = PRIMARY_TIMEFRAME, limit: int = OKX_CANDLE_LIMIT_DEFAULT) -> dict[str, MarketSnapshot]:
    snapshots: dict[str, MarketSnapshot] = {}
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        candles = provider_get_candles(provider, normalized, timeframe=timeframe, limit=limit)
        if candles:
            snapshots[normalized] = make_offline_snapshot(normalized, timeframe, candles)
    return snapshots


# =============================================================================
# Analysis orchestration
# =============================================================================

def infer_direction_from_sensor(sensor: Any) -> str:
    price = safe_float(getattr(sensor, "price", None), 0.0) or 0.0
    ema20 = safe_float(getattr(sensor, "ema20", None), None)
    vwap = safe_float(getattr(sensor, "vwap", None), None)
    rsi_slope = safe_float(getattr(sensor, "rsi_slope", None), 0.0) or 0.0
    macd_slope = safe_float(getattr(sensor, "macd_hist_slope", None), 0.0) or 0.0
    buy = safe_float(getattr(sensor, "buy_power", None), 50.0) or 50.0
    sell = safe_float(getattr(sensor, "sell_power", None), 50.0) or 50.0

    score = 0.0
    if ema20 is not None:
        score += 1.0 if price >= ema20 else -1.0
    if vwap is not None:
        score += 1.0 if price >= vwap else -1.0
    score += 1.0 if rsi_slope > 0 else -1.0 if rsi_slope < 0 else 0.0
    score += 1.0 if macd_slope > 0 else -1.0 if macd_slope < 0 else 0.0
    score += 1.0 if buy > sell else -1.0 if sell > buy else 0.0
    return DIRECTION_LONG if score >= 0 else DIRECTION_SHORT


def analyze_market_snapshot(
    snapshot: MarketSnapshot,
    *,
    direction: str = "",
    context_snapshots: Optional[Mapping[str, MarketSnapshot]] = None,
    trade_config: Optional[Mapping[str, Any]] = None,
    trade_state: Optional[Mapping[str, Any]] = None,
) -> AIDecision:
    symbol = normalize_symbol(snapshot.symbol)
    sensor = build_sensor_snapshot(snapshot)
    d = normalize_direction(direction) if direction else infer_direction_from_sensor(sensor)

    structure = build_structure_snapshot(snapshot, d, sensor)
    momentum = build_momentum_snapshot(sensor, d)
    liquidity = build_liquidity_snapshot(snapshot, d, structure, sensor)

    context_data = dict(context_snapshots or {}) or {symbol: snapshot}
    context = build_market_context_from_snapshots(context_data, d)

    reversal = build_reversal_snapshot(sensor=sensor, structure=structure, momentum=momentum, liquidity=liquidity, context=context, direction=d)
    timing = build_timing_snapshot(sensor=sensor, structure=structure, momentum=momentum, liquidity=liquidity, context=context, direction=d, reversal_snapshot=reversal)

    runtime = dict(trade_config or _get_trade_runtime())
    tp_sl = build_tp_sl_plan(symbol=symbol, direction=d, entry=sensor.price, sensor=sensor, structure=structure, momentum=momentum, liquidity=liquidity, context=context, trade_config=runtime)

    return build_ai_decision(
        symbol=symbol,
        direction=d,
        sensor=sensor,
        structure=structure,
        momentum=momentum,
        liquidity=liquidity,
        context=context,
        tp_sl=tp_sl,
        reversal_snapshot=reversal,
        timing_snapshot=timing,
        trade_state=trade_state,
    )


def analyze_symbol_with_provider(symbol: str, provider: Any, *, timeframe: str = PRIMARY_TIMEFRAME, limit: int = OKX_CANDLE_LIMIT_DEFAULT, context_symbols: Optional[list[str]] = None) -> AIDecision:
    normalized = normalize_symbol(symbol)
    symbols = [normalized]
    for item in context_symbols or ["BTCUSDT", "ETHUSDT"]:
        item_norm = normalize_symbol(item)
        if item_norm and item_norm not in symbols:
            symbols.append(item_norm)

    snapshots = build_snapshots_from_provider(provider, symbols, timeframe=timeframe, limit=limit)
    if normalized not in snapshots:
        return AIDecision(
            symbol=normalized,
            direction=DIRECTION_LONG,
            mode=MODE_REJECT,
            score=0.0,
            confidence=0.0,
            entry=0.0,
            reject_reason="MARKET_DATA_UNAVAILABLE",
            reason_codes=["MARKET_DATA_UNAVAILABLE"],
            metadata={"available_symbols": list(snapshots.keys())},
        )

    return analyze_market_snapshot(snapshots[normalized], context_snapshots=snapshots)


def scan_market_with_provider(symbols: list[str], provider: Any, *, timeframe: str = PRIMARY_TIMEFRAME, limit: int = OKX_CANDLE_LIMIT_DEFAULT, max_results: int = 5) -> list[AIDecision]:
    normalized_symbols = [normalize_symbol(s) for s in symbols if normalize_symbol(s)]
    fetch_symbols = list(dict.fromkeys(normalized_symbols + ["BTCUSDT", "ETHUSDT"]))
    snapshots = build_snapshots_from_provider(provider, fetch_symbols, timeframe=timeframe, limit=limit)

    decisions: list[AIDecision] = []
    for symbol in normalized_symbols:
        snapshot = snapshots.get(symbol)
        if snapshot is None:
            continue
        decisions.append(analyze_market_snapshot(snapshot, context_snapshots=snapshots))

    decisions.sort(key=lambda d: (safe_float(d.score, 0.0) or 0.0, safe_float(d.confidence, 0.0) or 0.0), reverse=True)
    return decisions[: max(1, safe_int(max_results, 5) or 5)]



# =============================================================================
# Signal / GHOST lifecycle persistence
# =============================================================================

def _tp_sl_payload(decision: AIDecision) -> dict[str, Any]:
    """Return a safe TP/SL dict from AIDecision without changing the model."""
    plan = decision.tp_sl
    if plan is None:
        return {}
    if isinstance(plan, Mapping):
        return dict(plan)
    return {
        "symbol": getattr(plan, "symbol", decision.symbol),
        "direction": getattr(plan, "direction", decision.direction),
        "entry": safe_float(getattr(plan, "entry", decision.entry), decision.entry),
        "tp1": safe_float(getattr(plan, "tp1", 0.0), 0.0),
        "tp2": safe_float(getattr(plan, "tp2", None), None),
        "sl": safe_float(getattr(plan, "sl", 0.0), 0.0),
        "rr": safe_float(getattr(plan, "rr", 0.0), 0.0),
        "tp1_net_profit_estimate": safe_float(getattr(plan, "tp1_net_profit_estimate", 0.0), 0.0),
        "valid": bool(getattr(plan, "valid", True)),
        "reason_codes": list(getattr(plan, "reason_codes", []) or []),
    }


def _ghost_quantity_for_decision(decision: AIDecision) -> tuple[float, float, int]:
    """Estimate GHOST quantity from runtime so virtual PnL is meaningful."""
    runtime = _get_trade_runtime()
    margin = safe_float(
        runtime.get("margin_usdt")
        or runtime.get("trade_margin_usdt")
        or runtime.get("margin_per_trade")
        or runtime.get("min_margin_usdt"),
        0.0,
    ) or 0.0
    leverage = safe_int(runtime.get("leverage") or runtime.get("trade_leverage"), 1) or 1
    entry = safe_float(decision.entry, 0.0) or 0.0
    quantity = (margin * leverage / entry) if entry > 0 and margin > 0 and leverage > 0 else 0.0
    return quantity, margin, leverage


def _build_ghost_position(decision: AIDecision) -> Optional[TradePosition]:
    """Build an ACTIVE_GHOST position from a GHOST decision for monitor/learning."""
    plan = decision.tp_sl
    if plan is None:
        return None
    entry = safe_float(getattr(plan, "entry", decision.entry), decision.entry) or safe_float(decision.entry, 0.0) or 0.0
    tp1 = safe_float(getattr(plan, "tp1", 0.0), 0.0) or 0.0
    tp2 = safe_float(getattr(plan, "tp2", None), None)
    sl = safe_float(getattr(plan, "sl", 0.0), 0.0) or 0.0
    if entry <= 0 or tp1 <= 0 or sl <= 0:
        return None

    quantity, margin, leverage = _ghost_quantity_for_decision(decision)
    return TradePosition(
        symbol=decision.symbol,
        direction=decision.direction,
        mode=MODE_GHOST,
        entry=entry,
        tp1=tp1,
        tp2=tp2,
        sl=sl,
        status=POSITION_ACTIVE_GHOST,
        signal_id=decision.signal_id,
        quantity=quantity,
        margin_usdt=margin,
        leverage=leverage,
        current_price=entry,
        highest_price=entry,
        lowest_price=entry,
        decision_metadata={
            "source": "bot.py",
            "decision_score": decision.score,
            "decision_confidence": decision.confidence,
            "reason_codes": list(decision.reason_codes or []),
            "reject_reason": decision.reject_reason,
            "decision_metadata": dict(decision.metadata or {}),
            "tp_sl": _tp_sl_payload(decision),
        },
        level=decision.level,
    )


def persist_signal_lifecycle(decision: AIDecision, *, execution: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Persist generated signal and create ACTIVE_GHOST position when needed."""
    result: dict[str, Any] = {
        "signal_recorded": False,
        "signal_id": safe_str(getattr(decision, "signal_id", "")),
        "mode": safe_str(getattr(decision, "mode", "")).upper(),
        "position_recorded": False,
        "position_id": "",
        "errors": [],
    }
    try:
        signal_result = record_signal(decision)
        result["signal_recorded"] = bool(signal_result.recorded or signal_result.message == "signal_id_exists")
        result["signal_result"] = {
            "status": signal_result.status,
            "recorded": signal_result.recorded,
            "record_id": signal_result.record_id,
            "message": signal_result.message,
            "error": signal_result.error,
        }
        if signal_result.record_id:
            result["signal_id"] = signal_result.record_id
    except Exception as exc:
        logger.exception("signal_record_failed")
        result["errors"].append(f"signal_record_failed:{exc}")

    mode = safe_str(decision.mode).upper()
    try:
        if mode == MODE_GHOST:
            if has_open_position(decision.symbol, decision.direction):
                result["position_skipped"] = "duplicate_open_position"
                try:
                    mark_ghost_opened(decision.signal_id, metadata={"skipped": "duplicate_open_position"})
                except Exception:
                    logger.exception("mark_ghost_opened_duplicate_failed")
                return result
            position = _build_ghost_position(decision)
            if position is None:
                result["errors"].append("ghost_position_build_failed")
                return result
            add_result = add_position(position, reject_duplicate=True)
            result["position_recorded"] = bool(add_result.recorded)
            result["position_id"] = add_result.record_id or position.position_id
            result["position_result"] = {
                "status": add_result.status,
                "recorded": add_result.recorded,
                "record_id": add_result.record_id,
                "message": add_result.message,
                "error": add_result.error,
            }
            mark_ghost_opened(decision.signal_id, metadata={"position_id": result["position_id"]})
            logger.info("ghost_position_recorded symbol=%s direction=%s position_id=%s recorded=%s", decision.symbol, decision.direction, result["position_id"], result["position_recorded"])
        elif mode == MODE_REAL:
            meta = dict(execution or {})
            if meta.get("status") == STATUS_FAILED:
                mark_real_open_failed(decision.signal_id, metadata=meta)
            else:
                mark_real_open_requested(decision.signal_id, metadata=meta)
        elif mode == MODE_REJECT:
            mark_rejected(decision.signal_id, reason=decision.reject_reason or ",".join(decision.reason_codes or []))
    except Exception as exc:
        logger.exception("signal_lifecycle_persist_failed")
        result["errors"].append(f"lifecycle_failed:{exc}")
    return result

# =============================================================================
# RealTrade integration
# =============================================================================

def force_real_to_ghost_when_trade_off(decision: AIDecision) -> AIDecision:
    """REAL signals are allowed only when real trading is ON.

    If the AI marks a signal as REAL while trading is OFF, it must be converted
    to GHOST before rendering, persistence, or execution. This prevents display-only
    REAL messages and prevents accidental REAL lifecycle records when trading is disabled.
    """
    if safe_str(getattr(decision, "mode", "")).upper() == MODE_REAL and not _real_trading_enabled():
        decision.mode = MODE_GHOST
        if "REAL_TRADE_OFF_CONVERTED_TO_GHOST" not in decision.reason_codes:
            decision.reason_codes.append("REAL_TRADE_OFF_CONVERTED_TO_GHOST")
        metadata = dict(decision.metadata or {})
        metadata["real_trade_off_converted_to_ghost"] = True
        decision.metadata = metadata
    return decision

def _convert_real_failure_to_ghost(decision: AIDecision, *, reason: str, metadata: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Convert a blocked/failed REAL decision into GHOST safely.

    Important rule: when AI wants REAL but REAL execution is blocked by risk
    guards (max exchange positions, duplicate, Toobit guard, preflight failure,
    open-order failure), the signal must still be recorded and learned as GHOST.
    This function mutates only the decision mode/metadata and does not create
    the position itself; persist_signal_lifecycle() will create the GHOST record.
    """
    original_mode = safe_str(getattr(decision, "mode", "")).upper()
    decision.mode = MODE_GHOST

    code = safe_str(reason, "real_failed_converted_to_ghost").upper()
    reason_code = f"REAL_BLOCKED_CONVERTED_TO_GHOST:{code}"
    if reason_code not in decision.reason_codes:
        decision.reason_codes.append(reason_code)

    meta = dict(decision.metadata or {})
    meta["real_blocked_converted_to_ghost"] = True
    meta["real_block_reason"] = safe_str(reason)
    meta["original_mode"] = original_mode or MODE_REAL
    if metadata:
        meta["real_block_details"] = dict(metadata)
    decision.metadata = meta

    return {
        "executed": False,
        "status": STATUS_OK,
        "reason": "real_blocked_converted_to_ghost",
        "converted_to_ghost": True,
        "real_block_reason": safe_str(reason),
        "real_block_details": dict(metadata or {}),
    }


def maybe_execute_real_decision(decision: AIDecision) -> dict[str, Any]:
    """
    Execute REAL decision through real_trade_manager only.

    If real trading is off, or REAL execution is blocked/fails before a
    confirmed order, the decision is converted to GHOST so it is still recorded
    and learned. This keeps safety guards strict without losing learning data.
    """
    force_real_to_ghost_when_trade_off(decision)
    if decision.mode != MODE_REAL:
        reason = "real_trading_disabled_converted_to_ghost" if "REAL_TRADE_OFF_CONVERTED_TO_GHOST" in decision.reason_codes else "not_real_decision"
        return {"executed": False, "status": STATUS_OK, "reason": reason, "converted_to_ghost": decision.mode == MODE_GHOST}

    pf = preflight_real_trade(decision)
    if not pf.get("ok"):
        return _convert_real_failure_to_ghost(decision, reason="preflight_failed", metadata={"preflight": pf})

    result = open_real_trade(decision)

    # Only keep REAL when the exchange order was accepted/recovered. Any failed
    # open attempt becomes GHOST. This includes max-position guards, duplicate
    # guards, stale TP/SL guards, quantity errors, and Toobit-side rejects.
    if result.status not in {STATUS_OK, STATUS_RECOVERED}:
        return _convert_real_failure_to_ghost(
            decision,
            reason="real_open_failed",
            metadata={
                "status": result.status,
                "error": result.error,
                "message": result.message,
                "raw": result.raw,
            },
        )

    return {
        "executed": True,
        "status": result.status,
        "position_id": result.position_id,
        "exchange_order_id": result.exchange_order_id,
        "error": result.error,
        "message": result.message,
        "raw": result.raw,
    }


def render_real_execution_note(execution: Mapping[str, Any]) -> str:
    if not execution or not execution.get("executed"):
        if execution.get("status") == STATUS_FAILED:
            return "\n\n⚠️ اجرای REAL انجام نشد:\n" + safe_str(execution.get("reason")) + "\n" + safe_str(execution.get("error"))
        return ""
    return "\n\n✅ سفارش REAL ارسال شد\nPosition ID: " + safe_str(execution.get("position_id"))


def find_open_position_for_symbol(symbol: str) -> Optional[TradePosition]:
    target = normalize_symbol(symbol)
    for position in get_open_positions():
        if normalize_symbol(position.symbol) == target:
            return position
    return None


def render_close_result(result: TradeCloseResult) -> str:
    if result.close_confirmed:
        pnl = result.pnl_usdt
        pnl_text = "-" if pnl is None else f"{pnl:.2f}$"
        confirmed = "تایید شده ✅" if result.pnl_confirmed else "تخمینی / تایید نشده ⚠️"
        return "\n".join([
            "✅ درخواست بستن پوزیشن تایید شد",
            f"Symbol: {normalize_symbol(result.symbol)}",
            f"Direction: {normalize_direction(result.direction)}",
            f"Qty: {result.closed_quantity}",
            f"PnL: {pnl_text}",
            f"PnL واقعی: {confirmed}",
        ])
    return "\n".join([
        "❌ بستن پوزیشن تایید نشد",
        f"Symbol: {normalize_symbol(result.symbol)}",
        f"Direction: {normalize_direction(result.direction)}",
        f"Error: {result.error or result.message or '-'}",
    ])


# =============================================================================
# Command execution
# =============================================================================

def execute_route(
    command_route: CommandRoute,
    *,
    market_provider: Optional[Any] = None,
    default_scan_symbols: Optional[list[str]] = None,
    auto_execute_real: bool = True,
) -> dict[str, Any]:
    validation = validate_route(command_route)
    if not validation.get("valid"):
        return make_bot_response(text=render_error("مسیر دستور نامعتبر است."), status=STATUS_FAILED, action=command_route.action, data={"validation": validation})

    action = command_route.action
    args = command_route.args

    try:
        if action == "HELP":
            return make_bot_response(text=command_route.reply_text or render_help(), action=action)
        if action == "UNKNOWN":
            return make_bot_response(text=command_route.reply_text or render_unknown_command(), status=STATUS_FAILED, action=action)

        if action == "SET_STRATEGY_LEVEL":
            level = safe_int(args.get("level"), STRATEGY_LEVEL) or STRATEGY_LEVEL
            if not (1 <= level <= 9):
                return make_bot_response(text=render_error("لول استراتژی نامعتبر است."), status=STATUS_FAILED, action=action)
            ok = _set_strategy_level(level)
            extra = "" if level == STRATEGY_LEVEL else "\n⚠️ این فایل فعلی فقط اجرای Level 4 را دارد؛ تا وقتی Level 4 فعال نباشد اسکن/تحلیل جدید اجرا نمی‌شود."
            return make_bot_response(text=(render_ok(f"استراتژی روی Level {level} تنظیم شد.") + extra) if ok else render_error("تغییر استراتژی انجام نشد."), status=STATUS_OK if ok else STATUS_FAILED, action=action, data={"level": level})

        if action == "LIST_STRATEGIES":
            return make_bot_response(text=_render_strategy_list(), action=action, data={"levels": _list_strategy_levels()})

        if action == "RESET_TRADE_SETTINGS":
            ok = _reset_trade_runtime()
            return make_bot_response(text=render_ok("تنظیمات ترید به مقدار پیش‌فرض برگشت.") if ok else render_error("ریست تنظیمات ترید انجام نشد."), status=STATUS_OK if ok else STATUS_FAILED, action=action)

        if action == "ENABLE_REAL_TRADING":
            ok = _enable_trade()
            return make_bot_response(text=render_ok("ترید واقعی فعال شد.") if ok else render_error("فعال‌سازی ترید واقعی انجام نشد."), status=STATUS_OK if ok else STATUS_FAILED, action=action)

        if action == "DISABLE_REAL_TRADING":
            ok = _disable_trade()
            return make_bot_response(text=render_ok("ترید واقعی غیرفعال شد. سیگنال‌های جدید GHOST می‌شوند.") if ok else render_error("غیرفعال‌سازی ترید انجام نشد."), status=STATUS_OK if ok else STATUS_FAILED, action=action)

        if action == "SET_MARGIN":
            value = safe_float(args.get("margin_usdt"), None)
            if value is None or value < 1 or value > 1000:
                return make_bot_response(text=render_error("مارجین نامعتبر است. عدد باید بین 1 تا 1000 دلار باشد."), status=STATUS_FAILED, action=action)
            ok = _update_runtime(margin_usdt=value)
            return make_bot_response(text=render_ok(f"مارجین ثابت روی {value}$ تنظیم شد.") if ok else render_error("ثبت مارجین انجام نشد."), status=STATUS_OK if ok else STATUS_FAILED, action=action)

        if action == "SET_MIN_MARGIN":
            value = safe_float(args.get("min_margin_usdt"), None)
            if value is None or value < 1 or value > 1000:
                return make_bot_response(text=render_error("حداقل مارجین نامعتبر است. عدد باید بین 1 تا 1000 دلار باشد."), status=STATUS_FAILED, action=action)
            ok = _update_runtime(min_margin_usdt=value)
            runtime = _get_trade_runtime()
            max_margin = safe_float(runtime.get("max_margin_usdt"), 0.0) or 0.0
            if ok:
                return make_bot_response(
                    text=render_ok(f"حداقل مارجین ترید روی {value}$ تنظیم شد. بازه فعلی: {value}$ تا {max_margin}$"),
                    action=action,
                    data={"min_margin_usdt": value, "max_margin_usdt": max_margin},
                )
            return make_bot_response(text=render_error("ثبت حداقل مارجین انجام نشد. حداقل باید بین 1 تا 1000 باشد و از حداکثر بیشتر نباشد."), status=STATUS_FAILED, action=action)

        if action == "SET_MAX_MARGIN":
            value = safe_float(args.get("max_margin_usdt"), None)
            if value is None or value < 1 or value > 1000:
                return make_bot_response(text=render_error("حداکثر مارجین نامعتبر است. عدد باید بین 1 تا 1000 دلار باشد."), status=STATUS_FAILED, action=action)
            ok = _update_runtime(max_margin_usdt=value)
            runtime = _get_trade_runtime()
            min_margin = safe_float(runtime.get("min_margin_usdt"), 0.0) or 0.0
            if ok:
                return make_bot_response(
                    text=render_ok(f"حداکثر مارجین ترید روی {value}$ تنظیم شد. بازه فعلی: {min_margin}$ تا {value}$"),
                    action=action,
                    data={"min_margin_usdt": min_margin, "max_margin_usdt": value},
                )
            return make_bot_response(text=render_error("ثبت حداکثر مارجین انجام نشد. حداکثر باید بین 1 تا 1000 باشد و از حداقل کمتر نباشد."), status=STATUS_FAILED, action=action)

        if action == "SET_LEVERAGE":
            value = safe_int(args.get("leverage"), None)
            if value is None or value <= 0:
                return make_bot_response(text=render_error("لوریج نامعتبر است."), status=STATUS_FAILED, action=action)
            ok = _update_runtime(leverage=value)
            return make_bot_response(text=render_ok(f"لوریج روی {value}x تنظیم شد.") if ok else render_error("ثبت لوریج انجام نشد."), status=STATUS_OK if ok else STATUS_FAILED, action=action)

        if action == "SET_MAX_POSITIONS":
            value = safe_int(args.get("max_positions"), None)
            if value is None or value <= 0:
                return make_bot_response(text=render_error("حداکثر پوزیشن نامعتبر است."), status=STATUS_FAILED, action=action)
            ok = _update_runtime(max_positions=value)
            return make_bot_response(text=render_ok(f"حداکثر پوزیشن روی {value} تنظیم شد.") if ok else render_error("ثبت حداکثر پوزیشن انجام نشد."), status=STATUS_OK if ok else STATUS_FAILED, action=action)

        if action == "SHOW_STRATEGY":
            return make_bot_response(text=render_strategy_status(), action=action)

        if action == "SHOW_TRADE_SETTINGS":
            status_payload = get_real_trade_status(include_exchange=auto_execute_real)
            runtime = _get_trade_runtime()
            for key in (
                "margin_usdt",
                "min_margin_usdt",
                "max_margin_usdt",
                "dynamic_position_sizing_enabled",
                "position_sizing_mode",
                "leverage",
            ):
                if key in runtime:
                    status_payload[key] = runtime.get(key)
            status_payload["watchlist"] = list(LEVEL_4_SYMBOLS)
            rendered = render_trade_runtime(status_payload)
            # Backward-compatible safety: older telegram_ui.py may not yet render
            # the new min/max margin fields, so bot.py appends them here.
            min_margin = safe_float(status_payload.get("min_margin_usdt"), None)
            max_margin = safe_float(status_payload.get("max_margin_usdt"), None)
            sizing_mode = safe_str(status_payload.get("position_sizing_mode"))
            if min_margin is not None and max_margin is not None and "حداقل مارجین" not in rendered and "مارجین حداقل" not in rendered:
                mode_text = "AI_DYNAMIC" if bool(status_payload.get("dynamic_position_sizing_enabled")) else "FIXED"
                if sizing_mode:
                    mode_text = sizing_mode
                rendered += "\n🎯 بازه مارجین AI"
                rendered += f"\nحداقل مارجین: {min_margin:+.2f}$"
                rendered += f"\nحداکثر مارجین: {max_margin:+.2f}$"
                rendered += f"\nحالت حجم‌دهی: {mode_text}"
            return make_bot_response(text=rendered, action=action, data={"trade_status": status_payload})

        if action == "SHOW_AI_STATUS":
            summary = get_learning_summary()
            return make_bot_response(text=render_ai_status(summary), action=action, data={"learning_summary": summary})

        if action == "RESET_STATS":
            result = reset_learning_memory(reset_archives=True, reset_stats=True)
            return make_bot_response(text=render_reset_stats_result(result), status=STATUS_OK if result.recorded else STATUS_FAILED, action=action, data={"reset": result.__dict__})

        if action == "SHOW_STATUS":
            snapshot = build_stats_snapshot()
            rtm = validate_real_trade_manager_light()
            text = render_strategy_status() + "\n\n" + render_stats_snapshot(snapshot)
            text += "\n\n🔌 RealTrade: " + ("OK ✅" if rtm.get("valid") else "FAILED ❌")
            return make_bot_response(text=text, action=action, data={"stats": snapshot, "real_trade_manager": rtm})

        if action == "SHOW_POSITIONS":
            positions = get_open_positions()
            return make_bot_response(text=render_positions_list(positions), action=action, data={"count": len(positions)})

        if action == "SHOW_STATS":
            snapshot = build_stats_snapshot()
            return make_bot_response(text=render_stats_snapshot(snapshot), action=action, data={"stats": snapshot})

        if action == "ANALYZE_SYMBOL":
            if not strategy_manager.is_level4_active():
                return make_bot_response(text=render_error("Level 4 فعال نیست؛ برای تحلیل جدید اول بنویس: استراتژی لول 4"), status=STATUS_FAILED, action=action)
            symbol = normalize_symbol(args.get("symbol"))
            if not market_provider:
                return make_bot_response(text=render_error("Market provider هنوز وصل نشده است."), status=STATUS_FAILED, action=action)
            decision = force_real_to_ghost_when_trade_off(analyze_symbol_with_provider(symbol, market_provider))
            validation = validate_ai_decision(decision)
            execution = maybe_execute_real_decision(decision) if auto_execute_real and validation.get("valid") else {"executed": False, "status": STATUS_OK}
            lifecycle = persist_signal_lifecycle(decision, execution=execution) if validation.get("valid") else {}
            text = render_ai_decision(decision) + render_real_execution_note(execution)
            status = STATUS_OK if validation.get("valid") and execution.get("status", STATUS_OK) != STATUS_FAILED else STATUS_FAILED
            return make_bot_response(text=text, status=status, action=action, data={"validation": validation, "execution": execution, "lifecycle": lifecycle})

        if action == "SCAN_MARKET":
            if not strategy_manager.is_level4_active():
                return make_bot_response(text=render_error("Level 4 فعال نیست؛ برای اسکن جدید اول بنویس: استراتژی لول 4"), status=STATUS_FAILED, action=action)
            if not market_provider:
                return make_bot_response(text=render_error("Market provider هنوز وصل نشده است."), status=STATUS_FAILED, action=action)
            symbols = default_scan_symbols or list(LEVEL_4_SYMBOLS)
            decisions = [force_real_to_ghost_when_trade_off(d) for d in scan_market_with_provider(symbols, market_provider)]
            if not decisions:
                return make_bot_response(text="سیگنال مناسبی پیدا نشد.", action=action, data={"count": 0})

            executions: list[dict[str, Any]] = []
            lifecycles: list[dict[str, Any]] = []
            rendered: list[str] = []
            for decision in decisions:
                execution = maybe_execute_real_decision(decision) if auto_execute_real else {"executed": False, "status": STATUS_OK}
                lifecycle = persist_signal_lifecycle(decision, execution=execution)
                executions.append(execution)
                lifecycles.append(lifecycle)
                rendered.append(render_ai_decision(decision, compact=True) + render_real_execution_note(execution))

            text = "📡 نتیجه اسکن Level 4\n\n" + "\n\n".join(rendered)
            failed_exec = any(x.get("status") == STATUS_FAILED for x in executions)
            return make_bot_response(text=text, status=STATUS_FAILED if failed_exec else STATUS_OK, action=action, data={"count": len(decisions), "executions": executions, "lifecycles": lifecycles})

        if action == "REQUEST_CLOSE_POSITION":
            symbol = normalize_symbol(args.get("symbol"))
            position = find_open_position_for_symbol(symbol)
            if not position:
                return make_bot_response(text=render_error("پوزیشن فعالی برای این نماد پیدا نشد."), status=STATUS_FAILED, action=action)
            if position.mode != MODE_REAL:
                return make_bot_response(text=render_error("این پوزیشن REAL نیست؛ بستن واقعی فقط برای REAL انجام می‌شود."), status=STATUS_FAILED, action=action)
            result = close_real_position(position, reason="USER_REQUEST")
            return make_bot_response(text=render_close_result(result), status=STATUS_OK if result.close_confirmed else STATUS_FAILED, action=action, data={"close_result": result.__dict__})

        if action == "WATCH_POSITION":
            return make_bot_response(text=render_ok("مانیتور پوزیشن فعال است و position_monitor پوزیشن‌های باز را بررسی می‌کند."), action=action)

        if action == "EMERGENCY_STOP":
            _disable_trade()
            return make_bot_response(text=render_ok("توقف اضطراری فعال شد و ترید واقعی خاموش شد."), action=action)

        return make_bot_response(text=render_unknown_command(), status=STATUS_FAILED, action=action)

    except Exception as exc:
        logger.exception("execute_route failed")
        return make_bot_response(text=render_error(f"خطای اجرای دستور: {exc}"), status=STATUS_FAILED, action=action, data={"error": str(exc)})


def _bot_level_command_fallback(text: Any, *, user_id: Optional[int] = None, chat_id: Optional[int] = None) -> Optional[CommandRoute]:
    """Small compatibility layer for locked Persian commands.

    command_router.py remains the primary parser, but bot.py must not return
    UNKNOWN for the key commands the user already locked.
    """
    normalized = safe_str(text).strip().replace("ي", "ی").replace("ك", "ک")
    while "  " in normalized:
        normalized = normalized.replace("  ", " ")
    low = normalized.lower()
    action = ""
    if low in {"ترید", "وضعیت ترید", "تنظیمات ترید", "trade", "trade status"}:
        action = "SHOW_TRADE_SETTINGS"
    elif low in {"هوش مصنوعی", "هوش مصنوعی و یادگیری", "ai", "ai status"}:
        action = "SHOW_AI_STATUS"
    elif low in {"حذف آمار", "پاک کردن آمار", "ریست آمار", "reset stats"}:
        action = "RESET_STATS"
    if not action:
        return None
    args: dict[str, Any] = {}
    if user_id is not None:
        args["user_id"] = user_id
    if chat_id is not None:
        args["chat_id"] = chat_id
    return CommandRoute(action=action, raw_text=safe_str(text), args=args)


def handle_text_message(
    text: str,
    *,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    market_provider: Optional[Any] = None,
    default_scan_symbols: Optional[list[str]] = None,
    auto_execute_real: bool = True,
) -> dict[str, Any]:
    command_route = parse_command(text, user_id=user_id, chat_id=chat_id)
    if command_route.action == "UNKNOWN":
        fallback = _bot_level_command_fallback(text, user_id=user_id, chat_id=chat_id)
        if fallback is not None:
            command_route = fallback
    return execute_route(
        command_route,
        market_provider=market_provider,
        default_scan_symbols=default_scan_symbols,
        auto_execute_real=auto_execute_real,
    )


def validate_bot_wiring() -> dict[str, Any]:
    errors: list[str] = []

    route_tests = [
        ("راهنما", "HELP"),
        ("آمار", "SHOW_STATS"),
        ("حذف آمار", "RESET_STATS"),
        ("هوش مصنوعی", "SHOW_AI_STATUS"),
        ("پوزیشن ها", "SHOW_POSITIONS"),
        ("وضعیت", "SHOW_STATUS"),
        ("ترید", "SHOW_TRADE_SETTINGS"),
        ("وضعیت ترید", "SHOW_TRADE_SETTINGS"),
        ("لیست استراتژی", "LIST_STRATEGIES"),
        ("استراتژی لول 4", "SET_STRATEGY_LEVEL"),
        ("ترید فعال", "ENABLE_REAL_TRADING"),
        ("ترید خاموش", "DISABLE_REAL_TRADING"),
        ("ترید دلار 7", "SET_MARGIN"),
        ("دلار ترید 8", "SET_MARGIN"),
        ("حجم ترید 9", "SET_MARGIN"),
        ("حداقل 5", "SET_MIN_MARGIN"),
        ("حداکثر 15", "SET_MAX_MARGIN"),
        ("ترید دلار حداقل 5", "SET_MIN_MARGIN"),
        ("ترید دلار حداکثر 15", "SET_MAX_MARGIN"),
        ("لوریج 10", "SET_LEVERAGE"),
        ("حداکثر پوزیشن 3", "SET_MAX_POSITIONS"),
        ("ریست ترید", "RESET_TRADE_SETTINGS"),
        ("تحلیل DOGEUSDT", "ANALYZE_SYMBOL"),
        ("اسکن", "SCAN_MARKET"),
        ("بستن DOGEUSDT", "REQUEST_CLOSE_POSITION"),
    ]

    mutating_actions = {
        "SET_STRATEGY_LEVEL",
        "ENABLE_REAL_TRADING",
        "DISABLE_REAL_TRADING",
        "SET_MARGIN",
        "SET_MIN_MARGIN",
        "SET_MAX_MARGIN",
        "SET_LEVERAGE",
        "SET_MAX_POSITIONS",
        "RESET_TRADE_SETTINGS",
        "RESET_STATS",
        "REQUEST_CLOSE_POSITION",
        "EMERGENCY_STOP",
    }

    for text, expected_action in route_tests:
        try:
            command_route = parse_command(text)
            if command_route.action == "UNKNOWN":
                command_route = _bot_level_command_fallback(text) or command_route
            if command_route.action != expected_action:
                errors.append(f"ROUTE_MISMATCH:{text}:{command_route.action}!={expected_action}")
                continue
            if expected_action in mutating_actions:
                # Do not mutate real strategy/learning/position state during health checks.
                continue
            response = handle_text_message(text, auto_execute_real=False)
            if validate_bot_response(response).get("valid") is not True:
                errors.append(f"{expected_action}_RESPONSE_INVALID")
        except Exception as exc:
            errors.append(f"{expected_action}_RESPONSE_EXCEPTION:{exc}")

    try:
        rtm = validate_real_trade_manager_light()
        if not rtm.get("valid"):
            errors.append(f"REAL_TRADE_MANAGER_INVALID:{rtm.get('errors')}")
    except Exception as exc:
        errors.append(f"REAL_TRADE_MANAGER_EXCEPTION:{exc}")

    return {
        "system_version": SYSTEM_VERSION,
        "bot_version": BOT_VERSION,
        "status": STATUS_OK if not errors else STATUS_FAILED,
        "valid": not errors,
        "errors": errors,
        "checked_at": utc_now_iso(),
    }


# =============================================================================
# Telegram runtime
# =============================================================================

def load_env_file(path: str = ".env") -> None:
    """Tiny .env loader, avoids python-dotenv stdin/assertion edge cases."""
    env_path = Path(path)
    if not env_path.exists():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        logger.exception("failed to load .env")


def get_bot_token() -> str:
    load_env_file(".env")
    return safe_str(os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN"))


def is_user_allowed(user_id: Optional[int]) -> bool:
    try:
        from users import is_allowed, get_owner_id
        if user_id is None:
            return False
        owner_id = get_owner_id(0)
        if owner_id and int(user_id) == int(owner_id):
            return True
        return bool(is_allowed(user_id))
    except Exception:
        # If access module is unavailable, do not break the running bot.
        return True


async def send_long_text(message: Any, text: str) -> None:
    msg = safe_str(text) or "-"
    max_len = 3900
    for i in range(0, len(msg), max_len):
        await message.reply_text(msg[i:i + max_len])


async def telegram_start(update: Any, context: Any) -> None:
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    if not is_user_allowed(user_id):
        await update.effective_message.reply_text("⛔ دسترسی مجاز نیست.")
        return
    await send_long_text(update.effective_message, render_help())


def _fast_trade_toggle_action(text: Any) -> str:
    """Return ON/OFF for urgent trade toggle commands.

    These commands must not wait behind Toobit status calls, market scans,
    or other slow synchronous work. They only update local strategy state.
    """
    normalized = safe_str(text).strip().replace("ي", "ی").replace("ك", "ک")
    while "  " in normalized:
        normalized = normalized.replace("  ", " ")
    low = normalized.lower()
    if not low:
        return ""

    has_trade_word = "ترید" in low or "trade" in low
    off_words = {"خاموش", "غیرفعال", "غيرفعال", "off", "disable", "disabled"}
    on_words = {"فعال", "روشن", "on", "enable", "enabled"}

    if has_trade_word and any(word in low for word in off_words):
        return "OFF"
    if has_trade_word and any(word in low for word in on_words) and not any(word in low for word in off_words):
        return "ON"
    if any(word in low for word in {"توقف فوری", "استاپ فوری", "emergency stop"}):
        return "OFF"
    return ""


async def telegram_message_handler(update: Any, context: Any) -> None:
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    text = safe_str(getattr(message, "text", ""))
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)

    if not is_user_allowed(user_id):
        await message.reply_text("⛔ دسترسی مجاز نیست.")
        return

    # Critical safety commands must answer immediately and must not wait for
    # Toobit, market-data calls, auto-scan, or heavy status rendering.
    fast_toggle = _fast_trade_toggle_action(text)
    if fast_toggle == "OFF":
        ok = _disable_trade()
        await message.reply_text(
            render_ok("ترید واقعی خاموش شد. سیگنال‌های جدید GHOST می‌شوند.")
            if ok else render_error("خاموش کردن ترید واقعی انجام نشد.")
        )
        return
    if fast_toggle == "ON":
        ok = _enable_trade()
        await message.reply_text(
            render_ok("ترید واقعی روشن شد.")
            if ok else render_error("روشن کردن ترید واقعی انجام نشد.")
        )
        return

    provider = context.application.bot_data.setdefault("market_provider", OKXMarketProvider())
    try:
        # handle_text_message may call Toobit or market-data synchronously. Run it
        # off the Telegram event loop so /start and trade ON/OFF stay responsive.
        response = await asyncio.to_thread(
            handle_text_message,
            text,
            user_id=user_id,
            chat_id=chat_id,
            market_provider=provider,
            default_scan_symbols=list(LEVEL_4_SYMBOLS),
            auto_execute_real=True,
        )
        await send_long_text(message, response.get("text", "-"))
    except Exception as exc:
        logger.exception("telegram_message_handler failed")
        await message.reply_text(render_error(f"خطای اجرای دستور: {exc}"))


async def telegram_error_handler(update: object, context: Any) -> None:
    logger.exception("Telegram error", exc_info=getattr(context, "error", None))



# =============================================================================
# Position monitor background loop
# =============================================================================

def _monitor_interval_seconds() -> int:
    load_env_file(".env")
    raw = os.getenv("POSITION_MONITOR_INTERVAL_SECONDS") or os.getenv("MONITOR_INTERVAL_SECONDS")
    value = safe_int(raw, 10) or 10
    return max(5, value)


def _current_price_from_provider(provider: Any, symbol: str) -> float:
    """Return the freshest usable price for position monitoring.

    The analysis engine may use PRIMARY_TIMEFRAME for Level 4 decisions, but
    the monitor must not wait for a 1H candle close. GHOST/REAL TP-SL can be
    touched intrabar, so we first try fast candles and use the latest close as
    the current executable/mark-like price. If fast data is unavailable, we
    fall back to PRIMARY_TIMEFRAME to keep the monitor alive.
    """
    normalized = normalize_symbol(symbol)
    timeframes = ["1m", "3m", "5m", "15m", PRIMARY_TIMEFRAME]
    seen: set[str] = set()

    for timeframe in timeframes:
        tf = safe_str(timeframe).strip()
        if not tf or tf in seen:
            continue
        seen.add(tf)
        try:
            candles = provider_get_candles(provider, normalized, timeframe=tf, limit=3)
        except Exception:
            logger.exception("monitor_price_fetch_failed symbol=%s timeframe=%s", normalized, tf)
            candles = []

        if not candles:
            continue

        last = candles[-1]
        for value in (
            getattr(last, "close", None),
            getattr(last, "price", None),
            getattr(last, "last", None),
        ):
            price = safe_float(value, None)
            if price is not None and price > 0:
                return price

    return 0.0


def _ai_monitor_decision_provider(position: TradePosition, current_price: float) -> Any:
    """Build AI exit/hold decision for open positions.

    Locked behavior:
    - Before TP1, AI exit is allowed only when progress to TP1 is >= 70%
      and weakness/reversal/trap risk is confirmed by ai_brain.
    - After TP1, only the runner may be closed on meaningful weakness.
    - This function only returns a decision; position_monitor still enforces
      the 70% rule and real_trade_manager/Toobit must confirm the close.
    """
    price = safe_float(current_price, None)
    if price is None or price <= 0:
        return None

    try:
        provider = OKXMarketProvider()
        candles = provider_get_candles(provider, position.symbol, timeframe=PRIMARY_TIMEFRAME, limit=OKX_CANDLE_LIMIT_DEFAULT)
        if not candles:
            # Fall back to faster candles so AI exit is not disabled when 1H data is temporarily unavailable.
            for tf in ("15m", "5m", "3m", "1m"):
                candles = provider_get_candles(provider, position.symbol, timeframe=tf, limit=OKX_CANDLE_LIMIT_DEFAULT)
                if candles:
                    break
        if not candles:
            return None

        snapshot = make_offline_snapshot(normalize_symbol(position.symbol), PRIMARY_TIMEFRAME, candles)
        sensor = build_sensor_snapshot(snapshot)
        direction = normalize_direction(position.direction)

        structure = build_structure_snapshot(snapshot, direction, sensor)
        momentum = build_momentum_snapshot(sensor, direction)
        liquidity = build_liquidity_snapshot(snapshot, direction, structure, sensor)
        context = build_market_context_from_snapshots({normalize_symbol(position.symbol): snapshot}, direction)
        reversal = build_reversal_snapshot(
            sensor=sensor,
            structure=structure,
            momentum=momentum,
            liquidity=liquidity,
            context=context,
            direction=direction,
        )

        after_tp1 = bool(getattr(position, "tp1_hit", False)) or safe_str(getattr(position, "status", "")).upper() == "PARTIAL_TP1"
        progress = progress_to_tp1(position, price)

        return evaluate_open_position(
            position_direction=direction,
            sensor=sensor,
            momentum=momentum,
            liquidity=liquidity,
            reversal_snapshot=getattr(reversal, "__dict__", reversal),
            progress_to_tp1=progress,
            after_tp1=after_tp1,
        )

    except Exception:
        logger.exception("ai_monitor_decision_failed symbol=%s position_id=%s", position.symbol, position.position_id)
        return None


def render_monitor_event(event: MonitorEvent) -> str:
    outcome = event.outcome
    close_result = event.close_result
    pnl = None
    if outcome is not None:
        pnl = outcome.pnl_usdt
    if pnl is None and close_result is not None:
        pnl = close_result.pnl_usdt
    pnl_text = "-" if pnl is None else f"{pnl:+.4f}$"
    exit_price = safe_float(getattr(outcome, "exit_price", None), None) if outcome is not None else None
    if exit_price is None and close_result is not None:
        exit_price = safe_float(close_result.close_price, None)
    exit_text = "-" if exit_price is None else str(exit_price)
    icon = "✅" if event.event in {"TP1", "TP2", "AI_EXIT", "MANUAL_CLOSE"} else "❌" if event.event == "SL" else "ℹ️"
    mode = safe_str(event.mode).upper()
    return "\n".join([
        f"{icon} نتیجه پوزیشن {mode}",
        f"Event: {event.event}",
        f"Symbol: {normalize_symbol(event.symbol)}",
        f"Direction: {normalize_direction(event.direction)}",
        f"Exit: {exit_text}",
        f"PnL: {pnl_text}",
        f"Position ID: {safe_str(event.position_id)}",
    ])


async def _send_monitor_event(application: Any, chat_id: int, event: MonitorEvent) -> None:
    text = render_monitor_event(event)
    kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if event.reply_to_message_id:
        kwargs["reply_to_message_id"] = event.reply_to_message_id
    await application.bot.send_message(**kwargs)


async def position_monitor_loop(application: Any) -> None:
    interval = _monitor_interval_seconds()
    logger.info("position_monitor_loop_started interval=%ss", interval)
    await asyncio.sleep(5)
    while True:
        try:
            chat_id = _owner_chat_id()
            provider = application.bot_data.setdefault("market_provider", OKXMarketProvider())
            events = monitor_positions_once(
                price_provider=lambda symbol: _current_price_from_provider(provider, symbol),
                ai_decision_provider=_ai_monitor_decision_provider,
                close_executor=close_position_executor,
                open_confirm_checker=confirm_real_open,
            )
            if events:
                logger.info("position_monitor_events count=%s events=%s", len(events), [e.event for e in events])
            if chat_id:
                for event in events:
                    if event.event in {"PRICE_UNAVAILABLE", "AI_EXIT_SKIPPED_EARLY"}:
                        continue
                    await _send_monitor_event(application, chat_id, event)
            elif events:
                logger.warning("position_monitor_events_not_sent owner_chat_id_missing count=%s", len(events))
        except asyncio.CancelledError:
            logger.info("position_monitor_loop_cancelled")
            raise
        except Exception as exc:
            logger.exception("position_monitor_loop_error:%s", exc)
        await asyncio.sleep(interval)


# =============================================================================
# Auto signal background loop
# =============================================================================

def _env_bool(name: str, default: bool = False) -> bool:
    load_env_file(".env")
    value = safe_str(os.getenv(name))
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "enable", "enabled", "فعال", "روشن"}


def _auto_signal_enabled() -> bool:
    """Return whether background auto-scan/signal loop is allowed to run."""
    if not _env_bool("AUTO_SIGNAL_ENABLED", default=False):
        return False
    try:
        runtime = _get_trade_runtime()
        if isinstance(runtime, Mapping):
            if "auto_signal_enabled" in runtime:
                return bool(runtime.get("auto_signal_enabled"))
            if "auto_signal" in runtime:
                return bool(runtime.get("auto_signal"))
    except Exception:
        logger.exception("auto signal runtime check failed")
    return True


def _auto_scan_interval_seconds() -> int:
    load_env_file(".env")
    raw = (
        os.getenv("AUTO_SCAN_INTERVAL_SECONDS")
        or os.getenv("AUTO_SIGNAL_INTERVAL_SECONDS")
        or os.getenv("SCAN_INTERVAL_SECONDS")
    )
    value = safe_int(raw, 300) or 300
    return max(60, value)


def _owner_chat_id() -> Optional[int]:
    load_env_file(".env")
    direct = safe_int(os.getenv("OWNER_ID") or os.getenv("TELEGRAM_OWNER_ID"), None)
    if direct:
        return direct
    try:
        from users import get_owner_id
        owner = safe_int(get_owner_id(0), None)
        if owner:
            return owner
    except Exception:
        logger.exception("owner id lookup failed")
    return None


async def _send_long_text_to_chat(application: Any, chat_id: int, text: str) -> list[Any]:
    msg = safe_str(text) or "-"
    max_len = 3900
    sent: list[Any] = []
    for i in range(0, len(msg), max_len):
        sent.append(await application.bot.send_message(chat_id=chat_id, text=msg[i:i + max_len]))
    return sent


async def auto_signal_loop(application: Any) -> None:
    """
    Background Level 4 auto scanner.

    Manual "بررسی/اسکن" uses SCAN_MARKET through the Telegram handler.
    This loop is the non-manual auto-signal path. It logs every pass so
    journalctl grep can prove whether it is actually running.
    """
    interval = _auto_scan_interval_seconds()
    logger.info("auto_signal_loop_started enabled=%s interval=%ss", _auto_signal_enabled(), interval)

    # Let Telegram polling finish startup before first scan.
    await asyncio.sleep(15)

    while True:
        try:
            enabled = _auto_signal_enabled()
            level4_active = bool(strategy_manager.is_level4_active())
            logger.info("auto_signal_tick enabled=%s level4_active=%s", enabled, level4_active)

            if enabled and level4_active:
                chat_id = _owner_chat_id()
                if not chat_id:
                    logger.warning("auto_signal_skipped owner_chat_id_missing")
                else:
                    provider = application.bot_data.setdefault("market_provider", OKXMarketProvider())
                    decisions = await asyncio.to_thread(scan_market_with_provider, list(LEVEL_4_SYMBOLS), provider)
                    mode_counts: dict[str, int] = {}
                    for decision in decisions:
                        mode_counts[decision.mode] = mode_counts.get(decision.mode, 0) + 1
                    logger.info(
                        "auto_signal_scan_result count=%s modes=%s symbols=%s",
                        len(decisions),
                        mode_counts,
                        [d.symbol for d in decisions],
                    )

                    # Avoid spamming pure reject-only scans, but log them.
                    sendable = [force_real_to_ghost_when_trade_off(d) for d in decisions if d.mode in {MODE_REAL, MODE_GHOST}]
                    if sendable:
                        executions: list[dict[str, Any]] = []
                        lifecycles: list[dict[str, Any]] = []
                        sent_count = 0
                        silent_count = 0
                        for decision in sendable:
                            execution = await asyncio.to_thread(maybe_execute_real_decision, decision)
                            lifecycle = await asyncio.to_thread(persist_signal_lifecycle, decision, execution=execution)
                            executions.append(execution)
                            lifecycles.append(lifecycle)

                            # Auto-signal rule:
                            # If AI wanted REAL but it was converted to GHOST because real trading
                            # is OFF, REAL slots are full, duplicate/open-order guard blocked it,
                            # or Toobit/preflight blocked it, keep the GHOST record for learning
                            # but do NOT send a Telegram auto-signal. This prevents noisy GHOST
                            # messages while preserving stats/learning.
                            converted_to_ghost = bool(execution.get("converted_to_ghost"))
                            original_mode = safe_str((decision.metadata or {}).get("original_mode")).upper()
                            block_reason = safe_str(
                                execution.get("real_block_reason")
                                or (decision.metadata or {}).get("real_block_reason")
                                or execution.get("reason")
                            ).lower()

                            silent_converted_ghost = (
                                safe_str(decision.mode).upper() == MODE_GHOST
                                and converted_to_ghost
                                and (
                                    original_mode == MODE_REAL
                                    or "trade" in block_reason
                                    or "slot" in block_reason
                                    or "max" in block_reason
                                    or "preflight" in block_reason
                                    or "duplicate" in block_reason
                                    or "open" in block_reason
                                    or "toobit" in block_reason
                                )
                            )

                            if silent_converted_ghost:
                                silent_count += 1
                                logger.info(
                                    "auto_signal_silent_converted_ghost symbol=%s direction=%s reason=%s lifecycle=%s",
                                    decision.symbol,
                                    decision.direction,
                                    block_reason,
                                    lifecycle,
                                )
                                continue

                            text = "🤖 اتو سیگنال Level 4\n\n" + render_ai_decision(decision, compact=True) + render_real_execution_note(execution)
                            sent_messages = await _send_long_text_to_chat(application, chat_id, text)
                            sent_count += 1
                            if sent_messages and lifecycle.get("position_id"):
                                msg_id = getattr(sent_messages[0], "message_id", None)
                                if msg_id:
                                    update_position(lifecycle["position_id"], {"signal_message_id": msg_id})
                                    logger.info("signal_message_id_saved position_id=%s message_id=%s", lifecycle["position_id"], msg_id)
                        logger.info("auto_signal_sent count=%s silent_converted_ghost=%s executions=%s lifecycles=%s", sent_count, silent_count, executions, lifecycles)
                    else:
                        logger.info("auto_signal_no_sendable_signal count=%s", len(decisions))
            else:
                logger.info("auto_signal_idle enabled=%s level4_active=%s", enabled, level4_active)

        except asyncio.CancelledError:
            logger.info("auto_signal_loop_cancelled")
            raise
        except Exception as exc:
            logger.exception("auto_signal_loop_error:%s", exc)

        await asyncio.sleep(interval)


def _start_background_tasks_now(application: Any) -> None:
    """Start background tasks after the Telegram app has entered its running loop.

    This is intentionally compatible with python-telegram-bot 20.7: that
    version has post_init/post_stop but not ApplicationBuilder.post_startup.
    We also avoid Application.create_task() here to prevent the warning:
    "Tasks created via Application.create_task while the application is not running".
    """
    application.bot_data["market_provider"] = application.bot_data.get("market_provider") or OKXMarketProvider()
    tasks: dict[str, asyncio.Task] = application.bot_data.setdefault("background_tasks", {})

    monitor_task = tasks.get("position_monitor")
    if monitor_task is None or monitor_task.done():
        tasks["position_monitor"] = asyncio.create_task(
            position_monitor_loop(application),
            name="level4_position_monitor_loop",
        )
        logger.info("position_monitor_task_created")
    else:
        logger.info("position_monitor_task_already_running")

    auto_task = tasks.get("auto_signal")
    if _auto_signal_enabled():
        if auto_task is None or auto_task.done():
            tasks["auto_signal"] = asyncio.create_task(
                auto_signal_loop(application),
                name="level4_auto_signal_loop",
            )
            logger.info("auto_signal_task_created")
        else:
            logger.info("auto_signal_task_already_running")
    else:
        logger.info("auto_signal_task_not_created disabled")


async def _delayed_background_start(application: Any, delay_seconds: float = 1.0) -> None:
    """Delay background task creation until PTB polling is running."""
    try:
        await asyncio.sleep(max(0.0, delay_seconds))
        _start_background_tasks_now(application)
    except asyncio.CancelledError:
        logger.info("background_start_cancelled")
        raise
    except Exception as exc:
        logger.exception("background_start_failed:%s", exc)


async def telegram_post_init(application: Any) -> None:
    """Prepare runtime state after Telegram Application initialization.

    PTB 20.7 does not provide ApplicationBuilder.post_startup().  Starting
    loops immediately with Application.create_task() from post_init produced
    pending-task warnings and slow manual replies.  Instead, create one raw
    asyncio delayed starter task; it starts monitor/auto-signal after polling
    has had time to enter the running state.
    """
    application.bot_data["market_provider"] = application.bot_data.get("market_provider") or OKXMarketProvider()
    application.bot_data.setdefault("background_tasks", {})

    starter = application.bot_data.get("background_start_task")
    if starter is None or starter.done():
        application.bot_data["background_start_task"] = asyncio.create_task(
            _delayed_background_start(application, delay_seconds=1.0),
            name="level4_background_start",
        )
        logger.info("background_start_task_created")
    else:
        logger.info("background_start_task_already_running")


async def telegram_post_shutdown(application: Any) -> None:
    """Cancel background tasks cleanly on restart/shutdown."""
    starter = application.bot_data.get("background_start_task")
    if starter and not starter.done():
        logger.info("background_start_cancel_requested")
        starter.cancel()
        try:
            await starter
        except asyncio.CancelledError:
            logger.info("background_start_cancelled")
        except Exception as exc:
            logger.exception("background_start_cancel_error:%s", exc)

    tasks = dict(application.bot_data.get("background_tasks") or {})
    if not tasks:
        logger.info("background_tasks_none_to_cancel")
        return

    for name, task in tasks.items():
        if task and not task.done():
            logger.info("background_task_cancel_requested name=%s", name)
            task.cancel()

    for name, task in tasks.items():
        if not task:
            continue
        try:
            await task
        except asyncio.CancelledError:
            logger.info("background_task_cancelled name=%s", name)
        except Exception as exc:
            logger.exception("background_task_cancel_error name=%s error=%s", name, exc)

    application.bot_data["background_tasks"] = {}


def build_application(token: str) -> Any:
    # Lazy import keeps integration_check/import usable even where telegram package is absent.
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

    builder = ApplicationBuilder().token(token).post_init(telegram_post_init)
    # python-telegram-bot 20.7 supports post_stop, not post_startup.
    # Use feature detection so this file stays compatible with close PTB versions.
    if hasattr(builder, "post_stop"):
        builder = builder.post_stop(telegram_post_shutdown)
    elif hasattr(builder, "post_shutdown"):
        builder = builder.post_shutdown(telegram_post_shutdown)

    application = builder.build()
    application.add_handler(CommandHandler(["start", "help"], telegram_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_message_handler))
    application.add_error_handler(telegram_error_handler)
    application.bot_data["market_provider"] = OKXMarketProvider()
    return application


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    )
    token = get_bot_token()
    if not token:
        logger.error("BOT_TOKEN is missing. Set BOT_TOKEN in .env or systemd environment.")
        raise SystemExit(1)
    wiring = validate_bot_wiring()
    if not wiring.get("valid"):
        logger.warning("Bot wiring warnings: %s", wiring.get("errors"))
    logger.info("Level 4 bot started. Version=%s", BOT_VERSION)
    app = build_application(token)
    app.run_polling(allowed_updates=None, drop_pending_updates=True)


__all__ = [
    "BOT_VERSION",
    "OKXMarketProvider",
    "make_bot_response",
    "validate_bot_response",
    "provider_get_candles",
    "build_snapshots_from_provider",
    "infer_direction_from_sensor",
    "analyze_market_snapshot",
    "analyze_symbol_with_provider",
    "scan_market_with_provider",
    "maybe_execute_real_decision",
    "_convert_real_failure_to_ghost",
    "render_real_execution_note",
    "find_open_position_for_symbol",
    "render_close_result",
    "persist_signal_lifecycle",
    "execute_route",
    "handle_text_message",
    "_bot_level_command_fallback",
    "validate_bot_wiring",
    "force_real_to_ghost_when_trade_off",
    "position_monitor_loop",
    "render_monitor_event",
    "load_env_file",
    "get_bot_token",
    "is_user_allowed",
    "_fast_trade_toggle_action",
    "auto_signal_loop",
    "telegram_post_init",
    "telegram_post_shutdown",
    "build_application",
    "main",
]


if __name__ == "__main__":
    main()
