# -*- coding: utf-8 -*-
"""
scanner.py

AI-controlled market scanner for the crypto futures bot.

Purpose:
- Scan configured symbols using analysis.analyze_symbol.
- Accept only technically valid ACTIVE signals.
- Apply AI Decision Layer before final selection:
    coin_risk.py       -> daily/long-term risk and strictness
    coin_learning.py   -> coin personality, SL/TP patterns, extra strength
    coin_rotation.py   -> symbol priority/penalty when available
- Rank final candidates by ai_final_rank, not raw classic score only.
- Save good but unused/rejected candidates as Ghost signals when appropriate.

This keeps the public function names used by bot.py:
    scan_market
    scan_for_auto_signals
    get_best_signal
    get_top_signals
    scan_market_overview
    scan_symbols_for_signals
    find_best_signal
    find_top_signals
"""

import time
from typing import Dict, List, Optional, Any, Tuple

from analysis import analyze_symbol, add_indicators, get_klines, ema_direction

try:
    from config import SCAN_SYMBOLS, AUTO_DIRECT_SCORE_MIN
except Exception:
    SCAN_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    AUTO_DIRECT_SCORE_MIN = 82

try:
    from slot_manager import get_free_slots, is_symbol_direction_active, select_best_candidates
except Exception:
    get_free_slots = None
    is_symbol_direction_active = None
    select_best_candidates = None

try:
    from ghost_signals import create_ghost_signal
except Exception:
    create_ghost_signal = None

try:
    from coin_rotation import sort_symbols_by_rotation, get_symbol_rotation_score
except Exception:
    sort_symbols_by_rotation = None
    get_symbol_rotation_score = None

try:
    from coin_risk import get_direction_risk_state
except Exception:
    get_direction_risk_state = None

try:
    from coin_learning import should_require_extra_strength, get_smart_tp_suggestion
except Exception:
    should_require_extra_strength = None
    get_smart_tp_suggestion = None


SCAN_DELAY_SECONDS = 0.05
MAX_SCAN_RESULTS = 10
MIN_SCANNER_SCORE = int(AUTO_DIRECT_SCORE_MIN or 82)


def normalize_symbol(symbol):
    s = str(symbol).upper().strip()
    return s if s.endswith("USDT") else f"{s}USDT"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def get_scan_symbols():
    symbols = list(dict.fromkeys([normalize_symbol(x) for x in SCAN_SYMBOLS if str(x).strip()]))
    if sort_symbols_by_rotation:
        try:
            rotated = sort_symbols_by_rotation(symbols)
            if isinstance(rotated, list) and rotated:
                return rotated
        except Exception:
            pass
    return symbols


def _base_valid_signal(r: Dict[str, Any]) -> bool:
    return (
        isinstance(r, dict)
        and r.get("status") == "ACTIVE"
        and bool(r.get("entry_confirmed"))
        and r.get("direction") in ["LONG", "SHORT"]
        and _safe_int(r.get("score"), 0) >= MIN_SCANNER_SCORE
        and r.get("entry") is not None
        and r.get("stop_loss") is not None
        and r.get("tp1") is not None
    )


def _build_scanner_snapshot(r: Dict[str, Any]) -> Dict[str, Any]:
    snap = r.get("snapshot") if isinstance(r.get("snapshot"), dict) else {}
    out = dict(snap)
    for key in [
        "symbol", "direction", "entry", "price", "score", "long_score", "short_score",
        "risk_level", "risk_reward", "confirmations", "freshness", "rsi", "rsi_5m",
        "adx", "macd", "macd_signal", "macd_hist", "power2_buy", "power2_sell",
        "power3_buy", "power3_sell", "buy_power", "sell_power", "atr",
        "market_mode", "market_regime", "coin_behavior", "btc_bias",
        "support", "resistance", "vwap_status", "entry_mode",
    ]:
        if key not in out and r.get(key) is not None:
            out[key] = r.get(key)
    return out


def _get_rotation_score(symbol: str) -> float:
    if not get_symbol_rotation_score:
        return 0.0
    try:
        val = get_symbol_rotation_score(symbol)
        if isinstance(val, dict):
            for k in ["score", "rotation_score", "priority", "rank_score"]:
                if val.get(k) is not None:
                    return _safe_float(val.get(k), 0.0)
            return 0.0
        return _safe_float(val, 0.0)
    except Exception:
        return 0.0


def _risk_adjustment(symbol: str, direction: str) -> Tuple[Dict[str, Any], float, int, List[str]]:
    if not get_direction_risk_state:
        return {}, 0.0, 0, []
    try:
        state = get_direction_risk_state(symbol, direction)
    except Exception:
        return {}, 0.0, 0, []

    strict = _safe_int(state.get("strictness_level"), 0)
    risk_score = _safe_float(state.get("risk_score"), 0.0)
    penalty = min(18.0, strict * 3.0 + risk_score * 0.08)
    extra_conf = min(2, max(0, strict // 2))
    reasons = []
    if strict > 0:
        reasons.append(f"AI Risk strict={strict}")
    if risk_score >= 40:
        reasons.append(f"AI Risk score={int(risk_score)}")
    if state.get("recommend_reduce"):
        penalty += 3.0
        reasons.append("AI Risk recommend_reduce")
    return state if isinstance(state, dict) else {}, penalty, extra_conf, reasons


def _learning_adjustment(symbol: str, direction: str, snapshot: Dict[str, Any]) -> Tuple[Dict[str, Any], float, int, List[str]]:
    if not should_require_extra_strength:
        return {}, 0.0, 0, []
    try:
        extra = should_require_extra_strength(symbol, direction, snapshot=snapshot)
    except TypeError:
        try:
            extra = should_require_extra_strength(symbol, direction, snapshot)
        except Exception:
            extra = {}
    except Exception:
        extra = {}

    if not isinstance(extra, dict):
        extra = {}
    extra_score = _safe_float(extra.get("extra_score"), 0.0)
    extra_conf = _safe_int(extra.get("extra_confirmations"), 0)
    reasons = []
    if extra.get("required") or extra_score or extra_conf:
        if extra.get("reason"):
            reasons.append(str(extra.get("reason")))
        else:
            reasons.append("AI Learning extra strength")
    return extra, min(12.0, max(0.0, extra_score)), min(2, max(0, extra_conf)), reasons


def _classic_rank_value(r: Dict[str, Any]) -> float:
    score = _safe_float(r.get("score"), 0.0)
    conf = _safe_float(r.get("confirmations"), 0.0)
    rr = _safe_float(r.get("risk_reward"), 0.0)
    risk = {"LOW": 4.0, "MEDIUM": 2.0}.get(str(r.get("risk_level") or "").upper(), 0.0)
    fresh = {"HIGH": 3.0, "MEDIUM": 1.0}.get(str(r.get("freshness") or "").upper(), 0.0)
    return score + conf * 1.5 + rr * 2.0 + risk + fresh


def apply_ai_scanner_decision(r: Dict[str, Any]) -> Dict[str, Any]:
    """Attach AI-controlled ranking/approval fields to a signal.

    analysis.py already applies the primary AI decision. Scanner adds the final
    portfolio/selection layer so weak learned coin-directions are deprioritized
    or ghosted instead of being chosen only by raw score.
    """
    r = dict(r)
    symbol = normalize_symbol(r.get("symbol"))
    direction = str(r.get("direction") or "").upper()
    snapshot = _build_scanner_snapshot(r)

    base_score = _safe_float(r.get("score"), 0.0)
    base_rank = _classic_rank_value(r)
    base_min = max(MIN_SCANNER_SCORE, _safe_int(r.get("min_score"), MIN_SCANNER_SCORE))
    base_req_conf = _safe_int(r.get("required_confirmations"), 0)
    actual_conf = _safe_int(r.get("confirmations"), 0)

    risk_state, risk_penalty, risk_extra_conf, risk_reasons = _risk_adjustment(symbol, direction)
    learning_state, learning_penalty, learning_extra_conf, learning_reasons = _learning_adjustment(symbol, direction, snapshot)
    rotation_score = _get_rotation_score(symbol)

    # Rotation is a priority layer, not a hard filter. Keep it small.
    rotation_adj = max(-4.0, min(4.0, rotation_score / 25.0 if abs(rotation_score) > 4 else rotation_score))

    final_score = base_score - risk_penalty - learning_penalty + rotation_adj
    required_score = base_min + learning_penalty + min(6.0, risk_penalty * 0.35)
    required_confirmations = base_req_conf + risk_extra_conf + learning_extra_conf

    approved = True
    reject_reasons: List[str] = []
    if final_score < required_score:
        approved = False
        reject_reasons.append(f"AI final score {round(final_score,1)} < required {round(required_score,1)}")
    if required_confirmations and actual_conf < required_confirmations:
        approved = False
        reject_reasons.append(f"confirmations {actual_conf} < required {required_confirmations}")

    # Do not fully erase high-quality analysis results; if rejected, they can
    # become Ghost signals for learning instead of Telegram/live entry.
    ai_rank = base_rank + (final_score - base_score) + rotation_adj

    r["symbol"] = symbol
    r["snapshot"] = snapshot
    r["coin_risk"] = risk_state
    r["ai_learning"] = learning_state
    r["rotation_score"] = rotation_score
    r["ai_scanner"] = {
        "approved": approved,
        "base_score": round(base_score, 4),
        "final_score": round(final_score, 4),
        "required_score": round(required_score, 4),
        "base_rank": round(base_rank, 4),
        "ai_final_rank": round(ai_rank, 4),
        "risk_penalty": round(risk_penalty, 4),
        "learning_penalty": round(learning_penalty, 4),
        "rotation_adjustment": round(rotation_adj, 4),
        "required_confirmations": required_confirmations,
        "actual_confirmations": actual_conf,
        "reasons": risk_reasons + learning_reasons + reject_reasons,
    }
    r["ai_final_score"] = round(final_score, 4)
    r["ai_final_rank"] = round(ai_rank, 4)
    r["ai_scanner_approved"] = approved

    # Optional learned TP suggestion is attached but does not force TP changes
    # unless later analysis/order code explicitly chooses to use it.
    if get_smart_tp_suggestion:
        try:
            smart_tp = get_smart_tp_suggestion(symbol, direction, snapshot=snapshot)
            if isinstance(smart_tp, dict) and smart_tp:
                r["smart_tp_suggestion"] = smart_tp
        except Exception:
            pass

    return r


def is_valid_signal(r):
    if not _base_valid_signal(r):
        return False
    enriched = apply_ai_scanner_decision(r)
    return bool(enriched.get("ai_scanner_approved"))


def signal_rank_value(r):
    try:
        return _safe_float(r.get("ai_final_rank"), _classic_rank_value(r))
    except Exception:
        return _classic_rank_value(r)


def should_skip_duplicate(r):
    if not is_symbol_direction_active:
        return False
    try:
        return bool(is_symbol_direction_active(r.get("symbol"), r.get("direction")))
    except Exception:
        return False


def save_as_ghost(r, reason="SLOT_FULL"):
    if not create_ghost_signal:
        return False
    try:
        rr = dict(r)
        snap = _build_scanner_snapshot(rr)
        if rr.get("ai_scanner"):
            snap["ai_scanner"] = rr.get("ai_scanner")
        create_ghost_signal(
            rr.get("symbol"),
            rr.get("direction"),
            rr.get("entry"),
            rr.get("stop_loss"),
            rr.get("tp1"),
            rr.get("tp2"),
            rr.get("score"),
            snap,
            "scanner",
            reason,
        )
        return True
    except Exception:
        return False


def scan_market(symbols: Optional[List[str]] = None, max_results: int = MAX_SCAN_RESULTS, allow_ghost: bool = True):
    symbols = symbols or get_scan_symbols()
    valid: List[Dict[str, Any]] = []
    ai_rejected: List[Dict[str, Any]] = []
    no_trade = 0
    errors = 0
    ghost_count = 0

    for sym in symbols:
        try:
            res = analyze_symbol(normalize_symbol(sym))
            if not _base_valid_signal(res):
                no_trade += 1
                continue

            enriched = apply_ai_scanner_decision(res)
            if should_skip_duplicate(enriched):
                continue

            if enriched.get("ai_scanner_approved"):
                valid.append(enriched)
            else:
                ai_rejected.append(enriched)
                if allow_ghost and save_as_ghost(enriched, "AI_SCANNER_REJECTED"):
                    ghost_count += 1
        except Exception:
            errors += 1
        time.sleep(SCAN_DELAY_SECONDS)

    valid.sort(key=signal_rank_value, reverse=True)
    return {
        "signals": valid[:max_results],
        "all_valid_signals": valid,
        "ai_rejected_signals": ai_rejected,
        "scanned": len(symbols),
        "no_trade_count": no_trade,
        "error_count": errors,
        "ghost_count": ghost_count,
        "timestamp": int(time.time()),
    }


def get_available_slots():
    if get_free_slots is None:
        return 1
    try:
        return max(0, int(get_free_slots()))
    except Exception:
        return 1


def scan_for_auto_signals(symbols: Optional[List[str]] = None, max_results: int = MAX_SCAN_RESULTS, allow_ghost: bool = True):
    sr = scan_market(symbols, max_results, allow_ghost)
    valid = sr.get("all_valid_signals", [])
    if not valid:
        sr["signals"] = []
        sr["mode"] = "NO_SIGNAL"
        return sr

    free = get_available_slots()
    sr["free_slots"] = free

    if free <= 0:
        gc = int(sr.get("ghost_count", 0) or 0)
        if allow_ghost:
            for sig in valid:
                if save_as_ghost(sig, "SLOT_FULL"):
                    gc += 1
        sr["signals"] = []
        sr["ghost_count"] = gc
        sr["mode"] = "GHOST_ONLY"
        return sr

    candidates = valid
    if select_best_candidates:
        try:
            selected = select_best_candidates(valid, min(max_results, free))
            if isinstance(selected, list) and selected:
                candidates = selected
        except Exception:
            candidates = valid

    # Final scanner authority: even if slot_manager returns candidates, sort by
    # AI final rank so learning/risk/rotation determine final priority.
    candidates = sorted(candidates, key=signal_rank_value, reverse=True)
    sr["signals"] = candidates[: min(max_results, free)]
    sr["mode"] = "ACTIVE_SIGNALS"
    return sr


def get_best_signal(symbols=None):
    r = scan_for_auto_signals(symbols, 1, False)
    return (r.get("signals") or [None])[0]


def get_top_signals(symbols=None, limit=5):
    return scan_for_auto_signals(symbols, limit, False).get("signals", [])[:limit]


def quick_market_bias(symbol):
    """Fast overview helper: only 1H + 15M, no full signal analysis.
    This keeps Telegram market overview from timing out.
    """
    df_1h = add_indicators(get_klines(normalize_symbol(symbol), "1h", limit=260))
    df_15m = add_indicators(get_klines(normalize_symbol(symbol), "15m", limit=260))
    t1 = ema_direction(df_1h)
    t15 = ema_direction(df_15m)
    l15 = df_15m.iloc[-1]
    score = 50
    if t1 == "bullish":
        score += 15
    if t15 == "bullish":
        score += 15
    if t1 == "bearish":
        score -= 15
    if t15 == "bearish":
        score -= 15
    if l15["close"] > l15["vwap"]:
        score += 5
    else:
        score -= 5
    if l15["macd"] > l15["macd_signal"]:
        score += 5
    else:
        score -= 5
    if l15["rsi"] >= 52:
        score += 5
    elif l15["rsi"] <= 48:
        score -= 5
    if t1 == "bullish" and t15 == "bullish":
        bias = "bullish"
    elif t1 == "bearish" and t15 == "bearish":
        bias = "bearish"
    else:
        bias = "neutral"
    return {
        "symbol": normalize_symbol(symbol),
        "bias": bias,
        "direction": "OVERVIEW",
        "score": max(0, min(100, int(score))),
        "trend_1h": t1,
        "trend_15m": t15,
    }


def scan_market_overview(symbols=None, limit=40):
    symbols = (symbols or get_scan_symbols())[:limit]
    bullish = bearish = neutral = errors = 0
    details = []
    error_details = []
    for sym in symbols:
        try:
            r = quick_market_bias(sym)
            bias = r.get("bias")
            if bias == "bullish":
                bullish += 1
            elif bias == "bearish":
                bearish += 1
            else:
                neutral += 1
            details.append(r)
        except Exception as e:
            errors += 1
            if len(error_details) < 5:
                error_details.append({"symbol": normalize_symbol(sym), "error": str(e)[:160]})
        time.sleep(SCAN_DELAY_SECONDS)
    total = max(bullish + bearish + neutral, 1)
    bp = round(bullish / total * 100, 1)
    sp = round(bearish / total * 100, 1)
    np = round(neutral / total * 100, 1)
    if bp >= 50:
        mb = "bullish"
        summary = "بازار بیشتر صعودی است"
    elif sp >= 50:
        mb = "bearish"
        summary = "بازار بیشتر نزولی است"
    elif np >= 45:
        mb = "neutral"
        summary = "بازار بیشتر رنج یا نامشخص است"
    elif bp > sp:
        mb = "slightly_bullish"
        summary = "بازار کمی تمایل صعودی دارد"
    elif sp > bp:
        mb = "slightly_bearish"
        summary = "بازار کمی تمایل نزولی دارد"
    else:
        mb = "neutral"
        summary = "بازار جهت مشخصی ندارد"
    return {
        "market_bias": mb,
        "summary": summary,
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "errors": errors,
        "error_details": error_details,
        "bullish_pct": bp,
        "bearish_pct": sp,
        "neutral_pct": np,
        "details": details,
        "scanned": len(symbols),
        "timestamp": int(time.time()),
    }


def scan_symbols_for_signals(symbols=None, max_results=MAX_SCAN_RESULTS):
    return scan_for_auto_signals(symbols, max_results, True).get("signals", [])


def find_best_signal(symbols=None):
    return get_best_signal(symbols)


def find_top_signals(symbols=None, limit=5):
    return get_top_signals(symbols, limit)
