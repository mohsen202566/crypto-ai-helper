from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Judgement:
    entry_quality: str
    tp_quality: str
    sl_quality: str
    failure_reason: str
    score_delta: int
    reasons: tuple[str, ...]


class AIJudge:
    def judge_closed_signal(self, signal: dict) -> Judgement:
        status = str(signal.get("status") or "")
        mfe = float(signal.get("mfe_pct") or 0.0)
        mae = float(signal.get("mae_pct") or 0.0)
        expected = abs(float(signal.get("tp") or 0.0) - float(signal.get("entry") or 0.0)) / max(float(signal.get("entry") or 1.0), 1e-9)
        risk = abs(float(signal.get("entry") or 0.0) - float(signal.get("sl") or 0.0)) / max(float(signal.get("entry") or 1.0), 1e-9)
        reasons: list[str] = []
        entry_quality = "good" if status == "TP" or mae <= risk * 0.55 else "needs_precision"
        tp_quality = "good" if status == "TP" else "review"
        sl_quality = "good" if status == "SL" and mae >= risk * 0.90 else "review"
        failure_reason = "none"
        delta = 2 if status == "TP" else -2 if status == "SL" else 0
        if status == "TP":
            reasons.append("جهت و TP برای این الگو تایید شد.")
            if mfe > expected * 1.5:
                reasons.append("Shadow: احتمالاً TP می‌توانست کمی بازتر باشد.")
        elif status == "SL":
            if mfe >= expected * 0.55:
                failure_reason = "tp_too_far_or_exit_missed"
                reasons.append("قیمت بخشی از مسیر سود را رفت اما TP یا خروج بهینه نبود.")
            elif mae < risk * 0.75:
                failure_reason = "noise_or_bad_price_tracking"
                reasons.append("SL کامل با حرکت طبیعی پر نشده؛ بررسی نویز/قیمت لازم است.")
            else:
                failure_reason = "direction_or_entry_precision"
                reasons.append("جهت یا دقت ورود برای این الگو نیاز به اصلاح دارد.")
        else:
            failure_reason = "early_exit_or_failed"
            reasons.append("نتیجه غیر TP/SL برای یادگیری جدا ثبت شد.")
        return Judgement(entry_quality, tp_quality, sl_quality, failure_reason, delta, tuple(reasons))
