from __future__ import annotations

"""
19 - tp_sl_engine.py

Light TP/SL engine for the simplified Level 1 / 5M crypto futures bot.

Locked goals:
- TP1 minimum = 0.95 ATR.
- SL minimum = 1.25 ATR.
- AI may make TP/SL wider/smarter, but never smaller than those ATR floors.
- TP should not sit exactly on support/resistance.
- SL should be behind support/resistance with a small buffer.
- Minimum useful distance for tiny-price symbols is enforced.
- Fee/net-profit check is included.
- No trap/state/confidence/correlation/meta/movement_hunter dependency.
- No REAL/GHOST/REJECT decision.
- No Toobit, no Telegram, no persistence, no paper/setup flow.

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
from coin_learning import LearningSummary
from movement_predictor import MovementPredictionResult
from config import SETTINGS


JsonDict = Dict[str, Any]

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"

TP_MODE_TP1_ONLY = "TP1_ONLY"
TP_MODE_TP1_TP2 = "TP1_TP2"

QUALITY_LOW = "LOW"
QUALITY_MEDIUM = "MEDIUM"
QUALITY_HIGH = "HIGH"

MIN_TP1_ATR = 0.95
MIN_SL_ATR = 1.25
DEFAULT_TP2_ATR = 1.65


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
    tp1_distance_percent: float
    tp2_distance_percent: float
    sl_distance_percent: float
    atr_percent: float
    quality_label: str

    notional_usdt: float = 0.0
    estimated_tp1_gross_usdt: float = 0.0
    estimated_tp1_fee_usdt: float = 0.0
    estimated_tp1_net_usdt: float = 0.0
    estimated_sl_loss_usdt: float = 0.0
    estimated_rr_net: float = 0.0
    min_required_net_profit_usdt: float = 0.0
    tradability_score: float = 50.0

    reason_codes: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    valid: bool = True

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
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, safe_float(value, low)))


def normalize_direction(direction: str) -> str:
    d = str(direction or "").upper().strip()
    if d in {"LONG", "BUY"}:
        return DIRECTION_LONG
    if d in {"SHORT", "SELL"}:
        return DIRECTION_SHORT
    return d


def obj_value(obj: Optional[Any], key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def obj_float(obj: Optional[Any], key: str, default: float = 0.0) -> float:
    return safe_float(obj_value(obj, key, default), default)


def price_from_percent(entry: float, direction: str, percent: float) -> float:
    entry = safe_float(entry)
    pct = safe_float(percent) / 100.0
    if normalize_direction(direction) == DIRECTION_LONG:
        return entry * (1.0 + pct)
    return entry * (1.0 - pct)


def sl_from_percent(entry: float, direction: str, percent: float) -> float:
    entry = safe_float(entry)
    pct = safe_float(percent) / 100.0
    if normalize_direction(direction) == DIRECTION_LONG:
        return entry * (1.0 - pct)
    return entry * (1.0 + pct)


def distance_percent(entry: float, price: float) -> float:
    entry = safe_float(entry)
    price = safe_float(price)
    if entry <= 0 or price <= 0:
        return 0.0
    return abs(price - entry) / entry * 100.0


def rr_ratio(entry: float, tp: float, sl: float) -> float:
    risk = abs(safe_float(entry) - safe_float(sl))
    reward = abs(safe_float(tp) - safe_float(entry))
    if risk <= 0:
        return 0.0
    return reward / risk


def round_price(price: float, symbol: str = "") -> float:
    price = safe_float(price)
    if price <= 0:
        return 0.0

    sym = str(symbol or "").upper()
    if any(x in sym for x in ("PEPE", "BONK", "SHIB", "FLOKI")) or price < 0.0001:
        decimals = 12
    elif price < 0.01:
        decimals = 10
    elif price < 0.1:
        decimals = 8
    elif price < 1:
        decimals = 6
    elif price < 10:
        decimals = 5
    elif price < 100:
        decimals = 4
    elif price < 1000:
        decimals = 3
    else:
        decimals = 2

    return round(price, decimals)


def min_distance_percent_for_symbol(symbol: str, entry: float) -> float:
    sym = str(symbol or "").upper()
    entry = safe_float(entry)

    if any(x in sym for x in ("PEPE", "BONK", "SHIB", "FLOKI")):
        return 0.28
    if 0 < entry < 0.01:
        return 0.22
    return 0.18


def settings_float(path: str, default: float) -> float:
    try:
        obj: Any = SETTINGS
        for part in str(path).split("."):
            obj = getattr(obj, part)
        return safe_float(obj, default)
    except Exception:
        return default


def trade_margin_usdt() -> float:
    return max(0.0, settings_float("trading.margin_usdt", 5.0))


def trade_leverage() -> float:
    return max(1.0, settings_float("trading.leverage", 10.0))


def notional_usdt() -> float:
    return trade_margin_usdt() * trade_leverage()


def fee_rate_per_side() -> float:
    fee = settings_float("tp.fee_rate_per_side", 0.0)
    if fee <= 0:
        fee = settings_float("trading.taker_fee_rate", 0.0006)
    return max(0.0, fee)


def min_net_profit_usdt() -> float:
    return max(0.0, settings_float("tp.min_net_profit_usdt", 0.20))


def estimated_fee_usdt(notional: float) -> float:
    return max(0.0, safe_float(notional) * fee_rate_per_side() * 2.0)


def gross_profit_usdt(notional: float, tp_percent: float) -> float:
    return max(0.0, safe_float(notional) * safe_float(tp_percent) / 100.0)


def estimated_loss_usdt(notional: float, sl_percent: float) -> float:
    return max(0.0, safe_float(notional) * safe_float(sl_percent) / 100.0)


def net_rr(gross: float, fee: float, loss: float) -> float:
    loss = safe_float(loss)
    if loss <= 0:
        return 0.0
    return (safe_float(gross) - safe_float(fee)) / loss


def iter_numeric_levels(value: Any) -> List[float]:
    levels: List[float] = []

    if value is None:
        return levels
    if isinstance(value, (int, float)):
        v = safe_float(value)
        if v > 0:
            levels.append(v)
        return levels
    if isinstance(value, dict):
        for key in ("price", "level", "value", "low", "high", "support", "resistance"):
            if key in value:
                levels.extend(iter_numeric_levels(value.get(key)))
        return levels
    if isinstance(value, (list, tuple, set)):
        for item in value:
            levels.extend(iter_numeric_levels(item))
        return levels

    return levels


def snapshot_levels(candidate: AnalysisCandidate, side: str) -> List[float]:
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
        out.extend(iter_numeric_levels(getattr(s, name, None)))

    dedup: List[float] = []
    for level in out:
        if level > 0 and all(abs(level - existing) / max(level, existing, 1e-12) > 0.00001 for existing in dedup):
            dedup.append(level)

    return dedup


def nearest_above(entry: float, levels: List[float]) -> Optional[float]:
    above = [x for x in levels if x > entry]
    return min(above) if above else None


def nearest_below(entry: float, levels: List[float]) -> Optional[float]:
    below = [x for x in levels if 0 < x < entry]
    return max(below) if below else None


def apply_support_resistance_rules(
    entry: float,
    direction: str,
    tp1_percent: float,
    tp2_percent: float,
    sl_percent: float,
    atr_percent: float,
    candidate: AnalysisCandidate,
) -> Tuple[float, float, float, List[str], List[str]]:
    reasons: List[str] = []
    warnings: List[str] = []

    direction = normalize_direction(direction)
    buffer_pct = max(0.06, safe_float(atr_percent) * 0.16)

    supports = snapshot_levels(candidate, "support")
    resistances = snapshot_levels(candidate, "resistance")

    if direction == DIRECTION_LONG:
        support = nearest_below(entry, supports)
        resistance = nearest_above(entry, resistances)

        if support:
            support_dist = distance_percent(entry, support)
            target_sl = support_dist + buffer_pct
            if target_sl > sl_percent:
                sl_percent = target_sl
                reasons.append("SL_BELOW_SUPPORT_WITH_BUFFER")

        if resistance:
            resistance_dist = distance_percent(entry, resistance)
            cautious_tp = resistance_dist - buffer_pct
            # Do not reduce below locked minimum. If resistance is too close, keep
            # minimum TP and warn so AI/REAL side can downgrade if needed.
            if cautious_tp > 0 and cautious_tp >= tp1_percent * 0.92 and cautious_tp < tp1_percent:
                tp1_percent = cautious_tp
                reasons.append("TP1_ADJUSTED_BEFORE_RESISTANCE")
            elif 0 < cautious_tp < tp1_percent * 0.92:
                warnings.append("RESISTANCE_TOO_CLOSE_TO_REDUCE_TP1_BELOW_MIN")

    elif direction == DIRECTION_SHORT:
        support = nearest_below(entry, supports)
        resistance = nearest_above(entry, resistances)

        if resistance:
            resistance_dist = distance_percent(entry, resistance)
            target_sl = resistance_dist + buffer_pct
            if target_sl > sl_percent:
                sl_percent = target_sl
                reasons.append("SL_ABOVE_RESISTANCE_WITH_BUFFER")

        if support:
            support_dist = distance_percent(entry, support)
            cautious_tp = support_dist - buffer_pct
            if cautious_tp > 0 and cautious_tp >= tp1_percent * 0.92 and cautious_tp < tp1_percent:
                tp1_percent = cautious_tp
                reasons.append("TP1_ADJUSTED_BEFORE_SUPPORT")
            elif 0 < cautious_tp < tp1_percent * 0.92:
                warnings.append("SUPPORT_TOO_CLOSE_TO_REDUCE_TP1_BELOW_MIN")

    return tp1_percent, tp2_percent, sl_percent, reasons, warnings


def base_distances(
    candidate: AnalysisCandidate,
    prediction: MovementPredictionResult,
    learning: Optional[LearningSummary],
) -> Tuple[float, float, float, List[str], List[str]]:
    reasons: List[str] = []
    warnings: List[str] = []

    m = candidate.momentum_state
    atr_percent = safe_float(m.atr_percent)
    if atr_percent <= 0:
        atr_percent = 0.55
        warnings.append("ATR_FALLBACK_USED")

    tp1_atr = MIN_TP1_ATR
    tp2_atr = DEFAULT_TP2_ATR
    sl_atr = MIN_SL_ATR

    phase = str(obj_value(prediction, "predicted_phase", "")).upper()
    movement_probability = obj_float(prediction, "movement_probability", 0.0)
    expected_move = obj_float(prediction, "expected_move_percent", 0.0)

    if phase in {"PRE_START", "START"}:
        tp2_atr += 0.15
        reasons.append("EARLY_PHASE_TP2_ALLOWED")
    elif phase == "MID":
        tp2_atr -= 0.10
        reasons.append("MID_PHASE_TP2_CONSERVATIVE")
    elif phase in {"LATE", "RANGE"}:
        tp2_atr -= 0.25
        sl_atr += 0.10
        reasons.append("LATE_OR_RANGE_MORE_CONSERVATIVE")

    if movement_probability >= 70:
        tp2_atr += 0.15
        reasons.append("HIGH_MOVEMENT_PROBABILITY_TP2_PLUS")
    elif movement_probability < 45:
        tp2_atr -= 0.15
        reasons.append("LOW_MOVEMENT_PROBABILITY_TP2_MINUS")

    if expected_move > 0:
        # Expected move can make TP1 wider, never smaller than 0.95 ATR.
        expected_tp1_pct = expected_move * 0.72
        if expected_tp1_pct > atr_percent * tp1_atr:
            tp1_atr = max(tp1_atr, expected_tp1_pct / atr_percent)
            reasons.append("EXPECTED_MOVE_WIDENED_TP1")

    if learning is not None:
        risk_label = str(obj_value(learning, "risk_label", "")).upper()
        early_success = obj_float(learning, "early_success_rate", 0.0)
        timing = obj_float(learning, "timing_score", 50.0)
        avg_mae = obj_float(learning, "avg_mae_percent", 0.0)
        avg_mfe = obj_float(learning, "avg_mfe_percent", 0.0)
        samples = obj_float(learning, "sample_count", 0.0)

        if risk_label == "FAVORABLE_CONDITION" and samples >= 3:
            tp2_atr += 0.10
            reasons.append("LEARNING_FAVORABLE_TP2_PLUS")
        elif risk_label == "RISKY_CONDITION":
            sl_atr += 0.12
            tp2_atr -= 0.20
            reasons.append("LEARNING_RISKY_CONDITION_MORE_CAREFUL")

        if early_success >= 40 or timing >= 65:
            tp1_atr += 0.05
            tp2_atr += 0.12
            reasons.append("LEARNING_EARLY_PATTERN_SUPPORT")

        if samples >= 5 and avg_mae > avg_mfe:
            sl_atr += 0.15
            reasons.append("LEARNING_PULLBACK_NOISE_WIDER_SL")

    # Locked floors.
    tp1_atr = max(tp1_atr, MIN_TP1_ATR)
    sl_atr = max(sl_atr, MIN_SL_ATR)
    tp2_atr = max(tp2_atr, tp1_atr * 1.25)

    # Keep Level 1 scalable and not oversized.
    tp1_atr = clamp(tp1_atr, MIN_TP1_ATR, 1.55)
    tp2_atr = clamp(tp2_atr, tp1_atr * 1.25, 2.80)
    sl_atr = clamp(sl_atr, MIN_SL_ATR, 2.60)

    return atr_percent * tp1_atr, atr_percent * tp2_atr, atr_percent * sl_atr, reasons, warnings


def apply_minimum_distance_and_fee_floor(
    entry: float,
    symbol: str,
    tp1_percent: float,
    tp2_percent: float,
    sl_percent: float,
) -> Tuple[float, float, float, float, float, float, float, List[str], List[str]]:
    reasons: List[str] = []
    warnings: List[str] = []

    min_dist = min_distance_percent_for_symbol(symbol, entry)

    if tp1_percent < min_dist:
        tp1_percent = min_dist
        reasons.append("TP1_EXCHANGE_MIN_DISTANCE_ENFORCED")
    if sl_percent < min_dist:
        sl_percent = min_dist
        reasons.append("SL_EXCHANGE_MIN_DISTANCE_ENFORCED")
    if tp2_percent < tp1_percent * 1.25:
        tp2_percent = tp1_percent * 1.25
        reasons.append("TP2_MIN_DISTANCE_FROM_TP1_ENFORCED")

    notional = notional_usdt()
    fee = estimated_fee_usdt(notional)
    min_net = min_net_profit_usdt()
    required_tp_percent = ((fee + min_net) / notional * 100.0) if notional > 0 else 0.0

    if required_tp_percent > 0 and tp1_percent < required_tp_percent:
        tp1_percent = required_tp_percent
        reasons.append("TP1_RAISED_FOR_FEES_AND_MIN_NET")
    if tp2_percent < tp1_percent * 1.25:
        tp2_percent = tp1_percent * 1.25
        reasons.append("TP2_RAISED_AFTER_FEE_FLOOR")

    gross = gross_profit_usdt(notional, tp1_percent)
    net = gross - fee
    if net < min_net:
        warnings.append("TP1_NET_PROFIT_STILL_BELOW_MIN")

    return tp1_percent, tp2_percent, sl_percent, notional, gross, fee, min_net, reasons, warnings


def choose_tp_mode(decision: AIDecision, prediction: MovementPredictionResult, learning: Optional[LearningSummary]) -> Tuple[str, List[str]]:
    reasons: List[str] = []

    tp2_enabled = bool(getattr(SETTINGS.tp, "tp2_enabled", True))
    if not tp2_enabled:
        reasons.append("TP2_DISABLED_BY_CONFIG")
        return TP_MODE_TP1_ONLY, reasons

    strong = (
        safe_float(decision.ai_score) >= 68
        and obj_float(prediction, "movement_probability", 0.0) >= 62
        and str(obj_value(prediction, "predicted_phase", "")).upper() in {"PRE_START", "START"}
    )

    if learning is not None:
        risk_label = str(obj_value(learning, "risk_label", "")).upper()
        if risk_label == "RISKY_CONDITION":
            strong = False
            reasons.append("TP2_BLOCKED_BY_RISKY_LEARNING")
        if (
            obj_float(learning, "early_success_rate", 0.0) >= 45
            and str(obj_value(prediction, "predicted_phase", "")).upper() in {"PRE_START", "START"}
        ):
            strong = True
            reasons.append("TP2_ALLOWED_BY_EARLY_LEARNING")

    if strong:
        reasons.append("TP2_ALLOWED")
        return TP_MODE_TP1_TP2, reasons

    reasons.append("TP1_ONLY_SIMPLE_SCALP")
    return TP_MODE_TP1_ONLY, reasons


def profit_quality_score(
    tp1_percent: float,
    sl_percent: float,
    notional: float,
    gross: float,
    fee: float,
    min_net: float,
    atr_percent: float,
    candidate: AnalysisCandidate,
) -> Tuple[float, float, float, List[str], List[str]]:
    reasons: List[str] = []
    warnings: List[str] = []

    loss = estimated_loss_usdt(notional, sl_percent)
    rr_net = net_rr(gross, fee, loss)
    net = safe_float(gross) - safe_float(fee)

    net_score = clamp((net / max(min_net, 0.01)) * 35.0, 0.0, 40.0)
    rr_score = clamp(rr_net * 25.0, 0.0, 25.0)
    noise_ratio = safe_float(tp1_percent) / max(safe_float(atr_percent), 0.05)
    noise_score = clamp(noise_ratio * 18.0, 0.0, 22.0)

    m = candidate.momentum_state
    volume_bonus = 0.0
    if m.relative_volume >= 1.8 or m.volume_spike:
        volume_bonus = 8.0
        reasons.append("VOLUME_SUPPORTS_TP_SL")
    elif 0 < m.relative_volume < 0.65:
        volume_bonus = -8.0
        warnings.append("LOW_VOLUME_TP_SL")

    score = clamp(net_score + rr_score + noise_score + volume_bonus)

    if net < min_net:
        warnings.append("NET_PROFIT_BELOW_MIN")
    if rr_net < 0.25:
        warnings.append("NET_RR_LOW")
    if score < 30:
        warnings.append("TP_SL_PROFIT_QUALITY_LOW")
    elif score >= 65:
        reasons.append("TP_SL_PROFIT_QUALITY_GOOD")

    return score, loss, rr_net, reasons, warnings


class TPSLValidator:
    def validate(self, plan: TPSLPlan) -> Tuple[bool, List[str]]:
        warnings: List[str] = []

        if plan.entry <= 0 or plan.tp1 <= 0 or plan.sl <= 0:
            warnings.append("INVALID_PRICE")
            return False, warnings

        if plan.direction == DIRECTION_LONG:
            if not (plan.sl < plan.entry < plan.tp1):
                warnings.append("INVALID_LONG_TP_SL")
                return False, warnings
            if plan.tp_mode == TP_MODE_TP1_TP2 and plan.tp2 > 0 and not (plan.tp2 > plan.tp1):
                warnings.append("INVALID_LONG_TP2")
                return False, warnings

        elif plan.direction == DIRECTION_SHORT:
            if not (plan.tp1 < plan.entry < plan.sl):
                warnings.append("INVALID_SHORT_TP_SL")
                return False, warnings
            if plan.tp_mode == TP_MODE_TP1_TP2 and plan.tp2 > 0 and not (plan.tp2 < plan.tp1):
                warnings.append("INVALID_SHORT_TP2")
                return False, warnings
        else:
            warnings.append("INVALID_DIRECTION")
            return False, warnings

        if plan.tp1_distance_percent <= 0 or plan.sl_distance_percent <= 0:
            warnings.append("ZERO_DISTANCE")
            return False, warnings

        if plan.estimated_tp1_net_usdt < plan.min_required_net_profit_usdt:
            warnings.append("NET_PROFIT_BELOW_REQUIRED")
            return False, warnings

        if plan.tradability_score < 25:
            warnings.append("TRADABILITY_TOO_LOW")
            return False, warnings

        return True, warnings


class TPSLEngine:
    def __init__(self):
        self.validator = TPSLValidator()

    def build_plan(
        self,
        decision: AIDecision,
        candidate: AnalysisCandidate,
        prediction: MovementPredictionResult,
        learning: Optional[LearningSummary] = None,
        **_: Any,
    ) -> TPSLPlan:
        reasons: List[str] = []
        warnings: List[str] = []

        direction = normalize_direction(decision.direction)
        entry = safe_float(decision.entry or getattr(candidate.sensor_snapshot, "price", 0.0), 0.0)
        symbol = str(decision.symbol or candidate.symbol)

        tp1_percent, tp2_percent, sl_percent, r, w = base_distances(candidate, prediction, learning)
        reasons.extend(r)
        warnings.extend(w)

        atr_percent = max(0.05, safe_float(candidate.momentum_state.atr_percent, 0.55))

        # Locked minimums are enforced after every smart adjustment.
        tp1_floor = atr_percent * MIN_TP1_ATR
        sl_floor = atr_percent * MIN_SL_ATR

        tp1_percent, tp2_percent, sl_percent, r, w = apply_support_resistance_rules(
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

        tp1_percent = max(tp1_percent, tp1_floor)
        sl_percent = max(sl_percent, sl_floor)
        tp2_percent = max(tp2_percent, tp1_percent * 1.25)

        tp1_percent, tp2_percent, sl_percent, notional, gross, fee, min_net, r, w = apply_minimum_distance_and_fee_floor(
            entry=entry,
            symbol=symbol,
            tp1_percent=tp1_percent,
            tp2_percent=tp2_percent,
            sl_percent=sl_percent,
        )
        reasons.extend(r)
        warnings.extend(w)

        # Keep Level 1 from becoming swing-style.
        max_tp1_percent = max(tp1_floor, max(1.80, atr_percent * 1.70))
        max_sl_percent = max(sl_floor, max(2.60, atr_percent * 2.60))
        tp1_percent = clamp(tp1_percent, tp1_floor, max_tp1_percent)
        sl_percent = clamp(sl_percent, sl_floor, max_sl_percent)
        tp2_percent = clamp(tp2_percent, tp1_percent * 1.25, max(3.80, atr_percent * 2.90))

        gross = gross_profit_usdt(notional, tp1_percent)
        fee = estimated_fee_usdt(notional)
        tradability, est_loss, rr_net, r, w = profit_quality_score(
            tp1_percent=tp1_percent,
            sl_percent=sl_percent,
            notional=notional,
            gross=gross,
            fee=fee,
            min_net=min_net,
            atr_percent=atr_percent,
            candidate=candidate,
        )
        reasons.extend(r)
        warnings.extend(w)

        mode, r = choose_tp_mode(decision, prediction, learning)
        reasons.extend(r)

        tp1 = round_price(price_from_percent(entry, direction, tp1_percent), symbol)
        tp2 = round_price(price_from_percent(entry, direction, tp2_percent), symbol) if mode == TP_MODE_TP1_TP2 else 0.0
        sl = round_price(sl_from_percent(entry, direction, sl_percent), symbol)

        rr1 = rr_ratio(entry, tp1, sl)
        rr2 = rr_ratio(entry, tp2, sl) if tp2 > 0 else 0.0

        quality_label = QUALITY_MEDIUM
        if decision.ai_score >= 72 and obj_float(prediction, "movement_probability", 0.0) >= 65 and tradability >= 60:
            quality_label = QUALITY_HIGH
        elif decision.ai_score < 52 or tradability < 40:
            quality_label = QUALITY_LOW

        plan = TPSLPlan(
            plan_id=f"tpsl_{uuid4().hex}",
            decision_id=decision.decision_id,
            symbol=symbol,
            direction=direction,
            entry=round_price(entry, symbol),
            tp1=tp1,
            tp2=tp2,
            sl=sl,
            tp_mode=mode,
            rr_tp1=rr1,
            rr_tp2=rr2,
            tp1_distance_percent=distance_percent(entry, tp1),
            tp2_distance_percent=distance_percent(entry, tp2) if tp2 > 0 else 0.0,
            sl_distance_percent=distance_percent(entry, sl),
            atr_percent=atr_percent,
            quality_label=quality_label,
            notional_usdt=safe_float(notional),
            estimated_tp1_gross_usdt=safe_float(gross),
            estimated_tp1_fee_usdt=safe_float(fee),
            estimated_tp1_net_usdt=safe_float(gross - fee),
            estimated_sl_loss_usdt=safe_float(est_loss),
            estimated_rr_net=safe_float(rr_net),
            min_required_net_profit_usdt=safe_float(min_net),
            tradability_score=safe_float(tradability),
            reason_codes=tuple(dict.fromkeys(reasons)),
            warnings=tuple(dict.fromkeys(warnings)),
            valid=True,
        )

        valid, validation_warnings = self.validator.validate(plan)
        if not valid or validation_warnings:
            plan = TPSLPlan(**{
                **plan.to_dict(),
                "valid": valid,
                "warnings": tuple(dict.fromkeys(list(plan.warnings) + validation_warnings)),
            })

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
    prediction: MovementPredictionResult,
    learning: Optional[LearningSummary] = None,
    **kwargs: Any,
) -> TPSLPlan:
    return engine().build_plan(
        decision=decision,
        candidate=candidate,
        prediction=prediction,
        learning=learning,
        **kwargs,
    )


def apply_tp_sl_to_decision(decision: AIDecision, plan: TPSLPlan) -> AIDecision:
    data = decision.to_dict()
    data.update({
        "tp1": plan.tp1,
        "tp2": plan.tp2,
        "sl": plan.sl,
        "tp_mode": plan.tp_mode,
        "warnings": tuple(dict.fromkeys(list(decision.warnings) + list(plan.warnings))),
        "reason_codes": tuple(dict.fromkeys(list(decision.reason_codes) + list(plan.reason_codes))),
        "meta": {
            **dict(decision.meta),
            "tp_sl_plan": plan.to_dict(),
            "tradability_score": plan.tradability_score,
            "estimated_tp1_net_usdt": plan.estimated_tp1_net_usdt,
        },
    })
    return AIDecision(**data)


def tp_sl_engine(
    decision: AIDecision,
    candidate: AnalysisCandidate,
    prediction: MovementPredictionResult,
    learning: Optional[LearningSummary] = None,
    **kwargs: Any,
) -> TPSLPlan:
    return build_tp_sl_plan(
        decision=decision,
        candidate=candidate,
        prediction=prediction,
        learning=learning,
        **kwargs,
    )
