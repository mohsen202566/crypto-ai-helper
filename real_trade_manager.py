from __future__ import annotations

"""
Real/Paper trade manager.

Responsibilities:
- Open Paper and Real trades.
- Enforce daily loss lock and emergency stop.
- Protected balance increases after realized profit.
- For real trades, reserve slot as PENDING_REAL_CONFIRM before order.
- Real orders are isolated-only through toobit_safety.
- Register active signals with signal_tracker.
"""

import time
import math
from typing import Any, Dict, Optional

from config import (
    CORE_DATA_FILES,
    runtime_defaults_dict,
    REAL_CONFIRM_TIMEOUT_SECONDS,
    DEFAULT_TRADE_MODE,
    DEFAULT_REAL_TRADING_ENABLED,
    TOOBIT_API_KEY,
    TOOBIT_API_SECRET,
)
from data_store import load_dict, save_json
from diagnostics import safe, record_error, warning
import slot_manager
import signal_tracker
import ai_memory
import tobit_client
import toobit_safety


TRADE_STATE_FILE = CORE_DATA_FILES.get("trade_state")


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


def _normalize_trade_mode(value: Any) -> str:
    mode = str(value or "").strip().upper()
    return mode if mode in {"PAPER", "REAL"} else "PAPER"


def _env_runtime_overrides() -> Dict[str, Any]:
    """
    Environment-controlled runtime fields that should be re-applied on restart.
    This prevents old data/trade_state.json from keeping the bot in PAPER after
    systemd/env was changed to REAL.
    """
    return {
        "trade_mode": _normalize_trade_mode(DEFAULT_TRADE_MODE),
        "real_trading_enabled": bool(DEFAULT_REAL_TRADING_ENABLED),
    }


def _sync_runtime_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    defaults = runtime_defaults_dict()
    for k, v in defaults.items():
        settings.setdefault(k, v)

    # Root fix: env mode/safety always wins at startup.
    settings.update(_env_runtime_overrides())

    # Consistency rules.
    settings["trade_mode"] = _normalize_trade_mode(settings.get("trade_mode"))
    settings["real_trading_enabled"] = bool(settings.get("real_trading_enabled", False))

    if settings["trade_mode"] == "REAL" and not settings["real_trading_enabled"]:
        # Never silently attempt real orders when the safety flag is off.
        settings["trade_mode"] = "PAPER"

    return settings


def real_trade_ready() -> Dict[str, Any]:
    issues = []
    if not DEFAULT_REAL_TRADING_ENABLED:
        issues.append("REAL_TRADING_ENABLED=false")
    if not TOOBIT_API_KEY:
        issues.append("TOOBIT_API_KEY missing")
    if not TOOBIT_API_SECRET:
        issues.append("TOOBIT_API_SECRET missing")
    return {"ok": len(issues) == 0, "issues": issues}


def _empty_state() -> Dict[str, Any]:
    d = runtime_defaults_dict()
    return {
        "version": 1,
        "updated_at": _ts(),
        "settings": d,
        "paper_positions": {},
        "real_positions": {},
        "daily": {
            "day": time.strftime("%Y-%m-%d"),
            "realized_pnl": 0.0,
            "tp": 0,
            "sl": 0,
            "locked_until": 0,
        },
        "totals": {
            "realized_pnl": 0.0,
            "tp": 0,
            "sl": 0,
        },
    }


@safe(default={})
def load_trade_state() -> Dict[str, Any]:
    st = load_dict(TRADE_STATE_FILE)
    if not st:
        st = _empty_state()
        save_json(TRADE_STATE_FILE, st)
    for k, v in _empty_state().items():
        st.setdefault(k, v)
    st["settings"] = _sync_runtime_settings(st.setdefault("settings", {}))
    _roll_day_if_needed(st)
    return st


@safe(default=False)
def save_trade_state(st: Dict[str, Any], make_backup: bool = False) -> bool:
    st["updated_at"] = _ts()
    return save_json(TRADE_STATE_FILE, st, make_backup=make_backup)


def _roll_day_if_needed(st: Dict[str, Any]) -> None:
    today = time.strftime("%Y-%m-%d")
    daily = st.setdefault("daily", {})
    if daily.get("day") != today:
        daily.clear()
        daily.update({"day": today, "realized_pnl": 0.0, "tp": 0, "sl": 0, "locked_until": 0})


@safe(default={})
def trade_status() -> Dict[str, Any]:
    st = load_trade_state()
    settings = st.get("settings", {})
    slots = slot_manager.slot_state()
    return {
        "mode": settings.get("trade_mode", "PAPER"),
        "real_trading_enabled": settings.get("real_trading_enabled", False),
        "real_trade_ready": real_trade_ready(),
        "last_trade_mode_error": settings.get("last_trade_mode_error", ""),
        "emergency_stop": settings.get("emergency_stop", False),
        "balance": settings.get("balance"),
        "protected_balance": settings.get("protected_balance"),
        "initial_capital": settings.get("initial_capital"),
        "position_size_usd": settings.get("position_size_usd"),
        "leverage": settings.get("leverage"),
        "max_positions": slots.get("max_positions"),
        "used_slots": slots.get("used_slots"),
        "free_slots": slots.get("free_slots"),
        "daily": st.get("daily", {}),
        "totals": st.get("totals", {}),
        "locked": is_daily_locked(st),
    }


def is_daily_locked(st: Optional[Dict[str, Any]] = None) -> bool:
    st = st or load_trade_state()
    return int(st.get("daily", {}).get("locked_until", 0)) > _ts()


@safe(default={})
def set_trade_setting(name: str, value: Any) -> Dict[str, Any]:
    st = load_trade_state()
    settings = st.setdefault("settings", {})

    if name == "trade_mode":
        value = _normalize_trade_mode(value)
    elif name in {"leverage", "max_positions"}:
        value = int(value)
    elif name in {"position_size_usd", "initial_capital", "daily_loss_lock_amount", "daily_lock_hours", "balance", "protected_balance"}:
        value = float(value)
    elif name in {"emergency_stop", "daily_loss_lock_enabled", "ai_enabled", "learning_enabled", "real_trading_enabled", "conservative_mode"}:
        value = bool(value)

    settings[name] = value

    # If user/runtime asks REAL, safety must also be on and keys must exist.
    if name == "trade_mode" and value == "REAL":
        ready = real_trade_ready()
        if not ready.get("ok"):
            settings["trade_mode"] = "PAPER"
            settings["last_trade_mode_error"] = ", ".join(ready.get("issues", []))
    if name == "real_trading_enabled" and not value and settings.get("trade_mode") == "REAL":
        settings["trade_mode"] = "PAPER"

    if name == "max_positions":
        slot_manager.set_max_positions(int(value))
    save_trade_state(st, make_backup=True)
    return trade_status()


@safe(default={})
def open_trade(decision: Dict[str, Any], mode: Optional[str] = None) -> Dict[str, Any]:
    st = load_trade_state()
    settings = st.get("settings", {})
    mode = (mode or settings.get("trade_mode", "PAPER")).upper()

    if settings.get("emergency_stop"):
        return {"ok": False, "reason": "emergency_stop"}
    if is_daily_locked(st):
        return {"ok": False, "reason": "daily_loss_locked", "locked_until": st.get("daily", {}).get("locked_until")}

    if mode == "REAL":
        ready = real_trade_ready()
        if not ready.get("ok"):
            return {"ok": False, "reason": "real_trade_not_ready", "issues": ready.get("issues", [])}
        return open_real_trade(decision)
    return open_paper_trade(decision)


@safe(default={})
def open_paper_trade(decision: Dict[str, Any]) -> Dict[str, Any]:
    st = load_trade_state()
    settings = st.get("settings", {})
    symbol = str(decision.get("symbol", "")).upper()
    direction = str(decision.get("direction", "")).upper()
    entry = _safe_float(decision.get("entry"))
    if not symbol or direction not in {"LONG", "SHORT"} or entry <= 0:
        return {"ok": False, "reason": "invalid_decision"}

    slot_id = slot_manager.reserve_slot(symbol, direction, mode="PAPER", status=slot_manager.STATUS_OPEN, ai_record_id=decision.get("record_id", ""), signal_id=decision.get("signal_id", ""), telegram_message_id=decision.get("telegram_message_id"))
    if not slot_id:
        return {"ok": False, "reason": "no_free_slot"}

    sid = signal_tracker.register_active_signal({**decision, "slot_id": slot_id}, mode=signal_tracker.TYPE_PAPER, slot_id=slot_id)
    pos = {
        "id": sid,
        "slot_id": slot_id,
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "tp1": decision.get("tp1"),
        "tp2": decision.get("tp2"),
        "sl": decision.get("sl"),
        "position_size_usd": settings.get("position_size_usd"),
        "leverage": settings.get("leverage"),
        "opened_at": _ts(),
        "status": "OPEN",
    }
    st.setdefault("paper_positions", {})[sid] = pos
    save_trade_state(st)
    return {"ok": True, "mode": "PAPER", "signal_id": sid, "slot_id": slot_id, "position": pos}


@safe(default={})
def open_real_trade(decision: Dict[str, Any]) -> Dict[str, Any]:
    st = load_trade_state()
    settings = st.get("settings", {})
    symbol = str(decision.get("symbol", "")).upper()
    direction = str(decision.get("direction", "")).upper()
    entry = _safe_float(decision.get("entry"))
    if not symbol or direction not in {"LONG", "SHORT"} or entry <= 0:
        return {"ok": False, "reason": "invalid_decision"}

    ready = real_trade_ready()
    if not ready.get("ok"):
        return {"ok": False, "reason": "real_trade_not_ready", "issues": ready.get("issues", [])}

    sides = toobit_safety.side_from_direction(direction)
    desired_qty = (_safe_float(settings.get("position_size_usd")) * int(settings.get("leverage", 1))) / entry

    c = tobit_client.client()
    pre = toobit_safety.preflight_real_order(symbol, sides["open_side"], desired_qty, entry, c)
    if not pre.get("ok"):
        return {"ok": False, "reason": "preflight_failed", "preflight": pre}

    slot_id = slot_manager.reserve_slot(
        symbol, direction, mode="REAL", status=slot_manager.STATUS_PENDING_REAL_CONFIRM,
        ai_record_id=decision.get("record_id", ""), signal_id=decision.get("signal_id", ""),
        telegram_message_id=decision.get("telegram_message_id"),
        metadata={"pending_confirm_timeout": REAL_CONFIRM_TIMEOUT_SECONDS},
    )
    if not slot_id:
        return {"ok": False, "reason": "no_free_slot"}

    order = c.create_order(symbol, sides["open_side"], pre["quantity"], order_type="MARKET")
    if not order.get("ok"):
        # Keep slot briefly? If order was not accepted at all, release now.
        slot_manager.release_slot(slot_id, reason="real_order_failed")
        return {"ok": False, "reason": "order_failed", "order": order}

    sid = signal_tracker.register_active_signal({**decision, "slot_id": slot_id}, mode=signal_tracker.TYPE_REAL, slot_id=slot_id)
    st.setdefault("real_positions", {})[sid] = {
        "id": sid,
        "slot_id": slot_id,
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "quantity": pre["quantity"],
        "order": order,
        "opened_at": _ts(),
        "status": "PENDING_CONFIRM",
    }
    save_trade_state(st, make_backup=True)
    return {"ok": True, "mode": "REAL", "signal_id": sid, "slot_id": slot_id, "order": order, "quantity": pre["quantity"]}


@safe(default=False)
def close_paper_trade(signal_id: str, result: str, exit_price: float, pnl: Optional[float] = None) -> bool:
    st = load_trade_state()
    pos = st.get("paper_positions", {}).pop(signal_id, None)
    if not pos:
        return False
    if pnl is None:
        pnl = calculate_paper_pnl(pos, exit_price)
    _apply_realized_pnl(st, float(pnl), result)
    signal_tracker.close_signal(signal_id, result=result, exit_price=exit_price, pnl=float(pnl))
    save_trade_state(st, make_backup=True)
    return True


def calculate_paper_pnl(pos: Dict[str, Any], exit_price: float) -> float:
    entry = _safe_float(pos.get("entry"))
    if entry <= 0:
        return 0.0
    size = _safe_float(pos.get("position_size_usd"))
    lev = _safe_float(pos.get("leverage"), 1)
    notional = size * lev
    direction = str(pos.get("direction")).upper()
    pct = (exit_price - entry) / entry
    if direction == "SHORT":
        pct *= -1
    return round(notional * pct, 4)


def _apply_realized_pnl(st: Dict[str, Any], pnl: float, result: str) -> None:
    settings = st.setdefault("settings", {})
    daily = st.setdefault("daily", {})
    totals = st.setdefault("totals", {})

    settings["balance"] = round(_safe_float(settings.get("balance")) + pnl, 4)
    daily["realized_pnl"] = round(_safe_float(daily.get("realized_pnl")) + pnl, 4)
    totals["realized_pnl"] = round(_safe_float(totals.get("realized_pnl")) + pnl, 4)

    if pnl > 0:
        # User rule: realized profit increases protected balance; near $1 can count as $1.
        protected_add = round(pnl)
        if protected_add <= 0 and pnl >= 0.99:
            protected_add = 1
        settings["protected_balance"] = round(_safe_float(settings.get("protected_balance")) + max(0, protected_add), 4)

    if str(result).upper().startswith("TP"):
        daily["tp"] = int(daily.get("tp", 0)) + 1
        totals["tp"] = int(totals.get("tp", 0)) + 1
    elif str(result).upper() == "SL":
        daily["sl"] = int(daily.get("sl", 0)) + 1
        totals["sl"] = int(totals.get("sl", 0)) + 1

    _update_daily_lock(st)


def _update_daily_lock(st: Dict[str, Any]) -> None:
    settings = st.get("settings", {})
    if not settings.get("daily_loss_lock_enabled", True):
        return
    protected = _safe_float(settings.get("protected_balance"))
    balance = _safe_float(settings.get("balance"))
    threshold = _safe_float(settings.get("daily_loss_lock_amount"), 5)
    if protected - balance >= threshold:
        hours = _safe_float(settings.get("daily_lock_hours"), 1)
        st.setdefault("daily", {})["locked_until"] = int(_ts() + hours * 3600)


@safe(default="")
def status_fa() -> str:
    s = trade_status()
    locked = "فعال" if s.get("locked") else "غیرفعال"
    em = "روشن" if s.get("emergency_stop") else "خاموش"
    return (
        "💼 وضعیت ترید\n"
        f"حالت: {s.get('mode')} | ترید واقعی: {'روشن' if s.get('real_trading_enabled') else 'خاموش'}\n"
        f"بالانس: {s.get('balance')}$ | محافظت‌شده: {s.get('protected_balance')}$\n"
        f"حجم هر پوزیشن: {s.get('position_size_usd')}$ | لوریج: {s.get('leverage')}x\n"
        f"پوزیشن‌ها: {s.get('used_slots')}/{s.get('max_positions')} | خالی: {s.get('free_slots')}\n"
        f"سود/ضرر امروز: {s.get('daily',{}).get('realized_pnl',0)}$\n"
        f"قفل ضرر روزانه: {locked} | توقف اضطراری: {em}"
        + (f"\n⚠️ آماده نبودن ترید واقعی: {', '.join(s.get('real_trade_ready', {}).get('issues', []))}" if not s.get('real_trade_ready', {}).get('ok', True) else "")
    )


@safe(default=True)
def initialize() -> bool:
    st = load_trade_state()
    save_trade_state(st)
    return True
