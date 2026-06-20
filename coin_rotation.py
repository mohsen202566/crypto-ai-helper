from __future__ import annotations

"""
Coin rotation and candidate prioritization.

Responsibilities:
- Rank best/worst coins using real + ghost results.
- Provide slot-aware candidate sorting.
- Account for correlation/theme exposure softly.
- Keep outputs fast for Telegram commands.
"""

import time
import math
from typing import Any, Dict, List, Optional

from config import CORE_DATA_FILES
from data_store import load_dict, save_json, cache_get, cache_set
from diagnostics import safe
import ai_memory
import coin_learning
import coin_risk


COIN_ROTATION_FILE = CORE_DATA_FILES.get("coin_rotation", "coin_rotation")


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


def _group(symbol: str) -> str:
    s = str(symbol).upper()
    if s in {"DOGEUSDT", "SHIBUSDT", "PEPEUSDT", "WIFUSDT"}:
        return "MEME"
    if s in {"SOLUSDT", "AVAXUSDT", "INJUSDT", "SUIUSDT", "APTUSDT", "SEIUSDT"}:
        return "HIGH_BETA_L1"
    if s in {"BTCUSDT", "ETHUSDT", "BNBUSDT"}:
        return "MAJOR"
    if s in {"ARBUSDT", "OPUSDT", "MATICUSDT"}:
        return "L2"
    return "ALT"


def _empty_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "updated_at": _ts(),
        "rankings": {},
        "group_scores": {},
        "last_rebuild": 0,
    }


@safe(default={})
def load_rotation() -> Dict[str, Any]:
    st = load_dict(COIN_ROTATION_FILE)
    if not st:
        st = _empty_state()
        save_json(COIN_ROTATION_FILE, st)
    for k, v in _empty_state().items():
        st.setdefault(k, v)
    return st


@safe(default=False)
def save_rotation(st: Dict[str, Any], make_backup: bool = False) -> bool:
    st["updated_at"] = _ts()
    return save_json(COIN_ROTATION_FILE, st, make_backup=make_backup)


@safe(default={})
def rebuild() -> Dict[str, Any]:
    mem = ai_memory.summary(use_cache=False)
    learn = coin_learning.summary()
    risk = coin_risk.summary()

    st = _empty_state()
    profiles = coin_learning.load_learning().get("profiles", {})
    risk_profiles = coin_risk.load_risk().get("risk_profiles", {})

    rankings = {}
    group_scores = {}

    for key, p in profiles.items():
        try:
            symbol, direction = key.split("::", 1)
        except ValueError:
            continue
        rp = risk_profiles.get(key, {})
        samples = int(p.get("samples", 0) or 0)
        wr = _safe_float(p.get("win_rate"))
        avg_mfe = _safe_float(p.get("avg_mfe_pct"))
        avg_mae = _safe_float(p.get("avg_mae_pct"))
        risk_score = _safe_float(rp.get("risk_score"))
        strictness = _safe_float(rp.get("strictness", 1.0), 1.0)

        # 0-100 score, soft.
        score = 50.0
        if samples >= 2:
            score += (wr - 50) * 0.45
            score += min(10, avg_mfe * 3)
            score -= min(10, avg_mae * 3)
            score -= risk_score * 18
            score -= max(0, strictness - 1.0) * 12
        else:
            score -= 5

        score = round(max(0, min(100, score)), 2)
        item = {
            "key": key,
            "symbol": symbol,
            "direction": direction,
            "group": _group(symbol),
            "score": score,
            "samples": samples,
            "wr": wr,
            "risk_score": risk_score,
            "strictness": strictness,
            "personality": p.get("personality", "UNKNOWN"),
            "updated_at": _ts(),
        }
        rankings[key] = item
        g = group_scores.setdefault(item["group"], {"samples": 0, "score_sum": 0.0, "avg_score": 0.0})
        g["samples"] += 1
        g["score_sum"] += score

    for g in group_scores.values():
        g["avg_score"] = round(g["score_sum"] / max(1, g["samples"]), 2)

    st["rankings"] = rankings
    st["group_scores"] = group_scores
    st["last_rebuild"] = _ts()
    save_rotation(st, make_backup=True)
    return summary()


@safe(default={})
def ranking_for(symbol: str, direction: str) -> Dict[str, Any]:
    st = load_rotation()
    key = f"{str(symbol).upper()}::{str(direction).upper()}"
    if key not in st.get("rankings", {}):
        rebuild()
        st = load_rotation()
    return st.get("rankings", {}).get(key, {
        "key": key, "symbol": str(symbol).upper(), "direction": str(direction).upper(),
        "group": _group(symbol), "score": 50.0, "samples": 0
    })


@safe(default=[])
def rank_candidates(candidates: List[Dict[str, Any]], open_positions: Optional[List[Dict[str, Any]]] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Sort candidates for slot filling. If one slot is free, caller takes first.
    Candidate expected fields: symbol, direction, ai_score/confidence/priority.
    """
    open_positions = open_positions or []
    open_groups = {}
    for p in open_positions:
        g = _group(p.get("symbol", ""))
        open_groups[g] = open_groups.get(g, 0) + 1

    ranked = []
    for c in candidates:
        symbol = str(c.get("symbol", "")).upper()
        direction = str(c.get("direction", "")).upper()
        rot = ranking_for(symbol, direction)
        group = rot.get("group", _group(symbol))
        base = _safe_float(c.get("priority", c.get("ai_score", c.get("confidence", 0.5)))) * 100
        score = base * 0.55 + _safe_float(rot.get("score", 50)) * 0.45

        exposure_penalty = max(0, open_groups.get(group, 0) - 0) * 7.5
        if group == "MEME" and open_groups.get(group, 0) >= 2:
            exposure_penalty += 8

        final = round(max(0, score - exposure_penalty), 4)
        cc = dict(c)
        cc["rotation_score"] = rot.get("score", 50)
        cc["correlation_group"] = group
        cc["exposure_penalty"] = round(exposure_penalty, 4)
        cc["final_priority"] = final
        ranked.append(cc)

    ranked.sort(key=lambda x: x.get("final_priority", 0), reverse=True)
    return ranked[:limit] if limit else ranked


@safe(default={})
def summary() -> Dict[str, Any]:
    st = load_rotation()
    rankings = list(st.get("rankings", {}).values())
    best = sorted(rankings, key=lambda x: x.get("score", 0), reverse=True)[:10]
    worst = sorted(rankings, key=lambda x: x.get("score", 0))[:10]
    return {
        "count": len(rankings),
        "best": best,
        "worst": worst,
        "groups": st.get("group_scores", {}),
        "updated_at": st.get("updated_at"),
    }


@safe(default="")
def summary_fa() -> str:
    s = summary()
    lines = ["🔁 چرخش کوین‌ها", f"تعداد پروفایل: {s.get('count', 0)}"]
    if s.get("best"):
        lines.append("بهترین: " + "، ".join(f"{x['key']} {x['score']}" for x in s["best"][:5]))
    if s.get("worst"):
        lines.append("ضعیف: " + "، ".join(f"{x['key']} {x['score']}" for x in s["worst"][:5]))
    return "\n".join(lines)


@safe(default=True)
def initialize() -> bool:
    st = load_rotation()
    save_rotation(st)
    return True
