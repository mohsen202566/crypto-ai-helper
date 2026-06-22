from __future__ import annotations

"""
06-A - analysis_layers_part_A.py

Part A of the final 06-analysis_layers.py module.

This file is a real, compile-ready foundation for the technical sensor layer.
It contains:
- Core sensor dataclasses
- Validation layer
- Math helpers
- Window helpers
- Candle helpers
- Volume helpers
- BaseSensor
- SnapshotBuilder skeleton

Strict architecture rules:
- This file produces sensors only.
- It must not output REAL/GHOST/REJECT.
- It must not trade.
- It must not call Toobit.
- It must not send Telegram messages.
- It must not do persistence.
- It must not contain Paper or Setup logic.

Parts B/C/D will extend this same module with:
- EMA/VWAP/RSI/MACD
- ADX/ATR/Volume/Power/Wick
- Breakout/Liquidity/Range/Exhaustion/BTC context
"""

import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


JsonDict = Dict[str, Any]


# =============================================================================
# Constants
# =============================================================================

STATE_UNKNOWN = "UNKNOWN"
STATE_ABOVE = "ABOVE"
STATE_BELOW = "BELOW"
STATE_RECLAIM = "RECLAIM"
STATE_LOSS = "LOSS"
STATE_UP = "UP"
STATE_DOWN = "DOWN"
STATE_FLAT = "FLAT"
STATE_EXPANDING = "EXPANDING"
STATE_SHRINKING = "SHRINKING"
STATE_NORMAL = "NORMAL"

TREND_WEAK = "WEAK"
TREND_NORMAL = "NORMAL"
TREND_STRONG = "STRONG"

MARKET_START = "START"
MARKET_MIDDLE = "MIDDLE"
MARKET_LATE = "LATE"
MARKET_EXHAUSTION = "EXHAUSTION"
MARKET_REVERSAL = "REVERSAL"
MARKET_RANGE = "RANGE"


# =============================================================================
# Core models
# =============================================================================

@dataclass(frozen=True)
class CandleView:
    """
    Minimal candle structure used by sensors.

    Compatible with:
    - market_data.Candle dataclass
    - dict candle records
    - common exchange candle arrays converted by market_data.py
    """

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0
    confirm: bool = True

    @property
    def body(self) -> float:
        return self.close - self.open

    @property
    def body_abs(self) -> float:
        return abs(self.body)

    @property
    def range(self) -> float:
        return max(0.0, self.high - self.low)

    @property
    def upper_wick(self) -> float:
        return max(0.0, self.high - max(self.open, self.close))

    @property
    def lower_wick(self) -> float:
        return max(0.0, min(self.open, self.close) - self.low)

    @property
    def direction(self) -> str:
        if self.close > self.open:
            return "BULL"
        if self.close < self.open:
            return "BEAR"
        return "DOJI"

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class SensorSnapshot:
    """
    Final sensor object consumed by later files:
    07-analysis_engine.py
    08-movement_hunter.py
    09-trap_engine.py
    10-state_engine.py
    18-ai_decision_engine.py

    It intentionally contains no decision_type and no trade command.
    """

    symbol: str
    timeframe: str
    timestamp: int

    # Price
    price: float = 0.0
    previous_close: float = 0.0
    price_change_percent: float = 0.0

    # EMA / trend
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_trend: float = 0.0
    ema_state: str = STATE_UNKNOWN
    ema_distance_percent: float = 0.0

    # VWAP
    vwap: float = 0.0
    vwap_state: str = STATE_UNKNOWN
    vwap_distance_percent: float = 0.0

    # RSI
    rsi: float = 50.0
    rsi_slope: float = 0.0
    rsi_velocity: float = 0.0
    rsi_acceleration: float = 0.0
    rsi_state: str = STATE_FLAT

    # MACD
    macd: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    macd_histogram_prev: float = 0.0
    histogram_slope: float = 0.0
    histogram_acceleration: float = 0.0
    histogram_state: str = STATE_FLAT

    # ADX / trend strength
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    trend_strength: str = TREND_WEAK

    # ATR / volatility
    atr: float = 0.0
    atr_percent: float = 0.0
    atr_slope: float = 0.0
    atr_expansion: str = STATE_NORMAL
    atr_explosion: bool = False

    # Volume
    volume: float = 0.0
    avg_volume: float = 0.0
    relative_volume: float = 0.0
    volume_expansion: bool = False
    volume_spike: bool = False

    # Buy/Sell power
    buy_power: float = 0.0
    sell_power: float = 0.0
    power_delta: float = 0.0
    power_delta_percent: float = 0.0

    # Candle structure
    body_size: float = 0.0
    body_percent: float = 0.0
    candle_range: float = 0.0
    upper_wick: float = 0.0
    lower_wick: float = 0.0
    upper_wick_percent: float = 0.0
    lower_wick_percent: float = 0.0
    close_quality: float = 0.5

    # Breakout candidates
    breakout_candidate: bool = False
    breakdown_candidate: bool = False
    failed_breakout: bool = False
    failed_breakdown: bool = False

    # Liquidity / stop hunt sensors
    liquidity_grab_up: bool = False
    liquidity_grab_down: bool = False
    stop_hunt_probability: float = 0.0

    # Range / compression
    range_probability: float = 0.0
    compression_score: float = 0.0
    expansion_probability: float = 0.0

    # Exhaustion / weakness
    bull_exhaustion: bool = False
    bear_exhaustion: bool = False
    momentum_weakness: bool = False

    # BTC / market context, filled in later parts when context is provided
    btc_trend: str = "NEUTRAL"
    btc_momentum: str = "NEUTRAL"
    btc_dominance: float = 0.0
    market_breadth: float = 50.0
    market_state: str = MARKET_START
    market_regime: str = "NEUTRAL"

    # Quality / validation
    valid: bool = True
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class SensorValidationResult:
    valid: bool
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    errors: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        return asdict(self)


# =============================================================================
# Math helpers
# =============================================================================

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    value = safe_float(value)
    if low > high:
        low, high = high, low
    return max(low, min(high, value))


def safe_div(numerator: Any, denominator: Any, default: float = 0.0) -> float:
    n = safe_float(numerator)
    d = safe_float(denominator)
    if abs(d) < 1e-12:
        return default
    result = n / d
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def safe_pct_change(current: Any, previous: Any, default: float = 0.0) -> float:
    c = safe_float(current)
    p = safe_float(previous)
    if abs(p) < 1e-12:
        return default
    return (c - p) / abs(p) * 100.0


def sign(value: Any) -> int:
    v = safe_float(value)
    if v > 0:
        return 1
    if v < 0:
        return -1
    return 0


def slope(values: Sequence[float], lookback: int = 3) -> float:
    vals = [safe_float(v) for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    if lookback > 0:
        vals = vals[-lookback:]
    if len(vals) < 2:
        return 0.0
    return vals[-1] - vals[0]


def acceleration(values: Sequence[float], lookback: int = 4) -> float:
    vals = [safe_float(v) for v in values if v is not None]
    if lookback > 0:
        vals = vals[-lookback:]
    if len(vals) < 3:
        return 0.0
    first_slope = vals[-2] - vals[-3]
    second_slope = vals[-1] - vals[-2]
    return second_slope - first_slope


# =============================================================================
# Window helpers
# =============================================================================

def last_n(values: Sequence[Any], n: int) -> List[Any]:
    if n <= 0:
        return []
    return list(values[-n:])


def rolling_sum(values: Sequence[Any], window: int) -> float:
    vals = [safe_float(v) for v in last_n(list(values), window)]
    return sum(vals)


def rolling_mean(values: Sequence[Any], window: int) -> float:
    vals = [safe_float(v) for v in last_n(list(values), window)]
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def rolling_std(values: Sequence[Any], window: int) -> float:
    vals = [safe_float(v) for v in last_n(list(values), window)]
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    variance = sum((v - mean) ** 2 for v in vals) / len(vals)
    return math.sqrt(max(0.0, variance))


def rolling_min(values: Sequence[Any], window: int) -> float:
    vals = [safe_float(v) for v in last_n(list(values), window)]
    return min(vals) if vals else 0.0


def rolling_max(values: Sequence[Any], window: int) -> float:
    vals = [safe_float(v) for v in last_n(list(values), window)]
    return max(vals) if vals else 0.0


def normalize_score(value: float, low: float, high: float) -> float:
    if abs(high - low) < 1e-12:
        return 0.0
    return clamp((safe_float(value) - low) / (high - low) * 100.0, 0.0, 100.0)


# =============================================================================
# Candle helpers
# =============================================================================

def candle_from_any(item: Any) -> CandleView:
    if isinstance(item, CandleView):
        return item

    if hasattr(item, "to_dict") and callable(item.to_dict):
        item = item.to_dict()
    elif hasattr(item, "__dict__") and not isinstance(item, dict):
        item = item.__dict__

    if isinstance(item, dict):
        return CandleView(
            timestamp=safe_int(item.get("timestamp", item.get("ts", item.get("time", 0)))),
            open=safe_float(item.get("open", item.get("o", 0.0))),
            high=safe_float(item.get("high", item.get("h", 0.0))),
            low=safe_float(item.get("low", item.get("l", 0.0))),
            close=safe_float(item.get("close", item.get("c", 0.0))),
            volume=safe_float(item.get("volume", item.get("vol", 0.0))),
            quote_volume=safe_float(item.get("quote_volume", item.get("volCcyQuote", 0.0))),
            confirm=bool(item.get("confirm", True)),
        )

    if isinstance(item, (list, tuple)) and len(item) >= 6:
        return CandleView(
            timestamp=safe_int(item[0]),
            open=safe_float(item[1]),
            high=safe_float(item[2]),
            low=safe_float(item[3]),
            close=safe_float(item[4]),
            volume=safe_float(item[5]),
            quote_volume=safe_float(item[7] if len(item) > 7 else 0.0),
            confirm=str(item[8]) == "1" if len(item) > 8 else True,
        )

    raise ValueError(f"unsupported_candle:{type(item)!r}")


def candles_from_any(items: Iterable[Any]) -> List[CandleView]:
    candles: List[CandleView] = []
    for item in items or []:
        try:
            candle = candle_from_any(item)
            if candle.close > 0 and candle.high >= candle.low:
                candles.append(candle)
        except Exception:
            continue
    candles.sort(key=lambda c: c.timestamp)
    return candles


def candle_body(candle: CandleView) -> float:
    return candle.body_abs


def candle_range(candle: CandleView) -> float:
    return candle.range


def upper_wick(candle: CandleView) -> float:
    return candle.upper_wick


def lower_wick(candle: CandleView) -> float:
    return candle.lower_wick


def wick_ratio(candle: CandleView) -> float:
    return safe_div(candle.upper_wick + candle.lower_wick, candle.range, 0.0)


def close_quality(candle: CandleView) -> float:
    """
    0 = close at low, 1 = close at high.
    Useful for breakout/trap sensors in later parts.
    """
    return clamp(safe_div(candle.close - candle.low, candle.range, 0.5), 0.0, 1.0)


def body_percent(candle: CandleView) -> float:
    return clamp(safe_div(candle.body_abs, candle.range, 0.0) * 100.0, 0.0, 100.0)


def upper_wick_percent(candle: CandleView) -> float:
    return clamp(safe_div(candle.upper_wick, candle.range, 0.0) * 100.0, 0.0, 100.0)


def lower_wick_percent(candle: CandleView) -> float:
    return clamp(safe_div(candle.lower_wick, candle.range, 0.0) * 100.0, 0.0, 100.0)


# =============================================================================
# Volume helpers
# =============================================================================

def volumes(candles: Sequence[CandleView]) -> List[float]:
    return [safe_float(c.volume) for c in candles]


def closes(candles: Sequence[CandleView]) -> List[float]:
    return [safe_float(c.close) for c in candles]


def highs(candles: Sequence[CandleView]) -> List[float]:
    return [safe_float(c.high) for c in candles]


def lows(candles: Sequence[CandleView]) -> List[float]:
    return [safe_float(c.low) for c in candles]


def relative_volume(current_volume: float, history: Sequence[float], window: int = 20) -> float:
    avg = rolling_mean(history, window)
    return safe_div(current_volume, avg, 0.0)


def is_volume_spike(current_volume: float, history: Sequence[float], window: int = 20, multiplier: float = 2.0) -> bool:
    return relative_volume(current_volume, history, window) >= multiplier


def is_volume_expanding(history: Sequence[float], short_window: int = 3, long_window: int = 20) -> bool:
    if len(history) < max(short_window, 2):
        return False
    short_avg = rolling_mean(history, short_window)
    long_avg = rolling_mean(history, long_window)
    return short_avg > long_avg and short_avg > 0


# =============================================================================
# Validation layer
# =============================================================================

class SensorValidator:
    """Validation and safety guard for sensor output."""

    @staticmethod
    def validate_candles(candles: Sequence[CandleView], min_count: int = 30) -> SensorValidationResult:
        warnings: List[str] = []
        errors: List[str] = []

        if not candles:
            return SensorValidationResult(False, tuple(warnings), ("no_candles",))

        if len(candles) < min_count:
            warnings.append(f"low_candle_count:{len(candles)}<{min_count}")

        previous_ts = -1
        for idx, candle in enumerate(candles):
            if candle.close <= 0 or candle.open <= 0 or candle.high <= 0 or candle.low <= 0:
                errors.append(f"non_positive_price_at:{idx}")
            if candle.high < candle.low:
                errors.append(f"high_less_than_low_at:{idx}")
            if candle.volume < 0:
                errors.append(f"negative_volume_at:{idx}")
            if candle.timestamp and candle.timestamp < previous_ts:
                warnings.append(f"unsorted_timestamp_at:{idx}")
            previous_ts = candle.timestamp

        return SensorValidationResult(len(errors) == 0, tuple(warnings), tuple(errors))

    @staticmethod
    def validate_snapshot(snapshot: SensorSnapshot) -> SensorValidationResult:
        warnings: List[str] = []
        errors: List[str] = []

        if not snapshot.symbol:
            errors.append("missing_symbol")
        if not snapshot.timeframe:
            errors.append("missing_timeframe")
        if snapshot.price <= 0:
            errors.append("invalid_price")
        if not 0 <= snapshot.rsi <= 100:
            errors.append("rsi_out_of_range")
        if not 0 <= snapshot.adx <= 100:
            errors.append("adx_out_of_range")
        if snapshot.atr < 0:
            errors.append("negative_atr")
        if snapshot.volume < 0:
            errors.append("negative_volume")
        if snapshot.relative_volume < 0:
            errors.append("negative_relative_volume")
        if snapshot.buy_power < 0 or snapshot.sell_power < 0:
            errors.append("negative_power")
        if not 0 <= snapshot.range_probability <= 100:
            warnings.append("range_probability_out_of_range")
        if not 0 <= snapshot.compression_score <= 100:
            warnings.append("compression_score_out_of_range")
        if not 0 <= snapshot.expansion_probability <= 100:
            warnings.append("expansion_probability_out_of_range")

        return SensorValidationResult(len(errors) == 0, tuple(warnings), tuple(errors))


# =============================================================================
# Base sensor
# =============================================================================

class BaseSensor:
    """
    Parent class for all sensor engines.

    Later parts B/C/D will subclass or use this class for calculation helpers.
    """

    def __init__(self, candles: Sequence[Any]):
        self.candles: List[CandleView] = candles_from_any(candles)

    @property
    def valid(self) -> bool:
        return bool(self.candles)

    @property
    def last(self) -> Optional[CandleView]:
        return self.candles[-1] if self.candles else None

    @property
    def previous(self) -> Optional[CandleView]:
        return self.candles[-2] if len(self.candles) >= 2 else None

    def closes(self) -> List[float]:
        return closes(self.candles)

    def highs(self) -> List[float]:
        return highs(self.candles)

    def lows(self) -> List[float]:
        return lows(self.candles)

    def volumes(self) -> List[float]:
        return volumes(self.candles)

    def last_n_candles(self, n: int) -> List[CandleView]:
        return last_n(self.candles, n)

    def safe_last_price(self) -> float:
        return self.last.close if self.last else 0.0


# =============================================================================
# Snapshot builder skeleton
# =============================================================================

class SnapshotBuilder:
    """
    Final builder skeleton.

    Parts B/C/D will fill all calculation fields. Part A only provides safe
    defaults and base candle-derived values so the module is compile-ready and
    independently testable.
    """

    def __init__(self, symbol: str, timeframe: str, candles: Sequence[Any], market_context: Optional[Any] = None):
        self.symbol = str(symbol or "").upper()
        self.timeframe = str(timeframe or "5m")
        self.candles = candles_from_any(candles)
        self.market_context = market_context
        self.validation = SensorValidator.validate_candles(self.candles, min_count=5)

    def _context_value(self, name: str, default: Any) -> Any:
        ctx = self.market_context
        if ctx is None:
            return default
        if isinstance(ctx, dict):
            return ctx.get(name, default)
        return getattr(ctx, name, default)

    def build_base_snapshot(self) -> SensorSnapshot:
        last = self.candles[-1] if self.candles else CandleView(0, 0, 0, 0, 0, 0)
        prev = self.candles[-2] if len(self.candles) >= 2 else last

        vol_history = volumes(self.candles[:-1]) if len(self.candles) > 1 else []
        avg_vol = rolling_mean(vol_history, 20)
        rel_vol = relative_volume(last.volume, vol_history, 20) if vol_history else 0.0

        snapshot = SensorSnapshot(
            symbol=self.symbol,
            timeframe=self.timeframe,
            timestamp=last.timestamp,
            price=last.close,
            previous_close=prev.close,
            price_change_percent=safe_pct_change(last.close, prev.close),
            volume=last.volume,
            avg_volume=avg_vol,
            relative_volume=rel_vol,
            volume_expansion=is_volume_expanding(volumes(self.candles)),
            volume_spike=is_volume_spike(last.volume, vol_history, 20, 2.0) if vol_history else False,
            body_size=last.body_abs,
            body_percent=body_percent(last),
            candle_range=last.range,
            upper_wick=last.upper_wick,
            lower_wick=last.lower_wick,
            upper_wick_percent=upper_wick_percent(last),
            lower_wick_percent=lower_wick_percent(last),
            close_quality=close_quality(last),
            btc_trend=str(self._context_value("btc_trend", "NEUTRAL")),
            btc_momentum=str(self._context_value("btc_momentum", "NEUTRAL")),
            btc_dominance=safe_float(self._context_value("btc_dominance", 0.0)),
            market_breadth=safe_float(self._context_value("market_breadth", 50.0)),
            market_state=str(self._context_value("market_state", MARKET_START)),
            market_regime=str(self._context_value("market_regime", "NEUTRAL")),
            valid=self.validation.valid,
            warnings=tuple(list(self.validation.warnings) + list(self.validation.errors)),
        )

        snapshot_validation = SensorValidator.validate_snapshot(snapshot)
        if not snapshot_validation.valid or snapshot_validation.warnings:
            all_warnings = tuple(list(snapshot.warnings) + list(snapshot_validation.warnings) + list(snapshot_validation.errors))
            snapshot = SensorSnapshot(**{**snapshot.to_dict(), "valid": snapshot_validation.valid, "warnings": all_warnings})

        return snapshot


def build_base_sensor_snapshot(symbol: str, timeframe: str, candles: Sequence[Any], market_context: Optional[Any] = None) -> SensorSnapshot:
    return SnapshotBuilder(symbol=symbol, timeframe=timeframe, candles=candles, market_context=market_context).build_base_snapshot()


# =============================================================================
# Part B - Trend and Momentum Engines
# =============================================================================

def sma(values: Sequence[float], period: int) -> float:
    vals = [safe_float(v) for v in values if v is not None]
    if not vals:
        return 0.0
    period = max(1, int(period))
    vals = vals[-period:]
    return sum(vals) / len(vals)


def ema_series(values: Sequence[float], period: int) -> List[float]:
    vals = [safe_float(v) for v in values if v is not None]
    if not vals:
        return []
    period = max(1, int(period))
    multiplier = 2.0 / (period + 1.0)

    result: List[float] = []
    ema_value = vals[0]
    result.append(ema_value)

    for price in vals[1:]:
        ema_value = (price - ema_value) * multiplier + ema_value
        result.append(ema_value)
    return result


def ema(values: Sequence[float], period: int) -> float:
    series = ema_series(values, period)
    return series[-1] if series else 0.0


def detect_cross(prev_a: float, prev_b: float, cur_a: float, cur_b: float) -> str:
    if prev_a <= prev_b and cur_a > cur_b:
        return "CROSS_UP"
    if prev_a >= prev_b and cur_a < cur_b:
        return "CROSS_DOWN"
    if cur_a > cur_b:
        return STATE_ABOVE
    if cur_a < cur_b:
        return STATE_BELOW
    return STATE_FLAT


class EMASensor(BaseSensor):
    """EMA trend sensor."""

    def compute(self, fast_period: int = 9, slow_period: int = 21, trend_period: int = 55) -> Dict[str, float | str]:
        prices = self.closes()
        if len(prices) < 2:
            return {
                "ema_fast": 0.0,
                "ema_slow": 0.0,
                "ema_trend": 0.0,
                "ema_state": STATE_UNKNOWN,
                "ema_distance_percent": 0.0,
            }

        fast_series = ema_series(prices, fast_period)
        slow_series = ema_series(prices, slow_period)
        trend_series = ema_series(prices, trend_period)

        fast = fast_series[-1] if fast_series else 0.0
        slow = slow_series[-1] if slow_series else 0.0
        trend = trend_series[-1] if trend_series else 0.0

        if len(fast_series) >= 2 and len(slow_series) >= 2:
            state = detect_cross(fast_series[-2], slow_series[-2], fast, slow)
        else:
            state = STATE_ABOVE if fast > slow else STATE_BELOW if fast < slow else STATE_FLAT

        price = prices[-1]
        distance = safe_pct_change(price, slow, 0.0)

        return {
            "ema_fast": fast,
            "ema_slow": slow,
            "ema_trend": trend,
            "ema_state": state,
            "ema_distance_percent": distance,
        }


def vwap_value(candles: Sequence[CandleView], window: int = 50) -> float:
    recent = list(candles)[-max(1, int(window)):]
    numerator = 0.0
    denominator = 0.0
    for candle in recent:
        typical_price = (candle.high + candle.low + candle.close) / 3.0
        volume = max(0.0, safe_float(candle.volume))
        numerator += typical_price * volume
        denominator += volume
    return safe_div(numerator, denominator, 0.0)


class VWAPSensor(BaseSensor):
    """VWAP position and reclaim/loss sensor."""

    def compute(self, window: int = 50) -> Dict[str, float | str]:
        if len(self.candles) < 2:
            return {
                "vwap": 0.0,
                "vwap_state": STATE_UNKNOWN,
                "vwap_distance_percent": 0.0,
            }

        current_vwap = vwap_value(self.candles, window)
        prev_vwap = vwap_value(self.candles[:-1], window) if len(self.candles) > 2 else current_vwap

        last = self.candles[-1]
        prev = self.candles[-2]

        if prev.close <= prev_vwap and last.close > current_vwap:
            state = STATE_RECLAIM
        elif prev.close >= prev_vwap and last.close < current_vwap:
            state = STATE_LOSS
        elif last.close > current_vwap:
            state = STATE_ABOVE
        elif last.close < current_vwap:
            state = STATE_BELOW
        else:
            state = STATE_FLAT

        return {
            "vwap": current_vwap,
            "vwap_state": state,
            "vwap_distance_percent": safe_pct_change(last.close, current_vwap, 0.0),
        }


def rsi_series(values: Sequence[float], period: int = 14) -> List[float]:
    prices = [safe_float(v) for v in values if v is not None]
    if len(prices) < 2:
        return [50.0] * len(prices)

    period = max(1, int(period))
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(0.0, d) for d in deltas]
    losses = [abs(min(0.0, d)) for d in deltas]

    result: List[float] = []
    avg_gain = 0.0
    avg_loss = 0.0

    for i in range(len(deltas)):
        gain = gains[i]
        loss = losses[i]

        if i < period:
            avg_gain = sum(gains[: i + 1]) / (i + 1)
            avg_loss = sum(losses[: i + 1]) / (i + 1)
        elif i == period:
            avg_gain = sum(gains[i - period + 1 : i + 1]) / period
            avg_loss = sum(losses[i - period + 1 : i + 1]) / period
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            rsi = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))
        result.append(clamp(rsi, 0.0, 100.0))

    return [50.0] + result


def rsi_value(values: Sequence[float], period: int = 14) -> float:
    series = rsi_series(values, period)
    return series[-1] if series else 50.0


def classify_slope(value: float, flat_threshold: float = 0.15) -> str:
    v = safe_float(value)
    if v > flat_threshold:
        return STATE_UP
    if v < -flat_threshold:
        return STATE_DOWN
    return STATE_FLAT


class RSISensor(BaseSensor):
    """RSI level, slope, velocity, acceleration sensor."""

    def compute(self, period: int = 14, slope_lookback: int = 3) -> Dict[str, float | str]:
        prices = self.closes()
        if len(prices) < 2:
            return {
                "rsi": 50.0,
                "rsi_slope": 0.0,
                "rsi_velocity": 0.0,
                "rsi_acceleration": 0.0,
                "rsi_state": STATE_FLAT,
            }

        series = rsi_series(prices, period)
        current = series[-1] if series else 50.0
        rsi_slope = slope(series, slope_lookback)
        rsi_velocity = series[-1] - series[-2] if len(series) >= 2 else 0.0
        rsi_acc = acceleration(series, max(4, slope_lookback + 1))

        return {
            "rsi": clamp(current, 0.0, 100.0),
            "rsi_slope": rsi_slope,
            "rsi_velocity": rsi_velocity,
            "rsi_acceleration": rsi_acc,
            "rsi_state": classify_slope(rsi_slope),
        }


def macd_series(values: Sequence[float], fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> Tuple[List[float], List[float], List[float]]:
    prices = [safe_float(v) for v in values if v is not None]
    if not prices:
        return [], [], []

    fast = ema_series(prices, fast_period)
    slow = ema_series(prices, slow_period)

    min_len = min(len(fast), len(slow))
    if min_len == 0:
        return [], [], []

    macd_line = [fast[-min_len + i] - slow[-min_len + i] for i in range(min_len)]
    signal_line = ema_series(macd_line, signal_period)
    min_len2 = min(len(macd_line), len(signal_line))
    if min_len2 == 0:
        return macd_line, signal_line, []

    macd_aligned = macd_line[-min_len2:]
    signal_aligned = signal_line[-min_len2:]
    hist = [macd_aligned[i] - signal_aligned[i] for i in range(min_len2)]
    return macd_aligned, signal_aligned, hist


class MACDSensor(BaseSensor):
    """MACD, histogram slope and acceleration sensor."""

    def compute(self, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> Dict[str, float | str]:
        prices = self.closes()
        macd_line, signal_line, hist = macd_series(prices, fast_period, slow_period, signal_period)

        if not macd_line or not signal_line or not hist:
            return {
                "macd": 0.0,
                "macd_signal": 0.0,
                "macd_histogram": 0.0,
                "macd_histogram_prev": 0.0,
                "histogram_slope": 0.0,
                "histogram_acceleration": 0.0,
                "histogram_state": STATE_FLAT,
            }

        current_hist = hist[-1]
        prev_hist = hist[-2] if len(hist) >= 2 else current_hist
        hist_slope = current_hist - prev_hist
        hist_acc = acceleration(hist, 4)

        if hist_acc > 0 and hist_slope > 0:
            hist_state = "ACCELERATING_UP"
        elif hist_acc > 0 and hist_slope < 0:
            hist_state = "DECELERATING_DOWN"
        elif hist_acc < 0 and hist_slope > 0:
            hist_state = "DECELERATING_UP"
        elif hist_acc < 0 and hist_slope < 0:
            hist_state = "ACCELERATING_DOWN"
        else:
            hist_state = STATE_FLAT

        return {
            "macd": macd_line[-1],
            "macd_signal": signal_line[-1],
            "macd_histogram": current_hist,
            "macd_histogram_prev": prev_hist,
            "histogram_slope": hist_slope,
            "histogram_acceleration": hist_acc,
            "histogram_state": hist_state,
        }


class TrendMomentumSensorPack(BaseSensor):
    """Runs all Part-B trend/momentum sensors on the same candle window."""

    def compute_all(self) -> Dict[str, float | str]:
        result: Dict[str, float | str] = {}
        result.update(EMASensor(self.candles).compute())
        result.update(VWAPSensor(self.candles).compute())
        result.update(RSISensor(self.candles).compute())
        result.update(MACDSensor(self.candles).compute())
        return result


class SnapshotBuilderB(SnapshotBuilder):
    """Enriches Part-A base snapshot with Part-B trend/momentum sensors."""

    def build_trend_momentum_snapshot(self) -> SensorSnapshot:
        base = self.build_base_snapshot()
        if not self.candles:
            return base

        indicators = TrendMomentumSensorPack(self.candles).compute_all()

        return replace(
            base,
            ema_fast=safe_float(indicators.get("ema_fast")),
            ema_slow=safe_float(indicators.get("ema_slow")),
            ema_trend=safe_float(indicators.get("ema_trend")),
            ema_state=str(indicators.get("ema_state", STATE_UNKNOWN)),
            ema_distance_percent=safe_float(indicators.get("ema_distance_percent")),
            vwap=safe_float(indicators.get("vwap")),
            vwap_state=str(indicators.get("vwap_state", STATE_UNKNOWN)),
            vwap_distance_percent=safe_float(indicators.get("vwap_distance_percent")),
            rsi=safe_float(indicators.get("rsi"), 50.0),
            rsi_slope=safe_float(indicators.get("rsi_slope")),
            rsi_velocity=safe_float(indicators.get("rsi_velocity")),
            rsi_acceleration=safe_float(indicators.get("rsi_acceleration")),
            rsi_state=str(indicators.get("rsi_state", STATE_FLAT)),
            macd=safe_float(indicators.get("macd")),
            macd_signal=safe_float(indicators.get("macd_signal")),
            macd_histogram=safe_float(indicators.get("macd_histogram")),
            macd_histogram_prev=safe_float(indicators.get("macd_histogram_prev")),
            histogram_slope=safe_float(indicators.get("histogram_slope")),
            histogram_acceleration=safe_float(indicators.get("histogram_acceleration")),
            histogram_state=str(indicators.get("histogram_state", STATE_FLAT)),
        )


def build_trend_momentum_snapshot(symbol: str, timeframe: str, candles: Sequence[Any], market_context: Optional[Any] = None) -> SensorSnapshot:
    return SnapshotBuilderB(symbol=symbol, timeframe=timeframe, candles=candles, market_context=market_context).build_trend_momentum_snapshot()


build_sensor_snapshot_part_b = build_trend_momentum_snapshot


# =============================================================================
# Part C - Strength, Volatility, Volume, Power Engines
# =============================================================================

# =============================================================================
# True Range / ATR
# =============================================================================

def true_range_series(candles: Sequence[CandleView]) -> List[float]:
    if not candles:
        return []

    trs: List[float] = []
    for i, candle in enumerate(candles):
        if i == 0:
            trs.append(max(0.0, candle.high - candle.low))
            continue
        prev_close = candles[i - 1].close
        tr = max(
            candle.high - candle.low,
            abs(candle.high - prev_close),
            abs(candle.low - prev_close),
        )
        trs.append(max(0.0, tr))
    return trs


def atr_series(candles: Sequence[CandleView], period: int = 14) -> List[float]:
    trs = true_range_series(candles)
    if not trs:
        return []
    period = max(1, int(period))

    result: List[float] = []
    atr_value = trs[0]
    result.append(atr_value)

    for i, tr in enumerate(trs[1:], start=1):
        if i < period:
            atr_value = sum(trs[: i + 1]) / (i + 1)
        else:
            atr_value = (atr_value * (period - 1) + tr) / period
        result.append(atr_value)
    return result


class ATRSensor(BaseSensor):
    """ATR, expansion and explosion sensor."""

    def compute(self, period: int = 14, expansion_window: int = 20, explosion_multiplier: float = 1.8) -> Dict[str, float | str | bool]:
        if len(self.candles) < 2:
            return {
                "atr": 0.0,
                "atr_percent": 0.0,
                "atr_slope": 0.0,
                "atr_expansion": STATE_NORMAL,
                "atr_explosion": False,
            }

        atrs = atr_series(self.candles, period)
        current_atr = atrs[-1] if atrs else 0.0
        price = self.candles[-1].close
        atr_percent = safe_div(current_atr, price, 0.0) * 100.0
        atr_slp = slope(atrs, 4)
        baseline = rolling_mean(atrs[:-1], expansion_window) if len(atrs) > 1 else current_atr

        if current_atr > baseline * 1.15 and atr_slp > 0:
            expansion = STATE_EXPANDING
        elif current_atr < baseline * 0.85 and atr_slp < 0:
            expansion = STATE_SHRINKING
        else:
            expansion = STATE_NORMAL

        explosion = bool(baseline > 0 and current_atr >= baseline * explosion_multiplier and atr_slp > 0)

        return {
            "atr": current_atr,
            "atr_percent": atr_percent,
            "atr_slope": atr_slp,
            "atr_expansion": expansion,
            "atr_explosion": explosion,
        }


# =============================================================================
# ADX / DI
# =============================================================================

def directional_movement_series(candles: Sequence[CandleView]) -> Tuple[List[float], List[float], List[float]]:
    if len(candles) < 2:
        return [], [], []

    plus_dm: List[float] = [0.0]
    minus_dm: List[float] = [0.0]
    trs = true_range_series(candles)

    for i in range(1, len(candles)):
        up_move = candles[i].high - candles[i - 1].high
        down_move = candles[i - 1].low - candles[i].low

        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

    return plus_dm, minus_dm, trs


def smoothed_wilder(values: Sequence[float], period: int) -> List[float]:
    vals = [safe_float(v) for v in values]
    if not vals:
        return []
    period = max(1, int(period))

    result: List[float] = []
    current = vals[0]
    result.append(current)

    for i, value in enumerate(vals[1:], start=1):
        if i < period:
            current = sum(vals[: i + 1]) / (i + 1)
        else:
            current = (current * (period - 1) + value) / period
        result.append(current)
    return result


def adx_components(candles: Sequence[CandleView], period: int = 14) -> Tuple[float, float, float]:
    if len(candles) < 3:
        return 0.0, 0.0, 0.0

    plus_dm, minus_dm, trs = directional_movement_series(candles)
    sm_plus = smoothed_wilder(plus_dm, period)
    sm_minus = smoothed_wilder(minus_dm, period)
    sm_tr = smoothed_wilder(trs, period)

    plus_di_series: List[float] = []
    minus_di_series: List[float] = []
    dx_series: List[float] = []

    for p, m, tr in zip(sm_plus, sm_minus, sm_tr):
        plus_di = safe_div(p, tr, 0.0) * 100.0
        minus_di = safe_div(m, tr, 0.0) * 100.0
        denom = plus_di + minus_di
        dx = safe_div(abs(plus_di - minus_di), denom, 0.0) * 100.0
        plus_di_series.append(clamp(plus_di, 0.0, 100.0))
        minus_di_series.append(clamp(minus_di, 0.0, 100.0))
        dx_series.append(clamp(dx, 0.0, 100.0))

    adx_vals = smoothed_wilder(dx_series, period)
    adx = adx_vals[-1] if adx_vals else 0.0
    plus_di = plus_di_series[-1] if plus_di_series else 0.0
    minus_di = minus_di_series[-1] if minus_di_series else 0.0
    return clamp(adx, 0.0, 100.0), clamp(plus_di, 0.0, 100.0), clamp(minus_di, 0.0, 100.0)


class ADXSensor(BaseSensor):
    """Trend strength sensor."""

    def compute(self, period: int = 14) -> Dict[str, float | str]:
        adx, plus_di, minus_di = adx_components(self.candles, period)

        if adx >= 28:
            strength = TREND_STRONG
        elif adx >= 18:
            strength = TREND_NORMAL
        else:
            strength = TREND_WEAK

        return {
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "trend_strength": strength,
        }


# =============================================================================
# Volume sensor
# =============================================================================

class VolumeSensor(BaseSensor):
    """Relative volume, expansion and spike sensor."""

    def compute(self, avg_window: int = 20, short_window: int = 3, spike_multiplier: float = 2.0) -> Dict[str, float | bool]:
        if not self.candles:
            return {
                "volume": 0.0,
                "avg_volume": 0.0,
                "relative_volume": 0.0,
                "volume_expansion": False,
                "volume_spike": False,
            }

        vols = volumes(self.candles)
        current = vols[-1]
        history = vols[:-1]
        avg = rolling_mean(history, avg_window) if history else 0.0
        rel = relative_volume(current, history, avg_window) if history else 0.0

        short_avg = rolling_mean(vols, short_window)
        long_avg = rolling_mean(vols, avg_window)
        expansion = bool(long_avg > 0 and short_avg > long_avg * 1.10)
        spike = bool(rel >= spike_multiplier)

        return {
            "volume": current,
            "avg_volume": avg,
            "relative_volume": rel,
            "volume_expansion": expansion,
            "volume_spike": spike,
        }


# =============================================================================
# Buy / Sell Power
# =============================================================================

def candle_buy_sell_power(candle: CandleView) -> Tuple[float, float]:
    """
    Approximate buy/sell power from candle position and body.

    This is not orderbook flow; it is a raw candle participation sensor.
    """
    if candle.range <= 0:
        return 50.0, 50.0

    cq = close_quality(candle)
    body_bias = 0.0
    if candle.close > candle.open:
        body_bias = min(25.0, safe_div(candle.body_abs, candle.range, 0.0) * 25.0)
    elif candle.close < candle.open:
        body_bias = -min(25.0, safe_div(candle.body_abs, candle.range, 0.0) * 25.0)

    buy = clamp(cq * 100.0 + body_bias, 0.0, 100.0)
    sell = clamp(100.0 - buy, 0.0, 100.0)
    return buy, sell


class PowerSensor(BaseSensor):
    """Buy/sell power and power delta sensor."""

    def compute(self, window: int = 3) -> Dict[str, float]:
        if not self.candles:
            return {
                "buy_power": 0.0,
                "sell_power": 0.0,
                "power_delta": 0.0,
                "power_delta_percent": 0.0,
            }

        recent = self.candles[-max(1, int(window)):]
        buys: List[float] = []
        sells: List[float] = []
        weights: List[float] = []

        for candle in recent:
            buy, sell = candle_buy_sell_power(candle)
            weight = max(1.0, safe_float(candle.volume))
            buys.append(buy * weight)
            sells.append(sell * weight)
            weights.append(weight)

        total_weight = sum(weights)
        buy_power = safe_div(sum(buys), total_weight, 50.0)
        sell_power = safe_div(sum(sells), total_weight, 50.0)
        delta = buy_power - sell_power
        delta_percent = clamp((delta + 100.0) / 2.0, 0.0, 100.0)

        return {
            "buy_power": clamp(buy_power, 0.0, 100.0),
            "sell_power": clamp(sell_power, 0.0, 100.0),
            "power_delta": delta,
            "power_delta_percent": delta_percent,
        }


# =============================================================================
# Candle / wick refinement
# =============================================================================

class CandleStructureSensor(BaseSensor):
    """Candle body, wick and close quality refinement."""

    def compute(self) -> Dict[str, float]:
        if not self.candles:
            return {
                "body_size": 0.0,
                "body_percent": 0.0,
                "candle_range": 0.0,
                "upper_wick": 0.0,
                "lower_wick": 0.0,
                "upper_wick_percent": 0.0,
                "lower_wick_percent": 0.0,
                "close_quality": 0.5,
            }

        candle = self.candles[-1]
        body_pct = safe_div(candle.body_abs, candle.range, 0.0) * 100.0

        return {
            "body_size": candle.body_abs,
            "body_percent": clamp(body_pct, 0.0, 100.0),
            "candle_range": candle.range,
            "upper_wick": candle.upper_wick,
            "lower_wick": candle.lower_wick,
            "upper_wick_percent": upper_wick_percent(candle),
            "lower_wick_percent": lower_wick_percent(candle),
            "close_quality": close_quality(candle),
        }


# =============================================================================
# Combined Part-C pack
# =============================================================================

class StrengthVolatilityVolumePack(BaseSensor):
    """Runs all Part-C sensors on the same candle window."""

    def compute_all(self) -> Dict[str, float | str | bool]:
        result: Dict[str, float | str | bool] = {}
        result.update(ADXSensor(self.candles).compute())
        result.update(ATRSensor(self.candles).compute())
        result.update(VolumeSensor(self.candles).compute())
        result.update(PowerSensor(self.candles).compute())
        result.update(CandleStructureSensor(self.candles).compute())
        return result


class SnapshotBuilderC(SnapshotBuilderB):
    """Enriches Part-B snapshot with Part-C strength/volatility/volume/power sensors."""

    def build_strength_volatility_snapshot(self) -> SensorSnapshot:
        base = self.build_trend_momentum_snapshot()
        if not self.candles:
            return base

        indicators = StrengthVolatilityVolumePack(self.candles).compute_all()

        return replace(
            base,
            adx=safe_float(indicators.get("adx")),
            plus_di=safe_float(indicators.get("plus_di")),
            minus_di=safe_float(indicators.get("minus_di")),
            trend_strength=str(indicators.get("trend_strength", TREND_WEAK)),
            atr=safe_float(indicators.get("atr")),
            atr_percent=safe_float(indicators.get("atr_percent")),
            atr_slope=safe_float(indicators.get("atr_slope")),
            atr_expansion=str(indicators.get("atr_expansion", STATE_NORMAL)),
            atr_explosion=bool(indicators.get("atr_explosion", False)),
            volume=safe_float(indicators.get("volume")),
            avg_volume=safe_float(indicators.get("avg_volume")),
            relative_volume=safe_float(indicators.get("relative_volume")),
            volume_expansion=bool(indicators.get("volume_expansion", False)),
            volume_spike=bool(indicators.get("volume_spike", False)),
            buy_power=safe_float(indicators.get("buy_power")),
            sell_power=safe_float(indicators.get("sell_power")),
            power_delta=safe_float(indicators.get("power_delta")),
            power_delta_percent=safe_float(indicators.get("power_delta_percent")),
            body_size=safe_float(indicators.get("body_size")),
            body_percent=safe_float(indicators.get("body_percent")),
            candle_range=safe_float(indicators.get("candle_range")),
            upper_wick=safe_float(indicators.get("upper_wick")),
            lower_wick=safe_float(indicators.get("lower_wick")),
            upper_wick_percent=safe_float(indicators.get("upper_wick_percent")),
            lower_wick_percent=safe_float(indicators.get("lower_wick_percent")),
            close_quality=safe_float(indicators.get("close_quality"), 0.5),
        )


def build_strength_volatility_snapshot(symbol: str, timeframe: str, candles: Sequence[Any], market_context: Optional[Any] = None) -> SensorSnapshot:
    return SnapshotBuilderC(symbol=symbol, timeframe=timeframe, candles=candles, market_context=market_context).build_strength_volatility_snapshot()


build_sensor_snapshot_part_c = build_strength_volatility_snapshot


# =============================================================================
# Part D - Breakout, Liquidity, Range, Exhaustion Engines
# =============================================================================

class BreakoutSensor(BaseSensor):

    def compute(self, lookback: int = 20) -> Dict[str, Any]:
        if len(self.candles) < lookback:
            return {
                "breakout_candidate": False,
                "breakdown_candidate": False,
                "failed_breakout": False,
                "failed_breakdown": False,
            }

        last = self.candles[-1]

        high_level = rolling_max([c.high for c in self.candles[:-1]], lookback)
        low_level = rolling_min([c.low for c in self.candles[:-1]], lookback)

        breakout = last.close > high_level
        breakdown = last.close < low_level

        failed_breakout = last.high > high_level and last.close < high_level
        failed_breakdown = last.low < low_level and last.close > low_level

        return {
            "breakout_candidate": breakout,
            "breakdown_candidate": breakdown,
            "failed_breakout": failed_breakout,
            "failed_breakdown": failed_breakdown,
        }


class LiquidityGrabSensor(BaseSensor):

    def compute(self, lookback: int = 20) -> Dict[str, Any]:
        if len(self.candles) < lookback:
            return {
                "liquidity_grab_up": False,
                "liquidity_grab_down": False,
                "stop_hunt_probability": 0.0,
            }

        last = self.candles[-1]

        high_level = rolling_max([c.high for c in self.candles[:-1]], lookback)
        low_level = rolling_min([c.low for c in self.candles[:-1]], lookback)

        grab_up = last.high > high_level and last.close < high_level
        grab_down = last.low < low_level and last.close > low_level

        probability = 0.0
        if grab_up or grab_down:
            probability = 70.0

        if last.upper_wick > last.body_abs * 2:
            probability += 15

        if last.lower_wick > last.body_abs * 2:
            probability += 15

        probability = clamp(probability, 0.0, 100.0)

        return {
            "liquidity_grab_up": grab_up,
            "liquidity_grab_down": grab_down,
            "stop_hunt_probability": probability,
        }


class RangeSensor(BaseSensor):

    def compute(self, lookback: int = 20) -> Dict[str, Any]:

        if len(self.candles) < lookback:
            return {
                "range_probability": 50.0,
                "compression_score": 0.0,
                "expansion_probability": 0.0,
            }

        highs = [c.high for c in self.candles[-lookback:]]
        lows = [c.low for c in self.candles[-lookback:]]

        total_range = max(highs) - min(lows)

        atr_like = rolling_mean(
            [c.high - c.low for c in self.candles[-lookback:]],
            lookback
        )

        compression = 0.0

        if total_range > 0:
            compression = clamp(
                (1.0 - (atr_like / total_range)) * 100.0,
                0.0,
                100.0,
            )

        range_probability = compression
        expansion_probability = 100.0 - compression

        return {
            "range_probability": range_probability,
            "compression_score": compression,
            "expansion_probability": expansion_probability,
        }


class ExhaustionSensor(BaseSensor):

    def compute(self) -> Dict[str, Any]:

        if len(self.candles) < 5:
            return {
                "bull_exhaustion": False,
                "bear_exhaustion": False,
                "momentum_weakness": False,
            }

        closes = [c.close for c in self.candles[-5:]]

        up_count = 0
        down_count = 0

        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                up_count += 1
            elif closes[i] < closes[i - 1]:
                down_count += 1

        last = self.candles[-1]

        bull_exhaustion = (
            up_count >= 4 and
            last.upper_wick > last.body_abs
        )

        bear_exhaustion = (
            down_count >= 4 and
            last.lower_wick > last.body_abs
        )

        momentum_weakness = bull_exhaustion or bear_exhaustion

        return {
            "bull_exhaustion": bull_exhaustion,
            "bear_exhaustion": bear_exhaustion,
            "momentum_weakness": momentum_weakness,
        }


class SnapshotBuilderD(SnapshotBuilderC):

    def build_final_sensor_snapshot(self) -> SensorSnapshot:

        base = self.build_strength_volatility_snapshot()

        breakout = BreakoutSensor(self.candles).compute()
        liquidity = LiquidityGrabSensor(self.candles).compute()
        ranges = RangeSensor(self.candles).compute()
        exhaustion = ExhaustionSensor(self.candles).compute()

        return replace(
            base,

            breakout_candidate=bool(
                breakout["breakout_candidate"]
            ),

            breakdown_candidate=bool(
                breakout["breakdown_candidate"]
            ),

            failed_breakout=bool(
                breakout["failed_breakout"]
            ),

            failed_breakdown=bool(
                breakout["failed_breakdown"]
            ),

            liquidity_grab_up=bool(
                liquidity["liquidity_grab_up"]
            ),

            liquidity_grab_down=bool(
                liquidity["liquidity_grab_down"]
            ),

            stop_hunt_probability=safe_float(
                liquidity["stop_hunt_probability"]
            ),

            range_probability=safe_float(
                ranges["range_probability"]
            ),

            compression_score=safe_float(
                ranges["compression_score"]
            ),

            expansion_probability=safe_float(
                ranges["expansion_probability"]
            ),

            bull_exhaustion=bool(
                exhaustion["bull_exhaustion"]
            ),

            bear_exhaustion=bool(
                exhaustion["bear_exhaustion"]
            ),

            momentum_weakness=bool(
                exhaustion["momentum_weakness"]
            ),
        )


def build_final_sensor_snapshot(
    symbol: str,
    timeframe: str,
    candles: Sequence[Any],
    market_context: Optional[Any] = None,
) -> SensorSnapshot:

    return SnapshotBuilderD(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        market_context=market_context,
    ).build_final_sensor_snapshot()


build_sensor_snapshot_part_d = build_final_sensor_snapshot


# =============================================================================
# Final public API
# =============================================================================

class FinalSensorBuilder(SnapshotBuilderD):
    """Canonical final builder used by analysis_engine.py and Movement Hunter layers."""
    pass


def build_sensor_snapshot(
    symbol: str,
    timeframe: str,
    candles: Sequence[Any],
    market_context: Optional[Any] = None,
) -> SensorSnapshot:
    """
    Build the final technical SensorSnapshot.

    This is the only public function other modules should normally call.
    It does not make any trade decision and does not output REAL/GHOST/REJECT.
    """
    return FinalSensorBuilder(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        market_context=market_context,
    ).build_final_sensor_snapshot()


def build_analysis_sensor_snapshot(
    symbol: str,
    timeframe: str,
    candles: Sequence[Any],
    market_context: Optional[Any] = None,
) -> SensorSnapshot:
    """Backward-compatible alias for build_sensor_snapshot."""
    return build_sensor_snapshot(symbol, timeframe, candles, market_context)


__all__ = [
    "CandleView",
    "SensorSnapshot",
    "SensorValidationResult",
    "SensorValidator",
    "BaseSensor",
    "SnapshotBuilder",
    "SnapshotBuilderB",
    "SnapshotBuilderC",
    "SnapshotBuilderD",
    "FinalSensorBuilder",
    "EMASensor",
    "VWAPSensor",
    "RSISensor",
    "MACDSensor",
    "ADXSensor",
    "ATRSensor",
    "VolumeSensor",
    "PowerSensor",
    "CandleStructureSensor",
    "BreakoutSensor",
    "LiquidityGrabSensor",
    "RangeSensor",
    "ExhaustionSensor",
    "build_sensor_snapshot",
    "build_analysis_sensor_snapshot",
    "build_base_sensor_snapshot",
    "build_trend_momentum_snapshot",
    "build_strength_volatility_snapshot",
    "build_final_sensor_snapshot",
]
