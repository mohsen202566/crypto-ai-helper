from __future__ import annotations

from dataclasses import replace

from fundamental_guard import FundamentalGuard
from guard_types import GuardVerdict, strongest_verdict
from risk_guard import StopLossGuard
from session_guard import SessionOpenGuard


class SafetyLayer:
    """External safety layer. It never changes the AI analysis itself.

    It only decides whether a finished AI decision may be sent as Real/Normal or should be held.
    """

    def __init__(self, storage) -> None:
        self.storage = storage
        self.news = FundamentalGuard(storage)
        self.session = SessionOpenGuard(storage)
        self.stop = StopLossGuard(storage)

    def pre_scan_verdict(self) -> GuardVerdict:
        # Fundamental high-impact events can block the whole scan before any market call.
        return self.news.evaluate()

    def pending_alert_messages(self) -> list[str]:
        return self.news.pending_alert_messages()

    def apply(self, symbol, decision):
        verdict = strongest_verdict([
            self.news.evaluate(),
            self.session.evaluate(symbol.name, decision),
            self.stop.evaluate(symbol.name, decision),
        ])
        if verdict.level == "ALLOW":
            return decision, verdict
        reason_suffix = f"\n\n⚠️ گارد ایمنی: {verdict.reason}"
        if verdict.level == "BLOCK":
            self.storage.record_guard_event("SAFETY_LAYER", "block", verdict.reason, "BLOCK", verdict.payload)
            return None, verdict
        if decision.confidence < verdict.min_confidence:
            block_reason = f"{verdict.reason} | اعتماد {decision.confidence}% کمتر از حد احتیاط {verdict.min_confidence}% است."
            block_verdict = GuardVerdict("BLOCK", "SAFETY_LAYER", block_reason, verdict.min_confidence, verdict.payload)
            self.storage.record_guard_event("SAFETY_LAYER", "caution_reject", block_reason, "BLOCK", verdict.payload)
            return None, block_verdict
        # CAUTION/REAL_BLOCK: keep Normal only, do not alter entry/TP/SL/score logic.
        guarded = replace(
            decision,
            real_allowed=False,
            signal_type_hint="normal",
            reason=(decision.reason or "") + reason_suffix,
        )
        self.storage.record_guard_event("SAFETY_LAYER", verdict.level.lower(), verdict.reason, "REAL_BLOCK_OR_CAUTION", verdict.payload)
        return guarded, verdict
