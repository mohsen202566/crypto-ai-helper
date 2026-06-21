from __future__ import annotations

"""
Scanner / Candidate Orchestrator.

Responsibilities:
- Convert market snapshots to AI candidates.
- Ask ai_movement_hunter for decisions.
- Route SETUP to signal_tracker.
- Route REAL/PAPER to real_trade_manager.
- Route GHOST to ghost_signals/signal_tracker.
- Connect real confirmation after REAL order through real_position_sync.
- Provide best-signal and auto-scan functions.

This module does not contain indicator logic itself.
"""

import time
from typing import Any, Dict, List, Optional

from config import DEFAULT_SYMBOLS, AUTO_SCAN_MAX_SYMBOLS_PER_CYCLE
from diagnostics import safe, record_error, warning
import market_scanner
import ai_movement_hunter
import signal_tracker
import ghost_signals
import slot_manager
import real_trade_manager
import real_position_sync
import reply_manager


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
def build_candidate_from_snapshot(symbol_snapshot: Dict[str, Any], market_context: Dict[str, Any], slot_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    symbol = str(symbol_snapshot.get("symbol", "")).upper()
    ctx = dict(market_context or {})
    # Add per-symbol leader context.
    ctx["symbol"] = symbol
    return {
        "symbol": symbol,
        "price": symbol_snapshot.get("price"),
        "features": symbol_snapshot.get("features", {}),
        "structure": symbol_snapshot.get("structure", {}),
        "market_context": ctx,
        "slot_state": slot_state or slot_manager.slot_state(),
        "created_at": _ts(),
    }


@safe(default=[])
def evaluate_market_scan(scan: Dict[str, Any], record: bool = False) -> List[Dict[str, Any]]:
    decisions: List[Dict[str, Any]] = []
    snapshots = scan.get("snapshots", {})
    market_context = scan.get("market_context", {})
    state = slot_manager.slot_state()

    for symbol, snap in snapshots.items():
        cand = build_candidate_from_snapshot(snap, market_context, state)
        d = ai_movement_hunter.decide(cand, record=record)
        decisions.append(d)

    return ai_movement_hunter.rank_decisions(decisions, open_positions=state.get("open_positions", []))


@safe(default={})
def best_signal(symbols: Optional[List[str]] = None, mode: Optional[str] = None) -> Dict[str, Any]:
    scan = market_scanner.scan_market(symbols=symbols or DEFAULT_SYMBOLS[:AUTO_SCAN_MAX_SYMBOLS_PER_CYCLE], use_cache=False)
    decisions = evaluate_market_scan(scan, record=False)
    if not decisions:
        return {"ok": False, "reason": "no_decisions"}
    best = decisions[0]
    return {"ok": True, "decision": best, "message": ai_movement_hunter.format_decision_fa(best)}


@safe(default=[])
def auto_scan_and_route(symbols: Optional[List[str]] = None, trade_mode: Optional[str] = None, record: bool = True) -> List[Dict[str, Any]]:
    """
    Main auto-scan action.
    - scans market
    - AI decides
    - routes only the best candidates that match free slots
    - stores ghosts when no slot or ghost decision
    """
    scan = market_scanner.scan_market(symbols=symbols or DEFAULT_SYMBOLS[:AUTO_SCAN_MAX_SYMBOLS_PER_CYCLE], use_cache=False)
    decisions = evaluate_market_scan(scan, record=False)
    routed: List[Dict[str, Any]] = []

    state = slot_manager.slot_state()
    free = int(state.get("free_slots", 0))

    # First route high priority REAL/ENTRY candidates limited by free slots.
    real_candidates = [d for d in decisions if d.get("decision") in {"REAL", "ENTRY_ACTIVATION"}]
    real_candidates = ai_movement_hunter.rank_decisions(real_candidates, open_positions=state.get("open_positions", []), limit=free)

    for d in real_candidates:
        res = route_decision(d, trade_mode=trade_mode, record=record)
        routed.append(res)

    # SETUPs are stored/watchlisted even if not active.
    setup_candidates = [d for d in decisions if d.get("decision") == "SETUP"]
    for d in setup_candidates[:20]:
        res = route_decision(d, trade_mode=trade_mode, record=record)
        routed.append(res)

    # Ghosts for learning when slots are full or AI selected GHOST.
    ghost_candidates = [d for d in decisions if d.get("decision") == "GHOST"]
    for d in ghost_candidates[:20]:
        res = route_decision(d, trade_mode=trade_mode, record=record)
        routed.append(res)

    real_position_sync.cleanup_pending_slots()
    return routed


@safe(default={})
def route_decision(decision: Dict[str, Any], trade_mode: Optional[str] = None, record: bool = True) -> Dict[str, Any]:
    d = str(decision.get("decision", "")).upper()
    if d == "REJECT" or d == "WAIT":
        return {"ok": True, "routed": False, "decision": d, "reason": decision.get("reason")}

    if d == "SETUP":
        sid = signal_tracker.create_setup(decision)
        return {"ok": bool(sid), "routed": bool(sid), "type": "SETUP", "signal_id": sid, "decision": decision}

    if d == "GHOST":
        gid = ghost_signals.create_ghost(decision, reason=decision.get("reason", "ai_ghost"))
        # Also register in tracker for unified lifecycle if ghost was created.
        sid = ""
        if gid:
            sid = signal_tracker.register_active_signal({**decision, "ghost_id": gid, "record_id": gid}, mode=signal_tracker.TYPE_GHOST)
        return {"ok": bool(gid), "routed": bool(gid), "type": "GHOST", "ghost_id": gid, "signal_id": sid, "decision": decision}

    if d in {"REAL", "ENTRY_ACTIVATION"}:
        mode = (trade_mode or real_trade_manager.trade_status().get("mode") or "PAPER").upper()
        res = real_trade_manager.open_trade(decision, mode=mode)
        # Do not block scanner for 60-70s. Bot/background sync confirms pending real positions.
        if res.get("ok") and mode == "REAL":
            res["needs_real_confirmation"] = True
        return {"ok": res.get("ok", False), "routed": res.get("ok", False), "type": mode, "trade": res, "decision": decision}

    return {"ok": False, "routed": False, "reason": "unknown_decision", "decision": decision}



@safe(default=[])
def process_watching_setups(limit: int = 20, trade_mode: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Re-check SETUP/WATCHING signals and activate them when AI confirms entry.
    This completes the SETUP -> ENTRY_ACTIVATION flow.
    """
    setups = [s for s in signal_tracker.active_signals() if s.get("status") == signal_tracker.STATUS_WATCHING][:limit]
    routed: List[Dict[str, Any]] = []
    if not setups:
        return routed

    state = slot_manager.slot_state()
    for s in setups:
        symbol = s.get("symbol")
        if not symbol:
            continue
        snap = market_scanner.build_symbol_snapshot(symbol, timeframes=["5m", "15m", "30m", "1h", "4h"], limit=140)
        if not snap.get("ok"):
            continue
        market_status = market_scanner.scan_market(symbols=[symbol, "BTCUSDT"], use_cache=True).get("market_context", {})
        cand = build_candidate_from_snapshot(snap, market_status, state)
        cand["existing_setup_id"] = s.get("signal_id")
        d = ai_movement_hunter.decide(cand, record=False)
        if d.get("decision") in {"ENTRY_ACTIVATION", "REAL", "GHOST"}:
            # activate existing setup rather than creating a duplicate setup
            mode = (trade_mode or real_trade_manager.trade_status().get("mode") or "PAPER").upper()
            if d.get("decision") == "GHOST":
                signal_tracker.activate_signal(s.get("signal_id"), d, mode=signal_tracker.TYPE_GHOST, reserve_slot=False)
                routed.append({"ok": True, "routed": True, "type": "GHOST_ACTIVATED", "signal_id": s.get("signal_id"), "decision": d})
            else:
                # Let trade manager open/register a real active trade from this decision.
                res = real_trade_manager.open_trade({**d, "signal_id": s.get("signal_id"), "record_id": s.get("ai_record_id")}, mode=mode)
                routed.append({"ok": res.get("ok", False), "routed": res.get("ok", False), "type": mode, "trade": res, "signal_id": s.get("signal_id"), "decision": d})
    return routed


@safe(default=0)
def update_active_from_market(symbols: Optional[List[str]] = None) -> int:
    """
    Updates active tracker and ghost signals using latest 5m candle high/low/close.
    """
    active = signal_tracker.active_signals()
    if not active:
        return 0
    needed = sorted({a.get("symbol") for a in active if a.get("symbol")})
    if symbols:
        needed = [s for s in needed if s in set(x.upper() for x in symbols)]

    price_map: Dict[str, Dict[str, Any]] = {}
    for symbol in needed:
        snap = market_scanner.build_symbol_snapshot(symbol, timeframes=["5m"], limit=120)
        if not snap.get("ok"):
            continue
        tf = snap.get("features", {}).get("timeframes", {}).get("5m", {})
        candles = market_scanner.fetch_symbol_candles(symbol, ["5m"], 3).get("candles", {}).get("5m", [])
        if candles:
            last = candles[-1]
            price_map[symbol] = {
                "price": last.get("close"),
                "high": last.get("high"),
                "low": last.get("low"),
                "snapshot": snap,
            }
    return signal_tracker.update_many(price_map)


def _short_error_reason(err: Any) -> str:
    if isinstance(err, dict):
        return str(err.get("reason") or err.get("error") or err.get("errors") or "unknown")
    return str(err or "unknown")


def _format_decision_rows(decisions: List[Dict[str, Any]], limit: int = 8) -> str:
    if not decisions:
        return ""
    rows = []
    for d in decisions[:limit]:
        symbol = str(d.get("symbol", ""))
        direction = str(d.get("direction", ""))
        decision = str(d.get("decision", ""))
        conf = d.get("confidence", d.get("score", ""))
        try:
            conf_txt = f"{float(conf) * 100:.0f}%" if float(conf) <= 1 else f"{float(conf):.0f}"
        except Exception:
            conf_txt = str(conf) if conf != "" else "-"
        rows.append(f"• {symbol} {direction} → {decision} | {conf_txt}")
    return "\n".join(rows)


def _format_error_rows(errors: Dict[str, Any], limit: int = 6) -> str:
    if not errors:
        return ""
    rows = []
    for symbol, err in list(errors.items())[:limit]:
        rows.append(f"• {symbol}: {_short_error_reason(err)}")
    return "\n".join(rows)


@safe(default="")
def scan_report_fa(symbols: Optional[List[str]] = None) -> str:
    requested_symbols = symbols or DEFAULT_SYMBOLS[:AUTO_SCAN_MAX_SYMBOLS_PER_CYCLE]
    scan = market_scanner.scan_market(symbols=requested_symbols, use_cache=False)
    decisions = evaluate_market_scan(scan, record=False) if scan.get("snapshots") else []

    requested = int(scan.get("symbols_requested", len(requested_symbols)) or 0)
    ok_count = int(scan.get("symbols_ok", len(scan.get("snapshots", {}))) or 0)
    errors = scan.get("errors", {}) or {}

    real = sum(1 for d in decisions if d.get("decision") in {"REAL", "ENTRY_ACTIVATION"})
    setup = sum(1 for d in decisions if d.get("decision") == "SETUP")
    ghost = sum(1 for d in decisions if d.get("decision") == "GHOST")
    wait = sum(1 for d in decisions if d.get("decision") == "WAIT")
    reject = sum(1 for d in decisions if d.get("decision") == "REJECT")
    no_trade = max(0, requested - ok_count)

    market_status = market_scanner.market_status_fa()
    top_rows = _format_decision_rows(decisions)
    error_rows = _format_error_rows(errors)

    # If the live/manual scan has no candle data, do not show a misleading empty report.
    # Show the real data-health problem and, when possible, also show the latest cached market status.
    if ok_count == 0:
        cached = market_scanner.get_cached_market_status()
        cached_txt = ""
        if cached and cached.get("symbols_ok"):
            cached_txt = (
                "\n\nآخرین وضعیت ذخیره‌شده:\n"
                f"بررسی‌شده قبلی: {cached.get('symbols_ok')}/{cached.get('symbols_requested')}\n"
                f"Market: {cached.get('market_mode')} | BTC: {cached.get('btc_bias')}"
            )
        return (
            "🔎 گزارش اسکن بازار\n"
            f"بررسی‌شده: 0/{requested}\n"
            f"خطا/دیتای ناقص: {len(errors)}\n"
            "نتیجه: در اسکن لحظه‌ای کندل معتبر دریافت نشد.\n"
            + (f"\nنمونه خطاها:\n{error_rows}" if error_rows else "")
            + cached_txt
        )

    return (
        "🔎 گزارش اسکن بازار\n"
        f"بررسی‌شده: {ok_count}/{requested} | خطا: {len(errors)}\n"
        f"Real/Entry: {real} | Setup: {setup} | Ghost: {ghost}\n"
        f"Wait: {wait} | Reject: {reject} | NoData/NoTrade: {no_trade}\n"
        + (f"\nکاندیدهای AI:\n{top_rows}\n" if top_rows else "\nکاندید AI پیدا نشد.\n")
        + f"\n{market_status}"
    )
