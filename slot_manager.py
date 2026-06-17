# -*- coding: utf-8 -*-
"""
slot_manager.py

AI-aware slot/position manager for the crypto futures bot.

Purpose:
- Enforce max concurrent positions from live trade settings.
- Prevent duplicate symbol/direction positions and per-symbol overexposure.
- Rank candidates by AI final rank first, then learning/risk/rotation fallback.
- Convert good but unused candidates to Ghost signals when slots are full or
  candidate was not selected.
- Support REAL trade pending confirmation state so slots do not free too early
  while Toobit position visibility is delayed.
- Keep a small AI candidate queue for best-signal refill after a slot is freed.

Compatibility:
- Keeps old public functions used by bot.py/scanner.py:
    get_active_positions, get_max_active_positions, get_free_slots,
    is_symbol_direction_active, can_open_new_position, add_position,
    close_position, select_best_candidates, format_slot_report
- Adds optional helpers for real-trade sync:
    add_pending_real_position, confirm_pending_real_position,
    fail_pending_real_position, cleanup_expired_pending_positions,
    queue_candidates, pop_best_queued_candidates
"""

import time
from typing import Dict, List, Any, Tuple, Optional

from data_store import load_json, save_json
from config import MAX_ACTIVE_POSITIONS, MAX_POSITIONS_PER_SYMBOL

try:
    from coin_rotation import get_coin_rotation_score, get_symbol_rotation_score
except Exception:
    get_coin_rotation_score = None
    get_symbol_rotation_score = None

try:
    from coin_risk import get_direction_risk_state
except Exception:
    get_direction_risk_state = None

try:
    from ghost_signals import create_ghost_signal
except Exception:
    create_ghost_signal = None

SLOT_FILE = "slot_state.json"
TRADE_SETTINGS_FILE = "trade_settings.json"
DEFAULT_PENDING_CONFIRM_SECONDS = 30
MAX_QUEUE_SIZE = 80
MIN_GHOST_SCORE = 80


def _now() -> int:
    return int(time.time())


def _state() -> Dict[str, Any]:
    s = load_json(SLOT_FILE, {"positions": {}, "queue": [], "history": []})
    if not isinstance(s, dict):
        s = {"positions": {}, "queue": [], "history": []}
    if not isinstance(s.get("positions"), dict):
        s["positions"] = {}
    if not isinstance(s.get("queue"), list):
        s["queue"] = []
    if not isinstance(s.get("history"), list):
        s["history"] = []
    return s


def _save_state(s: Dict[str, Any]) -> None:
    s["updated_at"] = _now()
    save_json(SLOT_FILE, s)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return int(default)


def _norm_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    return s if s.endswith("USDT") else f"{s}USDT" if s else ""


def _norm_direction(direction: str) -> str:
    return str(direction or "").upper().strip()


def _position_is_active(p: Dict[str, Any]) -> bool:
    status = str(p.get("status") or "ACTIVE").upper()
    # PENDING_REAL_CONFIRM must reserve a slot too.
    return status in {"ACTIVE", "OPEN", "PENDING", "PENDING_REAL_CONFIRM"}


def get_active_positions() -> List[Dict[str, Any]]:
    return [p for p in _state().get("positions", {}).values() if isinstance(p, dict) and _position_is_active(p)]


def get_all_positions() -> List[Dict[str, Any]]:
    return [p for p in _state().get("positions", {}).values() if isinstance(p, dict)]


def get_max_active_positions() -> int:
    """Use live trade settings first, then fall back to config default."""
    try:
        settings = load_json(TRADE_SETTINGS_FILE, {})
        if isinstance(settings, dict):
            value = int(settings.get("max_positions") or MAX_ACTIVE_POSITIONS)
            return max(1, value)
    except Exception:
        pass
    try:
        return max(1, int(MAX_ACTIVE_POSITIONS))
    except Exception:
        return 1


def get_free_slots() -> int:
    cleanup_expired_pending_positions(save=True)
    return max(0, get_max_active_positions() - len(get_active_positions()))


def is_symbol_direction_active(symbol: str, direction: str = None) -> bool:
    ns = _norm_symbol(symbol)
    nd = _norm_direction(direction) if direction is not None else None
    for p in get_active_positions():
        if p.get("symbol") == ns and (nd is None or p.get("direction") == nd):
            return True
    return False


def can_open_new_position(symbol: str = None, direction: str = None) -> Tuple[bool, str]:
    cleanup_expired_pending_positions(save=True)
    if get_free_slots() <= 0:
        return False, "slot_full"
    if symbol:
        ns = _norm_symbol(symbol)
        count = sum(1 for p in get_active_positions() if p.get("symbol") == ns)
        if count >= int(MAX_POSITIONS_PER_SYMBOL):
            return False, "symbol_limit"
        if direction and is_symbol_direction_active(ns, direction):
            return False, "duplicate"
    return True, "ok"


def _append_history(s: Dict[str, Any], event: Dict[str, Any]) -> None:
    h = s.setdefault("history", [])
    if isinstance(h, list):
        row = dict(event)
        row.setdefault("ts", _now())
        h.append(row)
        s["history"] = h[-300:]


def add_position(signal_id: str, symbol: str, direction: str, score=None, status: str = "ACTIVE", **kwargs):
    ok, reason = can_open_new_position(symbol, direction)
    if not ok:
        return False, reason
    s = _state()
    sid = str(signal_id or f"slot_{_norm_symbol(symbol)}_{_norm_direction(direction)}_{_now()}")
    p = {
        "signal_id": sid,
        "symbol": _norm_symbol(symbol),
        "direction": _norm_direction(direction),
        "score": score,
        "ai_final_rank": kwargs.get("ai_final_rank"),
        "ai_final_score": kwargs.get("ai_final_score"),
        "status": str(status or "ACTIVE").upper(),
        "opened_at": _now(),
        **kwargs,
    }
    s["positions"][sid] = p
    _append_history(s, {"event": "ADD_POSITION", "signal_id": sid, "symbol": p["symbol"], "direction": p["direction"], "status": p["status"]})
    _save_state(s)
    return True, "ok"


def add_pending_real_position(signal_id: str, symbol: str, direction: str, score=None, confirm_timeout_seconds: int = DEFAULT_PENDING_CONFIRM_SECONDS, **kwargs):
    """Reserve a slot while waiting for Toobit real position confirmation.

    This fixes the issue where a real order is accepted but exchange position
    visibility is delayed; the slot remains occupied until confirmed or expired.
    """
    expires_at = _now() + max(10, int(confirm_timeout_seconds or DEFAULT_PENDING_CONFIRM_SECONDS))
    return add_position(
        signal_id,
        symbol,
        direction,
        score=score,
        status="PENDING_REAL_CONFIRM",
        pending_real_confirm=True,
        pending_expires_at=expires_at,
        **kwargs,
    )


def confirm_pending_real_position(signal_id: str, exchange_position: Optional[Dict[str, Any]] = None) -> bool:
    s = _state()
    sid = str(signal_id)
    p = s.get("positions", {}).get(sid)
    if not isinstance(p, dict):
        return False
    p["status"] = "ACTIVE"
    p["pending_real_confirm"] = False
    p["confirmed_at"] = _now()
    if isinstance(exchange_position, dict):
        p["exchange_position"] = exchange_position
    _append_history(s, {"event": "CONFIRM_REAL", "signal_id": sid, "symbol": p.get("symbol"), "direction": p.get("direction")})
    _save_state(s)
    return True


def fail_pending_real_position(signal_id: str, reason: str = "real_confirm_failed") -> bool:
    s = _state()
    sid = str(signal_id)
    p = s.get("positions", {}).pop(sid, None)
    if not isinstance(p, dict):
        return False
    _append_history(s, {"event": "FAIL_PENDING_REAL", "signal_id": sid, "symbol": p.get("symbol"), "direction": p.get("direction"), "reason": reason})
    _save_state(s)
    return True


def cleanup_expired_pending_positions(save: bool = True) -> int:
    s = _state()
    now = _now()
    removed = 0
    for sid, p in list(s.get("positions", {}).items()):
        if not isinstance(p, dict):
            continue
        status = str(p.get("status") or "").upper()
        exp = _safe_int(p.get("pending_expires_at"), 0)
        if status == "PENDING_REAL_CONFIRM" and exp and now > exp:
            s["positions"].pop(sid, None)
            removed += 1
            _append_history(s, {"event": "EXPIRE_PENDING_REAL", "signal_id": sid, "symbol": p.get("symbol"), "direction": p.get("direction")})
    if removed and save:
        _save_state(s)
    return removed


def close_position(signal_id: str, result: str = None, **kwargs):
    s = _state()
    sid = str(signal_id)
    p = s.get("positions", {}).pop(sid, None)
    if isinstance(p, dict):
        _append_history(s, {"event": "CLOSE_POSITION", "signal_id": sid, "symbol": p.get("symbol"), "direction": p.get("direction"), "result": result, **kwargs})
        _save_state(s)
        return True
    return False


def _learning_context(symbol: str, direction: str) -> Dict[str, Any]:
    """Read learned per-coin/per-direction results without changing learning storage."""
    try:
        data = load_json("coin_learning.json", {"by_coin_direction": {}})
        rows = data.get("by_coin_direction", {}) if isinstance(data, dict) else {}
        key = f"{_norm_symbol(symbol)}:{_norm_direction(direction)}"
        row = rows.get(key, {}) if isinstance(rows, dict) else {}
        tp = _safe_int(row.get("tp1")) + _safe_int(row.get("tp2"))
        sl = _safe_int(row.get("sl"))
        real_tp = _safe_int(row.get("real_tp"))
        real_sl = _safe_int(row.get("real_sl"))
        ghost_tp = _safe_int(row.get("ghost_tp"))
        ghost_sl = _safe_int(row.get("ghost_sl"))
        weighted_tp = _safe_float(row.get("weighted_tp"), real_tp + ghost_tp * 0.45)
        weighted_sl = _safe_float(row.get("weighted_sl"), real_sl + ghost_sl * 0.45)
        total_w = weighted_tp + weighted_sl
        winrate = (weighted_tp / total_w * 100.0) if total_w > 0 else 50.0
        return {
            "tp": tp,
            "sl": sl,
            "real_tp": real_tp,
            "real_sl": real_sl,
            "ghost_tp": ghost_tp,
            "ghost_sl": ghost_sl,
            "weighted_tp": weighted_tp,
            "weighted_sl": weighted_sl,
            "total": tp + sl,
            "weighted_total": total_w,
            "winrate": winrate,
            "behavior": row.get("behavior", "UNKNOWN"),
            "personality": row.get("personality", "UNKNOWN"),
            "confidence": _safe_int(row.get("confidence"), 0),
        }
    except Exception:
        return {"tp": 0, "sl": 0, "total": 0, "weighted_total": 0.0, "winrate": 50.0, "confidence": 0}


def _rotation_context(symbol: str, direction: str, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = {"rotation_score": 70.0, "direction_score": 70.0, "risk_score": 0.0, "status": "UNKNOWN"}
    if get_coin_rotation_score:
        try:
            rot = get_coin_rotation_score(symbol, snapshot=snapshot) or {}
            out["rotation_score"] = _safe_float(rot.get("rotation_score"), 70.0)
            out["risk_score"] = _safe_float(rot.get("risk_score"), 0.0)
            out["status"] = rot.get("status", "UNKNOWN")
            ds = rot.get("direction_scores", {}) if isinstance(rot.get("direction_scores"), dict) else {}
            out["direction_score"] = _safe_float(ds.get(_norm_direction(direction)), out["rotation_score"])
        except TypeError:
            try:
                rot = get_coin_rotation_score(symbol) or {}
                out["rotation_score"] = _safe_float(rot.get("rotation_score"), 70.0)
                out["risk_score"] = _safe_float(rot.get("risk_score"), 0.0)
                out["status"] = rot.get("status", "UNKNOWN")
            except Exception:
                pass
        except Exception:
            pass
    if get_symbol_rotation_score:
        try:
            out["direction_score"] = _safe_float(get_symbol_rotation_score(symbol, direction=direction, snapshot=snapshot), out["direction_score"])
        except TypeError:
            try:
                out["direction_score"] = _safe_float(get_symbol_rotation_score(symbol), out["direction_score"])
            except Exception:
                pass
        except Exception:
            pass
    return out


def _risk_context(symbol: str, direction: str) -> Dict[str, Any]:
    if not get_direction_risk_state:
        return {"risk_score": 0.0, "strictness_level": 0, "sl_count": 0, "recommend_reduce": False}
    try:
        r = get_direction_risk_state(symbol, direction) or {}
        return r if isinstance(r, dict) else {}
    except Exception:
        return {"risk_score": 0.0, "strictness_level": 0, "sl_count": 0, "recommend_reduce": False}


def _candidate_snapshot(x: Dict[str, Any]) -> Dict[str, Any]:
    snap = x.get("snapshot") if isinstance(x.get("snapshot"), dict) else {}
    out = dict(snap)
    for key in ["symbol", "direction", "entry", "price", "score", "ai_final_score", "ai_final_rank", "risk_level", "risk_reward", "confirmations", "freshness", "market_mode", "market_regime", "btc_bias"]:
        if key not in out and x.get(key) is not None:
            out[key] = x.get(key)
    return out


def candidate_rank_value(x: Dict[str, Any]) -> float:
    """Final slot selection rank.

    Priority order:
    1) scanner.py ai_final_rank if present
    2) classic score + AI risk/learning/rotation fallback
    """
    if x.get("ai_final_rank") is not None:
        return _safe_float(x.get("ai_final_rank"), 0.0)
    if isinstance(x.get("ai_scanner"), dict) and x["ai_scanner"].get("ai_final_rank") is not None:
        return _safe_float(x["ai_scanner"].get("ai_final_rank"), 0.0)

    symbol = _norm_symbol(x.get("symbol"))
    direction = _norm_direction(x.get("direction"))
    snapshot = _candidate_snapshot(x)

    base_score = _safe_float(x.get("score"), 0.0)
    confirmations = _safe_float(x.get("confirmations"), 0.0)
    rr = _safe_float(x.get("risk_reward"), 0.0)
    risk_level_bonus = {"LOW": 4.0, "MEDIUM": 2.0}.get(str(x.get("risk_level") or "").upper(), 0.0)
    fresh_bonus = {"HIGH": 3.0, "MEDIUM": 1.0}.get(str(x.get("freshness") or "").upper(), 0.0)

    rot = _rotation_context(symbol, direction, snapshot)
    risk = _risk_context(symbol, direction)
    learned = _learning_context(symbol, direction)

    rotation_bonus = (_safe_float(rot.get("direction_score"), 70.0) - 70.0) * 0.35

    learned_total = _safe_float(learned.get("weighted_total"), 0.0)
    learned_wr = _safe_float(learned.get("winrate"), 50.0)
    learning_bonus = 0.0
    if learned_total >= 2.5:
        learning_bonus = max(-10.0, min(10.0, (learned_wr - 50.0) * 0.18))
    if str(learned.get("behavior", "")).upper() == "GOOD":
        learning_bonus += 3.0
    elif str(learned.get("behavior", "")).upper() in {"BAD", "WEAK"}:
        learning_bonus -= 4.0
    if str(learned.get("personality", "")).upper() == "RISKY_DIRECTION":
        learning_bonus -= 4.0

    risk_score = max(_safe_float(rot.get("risk_score"), 0.0), _safe_float(risk.get("risk_score"), 0.0))
    strictness = _safe_float(risk.get("strictness_level"), 0.0)
    sl_count = max(_safe_int(risk.get("sl_count"), 0), _safe_int(learned.get("sl"), 0))

    after_two_sl_penalty = 0.0
    if sl_count >= 2:
        after_two_sl_penalty = min(14.0, (sl_count - 1) * 4.5)
    risk_penalty = min(16.0, risk_score * 0.12) + strictness * 2.2 + after_two_sl_penalty
    if risk.get("recommend_reduce"):
        risk_penalty += 3.0

    return base_score + confirmations * 1.5 + rr * 2.0 + risk_level_bonus + fresh_bonus + rotation_bonus + learning_bonus - risk_penalty


def _good_for_ghost(x: Dict[str, Any]) -> bool:
    score = max(_safe_float(x.get("score"), 0.0), _safe_float(x.get("ai_final_score"), 0.0))
    rank = candidate_rank_value(x)
    return bool(x.get("symbol") and x.get("direction") and x.get("entry") is not None and x.get("stop_loss") is not None and x.get("tp1") is not None and (score >= MIN_GHOST_SCORE or rank >= MIN_GHOST_SCORE))


def save_candidate_as_ghost(x: Dict[str, Any], reason: str = "SLOT_MANAGER_UNUSED") -> bool:
    if not create_ghost_signal or not _good_for_ghost(x):
        return False
    try:
        snap = _candidate_snapshot(x)
        snap["slot_rank"] = candidate_rank_value(x)
        if isinstance(x.get("ai_scanner"), dict):
            snap["ai_scanner"] = x.get("ai_scanner")
        create_ghost_signal(
            _norm_symbol(x.get("symbol")),
            _norm_direction(x.get("direction")),
            x.get("entry"),
            x.get("stop_loss"),
            x.get("tp1"),
            x.get("tp2"),
            x.get("score"),
            snap,
            "slot_manager",
            reason,
        )
        return True
    except Exception:
        return False


def queue_candidates(candidates: List[Dict[str, Any]], reason: str = "QUEUE") -> int:
    if not candidates:
        return 0
    s = _state()
    q = s.setdefault("queue", [])
    now = _now()
    existing = {str(x.get("signal_id") or x.get("id") or "") for x in q if isinstance(x, dict)}
    added = 0
    for c in candidates:
        if not isinstance(c, dict):
            continue
        sid = str(c.get("signal_id") or c.get("id") or f"queued_{_norm_symbol(c.get('symbol'))}_{_norm_direction(c.get('direction'))}_{now}_{added}")
        if sid in existing:
            continue
        item = dict(c)
        item["signal_id"] = sid
        item["queued_at"] = now
        item["queue_reason"] = reason
        item["slot_rank"] = candidate_rank_value(item)
        q.append(item)
        existing.add(sid)
        added += 1
    q.sort(key=candidate_rank_value, reverse=True)
    s["queue"] = q[:MAX_QUEUE_SIZE]
    if added:
        _append_history(s, {"event": "QUEUE_CANDIDATES", "count": added, "reason": reason})
        _save_state(s)
    return added


def pop_best_queued_candidates(limit: int = 1, max_age_seconds: int = 900) -> List[Dict[str, Any]]:
    s = _state()
    now = _now()
    q = [x for x in s.get("queue", []) if isinstance(x, dict) and now - _safe_int(x.get("queued_at"), now) <= max_age_seconds]
    q.sort(key=candidate_rank_value, reverse=True)
    selected: List[Dict[str, Any]] = []
    remaining: List[Dict[str, Any]] = []
    for item in q:
        if len(selected) < max(0, int(limit)):
            ok, _ = can_open_new_position(item.get("symbol"), item.get("direction"))
            if ok and not is_symbol_direction_active(item.get("symbol"), item.get("direction")):
                selected.append(item)
                continue
        remaining.append(item)
    s["queue"] = remaining[:MAX_QUEUE_SIZE]
    if selected:
        _append_history(s, {"event": "POP_QUEUE", "count": len(selected)})
    _save_state(s)
    return selected


def select_best_candidates(candidates: List[Dict], limit: int = 1, ghost_unused: bool = True) -> List[Dict]:
    cleanup_expired_pending_positions(save=True)
    candidates = [c for c in (candidates or []) if isinstance(c, dict)]
    if not candidates:
        return []
    ranked = sorted(candidates, key=candidate_rank_value, reverse=True)
    selected: List[Dict[str, Any]] = []
    for c in ranked:
        if len(selected) >= max(0, int(limit)):
            break
        ok, _ = can_open_new_position(c.get("symbol"), c.get("direction"))
        if ok and not is_symbol_direction_active(c.get("symbol"), c.get("direction")):
            cc = dict(c)
            cc["slot_rank"] = candidate_rank_value(cc)
            selected.append(cc)

    selected_ids = {str(x.get("signal_id") or x.get("id") or "") for x in selected}
    unused = []
    for c in ranked:
        sid = str(c.get("signal_id") or c.get("id") or "")
        if sid and sid in selected_ids:
            continue
        unused.append(c)

    if unused:
        queue_candidates(unused, reason="NOT_SELECTED")
        if ghost_unused:
            for c in unused[:20]:
                save_candidate_as_ghost(c, "SLOT_MANAGER_NOT_SELECTED")
    return selected[:limit]


def format_slot_report() -> str:
    cleanup_expired_pending_positions(save=True)
    ps = get_active_positions()
    s = _state()
    q = s.get("queue", []) if isinstance(s.get("queue"), list) else []
    lines = [f"📌 Slot ها: {len(ps)}/{get_max_active_positions()} | خالی: {get_free_slots()} | صف AI: {len(q)}"]
    for p in ps:
        status = p.get("status", "ACTIVE")
        extra = ""
        if str(status).upper() == "PENDING_REAL_CONFIRM":
            exp = _safe_int(p.get("pending_expires_at"), 0)
            left = max(0, exp - _now()) if exp else 0
            extra = f" | pending:{left}s"
        rank = p.get("ai_final_rank") if p.get("ai_final_rank") is not None else p.get("slot_rank")
        lines.append(f"{p.get('symbol')} {p.get('direction')} | {p.get('score')} | {status} | rank:{rank}{extra}")
    return "\n".join(lines)
