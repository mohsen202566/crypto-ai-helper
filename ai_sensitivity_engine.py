from __future__ import annotations

from dataclasses import dataclass

from scorer import Direction


@dataclass(frozen=True)
class SensitivityResult:
    score_adjustment: int
    confidence_adjustment: int
    reasons: tuple[str, ...]


class AISensitivityEngine:
    def analyze(self, storage, symbol_name: str, direction: Direction) -> SensitivityResult:
        profile = storage.symbol_direction_profile(symbol_name, direction)
        samples = int(profile.get("total_signals", 0))
        wr = float(profile.get("win_rate", 0.0))
        consecutive_sl = int(profile.get("consecutive_sl", 0))
        reasons: list[str] = []
        adjustment = 0
        confidence = 0
        if consecutive_sl >= 3:
            adjustment -= 5
            confidence -= 8
            reasons.append("بعد از 3 استاپ پشت سر هم، AI محتاط‌تر شد اما متوقف نشد.")
        if samples >= 30 and wr >= 62:
            adjustment += 4
            confidence += 6
            reasons.append("پروفایل ارز/جهت در نمونه‌های کافی مثبت است.")
        elif samples >= 30 and wr <= 42:
            adjustment -= 5
            confidence -= 6
            reasons.append("پروفایل ارز/جهت ضعیف است؛ Real سخت‌تر شد.")
        elif samples >= 10:
            adjustment += max(-2, min(2, int((wr - 50) / 10)))
            reasons.append("نمونه متوسط است؛ تنظیم نرم اعمال شد.")
        else:
            reasons.append("نمونه کافی برای تغییر شدید حساسیت وجود ندارد.")
        return SensitivityResult(adjustment, confidence, tuple(reasons))
