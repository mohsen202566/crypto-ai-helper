from __future__ import annotations

"""
Ghost Signal Manager.

Ghost signals are learning-only virtual trades:
- Created when slots are full.
- Created when confidence is promising but not enough for REAL.
- Tracked like a real trade for TP/SL/MFE/MAE.
- Stored in both ghost_signals.json and ai_memory.
- Used for Real vs Ghost comparison and self-audit.

This module does not place real orders and does not send Telegram messages.
"""

import time
import uuid
from typing import Any, Dict, List, Optional

from config import CORE_DATA_FILES
from data_store import load_dict, save_json, backup_file, prune_records
from diagnostics import safe
import ai_memory


GHOST_FILE = CORE_DATA_FILES.get("ghost_signals")
MAX_GHOST_RECORDS = 30000


def _ts() -> int:
    return int(time.time())


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _empty_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "created_at": _ts(),
        "updated_at": _ts(),
        "open": {},
        "closed": {},
        "stats": {
            "created": 0,
            "closed": 0,
            "tp1": 0,
            "tp2": 0,
            "sl": 0,
            "be": 0,
            "cancelled": 0,
        },
    }


@safe(default={})
def load_ghosts() -> Dict[str, Any]:
    st = load_dict(GHOST_FILE)
    if not st:
        st = _empty_state()
        save_json(GHOST_FILE, st)
    for k, v in _empty_state().items():
        st.setdefault(k, v)
    return st


@safe(default=False)
def save_ghosts(st: Dict[str, Any], make_backup: bool = False) -> bool:
    st["updated_at"] = _ts()
    closed = st.get("closed", {})
    if isinstance(closed, dict) and len(closed) > MAX_GHOST_RECORDS:
        items = sorted(closed.items(), key=lambda kv: kv[1].get("updated_at", kv[1].get("created_at", 0)))
        st["closed"] = dict(items[-MAX_GHOST_RECORDS:])
    return save_json(GHOST_FILE, st, make_backup=make_backup)


def _direction_sign(direction: str) -> int:
    d = str(direction).upper()
    return 1 if d == "LONG" else -1 if d == "SHORT" else 0


@safe(default="")
def create_ghost(decision: Dict[str, Any], reason: str = "slot_full_or_learning") -> str:
    """
    Create a ghost trade from AI decision.
    Expected decision fields: symbol, direction, entry, tp1, tp2, sl, confidence, modules, metadata.
    """
    st = load_ghosts()
    symbol = str(decision.get("symbol", "")).upper()
    direction = str(decision.get("direction", "")).upper()
    if not symbol or direction not in {"LONG", "SHORT"}:
        return ""

    gid = str(decision.get("record_id") or f"ghost_{int(time.time())}_{symbol}_{direction}_{uuid.uuid4().hex[:8]}")
    entry = _safe_float(decision.get("entry"))
    tp1 = _safe_float(decision.get("tp1"))
    tp2 = _safe_float(decision.get("tp2"))
    sl = _safe_float(decision.get("sl"))
    if entry <= 0 or tp1 <= 0 or sl <= 0:
        return ""

    ai_record_id = decision.get("record_id")
    if not ai_record_id:
        ai_record_id = ai_memory.create_record(
            symbol=symbol,
            direction=direction,
            decision="GHOST",
            setup_snapshot=decision.get("metadata", {}),
            entry_price=entry,
            tp1=tp1,
            tp2=tp2,
            sl=sl,
            ai_confidence=_safe_float(decision.get("confidence")),
            ai_reason=reason,
            modules=decision.get("modules", {}),
            telegram_message_id=decision.get("telegram_message_id"),
            reply_chat_id=decision.get("reply_chat_id"),
            record_id=gid,
        )

    rec = {
        "id": gid,
        "ai_record_id": ai_record_id,
        "symbol": symbol,
        "direction": direction,
        "status": "OPEN",
        "created_at": _ts(),
        "updated_at": _ts(),
        "entry": entry,
        "tp1": tp1,
        "tp2": tp2,
        "sl": sl,
        "confidence": _safe_float(decision.get("confidence")),
        "priority": _safe_float(decision.get("priority")),
        "reason": reason,
        "metadata": decision.get("metadata", {}),
        "modules": decision.get("modules", {}),
        "mfe": 0.0,
        "mae": 0.0,
        "max_profit_pct": 0.0,
        "max_adverse_pct": 0.0,
        "hit_tp1": False,
        "hit_tp2": False,
        "result": "OPEN",
    }
    st["open"][gid] = rec
    st["stats"]["created"] = int(st["stats"].get("created", 0)) + 1
    save_ghosts(st)
    return gid


@safe(default=False)
def update_ghost_price(ghost_id: str, price: float, high_price: Optional[float] = None, low_price: Optional[float] = None) -> bool:
    st = load_ghosts()
    rec = st.get("open", {}).get(ghost_id)
    if not rec:
        return False

    price = _safe_float(price)
    high = _safe_float(high_price if high_price is not None else price)
    low = _safe_float(low_price if low_price is not None else price)
    entry = _safe_float(rec.get("entry"))
    if entry <= 0:
        return False

    direction = str(rec.get("direction", "")).upper()
    sign = _direction_sign(direction)

    if direction == "LONG":
        favorable = max(0.0, high - entry)
        adverse = max(0.0, entry - low)
        hit_tp1 = high >= _safe_float(rec.get("tp1"))
        hit_tp2 = high >= _safe_float(rec.get("tp2"))
        hit_sl = low <= _safe_float(rec.get("sl"))
    else:
        favorable = max(0.0, entry - low)
        adverse = max(0.0, high - entry)
        hit_tp1 = low <= _safe_float(rec.get("tp1"))
        hit_tp2 = low <= _safe_float(rec.get("tp2"))
        hit_sl = high >= _safe_float(rec.get("sl"))

    rec["mfe"] = max(_safe_float(rec.get("mfe")), favorable)
    rec["mae"] = max(_safe_float(rec.get("mae")), adverse)
    rec["max_profit_pct"] = round(rec["mfe"] / entry * 100, 6)
    rec["max_adverse_pct"] = round(rec["mae"] / entry * 100, 6)
    rec["updated_at"] = _ts()
    rec["last_price"] = price

    ai_memory.update_excursion(rec.get("ai_record_id", ghost_id), price, high, low)

    if hit_tp2:
        return close_ghost(ghost_id, "TP2", price)
    if hit_tp1 and not rec.get("hit_tp1"):
        rec["hit_tp1"] = True
        st["stats"]["tp1"] = int(st["stats"].get("tp1", 0)) + 1
        # keep open for TP2/SL learning unless caller uses close_on_tp1 policy
    if hit_sl:
        return close_ghost(ghost_id, "SL", price)

    st["open"][ghost_id] = rec
    save_ghosts(st)
    return True


@safe(default=False)
def close_ghost(ghost_id: str, result: str, exit_price: float = 0.0, exit_snapshot: Optional[Dict[str, Any]] = None) -> bool:
    st = load_ghosts()
    rec = st.get("open", {}).pop(ghost_id, None)
    if not rec:
        return False

    result = str(result).upper()
    rec["status"] = "CLOSED"
    rec["result"] = result
    rec["exit_price"] = _safe_float(exit_price)
    rec["exit_time"] = _ts()
    rec["updated_at"] = _ts()
    rec["exit_snapshot"] = exit_snapshot or {}

    st["closed"][ghost_id] = rec
    st["stats"]["closed"] = int(st["stats"].get("closed", 0)) + 1
    if result.startswith("TP"):
        st["stats"][result.lower()] = int(st["stats"].get(result.lower(), 0)) + 1
    elif result == "SL":
        st["stats"]["sl"] = int(st["stats"].get("sl", 0)) + 1
    elif result == "BE":
        st["stats"]["be"] = int(st["stats"].get("be", 0)) + 1
    elif result in {"CANCELLED", "CANCELED"}:
        st["stats"]["cancelled"] = int(st["stats"].get("cancelled", 0)) + 1

    ai_memory.close_record(
        rec.get("ai_record_id", ghost_id),
        result=result,
        exit_price=exit_price,
        pnl=0.0,
        exit_snapshot=exit_snapshot or {},
        final_mfe=rec.get("mfe"),
        final_mae=rec.get("mae"),
    )
    save_ghosts(st, make_backup=True)
    return True


@safe(default=False)
def cancel_ghost(ghost_id: str, reason: str = "") -> bool:
    return close_ghost(ghost_id, "CANCELLED", exit_snapshot={"cancel_reason": reason})


@safe(default=[])
def open_ghosts(symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    st = load_ghosts()
    rows = list(st.get("open", {}).values())
    if symbol:
        rows = [r for r in rows if r.get("symbol") == str(symbol).upper()]
    return sorted(rows, key=lambda r: r.get("created_at", 0), reverse=True)


@safe(default=[])
def recent_closed(limit: int = 20) -> List[Dict[str, Any]]:
    st = load_ghosts()
    rows = list(st.get("closed", {}).values())
    return sorted(rows, key=lambda r: r.get("exit_time", r.get("updated_at", 0)), reverse=True)[:limit]


@safe(default={})
def summary() -> Dict[str, Any]:
    st = load_ghosts()
    stats = dict(st.get("stats", {}))
    open_count = len(st.get("open", {}))
    closed_count = len(st.get("closed", {}))
    tp = int(stats.get("tp1", 0)) + int(stats.get("tp2", 0))
    sl = int(stats.get("sl", 0))
    wr = round(tp / max(1, tp + sl) * 100, 2)
    return {
        "open": open_count,
        "closed": closed_count,
        "created": stats.get("created", 0),
        "tp": tp,
        "tp1": stats.get("tp1", 0),
        "tp2": stats.get("tp2", 0),
        "sl": sl,
        "wr": wr,
        "stats": stats,
        "updated_at": st.get("updated_at"),
    }


@safe(default="")
def summary_fa() -> str:
    s = summary()
    return (
        "👻 سیگنال‌های مخفی\n"
        f"باز: {s.get('open',0)} | بسته: {s.get('closed',0)}\n"
        f"TP: {s.get('tp',0)} | SL: {s.get('sl',0)} | WR: {s.get('wr',0)}%"
    )


@safe(default=True)
def initialize() -> bool:
    st = load_ghosts()
    save_ghosts(st)
    return True
