from __future__ import annotations

"""
Support/Resistance behavior learning.

Responsibilities:
- Learn how each coin+direction reacts near support/resistance.
- Track bounce, clean break, fake break, liquidity reaction.
- Provide soft guidance for Smart TP/SL and AI Movement Hunter.
- Not a hard signal blocker.
"""

import time
import math
from typing import Any, Dict, List, Optional

from config import CORE_DATA_FILES
from data_store import load_dict, save_json
from diagnostics import safe
import ai_memory


SR_FILE = CORE_DATA_FILES.get("sr_learning")


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
        "profiles": {},
        "levels": {},
    }


@safe(default={})
def load_sr() -> Dict[str, Any]:
    st = load_dict(SR_FILE)
    if not st:
        st = _empty_state()
        save_json(SR_FILE, st)
    for k, v in _empty_state().items():
        st.setdefault(k, v)
    return st


@safe(default=False)
def save_sr(st: Dict[str, Any], make_backup: bool = False) -> bool:
    st["updated_at"] = _ts()
    return save_json(SR_FILE, st, make_backup=make_backup)


def _profile_template() -> Dict[str, Any]:
    return {
        "samples": 0,
        "bounce": 0,
        "clean_break": 0,
        "fake_break": 0,
        "tp_after_break": 0,
        "sl_after_fake": 0,
        "avg_move_after_break_pct": 0.0,
        "avg_reaction_mfe_pct": 0.0,
        "avg_reaction_mae_pct": 0.0,
        "fake_break_rate": 0.0,
        "break_quality": 0.0,
        "last_updated": _ts(),
    }


@safe(default=True)
def update_from_record(rec: Dict[str, Any], state: Optional[Dict[str, Any]] = None, autosave: bool = True) -> bool:
    st = state if state is not None else load_sr()
    symbol = str(rec.get("symbol", "")).upper()
    direction = str(rec.get("direction", "")).upper()
    if not symbol or not direction:
        return False

    key = _key(symbol, direction)
    prof = st.setdefault("profiles", {}).setdefault(key, _profile_template())
    struct = rec.get("activation_structure") or rec.get("structure") or {}
    result = str(rec.get("result", "")).upper()

    breakout_state = str(struct.get("breakout_state", "UNKNOWN")).upper()
    fake_risk = _safe_float(struct.get("fake_breakout_risk"))
    mfe_pct = _safe_float(rec.get("max_profit_pct"))
    mae_pct = _safe_float(rec.get("max_adverse_pct"))

    prof["samples"] += 1
    n = prof["samples"]

    if "BOUNCE" in breakout_state:
        prof["bounce"] += 1
    if "CLEAN" in breakout_state or "BREAK" in breakout_state and fake_risk < 0.35:
        prof["clean_break"] += 1
        if result.startswith("TP"):
            prof["tp_after_break"] += 1
    if "FAKE" in breakout_state or fake_risk >= 0.65:
        prof["fake_break"] += 1
        if result == "SL":
            prof["sl_after_fake"] += 1

    prof["avg_reaction_mfe_pct"] = _avg(prof["avg_reaction_mfe_pct"], mfe_pct, n)
    prof["avg_reaction_mae_pct"] = _avg(prof["avg_reaction_mae_pct"], mae_pct, n)
    if prof["clean_break"] > 0:
        prof["avg_move_after_break_pct"] = _avg(prof["avg_move_after_break_pct"], mfe_pct, max(1, prof["clean_break"]))

    prof["fake_break_rate"] = round(prof["fake_break"] / max(1, prof["samples"]), 4)
    prof["break_quality"] = round((prof["tp_after_break"] - prof["sl_after_fake"]) / max(1, prof["clean_break"] + prof["fake_break"]), 4)
    prof["last_updated"] = _ts()

    _store_level(st, symbol, direction, struct, rec)

    if autosave:
        save_sr(st)
    return True


def _avg(old: float, value: float, n: int) -> float:
    n = max(1, n)
    return round((old * (n - 1) + value) / n, 8)


def _store_level(st: Dict[str, Any], symbol: str, direction: str, struct: Dict[str, Any], rec: Dict[str, Any]) -> None:
    levels = st.setdefault("levels", {}).setdefault(symbol, [])
    for name in ["support_near", "resistance_near", "swing_high", "swing_low"]:
        price = _safe_float(struct.get(name))
        if price <= 0:
            continue
        levels.append({
            "ts": _ts(),
            "direction": direction,
            "type": name,
            "price": price,
            "result": rec.get("result"),
            "mfe_pct": _safe_float(rec.get("max_profit_pct")),
            "mae_pct": _safe_float(rec.get("max_adverse_pct")),
            "breakout_state": struct.get("breakout_state", "UNKNOWN"),
        })
    st["levels"][symbol] = levels[-300:]


@safe(default={})
def guidance(symbol: str, direction: str, structure_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    st = load_sr()
    key = _key(symbol, direction)
    prof = st.get("profiles", {}).get(key, {})
    fake_rate = _safe_float(prof.get("fake_break_rate"))
    break_quality = _safe_float(prof.get("break_quality"))
    fake_risk_now = _safe_float(structure_snapshot.get("fake_breakout_risk"))
    breakout_state = str(structure_snapshot.get("breakout_state", "UNKNOWN")).upper()

    score = 0.0
    notes = []
    risks = []

    if prof.get("samples", 0) >= 3:
        if break_quality > 0.2:
            score += 0.2
            notes.append("sr_break_behavior_positive")
        if fake_rate > 0.45 and ("BREAK" in breakout_state or fake_risk_now > 0.5):
            score -= 0.3
            risks.append("coin_has_fake_break_history")
        if prof.get("avg_reaction_mfe_pct", 0) > prof.get("avg_reaction_mae_pct", 0):
            score += 0.1

    return {
        "symbol": str(symbol).upper(),
        "direction": str(direction).upper(),
        "soft_score": round(score, 4),
        "fake_break_rate": fake_rate,
        "break_quality": break_quality,
        "avg_move_after_break_pct": _safe_float(prof.get("avg_move_after_break_pct")),
        "notes": notes,
        "risks": risks,
        "profile": prof,
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
    save_sr(st, make_backup=True)
    return summary()


@safe(default={})
def summary() -> Dict[str, Any]:
    st = load_sr()
    rows = []
    for key, p in st.get("profiles", {}).items():
        rows.append({
            "key": key,
            "samples": int(p.get("samples", 0)),
            "fake_rate": _safe_float(p.get("fake_break_rate")),
            "break_quality": _safe_float(p.get("break_quality")),
            "avg_move": _safe_float(p.get("avg_move_after_break_pct")),
        })
    best_break = sorted(rows, key=lambda x: x["break_quality"], reverse=True)[:10]
    fake_prone = sorted(rows, key=lambda x: x["fake_rate"], reverse=True)[:10]
    return {"profiles": len(rows), "best_break": best_break, "fake_prone": fake_prone, "updated_at": st.get("updated_at")}


@safe(default="")
def summary_fa() -> str:
    s = summary()
    lines = ["📊 یادگیری حمایت/مقاومت", f"پروفایل‌ها: {s.get('profiles', 0)}"]
    if s.get("fake_prone"):
        lines.append("فیک‌برک زیاد: " + "، ".join(f"{x['key']} {x['fake_rate']}" for x in s["fake_prone"][:5]))
    return "\n".join(lines)


@safe(default=True)
def initialize() -> bool:
    st = load_sr()
    save_sr(st)
    return True
