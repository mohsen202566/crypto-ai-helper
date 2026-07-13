from __future__ import annotations

from dataclasses import dataclass, field

from market_engine import MarketAnalysis
from setup_engine import SetupCandidate
from watch_engine import WatchEvaluation


@dataclass
class TradeDecision:
    action: str
    final_score: float  # شاخص کیفیت تشخیصی برای سازگاری؛ Gate نیست.
    confidence: float
    allowed: bool
    primary_reason: str
    module_scores: dict[str, float] = field(default_factory=dict)
    contradictions: list[str] = field(default_factory=list)


class DecisionEngine:
    """تصمیم مبتنی بر وضعیت، نه جمع امتیاز و AND چند Threshold."""

    def decide(self, m: MarketAnalysis, s: SetupCandidate, w: WatchEvaluation) -> TradeDecision:
        context_valid = (
            not m.hard_veto
            and m.primary_direction == s.side
            and s.meta.get("context_side") == s.side
        )
        opportunity_valid = s.state in {"READY", "WATCH"} and s.meta.get("scenario_state") != "INVALIDATED"
        activation_valid = w.confirmed and w.state == "TRIGGER_CONFIRMED"
        hard_invalid = w.state in {"INVALIDATED", "EXPIRED"}

        allowed = context_valid and opportunity_valid and activation_valid and not hard_invalid
        if hard_invalid:
            reason = w.reason
            action = "REJECT"
        elif not context_valid:
            reason = "زمینه ساختاری سناریو باطل یا جهت بازار عوض شده است"
            action = "REJECT"
        elif not opportunity_valid:
            reason = "فرصت سناریو دیگر معتبر نیست"
            action = "REJECT"
        elif not activation_valid:
            reason = w.reason
            action = "KEEP_WATCHING"
        else:
            reason = f"سناریو فعال شد: {w.reason}"
            action = f"SIGNAL_{s.side}"

        # کیفیت فقط برای گزارش و تنظیم محافظه‌کارانه RiskEngine است.
        quality = 78.0
        if w.meta.get("activation_path") == "PREPARED_FAST_BREAK":
            quality = 90.0
        elif w.meta.get("activation_path") == "PRESSURE_PROGRESS_ACCEPTANCE":
            quality = 87.0
        elif w.meta.get("activation_path") == "PRICE_RESPONSE_ACCEPTANCE":
            quality = 84.0
        quality -= min(10.0, 3.0 * len(m.contradictions))
        quality = max(0.0, min(100.0, quality))

        states = {
            "context": 100.0 if context_valid else 0.0,
            "opportunity": 100.0 if opportunity_valid else 0.0,
            "activation": 100.0 if activation_valid else 0.0,
        }
        return TradeDecision(
            action=action,
            final_score=round(quality, 2),
            confidence=round(quality, 2),
            allowed=allowed,
            primary_reason=reason,
            module_scores=states,
            contradictions=list(m.contradictions),
        )
