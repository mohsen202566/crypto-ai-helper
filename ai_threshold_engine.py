from __future__ import annotations

from dataclasses import dataclass

from config import (
    BASE_REAL_THRESHOLD,
    BASE_SIGNAL_THRESHOLD,
    MAX_DYNAMIC_REAL_THRESHOLD,
    MAX_DYNAMIC_SIGNAL_THRESHOLD,
    MIN_DYNAMIC_REAL_THRESHOLD,
    MIN_DYNAMIC_SIGNAL_THRESHOLD,
)
from scorer import Direction


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


@dataclass(frozen=True)
class ThresholdResult:
    signal_threshold: int
    real_threshold: int
    samples: int
    win_rate: float
    source: str
    reasons: tuple[str, ...]


class AIThresholdEngine:
    """Learns signal/real thresholds per symbol + direction.

    BASE thresholds are only boot values. After enough examples, score meaning is
    learned separately for each symbol/direction from TP/SL and score buckets.
    """

    def analyze(self, storage, symbol_name: str, direction: Direction) -> ThresholdResult:
        profile = storage.symbol_direction_profile(symbol_name, direction)
        samples = int(profile.get("total_signals", 0) or 0)
        wr = float(profile.get("win_rate", 0.0) or 0.0)
        consecutive_sl = int(profile.get("consecutive_sl", 0) or 0)
        net_profit = float(profile.get("net_profit", 0.0) or 0.0)
        buckets = storage.score_bucket_stats(symbol_name, direction)

        signal_threshold = int(profile.get("signal_threshold") or BASE_SIGNAL_THRESHOLD)
        real_threshold = int(profile.get("real_threshold") or BASE_REAL_THRESHOLD)
        reasons: list[str] = []
        source = "BOOT"

        if samples < 5:
            signal_threshold = BASE_SIGNAL_THRESHOLD
            real_threshold = BASE_REAL_THRESHOLD
            reasons.append(f"Threshold شروع یادگیری است: Signal={signal_threshold} Real={real_threshold}.")
        else:
            signal_threshold = BASE_SIGNAL_THRESHOLD
            real_threshold = BASE_REAL_THRESHOLD
            source = "PROFILE"

            if samples >= 10:
                if wr >= 60:
                    signal_threshold -= 2
                    real_threshold -= 1
                    reasons.append("WR پروفایل خوب است؛ AI کمی زودتر اجازه سیگنال می‌دهد.")
                elif wr <= 43:
                    signal_threshold += 3
                    real_threshold += 5
                    reasons.append("WR پروفایل ضعیف است؛ AI سخت‌گیرتر شد.")
                else:
                    reasons.append("WR پروفایل متوسط است؛ Threshold نزدیک مقدار شروع ماند.")

            if samples >= 30:
                if wr >= 62:
                    signal_threshold -= 2
                    real_threshold -= 1
                    reasons.append("نمونه کافی و مثبت است؛ Threshold همان ارز/جهت پایین‌تر آمد.")
                elif wr <= 42:
                    signal_threshold += 3
                    real_threshold += 4
                    reasons.append("نمونه کافی و ضعیف است؛ Threshold همان ارز/جهت بالا رفت.")

            good_buckets = [b for b in buckets if int(b.get("samples", 0)) >= 3 and float(b.get("win_rate", 0.0)) >= 60.0]
            strong_buckets = [b for b in buckets if int(b.get("samples", 0)) >= 4 and float(b.get("win_rate", 0.0)) >= 66.0]
            bad_buckets = [b for b in buckets if int(b.get("samples", 0)) >= 3 and float(b.get("win_rate", 0.0)) <= 38.0]

            if good_buckets:
                best_low = min(int(b["bucket_low"]) for b in good_buckets)
                signal_threshold = round((signal_threshold * 0.55) + (best_low * 0.45))
                source = "SCORE_BUCKETS"
                reasons.append(f"AI از score-bucketهای موفق فهمید Signal برای این ارز/جهت می‌تواند نزدیک {best_low} باشد.")

            if strong_buckets:
                best_real_low = min(int(b["bucket_low"]) for b in strong_buckets)
                real_threshold = round((real_threshold * 0.50) + (max(best_real_low, signal_threshold + 2) * 0.50))
                source = "SCORE_BUCKETS"
                reasons.append(f"score-bucket قوی برای Real نزدیک {best_real_low} دیده شد.")

            if bad_buckets:
                lowest_bad = min(int(b["bucket_low"]) for b in bad_buckets)
                if signal_threshold <= lowest_bad + 4:
                    signal_threshold += 2
                real_threshold += 3
                reasons.append("score-bucketهای بد پیدا شد؛ AI مخصوصاً Real را سخت‌تر کرد.")

            if consecutive_sl >= 3:
                signal_threshold += 2
                real_threshold += 6
                reasons.append("۳ استاپ پشت‌سرهم: سیگنال کمی سخت‌تر و Real خیلی محتاط‌تر شد.")

            if samples >= 15 and net_profit < 0:
                real_threshold += 3
                reasons.append("سود خالص پروفایل منفی است؛ Real محافظه‌کارتر شد.")

        signal_threshold = _clamp(signal_threshold, MIN_DYNAMIC_SIGNAL_THRESHOLD, MAX_DYNAMIC_SIGNAL_THRESHOLD)
        real_threshold = _clamp(real_threshold, MIN_DYNAMIC_REAL_THRESHOLD, MAX_DYNAMIC_REAL_THRESHOLD)
        real_threshold = max(real_threshold, signal_threshold + 2)
        real_threshold = _clamp(real_threshold, MIN_DYNAMIC_REAL_THRESHOLD, MAX_DYNAMIC_REAL_THRESHOLD)

        storage.store_profile_thresholds(
            symbol_name=symbol_name,
            direction=direction,
            signal_threshold=signal_threshold,
            real_threshold=real_threshold,
            source=source,
        )
        reasons.append(f"Threshold نهایی AI برای {symbol_name} {direction}: Signal={signal_threshold} Real={real_threshold}.")
        return ThresholdResult(signal_threshold, real_threshold, samples, wr, source, tuple(reasons))
