from __future__ import annotations

"""
Signal Tracker.

Responsibilities:
- Track SETUP / REAL / PAPER / GHOST lifecycle.
- Update MFE/MAE for all active signals.
- Detect TP1 / TP2 / SL / BE / cancellation.
- Connect outcomes to ai_memory.
- Connect ghost outcomes to ghost_signals.
- Release slots only after confirmed close/result.
- Preserve Telegram reply metadata for bot.py, without importing bot.py.

This module does not fetch market data by itself.
The scanner / bot loop passes latest prices/candles to update functions.
"""

import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from config import CORE_DATA_FILES
from data_store import load_dict, save_json
from diagnostics import safe
import ai_memory
import ghost_signals
import slot_manager
import coin_learning
import coin_risk
import coin_rotation
import sr_learning


TRACKER_FILE = CORE_DATA_FILES.get("stats")


TYPE_SETUP = "SETUP"
TYPE_REAL = "REAL"
TYPE_PAPER = "PAPER"
TYPE_GHOST = "GHOST"

STATUS_WATCHING = "WATCHING"
STATUS_ACTIVE = "ACTIVE"
STATUS_CLOSED = "CLOSED"
STATUS_CANCELLED = "CANCELLED"

RESULT_TP1 = "TP1"
RESULT_TP2 = "TP2"
RESULT_SL = "SL"
RESULT_BE = "BE"
RESULT_CANCELLED = "CANCELLED"


def _ts() -> int:
    return int(time.time())


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _direction_sign(direction: str) -> int:
    d = str(direction).upper()
    if d == "LONG":
        return 1
    if d == "SHORT":
        return -1
    return 0


def _empty_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "created_at": _ts(),
        "updated_at": _ts(),
        "signals": {},
        "closed": {},
        "events": [],
        "stats": {
            "setup_created": 0,
            "activated": 0,
            "cancelled": 0,
            "tp1": 0,
            "tp2": 0,
            "final_tp": 0,
            "sl": 0,
            "be": 0,
            "paper": 0,
            "real": 0,
            "ghost": 0,
        },
    }


@safe(default={})
def load_tracker() -> Dict[str, Any]:
    st = load_dict(TRACKER_FILE)
    if not st:
        st = _empty_state()
        save_json(TRACKER_FILE, st)
    for k, v in _empty_state().items():
        st.setdefault(k, v)
    return st


@safe(default=False)
def save_tracker(st: Dict[str, Any], make_backup: bool = False) -> bool:
    st["updated_at"] = _ts()
    if isinstance(st.get("events"), list):
        st["events"] = st["events"][-1500:]
    if isinstance(st.get("closed"), dict) and len(st["closed"]) > 30000:
        items = sorted(st["closed"].items(), key=lambda kv: kv[1].get("updated_at", kv[1].get("created_at", 0)))
        st["closed"] = dict(items[-30000:])
    return save_json(TRACKER_FILE, st, make_backup=make_backup)


def _event(st: Dict[str, Any], event: str, signal: Dict[str, Any], extra: Optional[Dict[str, Any]] = None) -> None:
    st.setdefault("events", []).append({
        "ts": _ts(),
        "event": event,
        "signal_id": signal.get("signal_id"),
        "ai_record_id": signal.get("ai_record_id"),
        "symbol": signal.get("symbol"),
        "direction": signal.get("direction"),
        "type": signal.get("type"),
        "status": signal.get("status"),
        "extra": extra or {},
    })
    st["events"] = st["events"][-1500:]


@safe(default="")
def create_setup(decision: Dict[str, Any], source: str = "scanner") -> str:
    """
    Create a predictive setup. It is not an active trade yet.
    """
    st = load_tracker()
    symbol = str(decision.get("symbol", "")).upper()
    direction = str(decision.get("direction", "")).upper()
    if not symbol or direction not in {"LONG", "SHORT"}:
        return ""

    sid = str(decision.get("record_id") or f"setup_{int(time.time())}_{symbol}_{uuid.uuid4().hex[:8]}")
    ai_record_id = decision.get("record_id")
    if not ai_record_id:
        ai_record_id = ai_memory.create_record(
            symbol=symbol,
            direction=direction,
            decision="SETUP",
            setup_snapshot=decision.get("metadata", {}),
            entry_price=_safe_float(decision.get("entry")),
            tp1=_safe_float(decision.get("tp1")),
            tp2=_safe_float(decision.get("tp2")),
            sl=_safe_float(decision.get("sl")),
            ai_confidence=_safe_float(decision.get("confidence")),
            ai_reason=decision.get("reason", source),
            modules=decision.get("modules", {}),
            telegram_message_id=decision.get("telegram_message_id"),
            reply_chat_id=decision.get("reply_chat_id"),
            record_id=sid,
        )

    rec = _base_signal(decision, sid, ai_record_id, TYPE_SETUP, STATUS_WATCHING)
    rec["source"] = source
    rec["valid_until"] = int(decision.get("valid_until", _ts() + 45 * 60))
    rec["activation_conditions"] = decision.get("activation_conditions", {})
    st["signals"][sid] = rec
    st["stats"]["setup_created"] = int(st["stats"].get("setup_created", 0)) + 1
    _event(st, "SETUP_CREATED", rec)
    save_tracker(st)
    return sid


@safe(default="")
def activate_signal(
    setup_id: str,
    decision: Optional[Dict[str, Any]] = None,
    mode: str = TYPE_PAPER,
    reserve_slot: bool = True,
) -> str:
    """
    Activate a setup into PAPER/REAL/GHOST.
    For REAL, slot should normally be reserved as PENDING_REAL_CONFIRM by real_trade_manager.
    Here we reserve PAPER slots directly and mark active.
    """
    st = load_tracker()
    setup = st.get("signals", {}).get(setup_id)
    if not setup:
        return ""

    decision = decision or {}
    mode = str(mode).upper()
    if mode not in {TYPE_PAPER, TYPE_REAL, TYPE_GHOST}:
        mode = TYPE_PAPER

    setup["type"] = mode
    setup["status"] = STATUS_ACTIVE
    setup["activated_at"] = _ts()
    setup["updated_at"] = _ts()

    for k in ["entry", "tp1", "tp2", "sl", "confidence", "priority", "metadata", "modules"]:
        if decision.get(k) is not None:
            setup[k] = decision.get(k)

    if setup.get("ai_record_id"):
        ai_memory.activate_record(
            setup["ai_record_id"],
            activation_snapshot=decision.get("metadata", setup.get("metadata", {})),
            entry_price=_safe_float(setup.get("entry")),
            tp1=_safe_float(setup.get("tp1")),
            tp2=_safe_float(setup.get("tp2")),
            sl=_safe_float(setup.get("sl")),
            decision="REAL" if mode in {TYPE_REAL, TYPE_PAPER} else "GHOST",
        )

    if mode == TYPE_GHOST:
        gid = ghost_signals.create_ghost({**setup, **decision, "record_id": setup.get("ai_record_id") or setup_id}, reason="tracker_activation")
        setup["ghost_id"] = gid
    elif reserve_slot:
        slot_status = slot_manager.STATUS_OPEN if mode == TYPE_PAPER else slot_manager.STATUS_PENDING_REAL_CONFIRM
        slot_id = slot_manager.reserve_slot(
            symbol=setup.get("symbol"),
            direction=setup.get("direction"),
            mode=mode,
            status=slot_status,
            ai_record_id=setup.get("ai_record_id"),
            signal_id=setup_id,
            telegram_message_id=setup.get("telegram_message_id"),
            metadata={"source": "signal_tracker.activate_signal"},
        )
        setup["slot_id"] = slot_id

    st["signals"][setup_id] = setup
    st["stats"]["activated"] = int(st["stats"].get("activated", 0)) + 1
    st["stats"][mode.lower()] = int(st["stats"].get(mode.lower(), 0)) + 1
    _event(st, "ACTIVATED", setup)
    save_tracker(st, make_backup=True)
    return setup_id


@safe(default="")
def register_active_signal(decision: Dict[str, Any], mode: str = TYPE_PAPER, slot_id: str = "") -> str:
    """
    Register an already-active signal, used by trade manager after order/paper open.
    """
    st = load_tracker()
    symbol = str(decision.get("symbol", "")).upper()
    direction = str(decision.get("direction", "")).upper()
    if not symbol or direction not in {"LONG", "SHORT"}:
        return ""

    sid = str(decision.get("signal_id") or decision.get("record_id") or f"sig_{int(time.time())}_{symbol}_{uuid.uuid4().hex[:8]}")
    ai_record_id = decision.get("record_id") or decision.get("ai_record_id")
    if not ai_record_id:
        ai_record_id = ai_memory.create_record(
            symbol=symbol,
            direction=direction,
            decision="REAL" if mode in {TYPE_REAL, TYPE_PAPER} else "GHOST",
            setup_snapshot=decision.get("metadata", {}),
            entry_price=_safe_float(decision.get("entry")),
            tp1=_safe_float(decision.get("tp1")),
            tp2=_safe_float(decision.get("tp2")),
            sl=_safe_float(decision.get("sl")),
            ai_confidence=_safe_float(decision.get("confidence")),
            ai_reason=decision.get("reason", "register_active_signal"),
            modules=decision.get("modules", {}),
            telegram_message_id=decision.get("telegram_message_id"),
            reply_chat_id=decision.get("reply_chat_id"),
            record_id=sid,
        )
        ai_memory.activate_record(ai_record_id, activation_snapshot=decision.get("metadata", {}))

    rec = _base_signal(decision, sid, ai_record_id, str(mode).upper(), STATUS_ACTIVE)
    rec["activated_at"] = _ts()
    rec["slot_id"] = slot_id or decision.get("slot_id", "")
    st["signals"][sid] = rec
    st["stats"]["activated"] = int(st["stats"].get("activated", 0)) + 1
    _event(st, "REGISTER_ACTIVE", rec)
    save_tracker(st, make_backup=True)
    return sid


def _base_signal(decision: Dict[str, Any], sid: str, ai_record_id: str, typ: str, status: str) -> Dict[str, Any]:
    return {
        "signal_id": sid,
        "ai_record_id": ai_record_id,
        "symbol": str(decision.get("symbol", "")).upper(),
        "direction": str(decision.get("direction", "")).upper(),
        "type": typ,
        "status": status,
        "created_at": _ts(),
        "updated_at": _ts(),
        "entry": _safe_float(decision.get("entry")),
        "tp1": _safe_float(decision.get("tp1")),
        "tp2": _safe_float(decision.get("tp2")),
        "sl": _safe_float(decision.get("sl")),
        "confidence": _safe_float(decision.get("confidence")),
        "priority": _safe_float(decision.get("priority")),
        "metadata": decision.get("metadata", {}),
        "modules": decision.get("modules", {}),
        "telegram_message_id": decision.get("telegram_message_id"),
        "reply_chat_id": decision.get("reply_chat_id"),
        "ghost_id": decision.get("ghost_id", ""),
        "mfe": 0.0,
        "mae": 0.0,
        "max_profit_pct": 0.0,
        "max_adverse_pct": 0.0,
        "hit_tp1": False,
        "hit_tp2": False,
        "break_even_enabled": False,
        "result": "OPEN",
    }


@safe(default=False)
def update_price(signal_id: str, price: float, high_price: Optional[float] = None, low_price: Optional[float] = None, snapshot: Optional[Dict[str, Any]] = None) -> bool:
    """
    Update one active signal with latest price/high/low.
    Returns True if updated or closed.
    """
    st = load_tracker()
    sig = st.get("signals", {}).get(signal_id)
    if not sig:
        return False
    if sig.get("status") not in {STATUS_ACTIVE, STATUS_WATCHING}:
        return False

    # Watching setups only update freshness; no TP/SL until activated.
    if sig.get("status") == STATUS_WATCHING:
        sig["last_price"] = _safe_float(price)
        sig["updated_at"] = _ts()
        if _ts() > int(sig.get("valid_until", _ts()+1)):
            return cancel_signal(signal_id, "setup_expired")
        st["signals"][signal_id] = sig
        save_tracker(st)
        return True

    price = _safe_float(price)
    high = _safe_float(high_price if high_price is not None else price)
    low = _safe_float(low_price if low_price is not None else price)
    entry = _safe_float(sig.get("entry"))
    if entry <= 0:
        return False

    direction = str(sig.get("direction", "")).upper()

    if direction == "LONG":
        favorable = max(0.0, high - entry)
        adverse = max(0.0, entry - low)
        hit_tp1 = high >= _safe_float(sig.get("tp1"))
        hit_tp2 = high >= _safe_float(sig.get("tp2"))
        hit_sl = low <= _safe_float(sig.get("sl"))
    else:
        favorable = max(0.0, entry - low)
        adverse = max(0.0, high - entry)
        hit_tp1 = low <= _safe_float(sig.get("tp1"))
        hit_tp2 = low <= _safe_float(sig.get("tp2"))
        hit_sl = high >= _safe_float(sig.get("sl"))

    sig["mfe"] = max(_safe_float(sig.get("mfe")), favorable)
    sig["mae"] = max(_safe_float(sig.get("mae")), adverse)
    sig["max_profit_pct"] = round(sig["mfe"] / entry * 100, 6)
    sig["max_adverse_pct"] = round(sig["mae"] / entry * 100, 6)
    sig["last_price"] = price
    sig["updated_at"] = _ts()

    ai_memory.update_excursion(sig.get("ai_record_id", signal_id), price, high, low)

    if sig.get("type") == TYPE_GHOST and sig.get("ghost_id"):
        ghost_signals.update_ghost_price(sig["ghost_id"], price, high, low)

    # Priority: SL after TP1 can become BE/profit protection in future trade manager.
    if hit_tp2:
        return close_signal(signal_id, RESULT_TP2, exit_price=price, snapshot=snapshot)
    if hit_tp1 and not sig.get("hit_tp1"):
        sig["hit_tp1"] = True
        sig["tp1_time"] = _ts()
        sig["break_even_enabled"] = True
        st["stats"]["tp1"] = int(st["stats"].get("tp1", 0)) + 1
        _event(st, "TP1_HIT", sig, {"price": price})
    if hit_sl:
        # If TP1 already hit, caller may later use BE/profit protection; for now final SL if price hits SL.
        return close_signal(signal_id, RESULT_SL, exit_price=price, snapshot=snapshot)

    st["signals"][signal_id] = sig
    save_tracker(st)
    return True


@safe(default=0)
def update_many(price_map: Dict[str, Dict[str, Any]]) -> int:
    """
    price_map:
    {
      "BTCUSDT": {"price":..., "high":..., "low":..., "snapshot":...}
    }
    """
    st = load_tracker()
    ids = list(st.get("signals", {}).keys())
    count = 0
    for sid in ids:
        sig = st.get("signals", {}).get(sid, {})
        symbol = sig.get("symbol")
        if symbol not in price_map:
            continue
        row = price_map[symbol]
        if update_price(sid, row.get("price"), row.get("high"), row.get("low"), row.get("snapshot")):
            count += 1
    return count


@safe(default=False)
def close_signal(signal_id: str, result: str, exit_price: float = 0.0, pnl: float = 0.0, snapshot: Optional[Dict[str, Any]] = None) -> bool:
    st = load_tracker()
    sig = st.get("signals", {}).pop(signal_id, None)
    if not sig:
        return False

    result = str(result).upper()
    sig["status"] = STATUS_CLOSED
    sig["result"] = result
    sig["exit_price"] = _safe_float(exit_price)
    sig["pnl"] = _safe_float(pnl)
    sig["exit_time"] = _ts()
    sig["updated_at"] = _ts()
    sig["exit_snapshot"] = snapshot or {}

    st["closed"][signal_id] = sig
    if result.startswith("TP"):
        if result.startswith("TP2"):
            st["stats"]["tp2"] = int(st["stats"].get("tp2", 0)) + 1
        elif result.startswith("TP1"):
            st["stats"]["tp1"] = int(st["stats"].get("tp1", 0)) + 1
        st["stats"]["final_tp"] = int(st["stats"].get("final_tp", 0)) + 1
    elif result == RESULT_SL:
        st["stats"]["sl"] = int(st["stats"].get("sl", 0)) + 1
    elif result == RESULT_BE:
        st["stats"]["be"] = int(st["stats"].get("be", 0)) + 1

    ai_memory.close_record(
        sig.get("ai_record_id", signal_id),
        result=result,
        exit_price=exit_price,
        pnl=pnl,
        exit_snapshot=snapshot or {},
        final_mfe=sig.get("mfe"),
        final_mae=sig.get("mae"),
    )

    _update_learning_modules(sig)

    if sig.get("type") == TYPE_GHOST and sig.get("ghost_id"):
        # Avoid double-closing Ghost:
        # ghost_signals.update_ghost_price may have already closed it before tracker close_signal runs.
        open_ghost_ids = {g.get("id") for g in ghost_signals.open_ghosts()}
        if sig["ghost_id"] in open_ghost_ids:
            ghost_signals.close_ghost(sig["ghost_id"], result, exit_price, snapshot)
    if sig.get("slot_id"):
        slot_manager.release_slot(sig["slot_id"], reason="signal_closed", result=result)

    _event(st, "CLOSED", sig, {"result": result, "exit_price": exit_price})
    save_tracker(st, make_backup=True)
    return True


def _update_learning_modules(sig: Dict[str, Any]) -> None:
    rec = ai_memory.get_record(sig.get("ai_record_id", sig.get("signal_id", "")))
    if not rec:
        rec = sig
    coin_learning.update_from_record(rec)
    coin_risk.update_from_record(rec)
    sr_learning.update_from_record(rec)
    coin_rotation.rebuild()


@safe(default=False)
def cancel_signal(signal_id: str, reason: str = "") -> bool:
    st = load_tracker()
    sig = st.get("signals", {}).pop(signal_id, None)
    if not sig:
        return False
    sig["status"] = STATUS_CANCELLED
    sig["result"] = RESULT_CANCELLED
    sig["cancel_reason"] = reason
    sig["exit_time"] = _ts()
    sig["updated_at"] = _ts()
    st["closed"][signal_id] = sig
    st["stats"]["cancelled"] = int(st["stats"].get("cancelled", 0)) + 1

    ai_memory.cancel_record(sig.get("ai_record_id", signal_id), reason=reason)
    if sig.get("type") == TYPE_GHOST and sig.get("ghost_id"):
        ghost_signals.cancel_ghost(sig["ghost_id"], reason)
    if sig.get("slot_id"):
        slot_manager.release_slot(sig["slot_id"], reason="signal_cancelled:" + reason)

    _event(st, "CANCELLED", sig, {"reason": reason})
    save_tracker(st, make_backup=True)
    return True


@safe(default=[])
def active_signals(symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    st = load_tracker()
    rows = [r for r in st.get("signals", {}).values() if r.get("status") in {STATUS_ACTIVE, STATUS_WATCHING}]
    if symbol:
        rows = [r for r in rows if r.get("symbol") == str(symbol).upper()]
    return sorted(rows, key=lambda r: r.get("created_at", 0), reverse=True)


@safe(default=[])
def closed_signals(limit: int = 20) -> List[Dict[str, Any]]:
    st = load_tracker()
    rows = list(st.get("closed", {}).values())
    return sorted(rows, key=lambda r: r.get("exit_time", r.get("updated_at", 0)), reverse=True)[:limit]


@safe(default={})
def summary() -> Dict[str, Any]:
    st = load_tracker()
    stats = dict(st.get("stats", {}))
    active = active_signals()
    # Win rate must be based on final closed trade result, not TP1+TP2 combined.
    tp = int(stats.get("final_tp", 0))
    sl = int(stats.get("sl", 0))
    wr = round(tp / max(1, tp + sl) * 100, 2)
    return {
        "active": len(active),
        "closed": len(st.get("closed", {})),
        "stats": stats,
        "tp": tp,
        "sl": sl,
        "wr": wr,
        "updated_at": st.get("updated_at"),
    }


@safe(default="")
def summary_fa() -> str:
    s = summary()
    return (
        "📊 پیگیری سیگنال‌ها\n"
        f"فعال/زیرنظر: {s.get('active',0)} | بسته: {s.get('closed',0)}\n"
        f"TP: {s.get('tp',0)} | SL: {s.get('sl',0)} | WR: {s.get('wr',0)}%"
    )


@safe(default="")
def result_message_fa(signal: Dict[str, Any]) -> str:
    result = signal.get("result", "")
    symbol = signal.get("symbol", "")
    direction = "لانگ" if signal.get("direction") == "LONG" else "شورت"
    if result.startswith("TP"):
        icon = "✅"
        label = "حد سود"
    elif result == "SL":
        icon = "❌"
        label = "حد ضرر"
    elif result == "BE":
        icon = "⚪"
        label = "سر به سر"
    else:
        icon = "ℹ️"
        label = result
    return (
        f"{icon} نتیجه سیگنال\n"
        f"ارز: {symbol}\n"
        f"جهت: {direction}\n"
        f"نتیجه: {label} {result}\n"
        f"MFE: {signal.get('max_profit_pct',0)}% | MAE: {signal.get('max_adverse_pct',0)}%"
    )


@safe(default=True)
def initialize() -> bool:
    st = load_tracker()
    save_tracker(st)
    return True
