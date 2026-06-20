from __future__ import annotations

"""
Coin risk and adaptive strictness.

Responsibilities:
- Apply user rule: after 2 SLs on same coin+direction, the 3rd similar setup needs extra confirmation.
- Tighten only the weak condition, not ban the whole coin.
- Provide soft risk penalties to AI Movement Hunter.
"""

import time
import math
from typing import Any, Dict, List, Optional

from config import CORE_DATA_FILES
from data_store import load_dict, save_json
from diagnostics import safe
import ai_memory
import coin_learning


COIN_RISK_FILE = CORE_DATA_FILES.get("coin_risk")


def _ts() -> int:
    return int(time.time())


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def _key(symbol: str, direction: str) -> str:
    return f"{str(symbol).upper()}::{str(direction).upper()}"


def _empty_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "updated_at": _ts(),
        "risk_profiles": {},
        "condition_tightening": {},
        "daily_risk": {},
    }


@safe(default={})
def load_risk() -> Dict[str, Any]:
    st = load_dict(COIN_RISK_FILE)
    if not st:
        st = _empty_state()
        save_json(COIN_RISK_FILE, st)
    for k, v in _empty_state().items():
        st.setdefault(k, v)
    return st


@safe(default=False)
def save_risk(st: Dict[str, Any], make_backup: bool = False) -> bool:
    st["updated_at"] = _ts()
    return save_json(COIN_RISK_FILE, st, make_backup=make_backup)


def _risk_template() -> Dict[str, Any]:
    return {
        "tp": 0,
        "sl": 0,
        "consecutive_sl": 0,
        "strictness": 1.0,
        "risk_score": 0.0,
        "last_result": "",
        "last_updated": _ts(),
        "notes": [],
    }


@safe(default=True)
def update_from_record(rec: Dict[str, Any], state: Optional[Dict[str, Any]] = None, autosave: bool = True) -> bool:
    st = state if state is not None else load_risk()
    symbol = str(rec.get("symbol", "")).upper()
    direction = str(rec.get("direction", "")).upper()
    if not symbol or not direction:
        return False

    key = _key(symbol, direction)
    rp = st.setdefault("risk_profiles", {}).setdefault(key, _risk_template())
    result = str(rec.get("result", "")).upper()

    if result.startswith("TP"):
        rp["tp"] += 1
        rp["consecutive_sl"] = 0
        rp["last_result"] = "TP"
    elif result == "SL":
        rp["sl"] += 1
        rp["consecutive_sl"] += 1
        rp["last_result"] = "SL"
        _learn_weak_conditions(st, key, rec)

    # User rule: stricter starts after 2 SLs, 3rd setup gets stricter.
    sl = int(rp.get("sl", 0))
    if sl >= 2:
        rp["strictness"] = round(min(1.8, 1.0 + (sl - 1) * 0.12 + rp.get("consecutive_sl", 0) * 0.05), 3)
    else:
        rp["strictness"] = 1.0

    rp["risk_score"] = round(rp["sl"] / max(1, rp["tp"] + rp["sl"]), 4)
    rp["last_updated"] = _ts()
    if autosave:
        save_risk(st)
    return True


def _learn_weak_conditions(st: Dict[str, Any], key: str, rec: Dict[str, Any]) -> None:
    ind = rec.get("activation_indicators") or rec.get("indicators") or {}
    root = st.setdefault("condition_tightening", {}).setdefault(key, {})

    candidates = {
        "adx": _safe_float(ind.get("adx")),
        "rsi": _safe_float(ind.get("rsi")),
        "power_2": _safe_float(ind.get("power_2")),
        "power_3": _safe_float(ind.get("power_3")),
        "fresh_momentum": _safe_float(ind.get("fresh_momentum")),
        "vwap_distance": _safe_float(ind.get("vwap_distance")),
        "trap_risk": _safe_float((rec.get("activation_structure") or rec.get("structure") or {}).get("trap_risk")),
        "liquidity_risk": _safe_float((rec.get("activation_structure") or rec.get("structure") or {}).get("liquidity_risk")),
    }

    for name, value in candidates.items():
        node = root.setdefault(name, {"sl_samples": 0, "avg_sl_value": 0.0, "extra_required": 0.0, "last_updated": _ts()})
        node["sl_samples"] += 1
        n = node["sl_samples"]
        node["avg_sl_value"] = round((node["avg_sl_value"] * (n - 1) + value) / n, 8)

        if name == "adx":
            node["extra_required"] = min(8.0, max(0.0, 25 - node["avg_sl_value"]) * 0.25 + n * 0.35)
        elif name in {"power_2", "power_3", "fresh_momentum"}:
            node["extra_required"] = min(0.25, n * 0.025)
        elif name in {"trap_risk", "liquidity_risk"}:
            node["extra_required"] = min(0.35, n * 0.04)
        else:
            node["extra_required"] = min(0.20, n * 0.02)
        node["last_updated"] = _ts()


@safe(default={})
def evaluate(symbol: str, direction: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return soft risk guidance for AI Movement Hunter.
    """
    st = load_risk()
    key = _key(symbol, direction)
    rp = st.get("risk_profiles", {}).get(key, _risk_template())
    cond = st.get("condition_tightening", {}).get(key, {})

    ind = snapshot.get("indicators", snapshot or {})
    struct = snapshot.get("structure", snapshot or {})

    penalty = 0.0
    warnings: List[str] = []
    requirements: Dict[str, Any] = {}

    strictness = _safe_float(rp.get("strictness", 1.0), 1.0)
    if strictness > 1.0:
        penalty += (strictness - 1.0) * 0.35
        warnings.append("coin_direction_after_sl_stricter")

    for name, node in cond.items():
        extra = _safe_float(node.get("extra_required"))
        if extra <= 0:
            continue
        cur = _safe_float(ind.get(name, struct.get(name, 0)))
        requirements[name] = {"current": cur, "extra_required": extra, "avg_sl_value": node.get("avg_sl_value")}
        # Soft penalty only when current condition resembles weak SL condition.
        if name == "adx" and cur <= _safe_float(node.get("avg_sl_value")) + extra:
            penalty += min(0.3, extra / 10)
            warnings.append("adx_similar_to_sl_condition")
        elif name in {"trap_risk", "liquidity_risk"} and cur >= _safe_float(node.get("avg_sl_value")):
            penalty += min(0.25, extra)
            warnings.append(f"{name}_similar_to_sl")
        elif name in {"power_2", "power_3", "fresh_momentum"} and cur <= _safe_float(node.get("avg_sl_value")) + extra:
            penalty += min(0.2, extra)
            warnings.append(f"{name}_weak_like_sl")

    return {
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "risk_score": round(_safe_float(rp.get("risk_score")) + penalty, 4),
        "strictness": strictness,
        "penalty": round(penalty, 4),
        "warnings": warnings[:8],
        "requirements": requirements,
        "profile": rp,
    }


@safe(default={})
def rebuild_from_ai_memory(limit: int = 20000) -> Dict[str, Any]:
    mem = ai_memory.load_memory()
    records = list(mem.get("records", {}).values())
    records = sorted(records, key=lambda r: r.get("updated_at", r.get("created_at", 0)))[-limit:]
    st = _empty_state()
    for rec in records:
        if rec.get("status") == "CLOSED":
            update_from_record(rec, state=st, autosave=False)
    save_risk(st, make_backup=True)
    return summary()


@safe(default={})
def summary() -> Dict[str, Any]:
    st = load_risk()
    rows = []
    for key, rp in st.get("risk_profiles", {}).items():
        rows.append({
            "key": key,
            "tp": int(rp.get("tp", 0)),
            "sl": int(rp.get("sl", 0)),
            "risk": _safe_float(rp.get("risk_score")),
            "strictness": _safe_float(rp.get("strictness", 1.0)),
        })
    risky = sorted(rows, key=lambda x: (x["risk"], x["sl"], x["strictness"]), reverse=True)[:10]
    return {"profiles": len(rows), "risky": risky, "updated_at": st.get("updated_at")}


@safe(default="")
def summary_fa() -> str:
    s = summary()
    lines = ["⚠️ ریسک کوین‌ها", f"پروفایل‌ها: {s.get('profiles', 0)}"]
    if s.get("risky"):
        lines.append("پرریسک: " + "، ".join(f"{x['key']} R:{x['risk']}" for x in s["risky"][:5]))
    return "\n".join(lines)


@safe(default=True)
def initialize() -> bool:
    st = load_risk()
    save_risk(st)
    return True
