from __future__ import annotations

"""
Integration Status.

Single place to build full AI/trade/system status.
"""

from typing import Any, Dict

from diagnostics import safe, health_report
import ai_memory
import coin_learning
import coin_risk
import coin_rotation
import ghost_signals
import slot_manager
import signal_tracker
import real_trade_manager
import market_scanner
import daily_report


@safe(default={})
def full_status() -> Dict[str, Any]:
    return {
        "trade": real_trade_manager.trade_status(),
        "ai_memory": ai_memory.summary(use_cache=True),
        "coin_learning": coin_learning.summary(),
        "coin_risk": coin_risk.summary(),
        "coin_rotation": coin_rotation.summary(),
        "ghost": ghost_signals.summary(),
        "slots": slot_manager.slot_state(),
        "tracker": signal_tracker.summary(),
        "market": market_scanner.get_cached_market_status(),
        "daily_report_enabled": daily_report.is_enabled(),
        "health": health_report(),
    }


@safe(default="")
def full_status_fa() -> str:
    s = full_status()
    tr = s.get("trade", {})
    mem = s.get("ai_memory", {})
    slots = s.get("slots", {})
    market = s.get("market", {})
    return (
        "🤖 وضعیت کامل AI و ترید\n"
        f"AI/Learning: روشن\n"
        f"گزارش روزانه: {'روشن' if s.get('daily_report_enabled') else 'خاموش'}\n"
        f"Market: {market.get('market_mode','UNKNOWN')} | BTC: {market.get('btc_bias','UNKNOWN')}\n"
        f"Real: {mem.get('real',0)} | TP:{mem.get('real_tp',0)} SL:{mem.get('real_sl',0)} | WR:{mem.get('real_wr',0)}%\n"
        f"Ghost: {mem.get('ghost',0)} | TP:{mem.get('ghost_tp',0)} SL:{mem.get('ghost_sl',0)} | WR:{mem.get('ghost_wr',0)}%\n"
        f"Memory: باز {mem.get('open',0)} | کل {mem.get('records',0)}\n"
        f"Slots: {slots.get('used_slots',0)}/{slots.get('max_positions',0)} | خالی {slots.get('free_slots',0)}\n"
        f"Trade: {tr.get('mode')} | Balance: {tr.get('balance')}$ | Protected: {tr.get('protected_balance')}$"
    )
