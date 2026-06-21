from __future__ import annotations

"""
Real position synchronization.

Responsibilities:
- After real order, poll Toobit for 60-70 seconds to confirm actual position.
- Confirm slot only when position is seen.
- Release pending slot only after timeout if no real position exists.
- Resolve closed real PnL/history after close with waiting window.
"""

import time
from typing import Any, Dict, Optional, List

from config import REAL_CONFIRM_TIMEOUT_SECONDS, REAL_CLOSED_PNL_WAIT_SECONDS
from diagnostics import safe
import tobit_client
import slot_manager
import signal_tracker
import real_trade_manager


def _ts() -> int:
    return int(time.time())


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


@safe(default={})
def confirm_pending_position(signal_id: str, slot_id: str, symbol: str, timeout_seconds: int = REAL_CONFIRM_TIMEOUT_SECONDS, poll_interval: float = 3.0) -> Dict[str, Any]:
    c = tobit_client.client()
    deadline = time.time() + timeout_seconds
    last = {}
    while time.time() <= deadline:
        pos = c.get_position(symbol)
        last = pos
        if pos.get("ok") and abs(_safe_float(pos.get("position_amt"))) > 0:
            slot_manager.confirm_slot(slot_id, exchange_position_id=str(pos.get("raw", {}).get("positionId", "")), metadata={"confirmed_by": "real_position_sync"})
            return {"ok": True, "signal_id": signal_id, "slot_id": slot_id, "symbol": symbol.upper(), "position": pos}
        time.sleep(max(0.5, poll_interval))
    slot_manager.release_slot(slot_id, reason="real_position_not_confirmed")
    return {"ok": False, "signal_id": signal_id, "slot_id": slot_id, "symbol": symbol.upper(), "reason": "timeout_no_position", "last": last}


@safe(default={})
def resolve_closed_pnl(signal_id: str, symbol: str, start_time_ms: Optional[int] = None, timeout_seconds: int = REAL_CLOSED_PNL_WAIT_SECONDS, poll_interval: float = 4.0) -> Dict[str, Any]:
    c = tobit_client.client()
    deadline = time.time() + timeout_seconds
    rows: List[Dict[str, Any]] = []
    while time.time() <= deadline:
        rows = c.closed_pnl_history(symbol, start_time=start_time_ms, limit=20)
        if rows:
            pnl = 0.0
            for r in rows:
                income_type = str(r.get("incomeType", r.get("type", ""))).upper()
                if income_type in {"REALIZED_PNL", "REALIZEDPNL", "PNL", ""}:
                    pnl += _safe_float(r.get("income", r.get("pnl", r.get("realizedPnl", 0))))
            return {"ok": True, "signal_id": signal_id, "symbol": symbol.upper(), "pnl": round(pnl, 6), "rows": rows}
        time.sleep(max(0.5, poll_interval))
    return {"ok": False, "signal_id": signal_id, "symbol": symbol.upper(), "reason": "pnl_history_timeout", "rows": rows}


@safe(default={})
def mark_real_closed(signal_id: str, result: str, exit_price: float, symbol: str, start_time_ms: Optional[int] = None) -> Dict[str, Any]:
    pnl_res = resolve_closed_pnl(signal_id, symbol, start_time_ms=start_time_ms)
    pnl = _safe_float(pnl_res.get("pnl")) if pnl_res.get("ok") else 0.0
    signal_tracker.close_signal(signal_id, result=result, exit_price=exit_price, pnl=pnl, snapshot={"real_pnl_resolution": pnl_res})
    return {"ok": True, "signal_id": signal_id, "result": result, "pnl": pnl, "pnl_resolution": pnl_res}


@safe(default=0)
def cleanup_pending_slots() -> int:
    """
    Release expired pending real slots and cancel their tracker signal.
    This preserves the 60-70s pending window and avoids orphan ACTIVE real signals.
    """
    now = _ts()
    count = 0
    # Read timeout from slot manager state.
    state = slot_manager.load_slots()
    timeout = int(state.get("settings", {}).get("pending_confirm_timeout", REAL_CONFIRM_TIMEOUT_SECONDS))
    for p in slot_manager.pending_slots():
        pending_since = int(p.get("pending_since", p.get("created_at", now)) or now)
        if now - pending_since < timeout:
            continue
        sid = p.get("signal_id", "")
        slot_id = p.get("slot_id", "")
        if slot_id:
            slot_manager.release_slot(slot_id, reason="real_position_not_confirmed")
        if sid:
            signal_tracker.cancel_signal(sid, "real_position_not_confirmed")
        count += 1
    return count


@safe(default=0)
def confirm_all_pending_slots(max_items: int = 3) -> int:
    """
    Non-blocking background helper.
    It checks a few pending real slots once per loop and confirms them if a real
    position exists. It does NOT release slots early; cleanup_pending_slots()
    handles release only after the full configured timeout.
    """
    c = tobit_client.client()
    pending = slot_manager.pending_slots()[:max_items]
    done = 0
    for p in pending:
        symbol = p.get("symbol", "")
        if not symbol:
            continue
        pos = c.get_position(symbol)
        if pos.get("ok") and abs(_safe_float(pos.get("position_amt"))) > 0:
            slot_manager.confirm_slot(
                p.get("slot_id", ""),
                exchange_position_id=str(pos.get("raw", {}).get("positionId", "")),
                metadata={"confirmed_by": "real_position_sync.background"},
            )
            done += 1
    cleanup_pending_slots()
    return done
