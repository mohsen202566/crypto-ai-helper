from __future__ import annotations

from dataclasses import replace

from adaptive_fix_engine import AdaptiveFixEngine
from fundamental_guard import FundamentalGuard
from guard_types import GuardVerdict, strongest_verdict
from risk_guard import StopLossGuard
from session_guard import SessionOpenGuard


class SafetyLayer:
    """External safety layer. It never changes the AI entry logic.

    It can only do three things after the AI creates a candidate:
    1) allow it,
    2) downgrade Real to Normal/Normal-Controlled,
    3) reject it completely when the risk is not tradeable.

    WATCH is intentionally removed. There is no Watch signal type anymore.
    """

    def __init__(self, storage) -> None:
        self.storage = storage
        self.news = FundamentalGuard(storage)
        self.session = SessionOpenGuard(storage)
        self.stop = StopLossGuard(storage)
        self.fix = AdaptiveFixEngine(storage)

    def pre_scan_verdict(self) -> GuardVerdict:
        return self.news.evaluate()

    def pending_alert_messages(self) -> list[str]:
        return self.news.pending_alert_messages()

    def apply(self, symbol, decision):
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
            self.storage.record_guard_event("SAFETY_LAYER", "block", verdict.reason, "REJECT", verdict.payload)
            return None, verdict

        reason_suffix = f"\n\n⚠️ گارد ایمنی: {verdict.reason}"
        hint = "normal_controlled" if verdict.is_caution else "normal"
        guarded = replace(
            decision,
            real_allowed=False,
            signal_type_hint=hint,
            decision_label="NORMAL_CONTROLLED" if hint == "normal_controlled" else "NORMAL",
            control_mode=hint,
            reason=(decision.reason or "") + reason_suffix,
        )
        self.storage.record_guard_event("SAFETY_LAYER", verdict.level.lower(), verdict.reason, "NORMAL_CONTROLLED", verdict.payload)
        return guarded, verdict
