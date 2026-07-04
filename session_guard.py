from __future__ import annotations

from config import (
    SESSION_GUARD_ENABLED,
    SESSION_OPEN_CAUTION_MIN_CONFIDENCE,
    SESSION_OPEN_HARD_BLOCK_MINUTES,
    SESSION_OPEN_REAL_BLOCK_MIN_CONFIDENCE,
)
from guard_types import GuardVerdict
from guard_utils import decision_market_is_calm, session_info


class SessionOpenGuard:
    """External guard for Asia/Europe/America session openings.

    It does not alter the AI's internal logic. It only decides whether the created signal should be
    blocked, downgraded to Normal, or allowed after the session-open noise calms down.
    """

    def __init__(self, storage) -> None:
        self.storage = storage

    def evaluate(self, symbol_name: str, decision) -> GuardVerdict:
        if not SESSION_GUARD_ENABLED:
            return GuardVerdict()
        info = session_info()
        if not info.is_open_watch:
            return GuardVerdict()
        calm = decision_market_is_calm(decision)
        learned = self.storage.get_time_risk_profile(symbol_name=symbol_name, direction=getattr(decision, "direction", None))
        learned_risk = int((learned or {}).get("risk_score") or 0)
        learned_action = str((learned or {}).get("action") or "ALLOW")
        label = f"{info.label} ({info.minutes_from_open} دقیقه از شروع)"
        payload = {"session": info.name, "minutes_from_open": info.minutes_from_open, "calm": calm, "learned_risk": learned_risk}

        if learned_action == "BLOCK" and not calm:
            return GuardVerdict("BLOCK", "SESSION_GUARD", f"{label} + حافظه ساعت بد؛ بازار هنوز آرام نیست.", 0, payload)
        if learned_action == "REAL_BLOCK":
            return GuardVerdict("REAL_BLOCK", "SESSION_GUARD", f"{label} در حافظه قبلی پرریسک بوده؛ Real بسته می‌شود.", SESSION_OPEN_REAL_BLOCK_MIN_CONFIDENCE, payload)
        if learned_action == "CAUTION":
            return GuardVerdict("CAUTION", "SESSION_GUARD", f"{label} در حافظه قبلی ضعیف بوده؛ فقط سیگنال قوی مجاز است.", SESSION_OPEN_CAUTION_MIN_CONFIDENCE, payload)

        # Fixed session-open protection. Europe/America openings are usually sharper than Asia.
        if info.minutes_from_open <= SESSION_OPEN_HARD_BLOCK_MINUTES and info.name in {"EUROPE", "AMERICA"} and not calm:
            return GuardVerdict("BLOCK", "SESSION_GUARD", f"{label}؛ کندل/حجم/ATR هنوز عادی نشده.", 0, payload)
        if not calm and info.name in {"EUROPE", "AMERICA"}:
            return GuardVerdict("REAL_BLOCK", "SESSION_GUARD", f"{label}؛ شروع سشن هنوز نوسانی است، Real بسته می‌شود.", SESSION_OPEN_REAL_BLOCK_MIN_CONFIDENCE, payload)
        if not calm:
            return GuardVerdict("CAUTION", "SESSION_GUARD", f"{label}؛ بازار هنوز کاملاً آرام نیست.", SESSION_OPEN_CAUTION_MIN_CONFIDENCE, payload)
        if info.minutes_from_open <= SESSION_OPEN_HARD_BLOCK_MINUTES:
            return GuardVerdict("CAUTION", "SESSION_GUARD", f"{label}؛ بازار آرام است ولی شروع سشن است، احتیاط سبک.", SESSION_OPEN_CAUTION_MIN_CONFIDENCE, payload)
        return GuardVerdict()
