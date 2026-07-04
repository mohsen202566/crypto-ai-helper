from __future__ import annotations

from dataclasses import replace

from adaptive_fix_engine import AdaptiveFixEngine
from fundamental_guard import FundamentalGuard
from guard_types import GuardVerdict, strongest_verdict
from risk_guard import StopLossGuard
from session_guard import SessionOpenGuard


class SafetyLayer:
    """External safety layer. It never changes the AI analysis itself.

    The old AI still decides direction/entry idea. This layer only adds learned safety treatments:
    temporary blocks for news/session/stop cooldown, Real-to-Normal caution, and learned TP/SL cure
    when historical results prove the old distances were too tight/too far.
    """

    def __init__(self, storage) -> None:
        self.storage = storage
        self.news = FundamentalGuard(storage)
        self.session = SessionOpenGuard(storage)
        self.stop = StopLossGuard(storage)
        self.fix = AdaptiveFixEngine(storage)

    def pre_scan_verdict(self) -> GuardVerdict:
        # Fundamental high-impact events can block the whole scan before any market call.
        return self.news.evaluate()

    def pending_alert_messages(self) -> list[str]:
        return self.news.pending_alert_messages()

    def apply(self, symbol, decision):
        # First let the learned cure layer adjust the outgoing signal without touching the AI brain.
        decision, fix_verdict = self.fix.apply(symbol.name, decision)
        verdict = strongest_verdict([
            fix_verdict,
            self.news.evaluate(),
            self.session.evaluate(symbol.name, decision),
            self.stop.evaluate(symbol.name, decision),
        ])
        if verdict.level == "ALLOW":
            return decision, verdict
        if verdict.level == "BLOCK":
            self.storage.record_guard_event("SAFETY_LAYER", "block", verdict.reason, "BLOCK", verdict.payload)
            return None, verdict
        # CAUTION/REAL_BLOCK: do not delete the signal just because confidence is lower.
        # It becomes Normal/safer and the reason is logged. Full no-signal is reserved for temporary hard guards.
        reason_suffix = f"\n\n⚠️ گارد ایمنی: {verdict.reason}"
        # Preserve WATCH from adaptive treatment; otherwise downgrade to Normal.
        hint = "watch" if getattr(decision, "signal_type_hint", "") == "watch" else "normal"
        guarded = replace(
            decision,
            real_allowed=False,
            signal_type_hint=hint,
            reason=(decision.reason or "") + reason_suffix,
        )
        self.storage.record_guard_event("SAFETY_LAYER", verdict.level.lower(), verdict.reason, "CAUTION_NORMAL", verdict.payload)
        return guarded, verdict
