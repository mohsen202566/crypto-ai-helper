from __future__ import annotations

from config import MAX_WATCH_SYMBOLS, READY_ALERT_COOLDOWN_SECONDS, READY_ALERT_MAX_STAGE_PCT, READY_ALERT_THRESHOLD, WATCH_EXPIRE_SECONDS
from scorer import SignalDecision
from storage import Storage
from symbols import MarketSymbol


class WatchEngine:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def register_watch(self, symbol: MarketSymbol, decision: SignalDecision) -> None:
        if decision.direction is None:
            return
        self.storage.upsert_watch(symbol_name=symbol.name, okx_symbol=symbol.okx_inst_id, toobit_symbol=symbol.toobit_symbol, direction=decision.direction, score=decision.score, ai_confidence=decision.ai_confidence, expire_seconds=WATCH_EXPIRE_SECONDS)
        self.storage.trim_watchlist(MAX_WATCH_SYMBOLS)

    def active_watches(self) -> list[dict]:
        return self.storage.active_watches()

    def should_send_ready(self, symbol_name: str, direction: str, decision: SignalDecision) -> bool:
        if not decision.ready_alert:
            return False
        if decision.score < READY_ALERT_THRESHOLD:
            return False
        if decision.entry_5m != "PRE_WATCH":
            return False
        if decision.entry_stage_pct > READY_ALERT_MAX_STAGE_PCT:
            return False
        if decision.estimated_profit_usdt <= 0 or decision.estimated_profit_pct <= 0:
            return False
        return self.storage.can_send_ready_alert(symbol_name, direction, READY_ALERT_COOLDOWN_SECONDS)

    def mark_ready_sent(self, symbol_name: str, direction: str) -> None:
        self.storage.mark_ready_alert_sent(symbol_name, direction)

    def remove_watch(self, symbol_name: str, direction: str | None = None) -> None:
        self.storage.remove_watch(symbol_name, direction)
