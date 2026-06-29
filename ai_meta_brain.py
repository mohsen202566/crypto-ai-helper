from __future__ import annotations

from dataclasses import dataclass

from config import (
    MAX_DYNAMIC_REAL_THRESHOLD,
    MAX_DYNAMIC_SIGNAL_THRESHOLD,
    MIN_DYNAMIC_REAL_THRESHOLD,
    MIN_DYNAMIC_SIGNAL_THRESHOLD,
    WATCH_THRESHOLD,
)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(round(value))))


@dataclass(frozen=True)
class MetaDecision:
    action: str
    accepted: bool
    ready_alert: bool
    signal_label: str
    real_allowed: bool
    real_block_reason: str | None
    reason: str
    signal_threshold: int
    real_threshold: int
    source: str
    reasons: tuple[str, ...]


class AIMetaBrain:
    """Soft/adaptive final decision brain.

    Nothing analytical is a permanent hard gate here. Entry quality, precision wait,
    weak movement, noisy/risky modes, pattern history, score buckets and sessions only
    move the adaptive thresholds and Real permission. Hard blocks must stay in the
    execution/safety layer: API, slots, duplicate open signal, Toobit sync/order safety,
    and net-profit protection for Real execution.
    """

    _REAL_ENTRY_QUALITIES = {"EARLY_IGNITION", "GOOD_ENTRY", "POWER_BUILDING", "REVERSAL_BUILDING"}

    _QUALITY_OFFSETS: dict[str, tuple[int, int, str]] = {
        "EARLY_IGNITION": (-3, -1, "شروع حرکت تمیز است؛ AI سیگنال را نرم‌تر می‌گیرد."),
        "GOOD_ENTRY": (-2, 0, "ورود قابل اجراست؛ AI اجازه سیگنال عادی/Real را طبیعی بررسی می‌کند."),
        "POWER_BUILDING": (0, 2, "قدرت در حال ساخت است؛ Normal آزادتر، Real کمی محتاط‌تر."),
        "REVERSAL_BUILDING": (1, 3, "برگشت قابل شکار است؛ AI Real را محتاط‌تر می‌کند."),
        "PRECISION_WAIT": (0, 10, "PRECISION_WAIT قفل نیست؛ برای Normal مجاز است ولی Real از شروع سخت‌تر می‌شود."),
        "WEAK_MOVEMENT": (4, 13, "حرکت ضعیف قفل نیست؛ فقط Thresholdها سخت‌تر می‌شوند."),
        "NOISE_RISK": (7, 16, "نویز ریسک است نه رد کامل؛ Normal سخت‌تر و Real خیلی محتاط‌تر می‌شود."),
        "EXHAUSTION_RISK": (7, 17, "ریسک خستگی حرکت فقط Threshold را بالا می‌برد؛ حذف کامل نمی‌کند."),
        "NO_ENTRY": (12, 22, "بدون ورود مشخص فقط با امتیاز/یادگیری خیلی قوی می‌تواند Normal شود؛ Real بسته می‌ماند."),
    }

    def decide(
        self,
        *,
        total_score: int,
        signal_threshold: int,
        real_threshold: int,
        entry_quality: str,
        risk_ok: bool,
        net_profit_ok: bool,
        range_verdict: str,
        pattern_verdict: str,
        session_state: str,
        market_mode: str,
        context_stats: tuple[dict, ...] = (),
    ) -> MetaDecision:
        reasons: list[str] = []
        source_parts: list[str] = ["THRESHOLD_PROFILE"]

        normal_th = int(signal_threshold)
        real_th = int(real_threshold)
        quality = str(entry_quality or "NO_ENTRY")
        q_signal_offset, q_real_offset, q_reason = self._QUALITY_OFFSETS.get(quality, (5, 12, "کیفیت ورود ناشناخته است؛ AI محتاط‌تر می‌شود."))
        normal_th += q_signal_offset
        real_th += q_real_offset
        reasons.append(q_reason)

        # Default policy is soft: only NO_ENTRY needs stronger evidence for a telegram normal signal.
        normal_quality_allowed = quality != "NO_ENTRY"
        learned_real_entry_allowed = quality in self._REAL_ENTRY_QUALITIES
        learned_normal_boost = False

        for stat in context_stats:
            samples = int(stat.get("samples", 0) or 0)
            if samples <= 0:
                continue
            label = str(stat.get("label") or "context")
            wr = float(stat.get("win_rate", 0.0) or 0.0)
            avg_mfe = float(stat.get("avg_mfe", 0.0) or 0.0)
            avg_mae = float(stat.get("avg_mae", 0.0) or 0.0)
            mfe_ok = avg_mfe >= max(avg_mae * 1.08, 0.00001)

            if samples < 5:
                reasons.append(f"{label}: {samples} نمونه؛ فقط مشاهده، بدون تغییر جدی.")
                continue

            source_parts.append(label)
            if wr >= 62.0 and mfe_ok:
                normal_th -= 2
                real_th -= 1
                normal_quality_allowed = True
                learned_normal_boost = True
                reasons.append(f"{label}: WR {wr:.1f}% و MFE بهتر از MAE؛ AI Normal را نرم‌تر کرد.")
            elif wr <= 42.0:
                normal_th += 3
                real_th += 5
                reasons.append(f"{label}: WR {wr:.1f}% ضعیف؛ AI Thresholdها را بالا برد، نه اینکه کامل حذف کند.")
            else:
                reasons.append(f"{label}: WR {wr:.1f}% متوسط؛ AI فقط نزدیک مقدار فعلی نگه داشت.")

            if samples >= 10 and wr >= 66.0 and mfe_ok:
                normal_th -= 1
                real_th -= 2
                if quality in {"PRECISION_WAIT", "WEAK_MOVEMENT", "NOISE_RISK"}:
                    learned_real_entry_allowed = True
                    reasons.append(f"{label}: برای {quality} نتیجه کافی خوب بوده؛ AI اجازه می‌دهد Real هم مشروط بررسی شود.")

            if samples >= 15 and wr <= 38.0:
                real_th += 4
                reasons.append(f"{label}: نمونه کافی بد؛ Real بسیار سخت‌تر شد.")

        # Memory verdicts are not full reject. They reshape thresholds.
        if range_verdict == "NEGATIVE":
            normal_th += 2
            real_th += 6
            reasons.append("حافظه Range منفی است؛ AI سخت‌تر کرد ولی حذف کامل نکرد.")
        if pattern_verdict == "NEGATIVE":
            normal_th += 2
            real_th += 6
            reasons.append("حافظه Pattern منفی است؛ AI سخت‌تر کرد ولی حذف کامل نکرد.")
        if range_verdict == "POSITIVE" or pattern_verdict == "POSITIVE":
            normal_th -= 1
            reasons.append("حافظه مثبت است؛ AI کمی زودتر Normal را می‌پذیرد.")

        if session_state == "BAD_REAL_ONLY_NORMAL":
            real_th += 10
            reasons.append("سشن برای Real مناسب نیست؛ Real سخت‌تر شد، Normal هنوز زیر تصمیم AI است.")
        if market_mode == "CLIMAX_RISK":
            normal_th += 2
            real_th += 8
            reasons.append("Market Mode کلایمکس/ریسکی است؛ AI مخصوصاً Real را سخت‌تر کرد.")

        # TP/SL quality is also soft for Normal, but Real still needs execution-safe TP/SL.
        if not risk_ok:
            normal_th += 5
            real_th += 12
            reasons.append("TP/SL یا RR کامل نیست؛ Normal سخت‌تر شد و Real نیاز به تایید قوی‌تر دارد.")

        # Net profit must protect Real only. Normal can still be monitored and learned.
        if not net_profit_ok:
            real_th += 20
            reasons.append("سود خالص Real کافی نیست؛ فقط اجرای واقعی محافظت می‌شود، Normal حذف نمی‌شود.")

        normal_th = _clamp(normal_th, MIN_DYNAMIC_SIGNAL_THRESHOLD, MAX_DYNAMIC_SIGNAL_THRESHOLD)
        real_th = _clamp(max(real_th, normal_th + 2), MIN_DYNAMIC_REAL_THRESHOLD, MAX_DYNAMIC_REAL_THRESHOLD)

        if learned_normal_boost and quality == "NO_ENTRY" and total_score >= normal_th + 4:
            normal_quality_allowed = True
            reasons.append("NO_ENTRY با یادگیری مثبت و امتیاز قوی نرم شد؛ Normal مشروط مجاز است.")

        if total_score < WATCH_THRESHOLD:
            return MetaDecision(
                "WATCH",
                False,
                False,
                "کاندید داخلی",
                False,
                "امتیاز زیر محدوده Watch است",
                "AI هنوز فقط مشاهده داخلی می‌کند؛ رد تحلیلی کامل انجام نشد.",
                normal_th,
                real_th,
                "+".join(dict.fromkeys(source_parts)),
                tuple(reasons),
            )

        if total_score >= normal_th and normal_quality_allowed:
            real_allowed = bool(
                total_score >= real_th
                and risk_ok
                and net_profit_ok
                and learned_real_entry_allowed
                and quality != "NO_ENTRY"
            )
            if real_allowed:
                return MetaDecision(
                    "SIGNAL",
                    True,
                    False,
                    "شکار AI واقعی",
                    True,
                    None,
                    "AI با Threshold و کیفیت ورود یادگیرنده، Real را مجاز دانست.",
                    normal_th,
                    real_th,
                    "+".join(dict.fromkeys(source_parts)),
                    tuple(reasons),
                )

            blocks: list[str] = []
            if total_score < real_th:
                blocks.append("امتیاز به Real Threshold یادگیرنده نرسید")
            if not learned_real_entry_allowed:
                blocks.append(f"Entry Quality={quality} هنوز برای Real کافی یاد نگرفته/تایید نشده")
            if not risk_ok:
                blocks.append("TP/SL یا RR برای Real کافی نیست")
            if not net_profit_ok:
                blocks.append("سود خالص Real کافی نیست")
            return MetaDecision(
                "SIGNAL",
                True,
                False,
                "سیگنال عادی AI",
                False,
                "؛ ".join(blocks) or "AI این مورد را فقط Normal مناسب دانست",
                "AI سیگنال عادی را مجاز دانست؛ Real جداگانه محافظت شد.",
                normal_th,
                real_th,
                "+".join(dict.fromkeys(source_parts)),
                tuple(reasons),
            )

        watch_reason = "امتیاز هنوز به Threshold نرم‌شده AI برای Normal نرسیده"
        if not normal_quality_allowed:
            watch_reason = f"Entry Quality={quality} هنوز برای Normal هم شواهد کافی ندارد"
        return MetaDecision(
            "WATCH",
            False,
            False,
            "واچ 1H AI",
            False,
            watch_reason,
            "AI فرصت را داخلی زیر نظر گرفت؛ این رد کامل نیست و با یادگیری می‌تواند نرم‌تر/سخت‌تر شود.",
            normal_th,
            real_th,
            "+".join(dict.fromkeys(source_parts)),
            tuple(reasons),
        )
