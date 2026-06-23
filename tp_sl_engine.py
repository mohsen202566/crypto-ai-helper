from __future__ import annotations

"""
19 - tp_sl_engine.py

Smart TP/SL engine for the locked Movement Hunter architecture.

Responsibilities:
- Calculate TP1, optional TP2, and SL for AIDecision.
- Adapt TP/SL to:
  ATR / volatility
  coin learning
  movement prediction
  market state
  trap/liquidity risk
  range/compression
  breakout survival / retest tolerance
  coin noise
- Keep SL not too close, especially around breakout/retest/liquidity zones.
- Decide TP mode:
  TP1_ONLY
  TP1_TP2

Strictly forbidden:
- No trade execution.
- No Toobit calls.
- No Telegram.
- No persistence.
- No REAL/GHOST/REJECT decision.
- No Paper mode.
- No Setup flow.

This file only calculates prices.
real_trade_manager.py opens orders later.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
import math
import time

from ai_decision_engine import AIDecision
from analysis_engine import AnalysisCandidate
from movement_hunter import MovementHunterResult
from trap_engine import TrapResult
from state_engine import StateResult
from confidence_engine import ConfidenceResult
from coin_learning import LearningSummary
from movement_predictor import MovementPredictionResult
from config import SETTINGS

try:
    from data_store import store
except Exception:  # keep compile-safe when data_store is unavailable in isolated tests
    store = None  # type: ignore


JsonDict = Dict[str, Any]

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"

TP_MODE_TP1_ONLY = "TP1_ONLY"
TP_MODE_TP1_TP2 = "TP1_TP2"

QUALITY_LOW = "LOW"
QUALITY_MEDIUM = "MEDIUM"
QUALITY_HIGH = "HIGH"


@dataclass(frozen=True)
class TPSLPlan:
    plan_id: str
    decision_id: str
    symbol: str
    direction: str
    entry: float
    tp1: float
    tp2: float
    sl: float
    tp_mode: str
    rr_tp1: float
    rr_tp2: float
    sl_distance_percent: float
    tp1_distance_percent: float
    tp2_distance_percent: float
    atr_percent: float
    quality_label: str

    # Fee-aware / capital-aware estimates. These are informational for reports
    # and validation; real order sizing is still done later by real_trade_manager.
    notional_usdt: float = 0.0
    estimated_tp1_gross_usdt: float = 0.0
    estimated_tp1_fee_usdt: float = 0.0
    estimated_tp1_net_usdt: float = 0.0
    min_required_net_profit_usdt: float = 0.0

    # Tradability / profit-quality estimates. These help downstream layers
    # avoid REAL trades where a coin technically moves, but the practical
    # TP is too small compared with fees/noise.
    estimated_sl_loss_usdt: float = 0.0
    estimated_rr_net: float = 0.0
    tradability_score: float = 50.0

    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    valid: bool = True

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class TPSLMultipliers:
    tp1_atr: float
    tp2_atr: float
    sl_atr: float
    min_sl_atr: float
    max_sl_atr: float

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


def clamp(value: float, low: float, high: float) -> float:
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return low
        return max(low, min(high, v))
    except Exception:
        return low


def normalize_direction(direction: str) -> str:
    d = str(direction or "").upper().strip()
    if d in {"LONG", "BUY"}:
        return DIRECTION_LONG
    if d in {"SHORT", "SELL"}:
        return DIRECTION_SHORT
    return d


def price_from_percent(entry: float, direction: str, percent: float) -> float:
    entry = safe_float(entry)
    p = safe_float(percent) / 100.0
    if normalize_direction(direction) == DIRECTION_LONG:
        return entry * (1.0 + p)
    return entry * (1.0 - p)


def sl_from_percent(entry: float, direction: str, percent: float) -> float:
    entry = safe_float(entry)
    p = safe_float(percent) / 100.0
    if normalize_direction(direction) == DIRECTION_LONG:
        return entry * (1.0 - p)
    return entry * (1.0 + p)


def distance_percent(entry: float, price: float) -> float:
    entry = safe_float(entry)
    price = safe_float(price)
    if entry <= 0 or price <= 0:
        return 0.0
    return abs(price - entry) / entry * 100.0


def rr_ratio(entry: float, tp: float, sl: float) -> float:
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return 0.0
    return reward / risk


def round_price(price: float, symbol: str = "") -> float:
    """
    Generic safe rounding. Exchange exact tick-size validation is done later
    inside tobit_client.py / real_trade_manager.py.

    Important fix for meme/very-low-price futures:
    PEPE/SHIB/BONK/FLOKI-style prices need more than 8 decimals. Rounding
    everything below 0.1 to 8 decimals can collapse TP/SL too close to Entry
    and Toobit rejects SHORT orders with: -3144 invalid short stop loss price.
    """
    price = safe_float(price)
    if price <= 0:
        return 0.0

    sym = str(symbol or "").upper()
    if any(x in sym for x in ("PEPE", "SHIB", "BONK", "FLOKI")) or price < 0.0001:
        decimals = 12
    elif price < 0.01:
        decimals = 10
    elif price >= 1000:
        decimals = 2
    elif price >= 100:
        decimals = 3
    elif price >= 10:
        decimals = 4
    elif price >= 1:
        decimals = 5
    elif price >= 0.1:
        decimals = 6
    else:
        decimals = 8
    return round(price, decimals)


def _min_distance_percent_for_symbol(symbol: str, entry: float) -> float:
    """Minimum practical TP/SL distance before exchange tick validation.

    This prevents tiny-price symbols from producing TP/SL values that are only
    one display tick away from entry after rounding. It is deliberately small
    enough for scalping, but large enough to avoid Toobit invalid stop prices.
    """
    sym = str(symbol or "").upper()
    entry = safe_float(entry)

    if any(x in sym for x in ("PEPE", "SHIB", "BONK", "FLOKI")):
        return 0.28
    if entry > 0 and entry < 0.0001:
        return 0.28
    if entry > 0 and entry < 0.01:
        return 0.22
    return 0.18


def _enforce_min_distances(entry: float, direction: str, tp1_percent: float, tp2_percent: float, sl_percent: float, symbol: str) -> Tuple[float, float, float, List[str]]:
    reasons: List[str] = []
    min_pct = _min_distance_percent_for_symbol(symbol, entry)

    if tp1_percent < min_pct:
        tp1_percent = min_pct
        reasons.append("TP1_MIN_DISTANCE_ENFORCED_FOR_EXCHANGE")
    if sl_percent < min_pct:
        sl_percent = min_pct
        reasons.append("SL_MIN_DISTANCE_ENFORCED_FOR_EXCHANGE")
    if tp2_percent > 0 and tp2_percent < tp1_percent * 1.25:
        tp2_percent = tp1_percent * 1.25
        reasons.append("TP2_MIN_DISTANCE_ENFORCED_FOR_EXCHANGE")

    return tp1_percent, tp2_percent, sl_percent, reasons




def _runtime_setting_float(key: str, default: float) -> float:
    """Read runtime_settings without creating a hard dependency on bot.py."""
    try:
        if store is not None:
            section = store().section("runtime_settings")
            if key in section:
                return safe_float(section.get(key), default)
    except Exception:
        pass
    return default


def _runtime_setting_int(key: str, default: int) -> int:
    try:
        return int(round(_runtime_setting_float(key, float(default))))
    except Exception:
        return default


def _trade_margin_usdt() -> float:
    default = safe_float(getattr(SETTINGS.trading, "margin_usdt", 5.0), 5.0)
    # Support both names used by bot.py / real_trade_manager.py.
    return max(0.0, _runtime_setting_float("margin_usdt", _runtime_setting_float("trade_margin_usdt", default)))


def _trade_leverage() -> int:
    default = int(safe_float(getattr(SETTINGS.trading, "leverage", 10), 10))
    return max(1, _runtime_setting_int("leverage", default))


def _fee_rate_per_side() -> float:
    # Conservative default taker fee. Config can override with trading.taker_fee_rate
    # or tp.fee_rate_per_side.
    cfg = safe_float(getattr(SETTINGS.tp, "fee_rate_per_side", 0.0), 0.0)
    if cfg <= 0:
        cfg = safe_float(getattr(SETTINGS.trading, "taker_fee_rate", 0.0006), 0.0006)
    return max(0.0, cfg)


def _min_net_profit_usdt() -> float:
    # User preference: avoid TP where profit is eaten by fees. Default 10 cents.
    cfg = safe_float(getattr(SETTINGS.tp, "min_net_profit_usdt", 0.10), 0.10)
    return max(0.0, cfg)


def _notional_usdt() -> float:
    return _trade_margin_usdt() * float(_trade_leverage())


def _estimated_fee_usdt(notional: float) -> float:
    # Entry + TP exit. This is intentionally conservative.
    return max(0.0, safe_float(notional) * _fee_rate_per_side() * 2.0)


def _gross_profit_usdt(notional: float, tp_percent: float) -> float:
    return max(0.0, safe_float(notional) * safe_float(tp_percent) / 100.0)


def _min_tp_percent_for_net_profit() -> Tuple[float, float, float, float, List[str]]:
    """Return minimum TP percent required to clear fee + min net profit."""
    reasons: List[str] = []
    notional = _notional_usdt()
    fee = _estimated_fee_usdt(notional)
    min_net = _min_net_profit_usdt()
    if notional <= 0:
        return 0.0, notional, fee, min_net, ["FEE_AWARE_SKIPPED_NO_NOTIONAL"]
    required = (fee + min_net) / notional * 100.0
    reasons.append("FEE_AWARE_MIN_NET_PROFIT_CHECK")
    return required, notional, fee, min_net, reasons


def _iter_numeric_levels(value: Any) -> List[float]:
    levels: List[float] = []
    if value is None:
        return levels
    if isinstance(value, (int, float)):
        v = safe_float(value)
        if v > 0:
            levels.append(v)
        return levels
    if isinstance(value, dict):
        for k in ("price", "level", "value", "low", "high", "support", "resistance"):
            if k in value:
                levels.extend(_iter_numeric_levels(value.get(k)))
        return levels
    if isinstance(value, (list, tuple, set)):
        for item in value:
            levels.extend(_iter_numeric_levels(item))
        return levels
    return levels


def _snapshot_levels(candidate: AnalysisCandidate, side: str) -> List[float]:
    """Extract support/resistance/swing levels from whatever analysis_layers provides."""
    s = candidate.sensor_snapshot
    if side == "support":
        names = (
            "nearest_support", "support", "support_price", "support_level", "supports",
            "swing_low", "last_swing_low", "recent_low", "low_20", "low_50",
            "demand_zone", "demand_zone_low", "demand_low", "sr_support",
        )
    else:
        names = (
            "nearest_resistance", "resistance", "resistance_price", "resistance_level", "resistances",
            "swing_high", "last_swing_high", "recent_high", "high_20", "high_50",
            "supply_zone", "supply_zone_high", "supply_high", "sr_resistance",
        )
    out: List[float] = []
    for name in names:
        out.extend(_iter_numeric_levels(getattr(s, name, None)))
    # Deduplicate while preserving meaningful levels only.
    dedup: List[float] = []
    for v in out:
        if v > 0 and all(abs(v - x) / max(v, x, 1e-12) > 0.00001 for x in dedup):
            dedup.append(v)
    return dedup


def _nearest_above(entry: float, levels: List[float]) -> Optional[float]:
    above = [v for v in levels if v > entry]
    return min(above) if above else None


def _nearest_below(entry: float, levels: List[float]) -> Optional[float]:
    below = [v for v in levels if 0 < v < entry]
    return max(below) if below else None


def _apply_sr_liquidity_rules(
    entry: float,
    direction: str,
    tp1_percent: float,
    tp2_percent: float,
    sl_percent: float,
    atr_percent: float,
    candidate: AnalysisCandidate,
) -> Tuple[float, float, float, List[str], List[str]]:
    """Make TP cautious near SR and put SL beyond the nearest invalidation level.

    LONG: SL should be below support; TP should normally be before resistance.
    SHORT: SL should be above resistance; TP should normally be before support.
    """
    reasons: List[str] = []
    warnings: List[str] = []
    d = normalize_direction(direction)
    buffer_pct = max(0.06, safe_float(atr_percent) * 0.16)

    supports = _snapshot_levels(candidate, "support")
    resistances = _snapshot_levels(candidate, "resistance")

    if d == DIRECTION_LONG:
        support = _nearest_below(entry, supports)
        resistance = _nearest_above(entry, resistances)
        if support:
            support_dist = distance_percent(entry, support)
            target_sl = support_dist + buffer_pct
            if target_sl > sl_percent:
                sl_percent = target_sl
                reasons.append("SL_PLACED_BELOW_SUPPORT_WITH_BUFFER")
        if resistance:
            res_dist = distance_percent(entry, resistance)
            # Take profit before resistance so TP1 is hit faster instead of waiting
            # for a clean break through resistance.
            cautious_tp = max(0.01, res_dist - buffer_pct)
            if cautious_tp > 0 and cautious_tp < tp1_percent:
                tp1_percent = cautious_tp
                reasons.append("TP1_CAUTIOUS_BEFORE_RESISTANCE")
            if tp2_percent > 0 and res_dist > 0:
                tp2_cap = max(tp1_percent * 1.15, res_dist + buffer_pct * 0.50)
                if tp2_percent > tp2_cap:
                    tp2_percent = tp2_cap
                    reasons.append("TP2_CAPPED_AROUND_RESISTANCE_ZONE")
    elif d == DIRECTION_SHORT:
        support = _nearest_below(entry, supports)
        resistance = _nearest_above(entry, resistances)
        if resistance:
            resistance_dist = distance_percent(entry, resistance)
            target_sl = resistance_dist + buffer_pct
            if target_sl > sl_percent:
                sl_percent = target_sl
                reasons.append("SL_PLACED_ABOVE_RESISTANCE_WITH_BUFFER")
        if support:
            sup_dist = distance_percent(entry, support)
            cautious_tp = max(0.01, sup_dist - buffer_pct)
            if cautious_tp > 0 and cautious_tp < tp1_percent:
                tp1_percent = cautious_tp
                reasons.append("TP1_CAUTIOUS_BEFORE_SUPPORT")
            if tp2_percent > 0 and sup_dist > 0:
                tp2_cap = max(tp1_percent * 1.15, sup_dist + buffer_pct * 0.50)
                if tp2_percent > tp2_cap:
                    tp2_percent = tp2_cap
                    reasons.append("TP2_CAPPED_AROUND_SUPPORT_ZONE")

    return tp1_percent, tp2_percent, sl_percent, reasons, warnings


def _apply_fee_aware_tp_floor(tp1_percent: float, tp2_percent: float) -> Tuple[float, float, float, float, float, float, List[str], List[str]]:
    """Ensure TP1 gross profit covers estimated fees plus minimum net profit."""
    reasons: List[str] = []
    warnings: List[str] = []
    required_pct, notional, fee, min_net, r = _min_tp_percent_for_net_profit()
    reasons.extend(r)
    if required_pct > 0 and tp1_percent < required_pct:
        tp1_percent = required_pct
        reasons.append("TP1_RAISED_TO_COVER_FEES_AND_MIN_NET_PROFIT")
    if tp2_percent > 0 and tp2_percent < tp1_percent * 1.20:
        tp2_percent = tp1_percent * 1.20
        reasons.append("TP2_RAISED_AFTER_FEE_AWARE_TP1")
    gross = _gross_profit_usdt(notional, tp1_percent)
    net = gross - fee
    if net < min_net:
        warnings.append("TP1_NET_PROFIT_BELOW_MIN_AFTER_FEES")
    return tp1_percent, tp2_percent, notional, gross, fee, min_net, reasons, warnings


def _estimated_loss_usdt(notional: float, sl_percent: float) -> float:
    return max(0.0, safe_float(notional) * safe_float(sl_percent) / 100.0)


def _net_rr(gross_profit: float, fee: float, loss: float) -> float:
    net_profit = safe_float(gross_profit) - safe_float(fee)
    if loss <= 0:
        return 0.0
    return net_profit / loss


def _profit_quality_score(
    tp1_percent: float,
    sl_percent: float,
    notional: float,
    gross: float,
    fee: float,
    min_net: float,
    atr_percent: float,
    candidate: AnalysisCandidate,
    prediction: MovementPredictionResult,
    learning: Optional[LearningSummary],
) -> Tuple[float, float, float, List[str], List[str]]:
    """Score whether this TP/SL plan is worth real capital.

    A coin can be technically active but still poor for REAL trading when the
    expected TP is only a few cents after fees or when TP is too close to normal
    candle noise. This score is informational for AI/reporting and is also used
    by the validator to mark truly useless plans invalid.
    """
    reasons: List[str] = []
    warnings: List[str] = []

    est_loss = _estimated_loss_usdt(notional, sl_percent)
    net_profit = safe_float(gross) - safe_float(fee)
    net_rr = _net_rr(gross, fee, est_loss)

    required_net_score = clamp((net_profit / max(min_net, 0.01)) * 35.0, 0.0, 40.0)
    fee_cover_score = clamp((safe_float(tp1_percent) / max(((fee + min_net) / max(notional, 1e-9) * 100.0), 1e-9)) * 35.0, 0.0, 40.0)
    rr_score = clamp(net_rr * 25.0, 0.0, 25.0)

    # TP must be bigger than normal micro-noise. For very tight coins, this
    # prevents two tiny ticks deciding the whole trade.
    noise_ratio = safe_float(tp1_percent) / max(safe_float(atr_percent), 0.05)
    noise_score = clamp(noise_ratio * 12.0, 0.0, 18.0)

    s = candidate.sensor_snapshot
    volume_bonus = 0.0
    rel_vol = safe_float(getattr(s, "relative_volume", 0.0), 0.0)
    if rel_vol >= 1.8 or bool(getattr(s, "volume_spike", False)):
        volume_bonus += 6.0
        reasons.append("PROFIT_QUALITY_VOLUME_SUPPORT")
    elif rel_vol > 0 and rel_vol < 0.65:
        volume_bonus -= 8.0
        warnings.append("PROFIT_QUALITY_LOW_VOLUME")

    # If learning says this exact condition catches early moves, tolerate a
    # slightly more ambitious TP. Otherwise keep low-value scalps in GHOST.
    try:
        early = safe_float(getattr(learning, "early_success_rate", 0.0), 0.0) if learning is not None else 0.0
        premove = safe_float(getattr(learning, "premove_success_rate", 0.0), 0.0) if learning is not None else 0.0
        timing = safe_float(getattr(learning, "timing_score", 50.0), 50.0) if learning is not None else 50.0
        if early >= 45.0 or premove >= 45.0 or timing >= 68.0:
            volume_bonus += 5.0
            reasons.append("PROFIT_QUALITY_EARLY_LEARNING_SUPPORT")
    except Exception:
        pass

    score = clamp(required_net_score + fee_cover_score + rr_score + noise_score + volume_bonus, 0.0, 100.0)

    if net_profit < min_net:
        warnings.append("PROFIT_QUALITY_NET_BELOW_MIN")
    if net_rr < 0.25:
        warnings.append("PROFIT_QUALITY_NET_RR_TOO_LOW")
    if noise_ratio < 0.45:
        warnings.append("PROFIT_QUALITY_TP_INSIDE_NORMAL_NOISE")
    if score < 35.0:
        warnings.append("PROFIT_QUALITY_TOO_LOW_FOR_REAL")
        reasons.append("PROFIT_QUALITY_GHOST_OR_BLOCK")
    elif score >= 65.0:
        reasons.append("PROFIT_QUALITY_REAL_USABLE")
    else:
        reasons.append("PROFIT_QUALITY_BORDERLINE")

    return score, est_loss, net_rr, reasons, warnings

class BaseMultiplierEngine:
    """Base scalping multipliers for 5M-15M Movement Hunter."""

    def base(self) -> TPSLMultipliers:
        min_sl = safe_float(getattr(SETTINGS.tp, "min_sl_atr_multiplier", 1.0), 1.0)
        max_sl = safe_float(getattr(SETTINGS.tp, "max_sl_atr_multiplier", 2.6), 2.6)
        return TPSLMultipliers(
            tp1_atr=0.85,
            tp2_atr=1.55,
            sl_atr=1.15,
            min_sl_atr=min_sl,
            max_sl_atr=max_sl,
        )


class VolatilityAdjustmentEngine:
    """Adjusts multipliers based on ATR/volatility/range."""

    def adjust(self, m: TPSLMultipliers, candidate: AnalysisCandidate, state: StateResult) -> Tuple[TPSLMultipliers, List[str]]:
        s = candidate.sensor_snapshot
        reasons: List[str] = []

        tp1 = m.tp1_atr
        tp2 = m.tp2_atr
        sl = m.sl_atr

        if s.atr_explosion:
            tp1 += 0.10
            tp2 += 0.25
            sl += 0.20
            reasons.append("ATR_EXPLOSION_WIDER_PLAN")
        elif s.atr_expansion == "EXPANDING":
            tp1 += 0.05
            tp2 += 0.15
            sl += 0.10
            reasons.append("ATR_EXPANDING_ADJUSTMENT")

        if state.range_probability >= 65:
            tp1 -= 0.10
            tp2 -= 0.25
            sl += 0.10
            reasons.append("RANGE_TIGHTER_TP_WIDER_SL")
        elif state.range_probability <= 30:
            tp2 += 0.10
            reasons.append("LOW_RANGE_ALLOW_TP2")

        return TPSLMultipliers(
            tp1_atr=clamp(tp1, 0.45, 1.40),
            tp2_atr=clamp(tp2, 0.90, 2.50),
            sl_atr=clamp(sl, m.min_sl_atr, m.max_sl_atr),
            min_sl_atr=m.min_sl_atr,
            max_sl_atr=m.max_sl_atr,
        ), reasons


class TrapLiquidityAdjustmentEngine:
    """Avoids SL being too close to liquidity/retest noise."""

    def adjust(self, m: TPSLMultipliers, trap: TrapResult, candidate: AnalysisCandidate) -> Tuple[TPSLMultipliers, List[str]]:
        s = candidate.sensor_snapshot
        reasons: List[str] = []

        tp1 = m.tp1_atr
        tp2 = m.tp2_atr
        sl = m.sl_atr

        if trap.trap_risk >= 65:
            tp1 -= 0.10
            tp2 -= 0.30
            sl += 0.20
            reasons.append("HIGH_TRAP_CAUTION")
        elif trap.trap_risk >= 40:
            sl += 0.10
            reasons.append("MEDIUM_TRAP_SL_TOLERANCE")

        if trap.liquidity_risk >= 60:
            sl += 0.20
            reasons.append("LIQUIDITY_RISK_WIDER_SL")

        if s.breakout_candidate or s.breakdown_candidate:
            sl += 0.12
            reasons.append("BREAKOUT_RETEST_TOLERANCE")

        if s.failed_breakout or s.failed_breakdown:
            tp1 -= 0.08
            tp2 -= 0.20
            reasons.append("FAILED_BREAK_CAUTION")

        return TPSLMultipliers(
            tp1_atr=clamp(tp1, 0.40, 1.35),
            tp2_atr=clamp(tp2, 0.75, 2.40),
            sl_atr=clamp(sl, m.min_sl_atr, m.max_sl_atr),
            min_sl_atr=m.min_sl_atr,
            max_sl_atr=m.max_sl_atr,
        ), reasons


class PredictionLearningAdjustmentEngine:
    """Uses prediction and coin learning to adapt targets."""

    def adjust(
        self,
        m: TPSLMultipliers,
        prediction: MovementPredictionResult,
        learning: Optional[LearningSummary],
    ) -> Tuple[TPSLMultipliers, List[str]]:
        reasons: List[str] = []
        tp1 = m.tp1_atr
        tp2 = m.tp2_atr
        sl = m.sl_atr

        if prediction.predicted_phase == "PRE_START":
            tp2 += 0.20
            reasons.append("PRE_START_ALLOW_MORE_TP2")
        elif prediction.predicted_phase == "START":
            tp2 += 0.10
            reasons.append("START_PHASE_TP2_OK")
        elif prediction.predicted_phase == "LATE":
            tp1 -= 0.18
            tp2 -= 0.45
            sl += 0.12
            reasons.append("LATE_CONSERVATIVE_TP_STRONGER")
        elif prediction.predicted_phase == "RANGE":
            tp1 -= 0.12
            tp2 -= 0.35
            sl += 0.10
            reasons.append("RANGE_CONSERVATIVE_TP")

        if prediction.expected_move_percent > 0:
            # Convert expected percent into soft ATR estimate when enough memory exists.
            if prediction.sample_count >= 5:
                if prediction.expected_move_percent > 1.2:
                    tp2 += 0.15
                    reasons.append("MEMORY_EXPECTS_LARGER_MOVE")
                elif prediction.expected_move_percent < 0.45:
                    tp1 -= 0.10
                    tp2 -= 0.25
                    reasons.append("MEMORY_EXPECTS_SMALL_MOVE")

        if learning is not None:
            if learning.risk_label == "FAVORABLE_CONDITION" and learning.win_rate >= 65:
                tp2 += 0.15
                sl -= 0.05
                reasons.append("LEARNING_FAVORABLE_CONDITION")
            elif learning.risk_label == "RISKY_CONDITION":
                tp1 -= 0.08
                tp2 -= 0.30
                sl += 0.10
                reasons.append("LEARNING_RISKY_CONDITION")

            if learning.avg_mae_percent > learning.avg_mfe_percent and learning.sample_count >= 5:
                sl += 0.15
                tp2 -= 0.15
                reasons.append("LEARNING_HIGH_ADVERSE_NOISE")

        return TPSLMultipliers(
            tp1_atr=clamp(tp1, 0.40, 1.45),
            tp2_atr=clamp(tp2, 0.70, 2.70),
            sl_atr=clamp(sl, m.min_sl_atr, m.max_sl_atr),
            min_sl_atr=m.min_sl_atr,
            max_sl_atr=m.max_sl_atr,
        ), reasons


class TPModeEngine:
    """Decides whether TP2 should be used."""

    def decide(
        self,
        decision: AIDecision,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
        confidence: ConfidenceResult,
        prediction: MovementPredictionResult,
        learning: Optional[LearningSummary],
    ) -> Tuple[str, List[str]]:
        reasons: List[str] = []

        if not bool(getattr(SETTINGS.tp, "tp2_enabled", True)):
            reasons.append("TP2_DISABLED_BY_CONFIG")
            return TP_MODE_TP1_ONLY, reasons

        strong = (
            decision.ai_score >= 70
            and movement.continuation_probability >= 60
            and confidence.confidence_score >= 60
            and prediction.movement_probability >= 60
            and trap.trap_risk < 60
            and state.market_state not in {"RANGE", "EXHAUSTION", "LATE"}
            and prediction.predicted_phase in {"PRE_START", "START", "MID"}
        )

        if learning is not None and learning.risk_label == "RISKY_CONDITION":
            strong = False
            reasons.append("TP2_BLOCKED_BY_RISKY_LEARNING")

        if strong:
            reasons.append("TP2_ALLOWED_STRONG_SIGNAL")
            return TP_MODE_TP1_TP2, reasons

        reasons.append("TP1_ONLY_CONSERVATIVE")
        return TP_MODE_TP1_ONLY, reasons


class TPSLValidator:
    """Validates price relationships and minimum RR."""

    def validate(self, plan: TPSLPlan) -> Tuple[bool, List[str]]:
        warnings: List[str] = []

        if plan.entry <= 0 or plan.tp1 <= 0 or plan.sl <= 0:
            warnings.append("INVALID_PRICE_IN_TP_SL_PLAN")
            return False, warnings

        if plan.direction == DIRECTION_LONG:
            if not (plan.sl < plan.entry < plan.tp1):
                warnings.append("INVALID_LONG_TP_SL_RELATION")
                return False, warnings
            if plan.tp_mode == TP_MODE_TP1_TP2 and plan.tp2 > 0 and not (plan.tp2 > plan.tp1):
                warnings.append("INVALID_LONG_TP2_RELATION")
                return False, warnings
        elif plan.direction == DIRECTION_SHORT:
            if not (plan.tp1 < plan.entry < plan.sl):
                warnings.append("INVALID_SHORT_TP_SL_RELATION")
                return False, warnings
            if plan.tp_mode == TP_MODE_TP1_TP2 and plan.tp2 > 0 and not (plan.tp2 < plan.tp1):
                warnings.append("INVALID_SHORT_TP2_RELATION")
                return False, warnings
        else:
            warnings.append("INVALID_DIRECTION")
            return False, warnings

        min_rr = safe_float(getattr(SETTINGS.tp, "min_rr", 1.1), 1.1)
        if plan.rr_tp1 < min_rr * 0.55:
            warnings.append("TP1_RR_LOW_BUT_ALLOWED_FOR_SCALP")
        if plan.tp_mode == TP_MODE_TP1_TP2 and plan.rr_tp2 < min_rr:
            warnings.append("TP2_RR_BELOW_MIN")

        if plan.sl_distance_percent <= 0:
            warnings.append("SL_DISTANCE_ZERO")
            return False, warnings

        if plan.min_required_net_profit_usdt > 0 and plan.estimated_tp1_net_usdt < plan.min_required_net_profit_usdt:
            warnings.append("TP1_NET_PROFIT_BELOW_MIN_AFTER_FEES")
            return False, warnings

        if safe_float(getattr(plan, "tradability_score", 50.0), 50.0) < 25.0:
            warnings.append("TP_SL_TRADABILITY_SCORE_TOO_LOW")
            return False, warnings

        return True, warnings


class TPSLEngine:
    """Main smart TP/SL engine."""

    def __init__(self):
        self.base = BaseMultiplierEngine()
        self.volatility = VolatilityAdjustmentEngine()
        self.trap = TrapLiquidityAdjustmentEngine()
        self.prediction_learning = PredictionLearningAdjustmentEngine()
        self.tp_mode = TPModeEngine()
        self.validator = TPSLValidator()

    def build_plan(
        self,
        decision: AIDecision,
        candidate: AnalysisCandidate,
        movement: MovementHunterResult,
        trap: TrapResult,
        state: StateResult,
        confidence: ConfidenceResult,
        prediction: MovementPredictionResult,
        learning: Optional[LearningSummary] = None,
    ) -> TPSLPlan:
        reasons: List[str] = []
        warnings: List[str] = []

        direction = normalize_direction(decision.direction)
        entry = safe_float(decision.entry or candidate.sensor_snapshot.price)
        atr_percent = safe_float(candidate.sensor_snapshot.atr_percent)

        if atr_percent <= 0:
            # Fallback when ATR is unavailable. Conservative scalping default.
            atr_percent = 0.55
            warnings.append("ATR_PERCENT_FALLBACK_USED")

        m = self.base.base()
        m, r = self.volatility.adjust(m, candidate, state)
        reasons.extend(r)

        m, r = self.trap.adjust(m, trap, candidate)
        reasons.extend(r)

        m, r = self.prediction_learning.adjust(m, prediction, learning)
        reasons.extend(r)

        # Convert ATR multipliers to percentage distances.
        tp1_percent = atr_percent * m.tp1_atr
        tp2_percent = atr_percent * m.tp2_atr
        sl_percent = atr_percent * m.sl_atr

        # First align with support/resistance and liquidity structure:
        # LONG => SL below support, TP before resistance.
        # SHORT => SL above resistance, TP before support.
        tp1_percent, tp2_percent, sl_percent, r, w = _apply_sr_liquidity_rules(
            entry=entry,
            direction=direction,
            tp1_percent=tp1_percent,
            tp2_percent=tp2_percent,
            sl_percent=sl_percent,
            atr_percent=atr_percent,
            candidate=candidate,
        )
        reasons.extend(r)
        warnings.extend(w)

        # Then ensure TP1 is not smaller than estimated round-trip fees +
        # minimum desired net profit. This prevents useless TPs such as 6 cents
        # profit against 6 cents fee.
        tp1_percent, tp2_percent, notional_usdt, est_gross, est_fee, min_net, r, w = _apply_fee_aware_tp_floor(
            tp1_percent=tp1_percent,
            tp2_percent=tp2_percent,
        )
        reasons.extend(r)
        warnings.extend(w)

        # Hard minimum SL distance so breakout/retest noise doesn't instantly stop out.
        min_sl_percent = max(atr_percent * m.min_sl_atr, 0.18)
        sl_percent = max(sl_percent, min_sl_percent)

        # Avoid absurd SL in sudden volatility.
        max_sl_percent = max(atr_percent * m.max_sl_atr, min_sl_percent)
        sl_percent = min(sl_percent, max_sl_percent)

        # TP1 should remain reachable for 5M-15M scalping.
        min_exchange_distance = _min_distance_percent_for_symbol(decision.symbol, entry)
        tp1_percent = clamp(tp1_percent, min_exchange_distance, max(1.50, atr_percent * 1.50))
        tp2_percent = clamp(tp2_percent, tp1_percent * 1.25, max(3.50, atr_percent * 2.80))
        sl_percent = max(sl_percent, min_exchange_distance)

        tp1_percent, tp2_percent, sl_percent, r = _enforce_min_distances(
            entry=entry,
            direction=direction,
            tp1_percent=tp1_percent,
            tp2_percent=tp2_percent,
            sl_percent=sl_percent,
            symbol=decision.symbol,
        )
        reasons.extend(r)

        # Recalculate fee/net/profit quality after all clamps/SR adjustments.
        # This keeps plan metadata consistent with the final TP1 percent.
        notional_usdt = _notional_usdt()
        est_fee = _estimated_fee_usdt(notional_usdt)
        min_net = _min_net_profit_usdt()
        est_gross = _gross_profit_usdt(notional_usdt, tp1_percent)
        tradability_score, est_loss, estimated_rr_net, r, w = _profit_quality_score(
            tp1_percent=tp1_percent,
            sl_percent=sl_percent,
            notional=notional_usdt,
            gross=est_gross,
            fee=est_fee,
            min_net=min_net,
            atr_percent=atr_percent,
            candidate=candidate,
            prediction=prediction,
            learning=learning,
        )
        reasons.extend(r)
        warnings.extend(w)

        mode, r = self.tp_mode.decide(
            decision=decision,
            movement=movement,
            trap=trap,
            state=state,
            confidence=confidence,
            prediction=prediction,
            learning=learning,
        )
        reasons.extend(r)

        tp1 = round_price(price_from_percent(entry, direction, tp1_percent), decision.symbol)
        tp2 = round_price(price_from_percent(entry, direction, tp2_percent), decision.symbol) if mode == TP_MODE_TP1_TP2 else 0.0
        sl = round_price(sl_from_percent(entry, direction, sl_percent), decision.symbol)

        rr1 = rr_ratio(entry, tp1, sl)
        rr2 = rr_ratio(entry, tp2, sl) if tp2 > 0 else 0.0

        quality_label = QUALITY_MEDIUM
        if decision.ai_score >= 75 and prediction.movement_probability >= 70 and trap.trap_risk < 45:
            quality_label = QUALITY_HIGH
        elif decision.ai_score < 55 or trap.trap_risk >= 65 or state.market_state in {"RANGE", "EXHAUSTION"}:
            quality_label = QUALITY_LOW

        plan = TPSLPlan(
            plan_id=f"tpsl_{uuid4().hex}",
            decision_id=decision.decision_id,
            symbol=decision.symbol,
            direction=direction,
            entry=round_price(entry, decision.symbol),
            tp1=tp1,
            tp2=tp2,
            sl=sl,
            tp_mode=mode,
            rr_tp1=rr1,
            rr_tp2=rr2,
            sl_distance_percent=distance_percent(entry, sl),
            tp1_distance_percent=distance_percent(entry, tp1),
            tp2_distance_percent=distance_percent(entry, tp2) if tp2 > 0 else 0.0,
            atr_percent=atr_percent,
            quality_label=quality_label,
            notional_usdt=safe_float(locals().get("notional_usdt", 0.0)),
            estimated_tp1_gross_usdt=safe_float(locals().get("est_gross", 0.0)),
            estimated_tp1_fee_usdt=safe_float(locals().get("est_fee", 0.0)),
            estimated_tp1_net_usdt=safe_float(locals().get("est_gross", 0.0)) - safe_float(locals().get("est_fee", 0.0)),
            min_required_net_profit_usdt=safe_float(locals().get("min_net", 0.0)),
            estimated_sl_loss_usdt=safe_float(locals().get("est_loss", 0.0)),
            estimated_rr_net=safe_float(locals().get("estimated_rr_net", 0.0)),
            tradability_score=safe_float(locals().get("tradability_score", 50.0), 50.0),
            reason_codes=tuple(dict.fromkeys(reasons)),
            warnings=tuple(warnings),
            valid=True,
        )

        valid, validation_warnings = self.validator.validate(plan)
        all_warnings = tuple(dict.fromkeys(list(plan.warnings) + validation_warnings))
        if not valid or validation_warnings:
            plan = TPSLPlan(**{**plan.to_dict(), "valid": valid, "warnings": all_warnings})

        return plan


_default_engine: Optional[TPSLEngine] = None


def engine() -> TPSLEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = TPSLEngine()
    return _default_engine


def build_tp_sl_plan(
    decision: AIDecision,
    candidate: AnalysisCandidate,
    movement: MovementHunterResult,
    trap: TrapResult,
    state: StateResult,
    confidence: ConfidenceResult,
    prediction: MovementPredictionResult,
    learning: Optional[LearningSummary] = None,
) -> TPSLPlan:
    return engine().build_plan(
        decision=decision,
        candidate=candidate,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        prediction=prediction,
        learning=learning,
    )


def apply_tp_sl_to_decision(decision: AIDecision, plan: TPSLPlan) -> AIDecision:
    """
    Return a new AIDecision with TP/SL fields filled.
    No trade execution happens here.
    """
    data = decision.to_dict()
    data.update(
        {
            "tp1": plan.tp1,
            "tp2": plan.tp2,
            "sl": plan.sl,
            "tp_mode": plan.tp_mode,
            "warnings": tuple(dict.fromkeys(list(decision.warnings) + list(plan.warnings))),
            "reason_codes": tuple(dict.fromkeys(list(decision.reason_codes) + list(plan.reason_codes))),
            "meta": {
                **dict(decision.meta),
                "tp_sl_plan": plan.to_dict(),
                "tradability_score": getattr(plan, "tradability_score", 50.0),
                "estimated_tp1_net_usdt": getattr(plan, "estimated_tp1_net_usdt", 0.0),
            },
        }
    )
    return AIDecision(**data)


def tp_sl_engine(
    decision: AIDecision,
    candidate: AnalysisCandidate,
    movement: MovementHunterResult,
    trap: TrapResult,
    state: StateResult,
    confidence: ConfidenceResult,
    prediction: MovementPredictionResult,
    learning: Optional[LearningSummary] = None,
) -> TPSLPlan:
    return build_tp_sl_plan(
        decision=decision,
        candidate=candidate,
        movement=movement,
        trap=trap,
        state=state,
        confidence=confidence,
        prediction=prediction,
        learning=learning,
    )
