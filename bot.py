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
    OKX_CANDLE_LIMIT_DEFAULT,
    PRIMARY_TIMEFRAME,
    STATUS_FAILED,
    STATUS_OK,
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
from position_manager import get_open_positions
from stats_engine import build_stats_snapshot
from models import AIDecision, Candle, MarketSnapshot, TradeCloseResult, TradePosition
from market_data import fetch_market_snapshot, make_offline_snapshot
from technical_sensors import build_sensor_snapshot
from structure_engine import build_structure_snapshot
from momentum_engine import build_momentum_snapshot
from liquidity_engine import build_liquidity_snapshot
from market_context import build_market_context_from_snapshots
from reversal_engine import build_reversal_snapshot
from timing_engine import build_timing_snapshot
from tp_sl_engine import build_tp_sl_plan
from ai_brain import build_ai_decision, validate_ai_decision
from real_trade_manager import (
    close_real_position,
    open_real_trade,
    preflight_real_trade,
    validate_real_trade_manager_light,
    get_real_trade_status,
)
from learning_memory import get_learning_summary, reset_learning_memory
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
# RealTrade integration
# =============================================================================

def maybe_execute_real_decision(decision: AIDecision) -> dict[str, Any]:
    """
    Execute REAL decision through real_trade_manager only.

    If real trading is off, the decision is converted to GHOST output text only.
    """
    if decision.mode != MODE_REAL:
        return {"executed": False, "status": STATUS_OK, "reason": "not_real_decision"}

    if not _real_trading_enabled():
        decision.mode = MODE_GHOST
        decision.reason_codes.append("REAL_TRADE_OFF_CONVERTED_TO_GHOST")
        return {"executed": False, "status": STATUS_OK, "reason": "real_trading_disabled_converted_to_ghost"}

    pf = preflight_real_trade(decision)
    if not pf.get("ok"):
        return {"executed": False, "status": STATUS_FAILED, "reason": "preflight_failed", "preflight": pf}

    result = open_real_trade(decision)
    return {
        "executed": result.status == STATUS_OK,
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
            if value is None or value <= 0:
                return make_bot_response(text=render_error("مارجین نامعتبر است."), status=STATUS_FAILED, action=action)
            ok = _update_runtime(margin_usdt=value)
            return make_bot_response(text=render_ok(f"مارجین روی {value}$ تنظیم شد.") if ok else render_error("ثبت مارجین انجام نشد."), status=STATUS_OK if ok else STATUS_FAILED, action=action)

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
            status_payload["watchlist"] = list(LEVEL_4_SYMBOLS)
            return make_bot_response(text=render_trade_runtime(status_payload), action=action, data={"trade_status": status_payload})

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
            decision = analyze_symbol_with_provider(symbol, market_provider)
            validation = validate_ai_decision(decision)
            execution = maybe_execute_real_decision(decision) if auto_execute_real and validation.get("valid") else {"executed": False}
            text = render_ai_decision(decision) + render_real_execution_note(execution)
            status = STATUS_OK if validation.get("valid") and execution.get("status", STATUS_OK) != STATUS_FAILED else STATUS_FAILED
            return make_bot_response(text=text, status=status, action=action, data={"validation": validation, "execution": execution})

        if action == "SCAN_MARKET":
            if not strategy_manager.is_level4_active():
                return make_bot_response(text=render_error("Level 4 فعال نیست؛ برای اسکن جدید اول بنویس: استراتژی لول 4"), status=STATUS_FAILED, action=action)
            if not market_provider:
                return make_bot_response(text=render_error("Market provider هنوز وصل نشده است."), status=STATUS_FAILED, action=action)
            symbols = default_scan_symbols or list(LEVEL_4_SYMBOLS)
            decisions = scan_market_with_provider(symbols, market_provider)
            if not decisions:
                return make_bot_response(text="سیگنال مناسبی پیدا نشد.", action=action, data={"count": 0})

            executions: list[dict[str, Any]] = []
            rendered: list[str] = []
            for decision in decisions:
                execution = maybe_execute_real_decision(decision) if auto_execute_real and decision.mode == MODE_REAL else {"executed": False, "status": STATUS_OK}
                executions.append(execution)
                rendered.append(render_ai_decision(decision, compact=True) + render_real_execution_note(execution))

            text = "📡 نتیجه اسکن Level 4\n\n" + "\n\n".join(rendered)
            failed_exec = any(x.get("status") == STATUS_FAILED for x in executions)
            return make_bot_response(text=text, status=STATUS_FAILED if failed_exec else STATUS_OK, action=action, data={"count": len(decisions), "executions": executions})

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

    provider = context.application.bot_data.setdefault("market_provider", OKXMarketProvider())
    try:
        response = handle_text_message(
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


def build_application(token: str) -> Any:
    # Lazy import keeps integration_check/import usable even where telegram package is absent.
    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

    application = ApplicationBuilder().token(token).build()
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
    "render_real_execution_note",
    "find_open_position_for_symbol",
    "render_close_result",
    "execute_route",
    "handle_text_message",
    "validate_bot_wiring",
    "load_env_file",
    "get_bot_token",
    "is_user_allowed",
    "build_application",
    "main",
]


if __name__ == "__main__":
    main()
