# -*- coding: utf-8 -*-
"""
coin_rotation.py

AI Coin Rotation / Priority Engine for the crypto futures bot.

Purpose:
- Rank scan symbols before market scanning.
- Provide direction-aware rotation scores for scanner.py.
- Use REAL + GHOST learning, coin personality, direction archive,
  daily/long-term coin risk, and market-mode compatibility.
- Keep rotation soft: it prioritizes/reduces symbols, it does not hard-ban
  unless other modules decide to block a signal.

Public functions kept for compatibility:
    get_coin_rotation_score(symbol)
    get_symbol_rotation_score(symbol, direction=None, snapshot=None)
    sort_symbols_by_rotation(symbols)
    format_rotation_report()
"""

from typing import List, Dict, Any, Optional, Tuple

try:
    from config import SCAN_SYMBOLS
except Exception:
    SCAN_SYMBOLS = []

try:
    from data_store import load_json
except Exception:
    load_json = None

try:
    from coin_risk import get_direction_risk_state
except Exception:
    get_direction_risk_state = None

LEARNING_FILE = "coin_learning.json"
BASE_SCORE = 70
MIN_SCORE = 0
MAX_SCORE = 100


# -------------------- safe helpers --------------------

def _clamp(value, low=MIN_SCORE, high=MAX_SCORE):
    try:
        return max(low, min(high, int(round(float(value)))))
    except Exception:
        return low


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value, default=0):
    try:
        return int(float(value or 0))
    except Exception:
        return int(default)


def _norm_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    return s if s.endswith("USDT") else f"{s}USDT"


def _norm_direction(direction: Optional[str]) -> Optional[str]:
    if direction is None:
        return None
    d = str(direction).upper().strip()
    return d if d in {"LONG", "SHORT"} else None


# -------------------- data loading --------------------

def _load_learning_state() -> Dict[str, Any]:
    if load_json:
        try:
            data = load_json(LEARNING_FILE, {"by_coin_direction": {}, "coin_archive": {}})
            return data if isinstance(data, dict) else {"by_coin_direction": {}, "coin_archive": {}}
        except Exception:
            pass
    return {"by_coin_direction": {}, "coin_archive": {}}


def _learning_bucket(symbol: str, direction: str) -> Dict[str, Any]:
    try:
        data = _load_learning_state()
        key = f"{_norm_symbol(symbol)}:{str(direction).upper()}"
        bucket = data.get("by_coin_direction", {}).get(key, {})
        return bucket if isinstance(bucket, dict) else {}
    except Exception:
        return {}


def _coin_archive(symbol: str) -> Dict[str, Any]:
    try:
        data = _load_learning_state()
        row = data.get("coin_archive", {}).get(_norm_symbol(symbol), {})
        return row if isinstance(row, dict) else {}
    except Exception:
        return {}


# -------------------- risk + learning models --------------------

def _direction_risk(symbol: str, direction: str) -> Dict[str, Any]:
    if not get_direction_risk_state:
        return {}
    try:
        r = get_direction_risk_state(_norm_symbol(symbol), direction)
        return r if isinstance(r, dict) else {}
    except Exception:
        return {}


def _weighted_counts(bucket: Dict[str, Any]) -> Tuple[float, float, float]:
    """Return weighted TP, weighted SL, weighted total.

    coin_learning.py stores weighted_tp/weighted_sl using REAL=1 and GHOST<1.
    If not available, fall back to raw counts while still reading real/ghost split.
    """
    tp_w = _safe_float(bucket.get("weighted_tp"), None)
    sl_w = _safe_float(bucket.get("weighted_sl"), None)
    if tp_w is not None and sl_w is not None and (tp_w + sl_w) > 0:
        return tp_w, sl_w, tp_w + sl_w

    real_tp = _safe_float(bucket.get("real_tp"), 0.0)
    real_sl = _safe_float(bucket.get("real_sl"), 0.0)
    ghost_tp = _safe_float(bucket.get("ghost_tp"), 0.0)
    ghost_sl = _safe_float(bucket.get("ghost_sl"), 0.0)
    if real_tp + real_sl + ghost_tp + ghost_sl > 0:
        tp = real_tp + ghost_tp * 0.45
        sl = real_sl + ghost_sl * 0.45
        return tp, sl, tp + sl

    tp = _safe_float(bucket.get("tp1"), 0.0) + _safe_float(bucket.get("tp2"), 0.0)
    sl = _safe_float(bucket.get("sl"), 0.0)
    return tp, sl, tp + sl


def _behavior_adjustment(bucket: Dict[str, Any]) -> int:
    behavior = str(bucket.get("behavior") or "UNKNOWN").upper()
    personality = str(bucket.get("personality") or "UNKNOWN").upper()
    adj = 0
    if behavior == "GOOD":
        adj += 10
    elif behavior == "NORMAL":
        adj += 3
    elif behavior == "WEAK":
        adj -= 7
    elif behavior == "BAD":
        adj -= 15

    # Backward-compatible old personalities + TP/SL v2 personalities from
    # coin_learning.py. This stays soft; scanner/coin_risk still make final blocks.
    if personality in {"TREND_FRIENDLY", "CLEAN_RUNNER"}:
        adj += 6
    elif personality == "NORMAL":
        adj += 0
    elif personality == "LOW_REACH":
        adj -= 6
    elif personality == "WICKY":
        adj -= 5
    elif personality == "FAKE_BREAK_RISK":
        adj -= 8
    elif personality == "RISKY_DIRECTION":
        adj -= 9
    return adj


def _market_compatibility_adjustment(bucket: Dict[str, Any], direction: str, snapshot: Optional[Dict[str, Any]]) -> int:
    if not isinstance(snapshot, dict):
        snapshot = {}
    market = str(snapshot.get("market_mode") or snapshot.get("market_regime") or "").upper()
    btc = str(snapshot.get("btc_bias") or "").upper()
    adj = 0

    if market:
        if direction == "LONG" and any(x in market for x in ["BULL", "UP"]):
            adj += 3
        elif direction == "SHORT" and any(x in market for x in ["BEAR", "DOWN"]):
            adj += 3
        elif direction == "LONG" and any(x in market for x in ["BEAR", "DOWN"]):
            adj -= 5
        elif direction == "SHORT" and any(x in market for x in ["BULL", "UP"]):
            adj -= 5
        elif "RANGE" in market or "NEUTRAL" in market:
            adj -= 2

    if btc:
        if direction == "LONG" and "BULL" in btc:
            adj += 2
        elif direction == "SHORT" and "BEAR" in btc:
            adj += 2
        elif direction == "LONG" and "BEAR" in btc:
            adj -= 3
        elif direction == "SHORT" and "BULL" in btc:
            adj -= 3
    return adj


def _sl_pattern_adjustment(bucket: Dict[str, Any], snapshot: Optional[Dict[str, Any]]) -> int:
    if not isinstance(snapshot, dict):
        return 0
    patterns = bucket.get("sl_patterns", [])[-25:]
    if not isinstance(patterns, list) or not patterns:
        return 0

    rsi = snapshot.get("rsi")
    adx = snapshot.get("adx")
    vwap = snapshot.get("vwap_status")
    market = snapshot.get("market_mode") or snapshot.get("market_regime")
    hits = 0
    for ev in patterns:
        sp = ev.get("snapshot", {}) if isinstance(ev, dict) else {}
        score = 0
        if rsi is not None and sp.get("rsi") is not None and abs(_safe_float(sp.get("rsi")) - _safe_float(rsi)) <= 5:
            score += 1
        if adx is not None and sp.get("adx") is not None and abs(_safe_float(sp.get("adx")) - _safe_float(adx)) <= 6:
            score += 1
        if vwap and sp.get("vwap_status") == vwap:
            score += 1
        if market and (sp.get("market_mode") == market or sp.get("market_regime") == market):
            score += 1
        if score >= 3:
            hits += 1
    return -min(10, hits * 3)


def _weighted_avg(bucket: Dict[str, Any], sum_key: str, weight_key: str, default: float = 0.0) -> float:
    w = _safe_float(bucket.get(weight_key), 0.0)
    if w <= 0:
        return float(default)
    return _safe_float(bucket.get(sum_key), 0.0) / max(w, 1e-9)


def _tp_sl_v2_adjustment(bucket: Dict[str, Any]) -> tuple[int, List[str]]:
    """Soft rotation adjustment from TP/SL v2 memory.

    Uses fields written by coin_learning.py: avg reachable TP in ATR, adverse
    wick survival, fake/clean breakout behavior and max favorable movement.
    It never hard-bans a coin; it only changes priority for scanner selection.
    """
    samples = _safe_int((bucket.get("tp_sl_v2") or {}).get("samples"), 0)
    if samples < 3:
        return 0, []

    adj = 0
    reasons: List[str] = []

    avg_tp1 = _weighted_avg(bucket, "tp1_reach_atr_sum", "tp1_reach_atr_weight", 0.0)
    avg_tp2 = _weighted_avg(bucket, "tp2_reach_atr_sum", "tp2_reach_atr_weight", 0.0)
    avg_sl = _weighted_avg(bucket, "sl_distance_atr_sum", "sl_distance_atr_weight", 0.0)
    avg_fav = _weighted_avg(bucket, "max_favorable_atr_sum", "max_favorable_atr_weight", 0.0)
    avg_adv = _weighted_avg(bucket, "max_adverse_atr_sum", "max_adverse_atr_weight", 0.0)

    fake = _safe_int(bucket.get("fake_breakouts"), 0)
    clean = _safe_int(bucket.get("clean_breakouts"), 0)
    bounce = _safe_int(bucket.get("bounces"), 0)
    sr_total = max(1, fake + clean + bounce)
    fake_rate = fake / sr_total
    clean_rate = clean / sr_total
    bounce_rate = bounce / sr_total

    if avg_tp1 >= 0.85:
        adj += 3
        reasons.append("TP1 reach +3")
    elif 0 < avg_tp1 < 0.55:
        adj -= 4
        reasons.append("low TP1 reach -4")

    if avg_tp2 >= 1.45:
        adj += 3
        reasons.append("TP2 reach +3")
    elif 0 < avg_tp2 < 0.95 and samples >= 5:
        adj -= 2
        reasons.append("low TP2 reach -2")

    if avg_fav >= 1.25:
        adj += 3
        reasons.append("MFE +3")
    elif 0 < avg_fav < 0.75 and samples >= 5:
        adj -= 4
        reasons.append("weak MFE -4")

    # Wicky coins need more survival room; reduce priority only when adverse
    # movement is large compared with reachable profit.
    if avg_adv >= 1.35 and (avg_fav <= 0 or avg_adv > avg_fav * 0.95):
        adj -= 5
        reasons.append("SL survival risk -5")
    elif 0 < avg_sl <= 1.05 and avg_fav >= 1.15:
        adj += 2
        reasons.append("clean SL profile +2")

    if fake_rate >= 0.45 and fake >= 3:
        adj -= 7
        reasons.append("fake break -7")
    elif clean_rate >= 0.45 and clean >= 3:
        adj += 4
        reasons.append("clean break +4")
    elif bounce_rate >= 0.50 and bounce >= 3:
        adj += 2
        reasons.append("bounce +2")

    return max(-18, min(14, int(round(adj)))), reasons[:5]


def _direction_learning_score(symbol: str, direction: str, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    b = _learning_bucket(symbol, direction)
    tp_w, sl_w, total_w = _weighted_counts(b)
    raw_tp = _safe_int(b.get("tp1")) + _safe_int(b.get("tp2"))
    raw_sl = _safe_int(b.get("sl"))
    real_tp = _safe_int(b.get("real_tp"))
    real_sl = _safe_int(b.get("real_sl"))
    ghost_tp = _safe_int(b.get("ghost_tp"))
    ghost_sl = _safe_int(b.get("ghost_sl"))
    win_rate = (tp_w / total_w) if total_w > 0 else None

    adj = 0
    reasons: List[str] = []
    if total_w >= 2.5 and win_rate is not None:
        wr_adj = int(round((win_rate - 0.50) * 36))
        wr_adj = max(-18, min(18, wr_adj))
        adj += wr_adj
        if wr_adj > 0:
            reasons.append(f"WR+{wr_adj}")
        elif wr_adj < 0:
            reasons.append(f"WR{wr_adj}")

    if raw_sl >= 2 and raw_sl > raw_tp:
        p = min(14, (raw_sl - raw_tp + 1) * 4)
        adj -= p
        reasons.append(f"SL penalty -{p}")
    if real_sl >= 2 and real_sl > real_tp:
        p = min(10, (real_sl - real_tp + 1) * 5)
        adj -= p
        reasons.append(f"REAL SL -{p}")
    if ghost_sl >= 3 and ghost_sl > ghost_tp:
        p = min(7, (ghost_sl - ghost_tp) * 2)
        adj -= p
        reasons.append(f"GHOST SL -{p}")

    beh_adj = _behavior_adjustment(b)
    if beh_adj:
        adj += beh_adj
        reasons.append(f"behavior {beh_adj:+d}")

    market_adj = _market_compatibility_adjustment(b, direction, snapshot)
    if market_adj:
        adj += market_adj
        reasons.append(f"market {market_adj:+d}")

    pattern_adj = _sl_pattern_adjustment(b, snapshot)
    if pattern_adj:
        adj += pattern_adj
        reasons.append(f"SL pattern {pattern_adj:+d}")

    tp_sl_adj, tp_sl_reasons = _tp_sl_v2_adjustment(b)
    if tp_sl_adj:
        adj += tp_sl_adj
        reasons.append(f"TP/SL v2 {tp_sl_adj:+d}")
        reasons.extend(tp_sl_reasons)

    confidence = _safe_int(b.get("confidence"), 0)
    if total_w < 2:
        # With very low data, keep adjustment small so new coins are not buried.
        adj = max(-4, min(4, adj))

    return {
        "tp": raw_tp,
        "sl": raw_sl,
        "real_tp": real_tp,
        "real_sl": real_sl,
        "ghost_tp": ghost_tp,
        "ghost_sl": ghost_sl,
        "weighted_tp": round(tp_w, 4),
        "weighted_sl": round(sl_w, 4),
        "weighted_total": round(total_w, 4),
        "win_rate": None if win_rate is None else round(win_rate * 100, 1),
        "behavior": b.get("behavior", "UNKNOWN"),
        "personality": b.get("personality", "UNKNOWN"),
        "tp_sl_v2": b.get("tp_sl_v2", {}),
        "confidence": confidence,
        "adjustment": max(-40, min(40, int(round(adj)))),
        "reasons": reasons[:8],
    }


def _direction_daily_risk(symbol: str, direction: str) -> Dict[str, Any]:
    r = _direction_risk(symbol, direction)
    sl_count = _safe_int(r.get("sl_count", r.get("sl")))
    tp_count = _safe_int(r.get("tp_count", r.get("tp")))
    risk_score = _safe_float(r.get("risk_score"), 0.0)
    strict = _safe_int(r.get("strictness_level"), 0)
    recommend_reduce = bool(r.get("recommend_reduce"))

    # User rule: after 2 SL on same coin+direction, the 3rd signal must be stricter.
    strict_penalty = 0
    if sl_count >= 2:
        strict_penalty += min(18, (sl_count - 1) * 6)
    if strict > 0:
        strict_penalty += min(18, strict * 4)
    if recommend_reduce:
        strict_penalty += 5
    strict_penalty = min(28, strict_penalty)

    return {
        "sl_count": sl_count,
        "tp_count": tp_count,
        "risk_score": round(risk_score, 4),
        "strictness_level": strict,
        "recommend_reduce": recommend_reduce,
        "strict_penalty": strict_penalty,
        "state": r,
    }


# -------------------- public API --------------------

def get_symbol_rotation_score(symbol: str, direction: Optional[str] = None, snapshot: Optional[Dict[str, Any]] = None):
    """Return a scalar priority score for scanner.py.

    If direction is provided, return direction-specific priority. Without
    direction, return the best of LONG/SHORT because scanner symbol ordering
    should not punish a symbol whose one side is bad if the other side is good.
    """
    data = get_coin_rotation_score(symbol, snapshot=snapshot)
    d = _norm_direction(direction)
    if d:
        return data.get("direction_scores", {}).get(d, data.get("rotation_score", BASE_SCORE))
    return data.get("rotation_score", BASE_SCORE)


def get_coin_rotation_score(symbol: str, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    symbol = _norm_symbol(symbol)
    archive = _coin_archive(symbol)

    long_risk = _direction_daily_risk(symbol, "LONG")
    short_risk = _direction_daily_risk(symbol, "SHORT")
    long_learn = _direction_learning_score(symbol, "LONG", snapshot=snapshot)
    short_learn = _direction_learning_score(symbol, "SHORT", snapshot=snapshot)

    def direction_score(learn: Dict[str, Any], risk: Dict[str, Any]) -> int:
        # Direction score preserves LONG/SHORT archive separately.
        risk_penalty = min(30, int(round(_safe_float(risk.get("risk_score")) * 0.18)))
        strict_penalty = _safe_int(risk.get("strict_penalty"), 0)
        adj = _safe_int(learn.get("adjustment"), 0)
        confidence_bonus = min(5, _safe_int(learn.get("confidence"), 0) // 25) if adj > 0 else 0
        return _clamp(BASE_SCORE + adj + confidence_bonus - risk_penalty - strict_penalty)

    long_score = direction_score(long_learn, long_risk)
    short_score = direction_score(short_learn, short_risk)

    # Symbol scan priority uses the better side, not the sum, so a bad LONG
    # does not erase a strong SHORT opportunity on the same coin.
    best_direction = "LONG" if long_score >= short_score else "SHORT"
    best_score = max(long_score, short_score)
    avg_score = int(round((long_score + short_score) / 2))

    # If both directions are bad, symbol gets reduced. If one direction is good,
    # keep it scan-worthy and let analysis/scanner choose direction later.
    if best_score >= 82:
        status = "FAVOR"
    elif best_score >= 65:
        status = "NORMAL"
    elif best_score >= 45:
        status = "REDUCE"
    else:
        status = "AVOID"

    combined_risk = _safe_float(long_risk.get("risk_score")) + _safe_float(short_risk.get("risk_score"))
    learning_adjustment = _safe_int(long_learn.get("adjustment")) + _safe_int(short_learn.get("adjustment"))
    strict_penalty = _safe_int(long_risk.get("strict_penalty")) + _safe_int(short_risk.get("strict_penalty"))

    return {
        "symbol": symbol,
        "rotation_score": best_score,
        "priority_score": best_score,
        "average_score": avg_score,
        "best_direction": best_direction,
        "direction_scores": {"LONG": long_score, "SHORT": short_score},
        "risk_score": round(combined_risk, 4),
        "status": status,
        "learning_adjustment": learning_adjustment,
        "strict_penalty": strict_penalty,
        "coin_behavior": archive.get("behavior", "UNKNOWN"),
        "coin_best_direction": archive.get("best_direction", "UNKNOWN"),
        "coin_confidence": archive.get("confidence", 0),
        "long": {"score": long_score, "risk": long_risk, "learning": long_learn},
        "short": {"score": short_score, "risk": short_risk, "learning": short_learn},
    }


def sort_symbols_by_rotation(symbols: List[str]) -> List[str]:
    # Use best directional score so scanner still sees coins with one strong side.
    return sorted(symbols, key=lambda s: get_coin_rotation_score(s).get("rotation_score", BASE_SCORE), reverse=True)


def get_best_rotation_symbols(limit: int = 10) -> List[Dict[str, Any]]:
    rows = [get_coin_rotation_score(s) for s in SCAN_SYMBOLS[:80]]
    rows.sort(key=lambda x: x.get("rotation_score", 0), reverse=True)
    return rows[:limit]


def get_worst_rotation_symbols(limit: int = 10) -> List[Dict[str, Any]]:
    rows = [get_coin_rotation_score(s) for s in SCAN_SYMBOLS[:80]]
    rows.sort(key=lambda x: x.get("rotation_score", 0))
    return rows[:limit]


def format_rotation_report() -> str:
    rows = [get_coin_rotation_score(s) for s in SCAN_SYMBOLS[:40]]
    rows.sort(key=lambda x: x.get("rotation_score", 0), reverse=True)
    best = rows[:5]
    worst = rows[-5:]
    lines = ["🔄 Coin Rotation", "بهترین‌ها:"]
    for r in best:
        lines.append(
            f"{r['symbol']} | {r['rotation_score']} | بهتر: {r.get('best_direction')} | وضعیت: {r.get('status')}"
        )
    lines.append("ضعیف‌ترین‌ها:")
    for r in reversed(worst):
        lines.append(
            f"{r['symbol']} | {r['rotation_score']} | بهتر: {r.get('best_direction')} | وضعیت: {r.get('status')}"
        )
    return "\n".join(lines)
