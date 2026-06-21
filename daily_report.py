from __future__ import annotations

"""
Daily Report.

Responsibilities:
- Build concise daily AI/trade reports.
- Toggle daily report setting through trade_state.
- No scheduling here; bot loop calls maybe_build_daily_report().
"""

import time
from typing import Any, Dict, Optional

from data_store import load_dict, save_json
from diagnostics import safe
import ai_memory
import signal_tracker
import ghost_signals
import coin_learning
import coin_risk
import coin_rotation
import real_trade_manager


REPORT_FILE = "daily_report_state"


def _ts() -> int:
    return int(time.time())


def _day() -> str:
    return time.strftime("%Y-%m-%d")


def _empty_state() -> Dict[str, Any]:
    return {"version": 1, "enabled": True, "last_report_day": "", "updated_at": _ts()}


@safe(default={})
def load_state() -> Dict[str, Any]:
    st = load_dict(REPORT_FILE)
    if not st:
        st = _empty_state()
        save_json(REPORT_FILE, st)
    for k, v in _empty_state().items():
        st.setdefault(k, v)
    return st


@safe(default=True)
def set_enabled(enabled: bool) -> bool:
    st = load_state()
    st["enabled"] = bool(enabled)
    st["updated_at"] = _ts()
    save_json(REPORT_FILE, st, make_backup=True)
    return True


@safe(default=False)
def is_enabled() -> bool:
    return bool(load_state().get("enabled", True))


@safe(default="")
def build_report_fa() -> str:
    mem = ai_memory.summary(use_cache=False)
    tr = signal_tracker.summary()
    gh = ghost_signals.summary()
    risk = coin_risk.summary()
    rot = coin_rotation.summary()
    best = rot.get("best", [])[:3]
    worst = rot.get("worst", [])[:3]
    return (
        f"📅 گزارش روزانه AI - {_day()}\n"
        f"Real: {mem.get('real',0)} | TP:{mem.get('real_tp',0)} SL:{mem.get('real_sl',0)} | WR:{mem.get('real_wr',0)}%\n"
        f"Ghost: {mem.get('ghost',0)} | TP:{mem.get('ghost_tp',0)} SL:{mem.get('ghost_sl',0)} | WR:{mem.get('ghost_wr',0)}%\n"
        f"Tracker: TP:{tr.get('tp',0)} SL:{tr.get('sl',0)} WR:{tr.get('wr',0)}%\n"
        f"Ghost باز: {gh.get('open',0)} | اسلات‌ها: {real_trade_manager.trade_status().get('used_slots',0)}/{real_trade_manager.trade_status().get('max_positions',0)}\n"
        f"بهترین‌ها: {', '.join(x.get('key','') for x in best) or 'داده کافی نیست'}\n"
        f"ضعیف‌ها: {', '.join(x.get('key','') for x in worst) or 'داده کافی نیست'}"
    )


@safe(default="")
def maybe_build_daily_report(force: bool = False) -> str:
    st = load_state()
    if not st.get("enabled", True) and not force:
        return ""
    today = _day()
    if not force and st.get("last_report_day") == today:
        return ""
    # Default send window can be enforced in bot loop; here only once-per-day.
    st["last_report_day"] = today
    st["updated_at"] = _ts()
    save_json(REPORT_FILE, st, make_backup=True)
    return build_report_fa()


@safe(default=True)
def initialize() -> bool:
    st = load_state()
    save_json(REPORT_FILE, st)
    return True
