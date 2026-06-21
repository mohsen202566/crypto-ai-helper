from __future__ import annotations

"""
Recovery Manager.

Responsibilities:
- Restore persistent stores on startup.
- Reconcile active tracker signals and slot manager state.
- Detect stale PENDING_REAL_CONFIRM slots.
- Produce startup health report.
- Do not import bot.py.
"""

import time
from typing import Any, Dict, List

from diagnostics import safe, health_report
import ai_memory
import coin_learning
import coin_risk
import coin_rotation
import sr_learning
import ghost_signals
import slot_manager
import signal_tracker
import real_trade_manager
import real_position_sync
import reply_manager


def _ts() -> int:
    return int(time.time())


@safe(default={})
def startup_recovery() -> Dict[str, Any]:
    results: Dict[str, Any] = {"started_at": _ts(), "steps": {}, "warnings": []}

    for name, fn in [
        ("ai_memory", ai_memory.initialize_memory_files),
        ("coin_learning", coin_learning.initialize),
        ("coin_risk", coin_risk.initialize),
        ("coin_rotation", coin_rotation.initialize),
        ("sr_learning", sr_learning.initialize),
        ("ghost_signals", ghost_signals.initialize),
        ("slot_manager", slot_manager.initialize),
        ("signal_tracker", signal_tracker.initialize),
        ("real_trade_manager", real_trade_manager.initialize),
        ("reply_manager", reply_manager.initialize),
    ]:
        try:
            results["steps"][name] = bool(fn())
        except Exception as e:
            results["steps"][name] = False
            results["warnings"].append(f"{name}:{type(e).__name__}")

    expired = real_position_sync.cleanup_pending_slots()
    results["expired_pending_slots"] = expired

    active = signal_tracker.active_signals()
    slots = slot_manager.slot_state()
    ghosts = ghost_signals.open_ghosts()

    results["active_signals"] = len(active)
    results["open_ghosts"] = len(ghosts)
    results["slots"] = slots
    results["health"] = health_report()
    results["finished_at"] = _ts()
    return results


@safe(default="")
def startup_report_fa() -> str:
    r = startup_recovery()
    return (
        "♻️ بازیابی ربات انجام شد\n"
        f"سیگنال فعال/زیرنظر: {r.get('active_signals',0)}\n"
        f"Ghost باز: {r.get('open_ghosts',0)}\n"
        f"اسلات: {r.get('slots',{}).get('used_slots',0)}/{r.get('slots',{}).get('max_positions',0)}\n"
        f"Pending منقضی‌شده: {r.get('expired_pending_slots',0)}"
    )
