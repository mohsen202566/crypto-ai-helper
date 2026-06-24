"""
bot.py
Level 4 / 1H Smart Scalp Bot

Main orchestration layer with RealTrade/Toobit integration.

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

from typing import Any, Mapping, Optional

from constants import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    MODE_GHOST,
    MODE_REAL,
    MODE_REJECT,
    STATUS_FAILED,
    STATUS_OK,
    STRATEGY_LEVEL,
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
    render_unknown_command,
    validate_rendered_text,
)
import strategy_manager
from position_manager import get_open_positions
from stats_engine import build_stats_snapshot
from models import AIDecision, Candle, MarketSnapshot, TradeCloseResult, TradePosition
from market_data import make_offline_snapshot
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
)
from utils import normalize_direction, normalize_symbol, safe_float, safe_int, safe_str, utc_now_iso


BOT_VERSION: str = SYSTEM_VERSION


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
    # In locked Level 4 manager, set_level4_active may be the only valid setter.
    if level == STRATEGY_LEVEL:
        result = _call_first(["set_strategy_level", "set_active_level", "switch_strategy_level", "set_level", "set_level4_active"], level)
        if result is None:
            result = _call_first(["set_level4_active"])
        return _result_ok(result)
    result = _call_first(["set_strategy_level", "set_active_level", "switch_strategy_level", "set_level"], level)
    return _result_ok(result)


def _update_runtime(**kwargs: Any) -> bool:
    result = _call_first(["update_trade_runtime_config", "update_runtime_config", "update_trade_settings", "set_trade_settings"], **kwargs)
    if result is None:
        result = _call_first(["update_trade_runtime_config", "update_runtime_config", "update_trade_settings", "set_trade_settings"], kwargs)
    return _result_ok(result)


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
            pass
    state = _call_first(["load_strategy_state", "get_strategy_state"])
    if isinstance(state, Mapping):
        return bool(state.get("real_trading_enabled", state.get("trade_enabled", False)))
    return False


# =============================================================================
# Market provider adapter
# =============================================================================

def provider_get_candles(provider: Any, symbol: str, *, timeframe: str = "1H", limit: int = 120) -> list[Candle]:
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


def build_snapshots_from_provider(provider: Any, symbols: list[str], *, timeframe: str = "1H", limit: int = 120) -> dict[str, MarketSnapshot]:
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


def analyze_symbol_with_provider(symbol: str, provider: Any, *, timeframe: str = "1H", limit: int = 120, context_symbols: Optional[list[str]] = None) -> AIDecision:
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


def scan_market_with_provider(symbols: list[str], provider: Any, *, timeframe: str = "1H", limit: int = 120, max_results: int = 5) -> list[AIDecision]:
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
            return make_bot_response(text=render_ok(f"استراتژی روی Level {level} تنظیم شد.") if ok else render_error("تغییر استراتژی انجام نشد."), status=STATUS_OK if ok else STATUS_FAILED, action=action, data={"level": level})

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
            return make_bot_response(text=render_trade_runtime(), action=action)

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
            if not market_provider:
                return make_bot_response(text=render_error("Market provider هنوز وصل نشده است."), status=STATUS_FAILED, action=action)
            symbols = default_scan_symbols or ["DOGEUSDT", "XRPUSDT", "SOLUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT"]
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

    for text, key in [("راهنما", "HELP"), ("آمار", "STATS"), ("پوزیشن ها", "POSITIONS"), ("وضعیت", "STATUS")]:
        try:
            response = handle_text_message(text, auto_execute_real=False)
            if validate_bot_response(response).get("valid") is not True:
                errors.append(f"{key}_RESPONSE_INVALID")
        except Exception as exc:
            errors.append(f"{key}_RESPONSE_EXCEPTION:{exc}")

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


__all__ = [
    "BOT_VERSION",
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
]
