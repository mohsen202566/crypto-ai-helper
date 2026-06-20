from __future__ import annotations

"""
Per-coin and per-direction learning engine.

Responsibilities:
- Read AI memory outcomes and convert them into compact behavior profiles.
- Learn indicator-range behavior per symbol + direction.
- Provide guidance to AI Movement Hunter without generating signals.
- Keep learning soft/ranking-first, not a hard global blocker.

Rules:
- Does not import bot.py, scanner.py, or real trade modules.
- Does not place trades.
- Uses ai_memory as the source of truth for detailed records.
"""

import math
import time
from typing import Any, Dict, List, Optional, Tuple

from config import CORE_DATA_FILES
from data_store import load_dict, save_json, backup_file, cache_get, cache_set
from diagnostics import safe
import ai_memory


COIN_LEARNING_FILE = CORE_DATA_FILES.get("coin_learning")


def _ts() -> int:
    return int(time.time())


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def _key(symbol: str, direction: str) -> str:
    return f"{str(symbol).upper()}::{str(direction).upper()}"


def _result_group(result: str) -> str:
    r = str(result or "").upper()
    if r.startswith("TP"):
        return "TP"
    if r == "SL":
        return "SL"
    if r in {"BE", "BREAKEVEN"}:
        return "BE"
    if r in {"NO_MOVE", "TIMEOUT"}:
        return "NO_MOVE"
    if r in {"CANCELLED", "CANCELED"}:
        return "CANCELLED"
    return r or "OPEN"


def _empty_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "updated_at": _ts(),
        "profiles": {},
        "daily_profiles": {},
        "indicator_rules": {},
        "tp_behavior": {},
        "risk_notes": {},
        "cache": {},
    }


@safe(default={})
def load_learning() -> Dict[str, Any]:
    st = load_dict(COIN_LEARNING_FILE)
    if not st:
        st = _empty_state()
        save_json(COIN_LEARNING_FILE, st)
    for k, v in _empty_state().items():
        st.setdefault(k, v)
    return st


@safe(default=False)
def save_learning(st: Dict[str, Any], make_backup: bool = False) -> bool:
    st["updated_at"] = _ts()
    return save_json(COIN_LEARNING_FILE, st, make_backup=make_backup)


def _profile_template() -> Dict[str, Any]:
    return {
        "samples": 0,
        "real_samples": 0,
        "ghost_samples": 0,
        "tp": 0,
        "sl": 0,
        "be": 0,
        "no_move": 0,
        "win_rate": 0.0,
        "ghost_win_rate": 0.0,
        "real_win_rate": 0.0,
        "personality": "UNKNOWN",
        "direction_bias": "UNKNOWN",
        "avg_mfe_pct": 0.0,
        "avg_mae_pct": 0.0,
        "avg_confidence_win": 0.0,
        "avg_confidence_loss": 0.0,
        "preferred_conditions": [],
        "danger_conditions": [],
        "last_updated": _ts(),
    }


def _add_avg(old: float, value: float, n: int) -> float:
    n = max(1, n)
    return round(((old * (n - 1)) + value) / n, 8)


@safe(default={})
def rebuild_from_ai_memory(limit: int = 20000) -> Dict[str, Any]:
    """
    Rebuild compact coin-learning profiles from AI memory records.
    Safe to run periodically. It does not delete ai_memory.
    """
    mem = ai_memory.load_memory()
    records = list(mem.get("records", {}).values())
    records = sorted(records, key=lambda r: r.get("updated_at", r.get("created_at", 0)))[-limit:]

    st = _empty_state()

    for rec in records:
        if rec.get("status") != "CLOSED":
            continue
        update_from_record(rec, state=st, autosave=False)

    save_learning(st, make_backup=True)
    return summary()


@safe(default=True)
def update_from_record(rec: Dict[str, Any], state: Optional[Dict[str, Any]] = None, autosave: bool = True) -> bool:
    st = state if state is not None else load_learning()

    symbol = str(rec.get("symbol", "")).upper()
    direction = str(rec.get("direction", "")).upper()
    if not symbol or not direction:
        return False

    key = _key(symbol, direction)
    profile = st.setdefault("profiles", {}).setdefault(key, _profile_template())
    result = _result_group(rec.get("result"))
    decision = str(rec.get("decision", "")).upper()

    profile["samples"] += 1
    if decision == "REAL":
        profile["real_samples"] += 1
    if decision == "GHOST":
        profile["ghost_samples"] += 1

    if result == "TP":
        profile["tp"] += 1
    elif result == "SL":
        profile["sl"] += 1
    elif result == "BE":
        profile["be"] += 1
    elif result == "NO_MOVE":
        profile["no_move"] += 1

    profile["win_rate"] = round(profile["tp"] / max(1, profile["tp"] + profile["sl"]) * 100, 2)

    # real/ghost WR from record scan using counters in indicator rules
    _learn_indicator_rules(st, key, rec)
    _learn_tp_behavior(st, key, rec)

    n = profile["samples"]
    profile["avg_mfe_pct"] = _add_avg(profile["avg_mfe_pct"], _safe_float(rec.get("max_profit_pct")), n)
    profile["avg_mae_pct"] = _add_avg(profile["avg_mae_pct"], _safe_float(rec.get("max_adverse_pct")), n)

    conf = _safe_float(rec.get("ai_confidence"))
    if result == "TP":
        profile["avg_confidence_win"] = _add_avg(profile["avg_confidence_win"], conf, max(1, profile["tp"]))
    if result == "SL":
        profile["avg_confidence_loss"] = _add_avg(profile["avg_confidence_loss"], conf, max(1, profile["sl"]))

    _refresh_personality(profile)
    profile["last_updated"] = _ts()

    if autosave:
        save_learning(st)
    return True


def _learn_indicator_rules(st: Dict[str, Any], key: str, rec: Dict[str, Any]) -> None:
    ind = rec.get("activation_indicators") or rec.get("indicators") or {}
    result = _result_group(rec.get("result"))
    decision = str(rec.get("decision", "")).upper()
    root = st.setdefault("indicator_rules", {}).setdefault(key, {})

    buckets = {
        "rsi": _bucket(_safe_float(ind.get("rsi")), 5, 0, 100),
        "adx": _bucket(_safe_float(ind.get("adx")), 5, 0, 60),
        "macd_hist": _signed_bucket(_safe_float(ind.get("macd_hist")), 0.0005, 0.05),
        "macd_slope": _signed_bucket(_safe_float(ind.get("macd_slope")), 0.0005, 0.05),
        "vwap_distance": _signed_bucket(_safe_float(ind.get("vwap_distance")), 0.10, 5),
        "power_2": _signed_bucket(_safe_float(ind.get("power_2")), 0.10, 10),
        "power_3": _signed_bucket(_safe_float(ind.get("power_3")), 0.10, 10),
        "fresh_momentum": _bucket(_safe_float(ind.get("fresh_momentum")) * 100, 10, 0, 100),
        "candle_quality": _bucket(_safe_float(ind.get("candle_quality")) * 100, 10, 0, 100),
    }

    for name, b in buckets.items():
        node = root.setdefault(name, {}).setdefault(b, {
            "samples": 0, "tp": 0, "sl": 0, "real_tp": 0, "real_sl": 0,
            "ghost_tp": 0, "ghost_sl": 0, "score": 0.0, "last_updated": _ts()
        })
        node["samples"] += 1
        if result == "TP":
            node["tp"] += 1
            if decision == "REAL":
                node["real_tp"] += 1
            if decision == "GHOST":
                node["ghost_tp"] += 1
        if result == "SL":
            node["sl"] += 1
            if decision == "REAL":
                node["real_sl"] += 1
            if decision == "GHOST":
                node["ghost_sl"] += 1
        node["score"] = round((node["tp"] - node["sl"] + node["ghost_tp"] * 0.3 - node["ghost_sl"] * 0.15) / max(1, node["samples"]), 4)
        node["last_updated"] = _ts()


def _learn_tp_behavior(st: Dict[str, Any], key: str, rec: Dict[str, Any]) -> None:
    node = st.setdefault("tp_behavior", {}).setdefault(key, {
        "samples": 0,
        "avg_reachable_profit_pct": 0.0,
        "median_hint_profit_pct": 0.0,
        "avg_adverse_pct": 0.0,
        "tp_too_far_count": 0,
        "sl_noise_count": 0,
        "last_updated": _ts(),
    })
    node["samples"] += 1
    n = node["samples"]
    max_profit = _safe_float(rec.get("max_profit_pct"))
    max_adv = _safe_float(rec.get("max_adverse_pct"))
    node["avg_reachable_profit_pct"] = _add_avg(node["avg_reachable_profit_pct"], max_profit, n)
    node["avg_adverse_pct"] = _add_avg(node["avg_adverse_pct"], max_adv, n)

    quality = rec.get("quality") or {}
    if quality.get("tp_quality") in {"TOO_FAR", "POSSIBLY_TOO_FAR_OR_EXIT_LATE"}:
        node["tp_too_far_count"] += 1
    if quality.get("sl_quality") == "POSSIBLY_TOO_CLOSE_OR_NOISY":
        node["sl_noise_count"] += 1

    node["median_hint_profit_pct"] = round(max(0.0, node["avg_reachable_profit_pct"] * 0.75), 4)
    node["last_updated"] = _ts()


def _bucket(v: float, step: int, lo: int, hi: int) -> str:
    v = max(lo, min(hi, v))
    a = int(v // step) * step
    return f"{a}-{a+step}"


def _signed_bucket(v: float, step: float, limit: float) -> str:
    v = max(-limit, min(limit, v))
    a = math.floor(v / step) * step
    return f"{a:.4f}:{a+step:.4f}"


def _refresh_personality(profile: Dict[str, Any]) -> None:
    wr = _safe_float(profile.get("win_rate"))
    avg_mfe = _safe_float(profile.get("avg_mfe_pct"))
    avg_mae = _safe_float(profile.get("avg_mae_pct"))
    sl = int(profile.get("sl", 0))
    tp = int(profile.get("tp", 0))

    if tp + sl < 3:
        profile["personality"] = "INSUFFICIENT_DATA"
    elif wr >= 60 and avg_mfe > avg_mae:
        profile["personality"] = "RESPONSIVE"
    elif sl >= tp * 1.5:
        profile["personality"] = "DANGEROUS"
    elif avg_mfe < 0.25:
        profile["personality"] = "LOW_RANGE"
    else:
        profile["personality"] = "MIXED"


@safe(default={})
def profile(symbol: str, direction: str) -> Dict[str, Any]:
    st = load_learning()
    key = _key(symbol, direction)
    return {
        "key": key,
        "profile": st.get("profiles", {}).get(key, {}),
        "indicator_rules": st.get("indicator_rules", {}).get(key, {}),
        "tp_behavior": st.get("tp_behavior", {}).get(key, {}),
        "ai_memory": ai_memory.learning_profile(symbol, direction),
    }


@safe(default={})
def guidance_for_snapshot(symbol: str, direction: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return soft guidance for AI Movement Hunter.
    No hard rejection here.
    """
    p = profile(symbol, direction)
    prof = p.get("profile", {})
    rules = p.get("indicator_rules", {})
    tp = p.get("tp_behavior", {})

    score = 0.0
    notes: List[str] = []
    risk_notes: List[str] = []
    ind = snapshot.get("indicators", snapshot or {})

    checks = {
        "rsi": _bucket(_safe_float(ind.get("rsi", 50)), 5, 0, 100),
        "adx": _bucket(_safe_float(ind.get("adx", 0)), 5, 0, 60),
        "power_2": _signed_bucket(_safe_float(ind.get("power_2", 0)), 0.10, 10),
        "power_3": _signed_bucket(_safe_float(ind.get("power_3", 0)), 0.10, 10),
    }

    for name, bucket in checks.items():
        node = rules.get(name, {}).get(bucket, {})
        node_score = _safe_float(node.get("score"))
        if node.get("samples", 0) >= 2:
            score += node_score
            if node_score > 0.15:
                notes.append(f"{name}:{bucket} historically positive")
            if node_score < -0.15:
                risk_notes.append(f"{name}:{bucket} historically weak")

    wr = _safe_float(prof.get("win_rate"))
    samples = int(prof.get("samples", 0) or 0)
    if samples >= 3:
        if wr >= 60:
            score += 0.25
            notes.append("coin_direction_positive")
        elif wr <= 40:
            score -= 0.25
            risk_notes.append("coin_direction_weak")

    if int(tp.get("tp_too_far_count", 0) or 0) >= 2:
        risk_notes.append("tp_may_need_closer_target")
    if int(tp.get("sl_noise_count", 0) or 0) >= 2:
        risk_notes.append("sl_may_be_noise_sensitive")

    return {
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "soft_score": round(score, 4),
        "samples": samples,
        "win_rate": wr,
        "personality": prof.get("personality", "UNKNOWN"),
        "notes": notes[:8],
        "risk_notes": risk_notes[:8],
        "tp_behavior": tp,
    }


@safe(default={})
def summary() -> Dict[str, Any]:
    st = load_learning()
    profiles = st.get("profiles", {})
    rows = []
    for key, p in profiles.items():
        rows.append({
            "key": key,
            "samples": int(p.get("samples", 0)),
            "wr": _safe_float(p.get("win_rate")),
            "tp": int(p.get("tp", 0)),
            "sl": int(p.get("sl", 0)),
            "personality": p.get("personality", "UNKNOWN"),
        })
    best = sorted([r for r in rows if r["samples"] >= 3], key=lambda x: (x["wr"], x["tp"]-x["sl"]), reverse=True)[:10]
    worst = sorted([r for r in rows if r["samples"] >= 3], key=lambda x: (x["wr"], -x["sl"]))[:10]
    return {"profiles": len(profiles), "best": best, "worst": worst, "updated_at": st.get("updated_at")}


@safe(default="")
def summary_fa() -> str:
    s = summary()
    lines = ["🧠 یادگیری رفتار کوین‌ها", f"پروفایل‌ها: {s.get('profiles', 0)}"]
    if s.get("best"):
        lines.append("بهترین‌ها: " + "، ".join(f"{x['key']} {x['wr']}%" for x in s["best"][:3]))
    if s.get("worst"):
        lines.append("پرریسک‌ها: " + "، ".join(f"{x['key']} {x['wr']}%" for x in s["worst"][:3]))
    return "\n".join(lines)


@safe(default=True)
def initialize() -> bool:
    st = load_learning()
    save_learning(st)
    return True
