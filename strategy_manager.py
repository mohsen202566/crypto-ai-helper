"""Strategy manager for Crypto AI Helper bot.

Level 4 / 15m/30m Fast Smart Scalp decision coordinator.

Locked responsibility:
- Owns only the strategy decision flow for the active Level 4 15m/30m profile.
- Connects market_context, coin_analyzer, probability_engine, ai_decision, and tp_sl_engine.
- Returns one simple decision object for bot.py / real_trade_manager.py.
- No OKX HTTP calls, no Toobit calls, no Telegram sending, no order execution, no slot accounting.

Design lock:
- Only Level 4 is active for new signals.
- One TP and one SL only.
- Quality 30m signal + 15m entry confirmation over late chase entry.
- Prefer NO_TRADE over weak, conflicted, late, or fee-invalid setups.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any, Literal, Mapping, Sequence

from ai_decision import AIDecision, make_ai_decision
from coin_analyzer import CoinAnalysis, analyze_coin
from config import (
    DEFAULT_LEVERAGE,
    DEFAULT_MIN_NET_PROFIT_USDT,
    DEFAULT_REAL_TRADE_ENABLED,
    DEFAULT_TRADE_DOLLAR,
    MIN_CONFIDENCE,
    TARGET_HOLD_MINUTES,
    TIMEFRAME,
    WATCHLIST,
    get_coin,
)
from market_context import Candle, MarketContext, build_market_context
from probability_engine import ProbabilityResult, calculate_probabilities
from tp_sl_engine import TPSLPlan, build_fast_tp_sl_plan

Direction = Literal["LONG", "SHORT", "NONE"]
Action = Literal["ENTER_LONG", "ENTER_SHORT", "NO_TRADE"]
ExecutionMode = Literal["REAL", "SIGNAL", "NO_TRADE"]

ACTIVE_STRATEGY_LEVEL = 4
STRATEGY_NAME = "Level 4 / 15m-30m Fast Smart Scalp"
STRATEGY_TIMEFRAME = "30m"
ENTRY_TIMEFRAME = "15m"
TREND_FILTER_TIMEFRAME = "1h"
MIN_MAIN_CANDLES_30M = 40
MIN_ENTRY_CANDLES_15M = 40
MIN_REAL_CONFIDENCE = 75.0
MIN_REAL_AGREEMENT = 70.0
MIN_REAL_DIRECTION_EDGE = 8.0

_real_trading_enabled_override: bool | None = None
_active_strategy_level: int = ACTIVE_STRATEGY_LEVEL


@dataclass(frozen=True)
class StrategyDecision:
    symbol: str
    action: Action
    decision: Action
    direction: Direction
    mode: ExecutionMode
    real: bool
    entry: float
    tp: float
    sl: float
    confidence: float
    reason: str
    signal_id: str
    strategy_level: int = ACTIVE_STRATEGY_LEVEL
    strategy_name: str = STRATEGY_NAME
    timeframe: str = STRATEGY_TIMEFRAME
    tp_sl: TPSLPlan | None = None
    probability: ProbabilityResult | None = None
    ai: AIDecision | None = None
    context: MarketContext | None = None
    analysis: CoinAnalysis | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_real(self) -> bool:
        return self.real


@dataclass(frozen=True)
class StrategyRuntimeConfig:
    active_level: int = ACTIVE_STRATEGY_LEVEL
    name: str = STRATEGY_NAME
    timeframe: str = STRATEGY_TIMEFRAME
    target_hold_minutes: tuple[int, int] = TARGET_HOLD_MINUTES
    real_trading_enabled: bool = DEFAULT_REAL_TRADE_ENABLED


def decide(symbol: str, market: Mapping[str, Any] | None = None, **kwargs: Any) -> StrategyDecision:
    """Build one final Level 4 decision for a symbol.

    Expected market keys when available:
    - price: latest entry/reference price.
    - candles / candles_30m: 30m candles for the symbol, as Candle objects, dicts, or OKX arrays.
    - entry_candles / candles_15m: 15m candles for entry confirmation and fast SL refinement.
    - btc_candles: BTC candles for market context/trend filter.
    - alt_market_candles: optional broad alt-market proxy candles.
    - real_trade_enabled/trade_margin_usdt/leverage/min_net_profit_usdt: optional runtime settings.

    If candle data is missing, this function fails closed with NO_TRADE.  The
    strategy layer must not invent analysis from price-only ticks.
    """
    market_map: Mapping[str, Any] = market or kwargs
    key = _normalize_symbol(symbol)
    if _active_strategy_level != 4:
        return _no_trade(key, "فقط Level 4 برای سیگنال جدید مجاز است")
    try:
        get_coin(key)
    except Exception as exc:
        return _no_trade(key, f"کوین خارج از واچ‌لیست قفل‌شده است: {exc}")

    entry = _safe_price(market_map.get("price") or market_map.get("entry"))
    candles = _coerce_candles(
        market_map.get("candles_30m")
        or market_map.get("candles")
        or market_map.get("symbol_candles")
    )
    entry_candles = _coerce_candles(
        market_map.get("candles_15m")
        or market_map.get("entry_candles")
        or market_map.get("entry_confirm_candles")
    )
    if entry <= 0 and candles:
        entry = candles[-1].close
    if entry <= 0:
        return _no_trade(key, "قیمت ورود معتبر نیست")
    if len(candles) < MIN_MAIN_CANDLES_30M:
        return _no_trade(key, "برای مدل 30m حداقل ۴۰ کندل ۳۰ دقیقه‌ای لازم است", entry=entry)
    if len(entry_candles) < MIN_ENTRY_CANDLES_15M:
        return _no_trade(key, "برای تأیید ورود 15m حداقل ۴۰ کندل ۱۵ دقیقه‌ای لازم است", entry=entry)

    btc_candles = _coerce_candles(market_map.get("btc_candles") or market_map.get("market_candles"))
    alt_candles = _coerce_candles(market_map.get("alt_market_candles"))
    context = _context_from_market(market_map, btc_candles, alt_candles)

    try:
        analysis = analyze_coin(key, candles, entry_candles=entry_candles)
        probability = calculate_probabilities(analysis, context)
        ai = make_ai_decision(probability, context)
    except Exception as exc:
        return _no_trade(key, f"خطای تحلیل استراتژی: {exc}", entry=entry, context=context)

    if ai.decision == "NO_TRADE" or ai.direction not in ("LONG", "SHORT"):
        return _no_trade(
            key,
            ai.reason,
            entry=entry,
            confidence=ai.confidence,
            context=context,
            analysis=analysis,
            probability=probability,
            ai=ai,
        )

    final_block = _final_entry_block_reason(probability, ai.confidence, context)
    if final_block:
        return _no_trade(
            key,
            final_block,
            entry=entry,
            confidence=ai.confidence,
            context=context,
            analysis=analysis,
            probability=probability,
            ai=ai,
        )

    direction: Literal["LONG", "SHORT"] = ai.direction  # type: ignore[assignment]
    trade_margin = _safe_float(market_map.get("trade_margin_usdt") or market_map.get("trade_dollar_usdt"), DEFAULT_TRADE_DOLLAR)
    leverage = int(_safe_float(market_map.get("leverage"), DEFAULT_LEVERAGE))
    min_net = _safe_float(market_map.get("min_net_profit_usdt"), DEFAULT_MIN_NET_PROFIT_USDT)

    try:
        # SL/TP construction belongs to tp_sl_engine.  strategy_manager only
        # coordinates the already-approved direction, market analysis, and runtime mode.
        plan = build_fast_tp_sl_plan(
            symbol=key,
            direction=direction,
            entry=entry,
            candles_30m=candles,
            candles_15m=entry_candles,
            move_strength=analysis.move_strength,
            trade_margin_usdt=trade_margin,
            leverage=leverage,
            min_net_profit_usdt=min_net,
        )
    except Exception as exc:
        return _no_trade(
            key,
            f"TP/SL معتبر ساخته نشد: {exc}",
            entry=entry,
            confidence=ai.confidence,
            context=context,
            analysis=analysis,
            probability=probability,
            ai=ai,
        )

    if plan.execution_mode != "REAL_ALLOWED":
        mode: ExecutionMode = "SIGNAL"
        real = False
    else:
        real_enabled = _runtime_real_enabled(market_map)
        mode = "REAL" if real_enabled else "SIGNAL"
        real = bool(real_enabled)

    action: Action = "ENTER_LONG" if direction == "LONG" else "ENTER_SHORT"
    return StrategyDecision(
        symbol=key,
        action=action,
        decision=action,
        direction=direction,
        mode=mode,
        real=real,
        entry=round(entry, 8),
        tp=plan.tp,
        sl=plan.sl,
        confidence=round(ai.confidence, 2),
        reason=_entry_reason(ai, probability, plan, mode),
        signal_id=f"L4_{key}_{int(time() * 1000)}",
        tp_sl=plan,
        probability=probability,
        ai=ai,
        context=context,
        analysis=analysis,
        metadata={
            "move_strength": analysis.move_strength,
            "risk_reward": plan.risk_reward,
            "net_profit_usdt": plan.net_profit_usdt,
            "estimated_fee_usdt": plan.estimated_fee_usdt,
            "target_hold_minutes": TARGET_HOLD_MINUTES,
            "main_timeframe": STRATEGY_TIMEFRAME,
            "entry_timeframe": ENTRY_TIMEFRAME,
            "trend_filter_timeframe": TREND_FILTER_TIMEFRAME,
        },
    )


# Compatibility aliases expected by bot.py.
analyze_symbol = decide
build_decision = decide
evaluate_symbol = decide
get_decision = decide


def set_strategy_level(level: int) -> dict[str, Any]:
    global _active_strategy_level
    if int(level) != 4:
        raise ValueError("فقط استراتژی لول 4 قفل و فعال است.")
    _active_strategy_level = 4
    return {"ok": True, "active_level": 4, "name": STRATEGY_NAME}


set_active_strategy = set_strategy_level
activate_strategy_level = set_strategy_level


def get_active_strategy_level() -> int:
    return _active_strategy_level


def get_strategy_status() -> dict[str, Any]:
    return {
        "active_level": _active_strategy_level,
        "name": STRATEGY_NAME,
        "timeframe": STRATEGY_TIMEFRAME,
        "entry_timeframe": ENTRY_TIMEFRAME,
        "trend_filter_timeframe": TREND_FILTER_TIMEFRAME,
        "target_hold_minutes": TARGET_HOLD_MINUTES,
        "only_level_4_active": _active_strategy_level == 4,
    }


def get_trade_runtime_config(state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    settings = _extract_settings(state)
    real_enabled = _runtime_real_enabled(settings)
    return {
        "strategy_level": _active_strategy_level,
        "strategy_name": STRATEGY_NAME,
        "timeframe": STRATEGY_TIMEFRAME,
        "entry_timeframe": ENTRY_TIMEFRAME,
        "trend_filter_timeframe": TREND_FILTER_TIMEFRAME,
        "target_hold_minutes": TARGET_HOLD_MINUTES,
        "real_trading_enabled": real_enabled,
        "trade_margin_usdt": _safe_float(_get(settings, "trade_dollar_usdt", DEFAULT_TRADE_DOLLAR), DEFAULT_TRADE_DOLLAR),
        "leverage": int(_safe_float(_get(settings, "leverage", DEFAULT_LEVERAGE), DEFAULT_LEVERAGE)),
        "min_net_profit_usdt": _safe_float(_get(settings, "min_net_profit_usdt", DEFAULT_MIN_NET_PROFIT_USDT), DEFAULT_MIN_NET_PROFIT_USDT),
        "margin_mode": "isolated",
        "tp_count": 1,
        "sl_count": 1,
    }


def is_real_trading_enabled(state: Mapping[str, Any] | None = None) -> bool:
    return bool(get_trade_runtime_config(state).get("real_trading_enabled"))


def enable_real_trading() -> None:
    global _real_trading_enabled_override
    _real_trading_enabled_override = True


def disable_real_trading() -> None:
    global _real_trading_enabled_override
    _real_trading_enabled_override = False


def _runtime_real_enabled(source: Mapping[str, Any] | Any | None) -> bool:
    if _real_trading_enabled_override is not None:
        return bool(_real_trading_enabled_override)
    value = _get(source, "real_trade_enabled", DEFAULT_REAL_TRADE_ENABLED)
    return _truthy(value)


def _context_from_market(market: Mapping[str, Any], btc_candles: list[Candle], alt_candles: list[Candle]) -> MarketContext:
    explicit = market.get("context") or market.get("market_context")
    if isinstance(explicit, MarketContext):
        return explicit
    if btc_candles:
        return build_market_context(
            btc_candles,
            alt_market_candles=alt_candles or None,
        )
    return MarketContext(
        btc_direction="neutral",
        market_direction="neutral",
        volatility_state="caution",
        sentiment_state="normal",
        trade_permission="caution",
        long_bias=0.0,
        short_bias=0.0,
        reason="BTC context candles missing; cautious mode",
    )


def _final_entry_block_reason(probability: ProbabilityResult, confidence: float, context: MarketContext) -> str:
    """Final fast-mode gate before TP/SL and real/signal execution.

    probability_engine already prefers NO_TRADE, but the strategy layer keeps the
    locked 15m/30m entry rules explicit and fail-closed.
    """
    edge = abs(probability.long_probability - probability.short_probability)
    if context.trade_permission == "blocked":
        return "ورود ممنوع: کانتکست بازار blocked است"
    if confidence < MIN_REAL_CONFIDENCE:
        return f"ورود ممنوع: Confidence کمتر از حد قفل‌شده است ({confidence:.2f} < {MIN_REAL_CONFIDENCE:.2f})"
    if probability.agreement_score < MIN_REAL_AGREEMENT:
        return f"ورود ممنوع: Agreement کمتر از حد قفل‌شده است ({probability.agreement_score:.2f} < {MIN_REAL_AGREEMENT:.2f})"
    if edge < MIN_REAL_DIRECTION_EDGE:
        return f"ورود ممنوع: اختلاف LONG/SHORT کافی نیست ({edge:.2f} < {MIN_REAL_DIRECTION_EDGE:.2f})"
    if probability.preferred_direction not in ("LONG", "SHORT"):
        return "ورود ممنوع: جهت نهایی معتبر نیست"
    return ""


def _entry_reason(ai: AIDecision, probability: ProbabilityResult, plan: TPSLPlan, mode: ExecutionMode) -> str:
    return (
        f"{STRATEGY_NAME} | {mode} | {ai.reason} | "
        f"Preferred={probability.preferred_direction} | "
        f"RR={plan.risk_reward:.1f} | Net={plan.net_profit_usdt:.4f} | "
        f"Fee={plan.estimated_fee_usdt:.4f} | {plan.reason}"
    )


def _no_trade(
    symbol: str,
    reason: str,
    *,
    entry: float = 0.0,
    confidence: float = 0.0,
    context: MarketContext | None = None,
    analysis: CoinAnalysis | None = None,
    probability: ProbabilityResult | None = None,
    ai: AIDecision | None = None,
) -> StrategyDecision:
    return StrategyDecision(
        symbol=_normalize_symbol(symbol),
        action="NO_TRADE",
        decision="NO_TRADE",
        direction="NONE",
        mode="NO_TRADE",
        real=False,
        entry=round(entry, 8) if entry > 0 else 0.0,
        tp=0.0,
        sl=0.0,
        confidence=round(confidence, 2),
        reason=reason,
        signal_id=f"L4_{_normalize_symbol(symbol)}_NO_TRADE_{int(time() * 1000)}",
        context=context,
        analysis=analysis,
        probability=probability,
        ai=ai,
        metadata={"min_confidence": MIN_CONFIDENCE, "target_hold_minutes": TARGET_HOLD_MINUTES},
    )


def _coerce_candles(value: Any) -> list[Candle]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    candles: list[Candle] = []
    for row in value:
        candle = _coerce_one_candle(row)
        if candle is not None:
            candles.append(candle)
    candles.sort(key=lambda c: c.timestamp)
    return candles


def _coerce_one_candle(row: Any) -> Candle | None:
    if isinstance(row, Candle):
        return row
    if isinstance(row, Mapping):
        try:
            return Candle(
                timestamp=int(float(row.get("timestamp") or row.get("ts") or row.get("time") or 0)),
                open=float(row.get("open")),
                high=float(row.get("high")),
                low=float(row.get("low")),
                close=float(row.get("close")),
                volume=float(row.get("volume") or row.get("vol") or 0.0),
            )
        except Exception:
            return None
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)) and len(row) >= 6:
        try:
            return Candle(
                timestamp=int(float(row[0])),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        except Exception:
            return None
    return None


def _extract_settings(state: Mapping[str, Any] | Any | None) -> Mapping[str, Any] | Any | None:
    return _get(state, "settings", state)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled", "active", "فعال"}
    return bool(value)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_price(value: Any) -> float:
    price = _safe_float(value, 0.0)
    return price if price > 0 else 0.0


def _normalize_symbol(symbol: Any) -> str:
    return str(symbol).upper().replace("-", "").replace("_", "").replace("SWAP", "").strip()


__all__ = [
    "ACTIVE_STRATEGY_LEVEL",
    "STRATEGY_NAME",
    "STRATEGY_TIMEFRAME",
    "ENTRY_TIMEFRAME",
    "TREND_FILTER_TIMEFRAME",
    "StrategyDecision",
    "StrategyRuntimeConfig",
    "decide",
    "analyze_symbol",
    "build_decision",
    "evaluate_symbol",
    "get_decision",
    "set_strategy_level",
    "set_active_strategy",
    "activate_strategy_level",
    "get_active_strategy_level",
    "get_strategy_status",
    "get_trade_runtime_config",
    "is_real_trading_enabled",
    "enable_real_trading",
    "disable_real_trading",
]
