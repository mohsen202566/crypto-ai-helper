# -*- coding: utf-8 -*-
"""
real_position_sync.py

Dedicated Toobit real-position synchronization layer for the crypto futures bot.

Purpose
-------
This module keeps the bot's internal real-trade slots aligned with actual
Toobit Futures positions. It is intentionally separated from the order-opening
logic so the bot can safely monitor real capital after an order has been sent.

Architecture
------------
Telegram signal/order flow:
    bot.py -> real_trade_manager.py -> tobit_client.py -> Toobit

Real position sync flow:
    real_position_sync.py
        - reads real open positions from Toobit
        - confirms pending positions
        - repairs missing TP/SL when possible
        - detects closed/missing positions
        - calculates real PnL from Toobit wallet balance via real_trade_manager
        - returns Telegram reply events for signal_tracker/bot

Important safety rule
---------------------
This module never opens a new entry order and never changes signal logic.
It only syncs, verifies, repairs TP/SL, and reports real position events.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_FAST_SYNC_SECONDS = 2
DEFAULT_FAST_SYNC_WINDOW_SECONDS = 30
DEFAULT_SLOW_SYNC_SECONDS = 10
DEFAULT_PENDING_TIMEOUT_SECONDS = 75


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _now() -> int:
    return int(time.time())


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or str(value).strip() == "":
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _round_usd(value: Any, digits: int = 6) -> float:
    try:
        return round(float(value), digits)
    except Exception:
        return 0.0


def _norm_symbol(symbol: Any) -> str:
    raw = str(symbol or "").upper().strip()
    if not raw:
        return ""
    raw = raw.replace("/", "").replace("_", "-")
    if "-SWAP-USDT" in raw:
        return raw.replace("-SWAP-USDT", "USDT")
    if "-SWAP-USDC" in raw:
        return raw.replace("-SWAP-USDC", "USDC")
    return raw.replace("-", "")


def _norm_direction(direction: Any) -> str:
    text = str(direction or "").upper().strip()
    if text in {"LONG", "BUY", "BUY_OPEN", "OPEN_LONG", "POSITION_LONG"}:
        return "LONG"
    if text in {"SHORT", "SELL", "SELL_OPEN", "OPEN_SHORT", "POSITION_SHORT"}:
        return "SHORT"
    # Toobit/ccxt-style fallbacks sometimes expose side/positionSide as "BOTH",
    # "NET", or empty. Keep unknown values unchanged rather than guessing.
    return text


def _extract_exchange_direction(ex: Dict[str, Any], fallback: Any = None) -> str:
    """Read direction from the common normalized/Toobit/ccxt position fields."""
    if not isinstance(ex, dict):
        return _norm_direction(fallback)
    for key in ("direction", "position_side", "positionSide", "holdSide", "side", "posSide"):
        val = ex.get(key)
        norm = _norm_direction(val)
        if norm in {"LONG", "SHORT"}:
            return norm
    return _norm_direction(fallback)


def _position_key(symbol: Any, direction: Any) -> str:
    return f"{_norm_symbol(symbol)}:{_norm_direction(direction)}"


def _positions_match(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return _position_key(a.get("symbol"), a.get("direction")) == _position_key(b.get("symbol"), _extract_exchange_direction(b, fallback=b.get("direction")))


# ---------------------------------------------------------------------------
# Lazy imports so this file can compile even while other files are being edited
# ---------------------------------------------------------------------------

def _rtm():
    import real_trade_manager as rtm  # type: ignore
    return rtm


def _client():
    from tobit_client import toobit_client  # type: ignore
    return toobit_client


# ---------------------------------------------------------------------------
# Exchange/state fetchers
# ---------------------------------------------------------------------------

def load_state() -> Dict[str, Any]:
    return _rtm().load_real_trade_state()


def save_state(state: Dict[str, Any]) -> None:
    _rtm().save_real_trade_state(state)


def get_exchange_positions(symbol: Optional[str] = None) -> Dict[str, Any]:
    """Return normalized open Toobit positions using the strongest available helper."""
    try:
        client = _client()
        if hasattr(client, "get_open_positions_full"):
            res = client.get_open_positions_full(symbol=symbol)
            if isinstance(res, dict) and res.get("ok"):
                return {"ok": True, "positions": res.get("positions") or [], "raw": res}
        if hasattr(client, "get_open_positions_normalized"):
            res = client.get_open_positions_normalized(symbol=symbol)
            if isinstance(res, dict):
                return res
        return _rtm().get_toobit_open_positions_normalized(symbol=symbol)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300], "positions": []}


def get_exchange_balance() -> Dict[str, Any]:
    """Read current Toobit Futures wallet balance through real_trade_manager."""
    try:
        rtm = _rtm()
        if hasattr(rtm, "get_exchange_balance_info"):
            return rtm.get_exchange_balance_info()
        # Fallback to private helper if available.
        if hasattr(rtm, "_extract_toobit_usdt_balance"):
            return rtm._extract_toobit_usdt_balance(_client().get_account_balance())
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    return {"ok": False, "error": "balance helper unavailable"}


# ---------------------------------------------------------------------------
# Verification and TP/SL repair
# ---------------------------------------------------------------------------

def verify_position_integrity(position: Dict[str, Any], exchange_position: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Verify an internal position against Toobit data.

    Checks:
    - symbol/direction match
    - configured leverage vs exchange leverage when exchange reports it
    - configured margin vs exchange margin when exchange reports it
    - TP/SL existence using tobit_client when supported
    """
    ex = exchange_position or {}
    client = _client()

    expected_symbol = _norm_symbol(position.get("symbol"))
    expected_direction = _norm_direction(position.get("direction"))
    actual_symbol = _norm_symbol(ex.get("symbol") or position.get("symbol"))
    actual_direction = _extract_exchange_direction(ex, fallback=position.get("direction"))

    expected_leverage = _safe_float(position.get("leverage") or position.get("configured_leverage"), 0)
    actual_leverage = _safe_float(ex.get("leverage") or position.get("exchange_leverage"), 0)

    expected_margin = _safe_float(position.get("position_size_usd") or position.get("margin_usd"), 0)
    actual_margin = _safe_float(ex.get("margin") or position.get("exchange_margin"), 0)

    problems: List[str] = []
    if expected_symbol and actual_symbol and expected_symbol != actual_symbol:
        problems.append(f"SYMBOL_MISMATCH expected={expected_symbol} actual={actual_symbol}")
    if expected_direction and actual_direction and expected_direction != actual_direction:
        problems.append(f"DIRECTION_MISMATCH expected={expected_direction} actual={actual_direction}")
    if expected_leverage > 0 and actual_leverage > 0 and abs(expected_leverage - actual_leverage) > 0.01:
        problems.append(f"LEVERAGE_MISMATCH expected={expected_leverage} actual={actual_leverage}")
    # Margin from exchanges can differ slightly because of fees/rounding/contract rules.
    if expected_margin > 0 and actual_margin > 0:
        tolerance = max(0.25, expected_margin * 0.20)
        if abs(expected_margin - actual_margin) > tolerance:
            problems.append(f"MARGIN_MISMATCH expected≈{expected_margin} actual={actual_margin}")

    tpsl = {"ok": False, "verified": False, "error": "verify_position_has_tpsl unavailable"}
    try:
        if hasattr(client, "verify_position_has_tpsl"):
            tpsl = client.verify_position_has_tpsl(expected_symbol, expected_direction)
        elif hasattr(client, "verify_tpsl"):
            tpsl = client.verify_tpsl(expected_symbol, expected_direction)
    except Exception as exc:
        tpsl = {"ok": False, "verified": False, "error": str(exc)[:250]}

    if isinstance(tpsl, dict):
        if tpsl.get("ok") and not tpsl.get("verified"):
            problems.append("TPSL_NOT_VERIFIED")
        elif not tpsl.get("ok"):
            problems.append("TPSL_VERIFY_FAILED")

    return {
        "ok": len(problems) == 0,
        "problems": problems,
        "symbol": expected_symbol,
        "direction": expected_direction,
        "expected_leverage": expected_leverage,
        "actual_leverage": actual_leverage,
        "expected_margin": expected_margin,
        "actual_margin": actual_margin,
        "tpsl": tpsl,
    }


def repair_position_tpsl(position: Dict[str, Any], exchange_position: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Repair TP/SL for an existing position. Never opens a new entry."""
    symbol = _norm_symbol(position.get("symbol"))
    direction = _norm_direction(position.get("direction"))
    tp = position.get("tp1") or position.get("take_profit")
    sl = position.get("sl") or position.get("stop_loss")
    quantity = _safe_float((exchange_position or {}).get("quantity") or position.get("quantity"), 0)

    if not symbol or direction not in {"LONG", "SHORT"}:
        return {"ok": False, "error": "symbol/direction missing"}
    if not tp or not sl:
        return {"ok": False, "error": "TP/SL missing on internal position"}

    client = _client()
    try:
        if hasattr(client, "repair_tpsl"):
            return client.repair_tpsl(symbol, direction, take_profit=tp, stop_loss=sl, quantity=quantity)
        if hasattr(client, "place_position_tpsl"):
            return client.place_position_tpsl(symbol, direction, take_profit=tp, stop_loss=sl, quantity=quantity)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}
    return {"ok": False, "error": "TP/SL repair helper unavailable"}


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def choose_next_sync_interval(position: Optional[Dict[str, Any]] = None) -> int:
    """2s during fresh/pending window, slower after position is stable."""
    if not position:
        return DEFAULT_SLOW_SYNC_SECONDS
    opened_at = _safe_int(position.get("opened_at"), _now())
    status = str(position.get("real_status") or position.get("status") or "").upper()
    age = _now() - opened_at
    if status in {"PENDING_REAL_CONFIRM", "PENDING", "ACCEPTED", "NEW", ""}:
        return DEFAULT_FAST_SYNC_SECONDS
    if age <= DEFAULT_FAST_SYNC_WINDOW_SECONDS:
        return DEFAULT_FAST_SYNC_SECONDS
    return DEFAULT_SLOW_SYNC_SECONDS


def sync_once(*, repair_tpsl: bool = True, save: bool = True) -> Dict[str, Any]:
    """
    Run one real-position sync cycle.

    This primarily delegates state reconciliation and balance-based PnL accounting
    to real_trade_manager.sync_real_positions_with_toobit, then adds a clean event
    and integrity layer for tracker/bot usage.
    """
    rtm = _rtm()
    state = rtm.load_real_trade_state()

    # Primary source of truth and accounting.
    sync = rtm.sync_real_positions_with_toobit(state, save=save)
    if not isinstance(sync, dict) or not sync.get("ok"):
        return {
            "ok": False,
            "error": str((sync or {}).get("error") or "sync failed")[:300],
            "events": [],
            "messages": [],
            "state": state,
        }

    state = sync.get("state") or state
    exchange_positions = sync.get("exchange_positions") or []
    events = list(sync.get("events") or [])
    integrity_reports: List[Dict[str, Any]] = []

    # Verify/repair every active internal position.
    open_positions = state.get("open_positions", {}) if isinstance(state.get("open_positions"), dict) else {}
    changed = False
    for sid, pos in list(open_positions.items()):
        if not isinstance(pos, dict):
            continue
        ex_match = None
        for ex in exchange_positions:
            if isinstance(ex, dict) and _positions_match(pos, ex):
                ex_match = ex
                break
        if not ex_match:
            continue

        report = verify_position_integrity(pos, ex_match)
        report["signal_id"] = sid
        integrity_reports.append(report)
        pos["last_integrity_check_at"] = _now()
        pos["last_integrity_report"] = report
        changed = True

        tpsl = report.get("tpsl") if isinstance(report, dict) else None
        tpsl_needs_repair = isinstance(tpsl, dict) and (not tpsl.get("ok") or not tpsl.get("verified"))
        if repair_tpsl and tpsl_needs_repair:
            repair = repair_position_tpsl(pos, ex_match)
            pos["last_real_position_sync_tpsl_repair"] = repair
            pos["last_real_position_sync_tpsl_repair_at"] = _now()
            changed = True
            events.append({
                "type": "TPSL_REPAIR_ATTEMPTED",
                "signal_id": sid,
                "position": pos,
                "repair": repair,
            })

    if changed and save:
        rtm.save_real_trade_state(state)

    messages = build_tracker_messages(events)

    return {
        "ok": True,
        "added": sync.get("added", 0),
        "removed": sync.get("removed", 0),
        "updated": sync.get("updated", 0),
        "state": state,
        "exchange_positions": exchange_positions,
        "balance": sync.get("balance"),
        "events": events,
        "messages": messages,
        "integrity_reports": integrity_reports,
    }


# ---------------------------------------------------------------------------
# Telegram tracker messages
# ---------------------------------------------------------------------------

def _fa_direction(direction: Any) -> str:
    d = _norm_direction(direction)
    if d == "LONG":
        return "لانگ"
    if d == "SHORT":
        return "شورت"
    return str(direction or "")


def _closed_result_text(pnl: float) -> Tuple[str, str]:
    if pnl > 0:
        return "✅", "سود / احتمالاً TP"
    if pnl < 0:
        return "❌", "ضرر / احتمالاً SL"
    return "ℹ️", "بسته شد"


def build_tracker_messages(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert sync events into bot/signal_tracker sendable message dicts."""
    messages: List[Dict[str, Any]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        etype = str(event.get("type") or "")
        pos = event.get("position") or {}
        if not isinstance(pos, dict):
            pos = {}

        if etype == "POSITION_CLOSED_OR_MISSING":
            acc = event.get("accounting") or pos.get("accounting") or {}
            pnl = _safe_float(acc.get("pnl_usd"), 0)
            icon, result_fa = _closed_result_text(pnl)
            sign = "+" if pnl > 0 else ""
            text = (
                f"{icon} نتیجه پوزیشن واقعی {pos.get('symbol')}\n"
                f"جهت: {_fa_direction(pos.get('direction'))}\n"
                f"نتیجه: {result_fa}\n"
                f"سود/ضرر واقعی از بالانس توبیت: {sign}{round(pnl, 6)}$\n"
                f"بالانس بعد: {acc.get('balance')}$"
            )
            if acc.get("daily_locked"):
                text += "\n🚨 قفل ضرر روزانه فعال شد."
            messages.append({
                "chat_id": pos.get("chat_id"),
                "message": text,
                "reply_to_message_id": pos.get("message_id") or pos.get("reply_to_message_id"),
                "signal_id": pos.get("signal_id") or event.get("signal_id"),
                "event_type": etype,
            })

        elif etype == "POSITION_CONFIRMED":
            # Keep this event quiet by default. It is useful for logs/state, not Telegram spam.
            continue

        elif etype == "POSITION_RECOVERED":
            text = (
                f"⚠️ پوزیشن واقعی از توبیت Sync شد\n"
                f"ارز: {pos.get('symbol')}\n"
                f"جهت: {_fa_direction(pos.get('direction'))}\n"
                "این پوزیشن در صرافی باز بود ولی داخل اسلات ربات نبود."
            )
            messages.append({
                "chat_id": pos.get("chat_id"),
                "message": text,
                "reply_to_message_id": pos.get("message_id") or pos.get("reply_to_message_id"),
                "signal_id": pos.get("signal_id") or event.get("signal_id"),
                "event_type": etype,
            })

        elif etype == "TPSL_REPAIR_ATTEMPTED":
            repair = event.get("repair") or {}
            # Send only failures. Successful repairs are kept in state/logs to avoid noise.
            if isinstance(repair, dict) and repair.get("ok"):
                continue
            text = (
                f"⚠️ هشدار TP/SL برای {pos.get('symbol')}\n"
                f"جهت: {_fa_direction(pos.get('direction'))}\n"
                "ربات تلاش کرد TP/SL را بررسی/ترمیم کند، اما تأیید کامل نگرفت.\n"
                f"جزئیات: {str((repair or {}).get('error') or repair)[:180]}"
            )
            messages.append({
                "chat_id": pos.get("chat_id"),
                "message": text,
                "reply_to_message_id": pos.get("message_id") or pos.get("reply_to_message_id"),
                "signal_id": pos.get("signal_id") or event.get("signal_id"),
                "event_type": etype,
            })

    return messages


def check_real_position_events_for_tracker() -> List[Dict[str, Any]]:
    """Small helper for signal_tracker.py / bot.py loops."""
    result = sync_once(repair_tpsl=True, save=True)
    if not result.get("ok"):
        return []
    return result.get("messages") or []


# Backward-compatible alias names for easy integration.
check_events_for_tracker = check_real_position_events_for_tracker
sync_real_positions_once = sync_once


# ---------------------------------------------------------------------------
# Human-readable status
# ---------------------------------------------------------------------------

def format_sync_status() -> str:
    result = sync_once(repair_tpsl=True, save=True)
    if not result.get("ok"):
        return f"❌ خطا در Sync پوزیشن‌های واقعی:\n{result.get('error')}"

    state = result.get("state") or {}
    open_positions = state.get("open_positions", {}) if isinstance(state.get("open_positions"), dict) else {}
    lines = [
        "🔁 Sync پوزیشن‌های واقعی Toobit",
        f"پوزیشن واقعی در توبیت: {len(result.get('exchange_positions') or [])}",
        f"اسلات داخلی باز: {len(open_positions)}",
        f"اضافه‌شده: {result.get('added', 0)}",
        f"حذف/بسته‌شده: {result.get('removed', 0)}",
        f"آپدیت‌شده: {result.get('updated', 0)}",
    ]

    reports = result.get("integrity_reports") or []
    if reports:
        lines.append("\nچک امنیت پوزیشن‌ها:")
        for rep in reports[:10]:
            ok = "✅" if rep.get("ok") else "⚠️"
            problems = ", ".join(rep.get("problems") or []) or "OK"
            lines.append(f"{ok} {rep.get('symbol')} {rep.get('direction')} | {problems}")

    events = result.get("events") or []
    if events:
        lines.append("\nرویدادها:")
        for event in events[:10]:
            p = event.get("position") or {}
            lines.append(f"• {event.get('type')} | {p.get('symbol')} {p.get('direction')}")

    return "\n".join(lines)


if __name__ == "__main__":
    print(format_sync_status())
