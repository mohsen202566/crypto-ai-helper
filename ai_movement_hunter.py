# -*- coding: utf-8 -*-
"""
ai_movement_hunter.py

AI Movement Hunter / Movement Prediction brain for the crypto futures bot.

Architecture:
- Classic technical engine must NOT issue final signals or final scores.
- Technical indicators are treated as raw sensors/features only.
- This module decides:
    SETUP / ENTRY
    direction LONG / SHORT / NONE
    move freshness and move phase
    trap/liquidity/reversal risk
    REAL / GHOST / REJECT
    TP/SL levels when caller has not already supplied safe levels

Design goals:
- Hunt fresh pump/dump movements before the main move or in the first candle.
- Reject late entries after the main move is already done.
- Keep compatibility with existing bot/scanner/tracker/real-trade code.
- Never place real orders directly. It only returns a decision dictionary.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple


VERSION = "1.0.1-soft-range-safe"

DECISION_REAL = "REAL"
DECISION_GHOST = "GHOST"
DECISION_REJECT = "REJECT"

PHASE_PRE_START = "PRE_START"
PHASE_START = "START"
PHASE_EARLY = "EARLY"
PHASE_MID = "MID"
PHASE_EXHAUSTION = "EXHAUSTION"
PHASE_RANGE_AFTER_MOVE = "RANGE_AFTER_MOVE"
PHASE_RANGE = "RANGE"
PHASE_UNKNOWN = "UNKNOWN"

MOVE_PUMP_START = "PUMP_START"
MOVE_DUMP_START = "DUMP_START"
MOVE_PUMP_SETUP = "PUMP_SETUP"
MOVE_DUMP_SETUP = "DUMP_SETUP"
MOVE_NONE = "NONE"

DEFAULT_AI_REAL_THRESHOLD = 72.0
DEFAULT_AI_GHOST_THRESHOLD = 55.0

# Soft activation rules:
# - Keep obvious danger blocked.
# - Do not hard-reject RANGE_AFTER_MOVE by itself when evidence is strong and trap is LOW.
# - Let strong MID/RANGE_AFTER_MOVE candidates become SETUP/ENTRY so scanner can learn and send real signals selectively.
SOFT_RANGE_REAL_THRESHOLD = 66.0
SOFT_RANGE_GHOST_THRESHOLD = 52.0
SOFT_MID_REAL_THRESHOLD = 64.0
MIN_SOFT_EVIDENCE = 3
MIN_SOFT_STRENGTH = 3.0


# ---------------------------------------------------------------------------
# Safe readers
# ---------------------------------------------------------------------------

def _now() -> int:
    return int(time.time())


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        if isinstance(value, str) and not value.strip():
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "bullish", "bearish", "long", "short"}:
        return True
    if text in {"0", "false", "no", "off", "n", "none", "neutral"}:
        return False
    return default


def _upper(value: Any, default: str = "") -> str:
    try:
        text = str(value or default).upper().strip()
        return text or default
    except Exception:
        return default


def _first_number(snapshot: Dict[str, Any], keys: List[str], default: float = 0.0) -> float:
    if not isinstance(snapshot, dict):
        return float(default)
    for key in keys:
        if snapshot.get(key) is not None:
            return _safe_float(snapshot.get(key), default)
    for nest_key in (
        "sensor_snapshot",
        "technical_features",
        "features",
        "ai_layers",
        "movement",
        "state_awareness",
        "prediction_layer",
        "liquidity_trap",
    ):
        obj = snapshot.get(nest_key)
        if isinstance(obj, dict):
            for key in keys:
                if obj.get(key) is not None:
                    return _safe_float(obj.get(key), default)
    return float(default)


def _first_text(snapshot: Dict[str, Any], keys: List[str], default: str = "") -> str:
    if not isinstance(snapshot, dict):
        return default
    for key in keys:
        if snapshot.get(key) not in (None, ""):
            return str(snapshot.get(key))
    for nest_key in (
        "sensor_snapshot",
        "technical_features",
        "features",
        "ai_layers",
        "movement",
        "state_awareness",
        "prediction_layer",
        "liquidity_trap",
    ):
        obj = snapshot.get(nest_key)
        if isinstance(obj, dict):
            for key in keys:
                if obj.get(key) not in (None, ""):
                    return str(obj.get(key))
    return default


def normalize_symbol(symbol: Any) -> str:
    s = str(symbol or "").upper().strip().replace("/", "").replace("-", "")
    if not s:
        return ""
    if s.endswith("USDT") or s.endswith("USDC"):
        return s
    return f"{s}USDT"


# ---------------------------------------------------------------------------
# Optional learning/context hooks
# ---------------------------------------------------------------------------

def _try_coin_learning(symbol: str, direction: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from coin_learning import get_similarity_adjustment  # type: ignore
    except Exception:
        return {}
    try:
        try:
            res = get_similarity_adjustment(symbol, direction, snapshot=snapshot, mode="movement_hunter")
        except TypeError:
            res = get_similarity_adjustment(symbol, direction, snapshot)
        return res if isinstance(res, dict) else {}
    except Exception:
        return {}


def _try_coin_risk(symbol: str, direction: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from coin_risk import get_direction_risk_state  # type: ignore
    except Exception:
        return {}
    try:
        try:
            res = get_direction_risk_state(symbol, direction, snapshot=snapshot)
        except TypeError:
            res = get_direction_risk_state(symbol, direction)
        return res if isinstance(res, dict) else {}
    except Exception:
        return {}


def _try_market_breadth() -> Dict[str, Any]:
    try:
        from market_scanner import get_market_breadth_profile  # type: ignore
    except Exception:
        return {}
    try:
        res = get_market_breadth_profile()
        return res if isinstance(res, dict) else {}
    except Exception:
        return {}


def _try_smart_tp(symbol: str, direction: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from coin_learning import get_smart_tp_suggestion  # type: ignore
    except Exception:
        return {}
    try:
        try:
            res = get_smart_tp_suggestion(symbol, direction, snapshot=snapshot)
        except TypeError:
            res = get_smart_tp_suggestion(symbol, direction, snapshot)
        return res if isinstance(res, dict) else {}
    except Exception:
        return {}


def _update_ai_movement_memory(decision: Dict[str, Any]) -> None:
    try:
        from ai_memory import update_movement_hunter_memory  # type: ignore
    except Exception:
        update_movement_hunter_memory = None
    if callable(update_movement_hunter_memory):
        try:
            update_movement_hunter_memory(decision)
        except Exception:
            pass
            return
    # Backward compatible market memory update only.
    try:
        from ai_memory import update_ai_summary  # type: ignore
        snap = decision.get("snapshot") if isinstance(decision.get("snapshot"), dict) else {}
        update_ai_summary(
            market_mode=snap.get("market_mode") or snap.get("market_regime"),
            btc_bias=snap.get("btc_bias"),
            source="ai_movement_hunter",
            snapshot=snap,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def build_feature_snapshot(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize any analysis/scanner result into a sensor snapshot.

    This function deliberately keeps classic fields like score/long_score only
    as metadata. They are not used as final authority.
    """
    raw = raw if isinstance(raw, dict) else {}
    snap = raw.get("snapshot") if isinstance(raw.get("snapshot"), dict) else {}
    out = dict(snap)

    for key in [
        "symbol", "direction_hint", "direction", "entry", "price", "close",
        "high", "low", "open", "volume", "rsi", "rsi_5m", "rsi_15m",
        "rsi_slope", "rsi_slope_5m", "rsi_slope_15m", "macd", "macd_signal",
        "macd_hist", "macd_hist_5m", "macd_hist_15m", "macd_hist_slope",
        "macd_hist_slope_5m", "macd_hist_slope_15m", "macd_hist_accel",
        "macd_hist_accel_5m", "macd_hist_accel_15m", "adx", "adx_5m",
        "adx_15m", "adx_slope", "adx_slope_5m", "adx_slope_15m",
        "ema20", "ema50", "ema200", "ema20_distance_pct",
        "ema50_distance_pct", "vwap", "vwap_status", "vwap_distance_pct",
        "atr", "atr_pct", "atr_expansion", "atr_compression",
        "volume_ratio", "volume_spike", "buy_power", "sell_power",
        "power2_buy", "power2_sell", "power3_buy", "power3_sell",
        "power6_buy", "power6_sell", "support", "resistance",
        "support_distance_pct", "resistance_distance_pct",
        "range_high", "range_low", "range_position_pct", "range_breakout",
        "range_breakdown", "candle_body_pct", "upper_wick_pct",
        "lower_wick_pct", "btc_bias", "market_mode", "market_regime",
        "coin_behavior", "relative_status", "score", "long_score",
        "short_score", "risk_reward", "risk_level", "confirmations",
        "freshness", "move_state", "move_phase", "trap_risk",
        "liquidity_risk_score", "reversal_risk_score",
        "expected_move_pct", "prediction_score",
    ]:
        if key not in out and raw.get(key) is not None:
            out[key] = raw.get(key)

    symbol = normalize_symbol(out.get("symbol") or raw.get("symbol"))
    if symbol:
        out["symbol"] = symbol

    # Normalize useful booleans/texts.
    if "price" not in out:
        out["price"] = out.get("close") or out.get("entry")
    if "entry" not in out:
        out["entry"] = out.get("price") or out.get("close")

    out["created_at"] = out.get("created_at") or out.get("snapshot_at") or _now()
    out["feature_version"] = VERSION
    return out


# ---------------------------------------------------------------------------
# Direction detection
# ---------------------------------------------------------------------------

def _power_delta(snapshot: Dict[str, Any], window: str = "3") -> float:
    b = _first_number(snapshot, [f"power{window}_buy", f"buy_power_{window}", f"buy_power{window}"], 0.0)
    s = _first_number(snapshot, [f"power{window}_sell", f"sell_power_{window}", f"sell_power{window}"], 0.0)
    if b == 0 and s == 0 and window in {"3", "6"}:
        b = _first_number(snapshot, ["buy_power"], 0.0)
        s = _first_number(snapshot, ["sell_power"], 0.0)
    return b - s


def detect_direction(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Predict likely near-term movement direction from raw sensors."""
    long_points = 0.0
    short_points = 0.0
    reasons: List[str] = []

    direction_hint = _upper(_first_text(snapshot, ["direction_hint", "raw_direction", "sensor_direction", "direction"], ""))
    if direction_hint == "LONG":
        long_points += 1.5
        reasons.append("direction_hint LONG")
    elif direction_hint == "SHORT":
        short_points += 1.5
        reasons.append("direction_hint SHORT")

    rsi = _first_number(snapshot, ["rsi_5m", "rsi", "rsi_15m"], 50.0)
    rsi_slope = _first_number(snapshot, ["rsi_slope_5m", "rsi_slope", "rsi_slope_15m"], 0.0)
    if rsi_slope > 0.25:
        long_points += min(6.0, 1.2 + abs(rsi_slope) * 0.45)
        reasons.append("RSI slope up")
    elif rsi_slope < -0.25:
        short_points += min(6.0, 1.2 + abs(rsi_slope) * 0.45)
        reasons.append("RSI slope down")

    # RSI crosses are early sensors, not final signals.
    if 28 <= rsi <= 42 and rsi_slope > 0:
        long_points += 3.2
        reasons.append("RSI rebound zone")
    if 58 <= rsi <= 72 and rsi_slope < 0:
        short_points += 3.2
        reasons.append("RSI rejection zone")
    if rsi > 50 and rsi_slope > 0:
        long_points += 1.6
    if rsi < 50 and rsi_slope < 0:
        short_points += 1.6

    hist = _first_number(snapshot, ["macd_hist_5m", "macd_hist", "macd_hist_15m"], 0.0)
    hist_slope = _first_number(snapshot, ["macd_hist_slope_5m", "macd_hist_slope", "macd_hist_slope_15m"], 0.0)
    hist_accel = _first_number(snapshot, ["macd_hist_accel_5m", "macd_hist_accel", "macd_hist_accel_15m"], 0.0)
    if hist > 0:
        long_points += 1.6
    elif hist < 0:
        short_points += 1.6
    if hist_slope > 0:
        long_points += min(5.0, 1.5 + abs(hist_slope) * 500.0)
        reasons.append("MACD histogram rising")
    elif hist_slope < 0:
        short_points += min(5.0, 1.5 + abs(hist_slope) * 500.0)
        reasons.append("MACD histogram falling")
    if hist_accel > 0:
        long_points += min(4.0, 1.0 + abs(hist_accel) * 700.0)
    elif hist_accel < 0:
        short_points += min(4.0, 1.0 + abs(hist_accel) * 700.0)

    p2 = _power_delta(snapshot, "2")
    p3 = _power_delta(snapshot, "3")
    p6 = _power_delta(snapshot, "6")
    power_score = p2 * 0.12 + p3 * 0.09 + p6 * 0.045
    if power_score > 0:
        long_points += min(8.5, power_score)
        reasons.append("Buy power shift")
    elif power_score < 0:
        short_points += min(8.5, abs(power_score))
        reasons.append("Sell power shift")

    vwap_status = _upper(_first_text(snapshot, ["vwap_status"], ""))
    vwap_dist = _first_number(snapshot, ["vwap_distance_pct"], 0.0)
    if vwap_status in {"ABOVE", "BULLISH", "RECLAIM", "PRICE_ABOVE"} or vwap_dist > 0.03:
        long_points += 2.2
    elif vwap_status in {"BELOW", "BEARISH", "LOSS", "PRICE_BELOW"} or vwap_dist < -0.03:
        short_points += 2.2

    # Range break sensors.
    if _safe_bool(snapshot.get("range_breakout")):
        long_points += 4.0
        reasons.append("range breakout")
    if _safe_bool(snapshot.get("range_breakdown")):
        short_points += 4.0
        reasons.append("range breakdown")

    # Candle pressure.
    body = _first_number(snapshot, ["candle_body_pct", "body_pct"], 0.0)
    upper_wick = _first_number(snapshot, ["upper_wick_pct"], 0.0)
    lower_wick = _first_number(snapshot, ["lower_wick_pct"], 0.0)
    if body > 0:
        close_pos = _first_number(snapshot, ["candle_close_position", "range_position_pct"], 50.0)
        if close_pos >= 65 and lower_wick >= upper_wick * 0.8:
            long_points += min(3.0, body * 20.0)
        elif close_pos <= 35 and upper_wick >= lower_wick * 0.8:
            short_points += min(3.0, body * 20.0)

    market = _upper(_first_text(snapshot, ["market_mode", "market_regime"], ""))
    btc = _upper(_first_text(snapshot, ["btc_bias"], ""))
    if "BULL" in market:
        long_points += 1.0
    elif "BEAR" in market:
        short_points += 1.0
    if "BULL" in btc:
        long_points += 1.0
    elif "BEAR" in btc:
        short_points += 1.0

    if long_points > short_points + 1.2:
        direction = "LONG"
        strength = long_points - short_points
    elif short_points > long_points + 1.2:
        direction = "SHORT"
        strength = short_points - long_points
    else:
        direction = "NONE"
        strength = abs(long_points - short_points)

    return {
        "direction": direction,
        "long_points": round(long_points, 4),
        "short_points": round(short_points, 4),
        "direction_strength": round(strength, 4),
        "reasons": reasons[:8],
    }


# ---------------------------------------------------------------------------
# Movement freshness / phase
# ---------------------------------------------------------------------------

def _impulse_done_pct(snapshot: Dict[str, Any], direction: str) -> float:
    """Estimate how much of the recent move is already consumed.

    Uses range position, distance from recent high/low, ATR extension and
    expected_move if available. Conservative: higher means later.
    """
    range_pos = _first_number(snapshot, ["range_position_pct"], 50.0)
    atr_pct = abs(_first_number(snapshot, ["atr_pct"], 0.0))
    price = _first_number(snapshot, ["price", "entry", "close"], 0.0)
    support = _first_number(snapshot, ["support", "range_low"], 0.0)
    resistance = _first_number(snapshot, ["resistance", "range_high"], 0.0)

    done = 50.0
    if direction == "LONG":
        done = range_pos
        if price > 0 and resistance > 0 and support > 0 and resistance > support:
            done = max(done, (price - support) / (resistance - support) * 100.0)
    elif direction == "SHORT":
        done = 100.0 - range_pos
        if price > 0 and resistance > 0 and support > 0 and resistance > support:
            done = max(done, (resistance - price) / (resistance - support) * 100.0)

    entry_move_pct = abs(_first_number(snapshot, ["move_from_recent_low_pct", "move_from_recent_high_pct", "impulse_move_pct"], 0.0))
    expected = abs(_first_number(snapshot, ["expected_move_pct"], 0.0))
    if expected > 0 and entry_move_pct > 0:
        done = max(done, min(100.0, entry_move_pct / max(expected, 0.0001) * 100.0))

    # ATR extension proxy. If price has already travelled several ATRs, it is late.
    atr_extension = abs(_first_number(snapshot, ["atr_extension", "atr_extension_pct"], 0.0))
    if atr_extension > 0:
        done = max(done, min(100.0, atr_extension * 35.0))
    elif atr_pct > 0:
        recent_move = abs(_first_number(snapshot, ["recent_move_pct", "impulse_move_pct"], 0.0))
        if recent_move > 0:
            done = max(done, min(100.0, recent_move / max(atr_pct, 0.0001) * 32.0))

    return round(max(0.0, min(100.0, done)), 4)


def detect_move_phase(snapshot: Dict[str, Any], direction: str, direction_strength: float = 0.0) -> Dict[str, Any]:
    phase_hint = _upper(_first_text(snapshot, ["move_phase", "move_state"], ""))
    if phase_hint in {PHASE_PRE_START, PHASE_START, PHASE_EARLY, PHASE_MID, PHASE_EXHAUSTION, PHASE_RANGE_AFTER_MOVE, PHASE_RANGE}:
        return {
            "phase": phase_hint,
            "freshness_score": {
                PHASE_PRE_START: 88,
                PHASE_START: 84,
                PHASE_EARLY: 76,
                PHASE_MID: 48,
                PHASE_EXHAUSTION: 18,
                PHASE_RANGE_AFTER_MOVE: 12,
                PHASE_RANGE: 35,
            }.get(phase_hint, 40),
            "move_done_pct": 0 if phase_hint in {PHASE_PRE_START, PHASE_START} else 60,
            "reason": "phase_hint",
        }

    if direction not in {"LONG", "SHORT"}:
        return {"phase": PHASE_UNKNOWN, "freshness_score": 0.0, "move_done_pct": 100.0, "reason": "no_direction"}

    done = _impulse_done_pct(snapshot, direction)
    atr_compression = _safe_bool(snapshot.get("atr_compression")) or _first_number(snapshot, ["atr_compression_score"], 0.0) >= 55
    atr_expansion = _safe_bool(snapshot.get("atr_expansion")) or _first_number(snapshot, ["atr_expansion_score"], 0.0) >= 55
    volume_ratio = _first_number(snapshot, ["volume_ratio"], 1.0)
    volume_spike = _safe_bool(snapshot.get("volume_spike")) or volume_ratio >= 1.35
    hist_slope = _first_number(snapshot, ["macd_hist_slope_5m", "macd_hist_slope"], 0.0)
    rsi_slope = _first_number(snapshot, ["rsi_slope_5m", "rsi_slope"], 0.0)
    adx_slope = _first_number(snapshot, ["adx_slope_5m", "adx_slope"], 0.0)

    slope_ok = (direction == "LONG" and (hist_slope > 0 or rsi_slope > 0)) or (direction == "SHORT" and (hist_slope < 0 or rsi_slope < 0))
    momentum_accel = abs(hist_slope) > 0 or abs(rsi_slope) > 0.25 or abs(adx_slope) > 0.05

    # Pre-start: compression + early power/RSI/MACD without large range travel.
    if done <= 25 and atr_compression and slope_ok:
        phase = PHASE_PRE_START
        fresh = 88.0
        reason = "compression before move"
    elif done <= 35 and (slope_ok or volume_spike) and direction_strength >= 3:
        phase = PHASE_START
        fresh = 84.0
        reason = "first movement signs"
    elif done <= 52 and (momentum_accel or volume_spike):
        phase = PHASE_EARLY
        fresh = 74.0
        reason = "early movement"
    elif done <= 72:
        phase = PHASE_MID
        fresh = 48.0
        reason = "movement already mid"
    else:
        # Distinguish exhaustion from range-after-move.
        range_bias = _upper(_first_text(snapshot, ["market_mode", "market_regime"], ""))
        if "RANGE" in range_bias or not atr_expansion:
            phase = PHASE_RANGE_AFTER_MOVE
            fresh = 14.0
            reason = "range after impulse"
        else:
            phase = PHASE_EXHAUSTION
            fresh = 18.0
            reason = "late/exhausted move"

    return {
        "phase": phase,
        "freshness_score": round(fresh, 4),
        "move_done_pct": done,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Trap / reversal / risk
# ---------------------------------------------------------------------------

def detect_trap_risk(snapshot: Dict[str, Any], direction: str) -> Dict[str, Any]:
    risk = 0.0
    reasons: List[str] = []

    explicit = _upper(_first_text(snapshot, ["trap_risk"], ""))
    if explicit == "HIGH":
        risk += 55
        reasons.append("explicit trap risk high")
    elif explicit == "MEDIUM":
        risk += 30
        reasons.append("explicit trap risk medium")

    liq = _first_number(snapshot, ["liquidity_risk_score", "trap_risk_score"], 0.0)
    risk += min(45.0, liq * 0.55)

    range_pos = _first_number(snapshot, ["range_position_pct"], 50.0)
    upper_wick = _first_number(snapshot, ["upper_wick_pct"], 0.0)
    lower_wick = _first_number(snapshot, ["lower_wick_pct"], 0.0)
    body = _first_number(snapshot, ["candle_body_pct", "body_pct"], 0.0)

    # Buying high into upper wick or shorting low into lower wick is often late/trap.
    if direction == "LONG" and range_pos >= 82 and upper_wick > max(body, lower_wick) * 1.3:
        risk += 28
        reasons.append("long near range high with upper wick")
    if direction == "SHORT" and range_pos <= 18 and lower_wick > max(body, upper_wick) * 1.3:
        risk += 28
        reasons.append("short near range low with lower wick")

    # Failed breakout/breakdown flags if provided by sensors.
    if direction == "LONG" and _safe_bool(snapshot.get("failed_breakout")):
        risk += 35
        reasons.append("failed breakout")
    if direction == "SHORT" and _safe_bool(snapshot.get("failed_breakdown")):
        risk += 35
        reasons.append("failed breakdown")

    level = "LOW"
    if risk >= 65:
        level = "HIGH"
    elif risk >= 35:
        level = "MEDIUM"

    return {"trap_risk": level, "trap_risk_score": round(min(100.0, risk), 4), "reasons": reasons[:6]}


def detect_reversal_risk(snapshot: Dict[str, Any], direction: str, phase: str) -> Dict[str, Any]:
    explicit = _first_number(snapshot, ["reversal_risk_score", "reversal_risk"], 0.0)
    risk = explicit if explicit > 0 else 0.0
    reasons: List[str] = []

    rsi = _first_number(snapshot, ["rsi_5m", "rsi"], 50.0)
    rsi_slope = _first_number(snapshot, ["rsi_slope_5m", "rsi_slope"], 0.0)
    hist_slope = _first_number(snapshot, ["macd_hist_slope_5m", "macd_hist_slope"], 0.0)

    if direction == "LONG":
        if rsi >= 72 and rsi_slope < 0:
            risk += 28
            reasons.append("RSI overbought turning down")
        if hist_slope < 0:
            risk += 12
    elif direction == "SHORT":
        if rsi <= 28 and rsi_slope > 0:
            risk += 28
            reasons.append("RSI oversold turning up")
        if hist_slope > 0:
            risk += 12

    if phase in {PHASE_EXHAUSTION, PHASE_RANGE_AFTER_MOVE}:
        risk += 35
        reasons.append("late move phase")
    elif phase == PHASE_MID:
        risk += 12

    level = "LOW"
    if risk >= 65:
        level = "HIGH"
    elif risk >= 38:
        level = "MEDIUM"

    return {"reversal_risk": level, "reversal_risk_score": round(min(100.0, risk), 4), "reasons": reasons[:6]}


# ---------------------------------------------------------------------------
# TP/SL construction
# ---------------------------------------------------------------------------

def _round_price_like(price: float) -> float:
    price = float(price or 0.0)
    if price <= 0:
        return 0.0
    if price >= 1000:
        return round(price, 2)
    if price >= 100:
        return round(price, 3)
    if price >= 10:
        return round(price, 4)
    if price >= 1:
        return round(price, 5)
    if price >= 0.01:
        return round(price, 6)
    return round(price, 8)


def build_trade_levels(snapshot: Dict[str, Any], direction: str) -> Dict[str, Any]:
    entry = _first_number(snapshot, ["entry", "price", "close"], 0.0)
    existing_sl = _first_number(snapshot, ["stop_loss", "sl"], 0.0)
    existing_tp1 = _first_number(snapshot, ["tp1"], 0.0)
    existing_tp2 = _first_number(snapshot, ["tp2"], 0.0)
    if entry <= 0:
        return {"entry": None, "stop_loss": None, "tp1": None, "tp2": None, "risk_reward": None}

    if existing_sl > 0 and existing_tp1 > 0:
        rr = abs(existing_tp1 - entry) / max(abs(entry - existing_sl), 1e-12)
        return {
            "entry": _round_price_like(entry),
            "stop_loss": _round_price_like(existing_sl),
            "tp1": _round_price_like(existing_tp1),
            "tp2": _round_price_like(existing_tp2) if existing_tp2 > 0 else None,
            "risk_reward": round(rr, 3),
        }

    smart = _try_smart_tp(normalize_symbol(snapshot.get("symbol")), direction, snapshot)
    atr = _first_number(snapshot, ["atr"], 0.0)
    atr_pct = _first_number(snapshot, ["atr_pct"], 0.0)

    if atr <= 0 and atr_pct > 0:
        atr = entry * atr_pct / 100.0
    if atr <= 0:
        atr = max(entry * 0.004, 1e-12)

    # Movement hunter scalping: cautious TP, not long forecasting.
    tp1_mult = _safe_float(smart.get("tp1_atr_mult"), 0.75)
    tp2_mult = _safe_float(smart.get("tp2_atr_mult"), 1.25)
    sl_mult = _safe_float(smart.get("sl_atr_mult"), 1.05)

    # Protect against nonsense.
    tp1_mult = max(0.45, min(tp1_mult, 1.20))
    tp2_mult = max(tp1_mult + 0.15, min(tp2_mult, 1.90))
    sl_mult = max(0.75, min(sl_mult, 1.45))

    if direction == "LONG":
        sl = entry - atr * sl_mult
        tp1 = entry + atr * tp1_mult
        tp2 = entry + atr * tp2_mult
    elif direction == "SHORT":
        sl = entry + atr * sl_mult
        tp1 = entry - atr * tp1_mult
        tp2 = entry - atr * tp2_mult
    else:
        return {"entry": _round_price_like(entry), "stop_loss": None, "tp1": None, "tp2": None, "risk_reward": None}

    rr = abs(tp1 - entry) / max(abs(entry - sl), 1e-12)
    return {
        "entry": _round_price_like(entry),
        "stop_loss": _round_price_like(sl),
        "tp1": _round_price_like(tp1),
        "tp2": _round_price_like(tp2),
        "risk_reward": round(rr, 3),
        "smart_tp": smart,
    }


# ---------------------------------------------------------------------------
# Final decision
# ---------------------------------------------------------------------------

def decide_movement(raw: Dict[str, Any], *, mode: str = "auto") -> Dict[str, Any]:
    """Main AI Movement Hunter decision.

    Returns a result compatible with scanner.py and bot.py.
    """
    snapshot = build_feature_snapshot(raw)
    symbol = normalize_symbol(snapshot.get("symbol"))
    market = _try_market_breadth()
    if market:
        snapshot.setdefault("market_breadth", market)
        snapshot.setdefault("market_mode", market.get("market_breadth_bias") or snapshot.get("market_mode"))

    direction_info = detect_direction(snapshot)
    direction = direction_info.get("direction", "NONE")
    strength = _safe_float(direction_info.get("direction_strength"), 0.0)

    phase_info = detect_move_phase(snapshot, direction, strength)
    phase = phase_info.get("phase", PHASE_UNKNOWN)
    freshness = _safe_float(phase_info.get("freshness_score"), 0.0)

    trap = detect_trap_risk(snapshot, direction)
    reversal = detect_reversal_risk(snapshot, direction, phase)

    learning = _try_coin_learning(symbol, direction, snapshot) if direction in {"LONG", "SHORT"} else {}
    risk_state = _try_coin_risk(symbol, direction, snapshot) if direction in {"LONG", "SHORT"} else {}

    # Base AI score prioritizes freshness first, then direction.
    ai_score = 0.0
    ai_score += freshness * 0.42
    ai_score += min(100.0, strength * 8.0) * 0.24

    # Early acceleration sensors.
    volume_ratio = _first_number(snapshot, ["volume_ratio"], 1.0)
    adx = _first_number(snapshot, ["adx_5m", "adx"], 0.0)
    adx_slope = abs(_first_number(snapshot, ["adx_slope_5m", "adx_slope"], 0.0))
    expected_move = abs(_first_number(snapshot, ["expected_move_pct"], 0.0))

    if volume_ratio >= 1.25:
        ai_score += min(8.0, (volume_ratio - 1.0) * 5.0)
    if adx >= 18 and adx_slope >= 0:
        ai_score += min(7.0, (adx - 18.0) * 0.4 + adx_slope * 0.4)
    if expected_move > 0:
        ai_score += min(7.0, expected_move * 2.2)

    # Phase authority.
    if phase in {PHASE_PRE_START, PHASE_START}:
        ai_score += 14.0
    elif phase == PHASE_EARLY:
        ai_score += 7.0
    elif phase == PHASE_MID:
        # MID is not ideal, but for 5M/15M scalping it can still be a valid continuation entry.
        ai_score -= 10.0
    elif phase == PHASE_RANGE_AFTER_MOVE:
        # Previously this was a near-hard rejection (-45). That made the bot too dry:
        # many strong candidates with LOW trap were forced to REJECT. Keep caution, not a kill-switch.
        ai_score -= 16.0
    elif phase == PHASE_EXHAUSTION:
        # True exhaustion remains dangerous.
        ai_score -= 42.0
    elif phase == PHASE_RANGE:
        ai_score -= 8.0

    trap_score = _safe_float(trap.get("trap_risk_score"), 0.0)
    reversal_score = _safe_float(reversal.get("reversal_risk_score"), 0.0)
    ai_score -= trap_score * 0.18
    ai_score -= reversal_score * 0.16

    # Historical learning is a soft adviser, not ruler.
    sim_adj = 0.0
    if isinstance(learning, dict):
        for key in ("rank_adjustment", "score_adjustment", "adjustment", "ai_adjustment"):
            if learning.get(key) is not None:
                sim_adj = _safe_float(learning.get(key), 0.0)
                break
        if sim_adj == 0.0 and learning.get("win_rate") is not None:
            matches = _safe_int(learning.get("matches") or learning.get("samples") or learning.get("similar_count"), 0)
            wr = _safe_float(learning.get("win_rate"), 50.0)
            sim_adj = (wr - 52.0) / 8.0 * min(1.0, matches / 20.0)
    ai_score += max(-7.0, min(7.0, sim_adj))

    # Daily/condition risk is a soft-but-meaningful penalty.
    if isinstance(risk_state, dict):
        strict = _safe_int(risk_state.get("strictness_level"), 0)
        risk_score = _safe_float(risk_state.get("risk_score"), 0.0)
        sl_count = _safe_int(risk_state.get("sl_count"), 0)
        ai_score -= min(18.0, strict * 3.0 + risk_score * 0.06 + max(0, sl_count - 1) * 2.0)

    ai_score = round(max(0.0, min(100.0, ai_score)), 4)

    evidence_count = len(direction_info.get("reasons", []) or [])
    trap_level = str(trap.get("trap_risk") or "LOW").upper()
    reversal_level = str(reversal.get("reversal_risk") or "LOW").upper()
    trap_score_value = _safe_float(trap.get("trap_risk_score"), 0.0)
    reversal_score_value = _safe_float(reversal.get("reversal_risk_score"), 0.0)

    strong_evidence = (
        direction in {"LONG", "SHORT"}
        and evidence_count >= MIN_SOFT_EVIDENCE
        and strength >= MIN_SOFT_STRENGTH
        and trap_level != "HIGH"
    )
    low_risk_context = trap_level == "LOW" and reversal_level != "HIGH"

    early_phase = phase in {PHASE_PRE_START, PHASE_START, PHASE_EARLY}
    mid_phase = phase == PHASE_MID
    soft_range_phase = phase == PHASE_RANGE_AFTER_MOVE and low_risk_context and strong_evidence

    setup_detected = (
        direction in {"LONG", "SHORT"}
        and (
            (early_phase and (freshness >= 52 or ai_score >= DEFAULT_AI_GHOST_THRESHOLD))
            or (mid_phase and ai_score >= SOFT_RANGE_GHOST_THRESHOLD and strong_evidence)
            or (soft_range_phase and ai_score >= SOFT_RANGE_GHOST_THRESHOLD)
            or ai_score >= DEFAULT_AI_GHOST_THRESHOLD
        )
    )

    entry_confirmed = (
        direction in {"LONG", "SHORT"}
        and (
            (phase in {PHASE_START, PHASE_EARLY} and ai_score >= DEFAULT_AI_REAL_THRESHOLD)
            or (mid_phase and strong_evidence and low_risk_context and ai_score >= SOFT_MID_REAL_THRESHOLD)
            or (soft_range_phase and ai_score >= SOFT_RANGE_REAL_THRESHOLD)
        )
    )
    if phase == PHASE_PRE_START:
        entry_confirmed = False

    hard_reject_reasons: List[str] = []
    if direction not in {"LONG", "SHORT"}:
        hard_reject_reasons.append("NO_CLEAR_DIRECTION")
    # Keep true exhaustion as hard reject, but do not kill RANGE_AFTER_MOVE by label alone.
    if phase == PHASE_EXHAUSTION and not (ai_score >= 88 and trap_level == "LOW" and reversal_level != "HIGH"):
        hard_reject_reasons.append("MOVE_EXHAUSTION")
    if phase == PHASE_RANGE_AFTER_MOVE and not soft_range_phase and ai_score < SOFT_RANGE_GHOST_THRESHOLD:
        hard_reject_reasons.append("RANGE_AFTER_MOVE_WEAK")
    if trap_level == "HIGH" and ai_score < 86:
        hard_reject_reasons.append("HIGH_TRAP_RISK")
    if reversal_level == "HIGH" and ai_score < 86:
        hard_reject_reasons.append("HIGH_REVERSAL_RISK")
    if trap_score_value >= 85 and ai_score < 90:
        hard_reject_reasons.append("EXTREME_TRAP_SCORE")
    if reversal_score_value >= 88 and ai_score < 90:
        hard_reject_reasons.append("EXTREME_REVERSAL_SCORE")

    if hard_reject_reasons:
        decision = DECISION_REJECT
    elif entry_confirmed:
        decision = DECISION_REAL
    elif setup_detected or ai_score >= DEFAULT_AI_GHOST_THRESHOLD:
        decision = DECISION_GHOST
    else:
        decision = DECISION_REJECT

    movement_type = MOVE_NONE
    if direction == "LONG":
        movement_type = MOVE_PUMP_START if entry_confirmed else MOVE_PUMP_SETUP
    elif direction == "SHORT":
        movement_type = MOVE_DUMP_START if entry_confirmed else MOVE_DUMP_SETUP

    levels = build_trade_levels(snapshot, direction)

    result = {
        "ok": True,
        "version": VERSION,
        "symbol": symbol,
        "status": "ACTIVE" if decision == DECISION_REAL else ("SETUP" if decision == DECISION_GHOST else "NO_SIGNAL"),
        "decision": decision,
        "ai_decision_type": decision,
        "ai_score": ai_score,
        "score": ai_score,  # compatibility: display only; not classic score
        "classic_score_disabled": True,
        "direction": direction if direction in {"LONG", "SHORT"} else None,
        "direction_strength": direction_info.get("direction_strength"),
        "long_points": direction_info.get("long_points"),
        "short_points": direction_info.get("short_points"),
        "movement_type": movement_type,
        "move_phase": phase,
        "move_state": phase,
        "freshness_score": freshness,
        "freshness": "HIGH" if freshness >= 75 else ("MEDIUM" if freshness >= 55 else "LOW"),
        "move_done_pct": phase_info.get("move_done_pct"),
        "setup_detected": bool(setup_detected),
        "entry_confirmed": bool(entry_confirmed),
        "trap_risk": trap.get("trap_risk"),
        "trap_risk_score": trap.get("trap_risk_score"),
        "reversal_risk": reversal.get("reversal_risk"),
        "reversal_risk_score": reversal.get("reversal_risk_score"),
        "entry": levels.get("entry"),
        "price": levels.get("entry") or _first_number(snapshot, ["price", "entry", "close"], 0.0),
        "stop_loss": levels.get("stop_loss"),
        "tp1": levels.get("tp1"),
        "tp2": levels.get("tp2"),
        "risk_reward": levels.get("risk_reward"),
        "risk_level": "LOW" if ai_score >= 82 and trap.get("trap_risk") == "LOW" else ("MEDIUM" if ai_score >= 65 else "HIGH"),
        "validity": "15 تا 45 دقیقه",
        "entry_mode": "AI_MOVEMENT_HUNTER",
        "movement_hunter": {
            "decision": decision,
            "ai_score": ai_score,
            "phase": phase,
            "freshness_score": freshness,
            "move_done_pct": phase_info.get("move_done_pct"),
            "movement_type": movement_type,
            "setup_detected": bool(setup_detected),
            "entry_confirmed": bool(entry_confirmed),
            "classic_engine_role": "SENSOR_ONLY",
            "evidence_count": evidence_count,
            "strong_evidence": bool(strong_evidence),
            "soft_range_phase": bool(soft_range_phase),
        },
        "ai_layers": {
            "direction": direction_info,
            "phase": phase_info,
            "trap": trap,
            "reversal": reversal,
            "learning": learning,
            "coin_risk": risk_state,
            "market_breadth": market,
            "trade_levels": levels,
        },
        "snapshot": {
            **snapshot,
            "ai_movement_hunter": {
                "decision": decision,
                "ai_score": ai_score,
                "direction": direction,
                "phase": phase,
                "freshness_score": freshness,
                "move_done_pct": phase_info.get("move_done_pct"),
                "trap_risk": trap.get("trap_risk"),
                "reversal_risk": reversal.get("reversal_risk"),
                "movement_type": movement_type,
                "classic_score_disabled": True,
            },
            "move_phase": phase,
            "move_state": phase,
            "freshness_score": freshness,
            "trap_risk": trap.get("trap_risk"),
            "reversal_risk": reversal.get("reversal_risk"),
            "result_source": "AI_MOVEMENT_HUNTER",
            "evidence_count": evidence_count,
            "strong_evidence": bool(strong_evidence),
            "soft_range_phase": bool(soft_range_phase),
        },
        "reasons": (
            direction_info.get("reasons", [])
            + [phase_info.get("reason")]
            + trap.get("reasons", [])
            + reversal.get("reasons", [])
            + hard_reject_reasons
        )[:12],
        "created_at": _now(),
    }

    _update_ai_movement_memory(result)
    return result


# ---------------------------------------------------------------------------
# Compatibility aliases expected by different files
# ---------------------------------------------------------------------------

def ai_movement_decision(raw: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    return decide_movement(raw, **kwargs)


def decide(raw: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    return decide_movement(raw, **kwargs)


def analyze_movement(raw: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    return decide_movement(raw, **kwargs)


def evaluate_candidate(raw: Dict[str, Any], **kwargs) -> Dict[str, Any]:
    return decide_movement(raw, **kwargs)


def should_send_real_signal(raw: Dict[str, Any]) -> bool:
    return decide_movement(raw).get("decision") == DECISION_REAL


__all__ = [
    "VERSION",
    "DECISION_REAL",
    "DECISION_GHOST",
    "DECISION_REJECT",
    "PHASE_PRE_START",
    "PHASE_START",
    "PHASE_EARLY",
    "PHASE_MID",
    "PHASE_EXHAUSTION",
    "PHASE_RANGE_AFTER_MOVE",
    "build_feature_snapshot",
    "detect_direction",
    "detect_move_phase",
    "detect_trap_risk",
    "detect_reversal_risk",
    "build_trade_levels",
    "decide_movement",
    "ai_movement_decision",
    "decide",
    "analyze_movement",
    "evaluate_candidate",
    "should_send_real_signal",
]
