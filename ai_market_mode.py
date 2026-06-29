from __future__ import annotations

from dataclasses import dataclass

from config import WEIGHTS
from indicators import IndicatorSnapshot


@dataclass(frozen=True)
class MarketModeResult:
    mode: str
    score: int
    risk: int
    reasons: tuple[str, ...]


class MarketModeBrain:
    def analyze(self, snapshot_5m: IndicatorSnapshot, snapshot_15m: IndicatorSnapshot) -> MarketModeResult:
        # compatibility names: snapshot_5m=30m, snapshot_15m=1H
        s30 = snapshot_5m
        s1h = snapshot_15m
        atr_ratio = s1h.atr / max(s1h.prev_atr, s1h.close * 0.0001)
        volume = max(s30.volume_ratio, s1h.volume_ratio)
        squeeze = s1h.bb_width_pct < max(s1h.atr_pct * 2.35, 0.010)
        if volume > 4.4 or atr_ratio > 2.55:
            return MarketModeResult("CLIMAX_RISK", max(2, WEIGHTS.market_mode - 7), 72, ("بازار 1H حالت کلایمکس/مصرف حرکت دارد؛ AI مخصوصاً Real را سخت‌تر می‌کند.",))
        if squeeze and 0.70 <= volume <= 3.4:
            return MarketModeResult("SQUEEZE_READY", WEIGHTS.market_mode, 24, ("فشردگی 1H با ولوم قابل استفاده؛ آماده حرکت احتمالی است.",))
        if 0.75 <= atr_ratio <= 2.10 and 0.65 <= volume <= 3.6:
            if abs(s30.macd_hist_slope) > 0 and abs(s1h.rsi_delta) > 0.15:
                return MarketModeResult("MOMENTUM_BUILDING", WEIGHTS.market_mode, 25, ("بازار برای شکار حرکت 1H فعال است.",))
            return MarketModeResult("NORMAL", max(5, WEIGHTS.market_mode - 2), 35, ("بازار 1H عادی و قابل بررسی است.",))
        if volume < 0.55 or atr_ratio < 0.65:
            return MarketModeResult("QUIET", 3, 45, ("بازار 1H کم‌جان است؛ AI صبورتر عمل می‌کند.",))
        return MarketModeResult("NOISY", 4, 55, ("بازار 1H نویزی است؛ TP/SL و Real باید محتاط‌تر باشد.",))
