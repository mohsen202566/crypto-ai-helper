from __future__ import annotations

from dataclasses import dataclass

from indicators import IndicatorSnapshot
from utils import session_bucket


@dataclass(frozen=True)
class RangeVerdict:
    features_key: str
    samples: int
    win_rate: float
    net_profit: float
    confidence: int
    message: str


def _bin(value: float, step: float, prefix: str) -> str:
    bucket = int(value / step)
    return f"{prefix}{bucket}"


class RangeLearningBrain:
    def make_features_key(self, *, symbol_name: str, market_state: str, alignment: str, snapshot: IndicatorSnapshot) -> str:
        parts = [
            symbol_name,
            "LONG",
            session_bucket(),
            market_state,
            alignment.replace(" ", ""),
            _bin(snapshot.rsi14, 5, "rsi"),
            _bin(snapshot.adx14, 5, "adx"),
            _bin(snapshot.volume_ratio, 0.5, "vol"),
            _bin(snapshot.atr_pct * 100, 0.2, "atr"),
            _bin(snapshot.dist_vwap_pct * 100, 0.5, "vwap"),
            _bin(snapshot.dist_ema20_pct * 100, 0.5, "ema20"),
        ]
        return "|".join(parts)

    def verdict(self, storage, features_key: str) -> RangeVerdict:
        profile = storage.get_range_profile(features_key)
        if not profile:
            return RangeVerdict(features_key, 0, 0.0, 0.0, 0, "بازه جدید است؛ برای یادگیری نرم بررسی می‌شود.")
        samples = int(profile.get("samples") or 0)
        win_rate = float(profile.get("win_rate") or 0.0)
        net_profit = float(profile.get("net_profit") or 0.0)
        confidence = int(profile.get("confidence") or 0)
        if samples < 10:
            message = "نمونه کم است؛ تصمیم نرم باقی می‌ماند."
        elif net_profit > 0 and win_rate >= 45:
            message = "این بازه قبلاً سودده بوده است."
        else:
            message = "این بازه سابقه ضعیف دارد و سخت‌تر بررسی می‌شود."
        return RangeVerdict(features_key, samples, win_rate, net_profit, confidence, message)
