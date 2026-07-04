from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from indicators import IndicatorSnapshot

MarketState = Literal["TREND", "RANGE", "BREAKOUT", "FAKE_BREAKOUT_RISK", "CLIMAX", "DEAD_MARKET", "NOISY"]


@dataclass(frozen=True)
class MarketStateResult:
    state: MarketState
    reasons: tuple[str, ...]


class MarketStateEngine:
    def analyze(self, snapshot: IndicatorSnapshot, direction: str) -> MarketStateResult:
        reasons: list[str] = []
        if snapshot.volume_ratio < 0.55 and snapshot.adx < 14:
            return MarketStateResult("DEAD_MARKET", ("حجم و ADX پایین است؛ بازار بی‌جان است.",))
        if snapshot.volume_ratio > 4.5 and snapshot.body_pct > 0.65:
            return MarketStateResult("CLIMAX", ("ولوم و بدنه کندل کلایمکس است؛ ورود دیر محتمل است.",))
        if snapshot.adx < 16 and abs(snapshot.price_vs_vwap_pct) < 0.004:
            return MarketStateResult("RANGE", ("ADX پایین و قیمت نزدیک VWAP است؛ حالت رنج.",))
        if snapshot.adx >= 19 and abs(snapshot.ema20_50_gap_pct) > 0.0008:
            reasons.append("ADX و EMAها روند قابل استفاده نشان می‌دهند.")
            return MarketStateResult("TREND", tuple(reasons))
        if snapshot.volume_ratio > 1.35 and snapshot.adx >= 17:
            return MarketStateResult("BREAKOUT", ("حجم و ADX برای شکست قابل بررسی است.",))
        if snapshot.atr_pct > 0.014 and abs(snapshot.price_vs_vwap_pct) > 0.018:
            return MarketStateResult("FAKE_BREAKOUT_RISK", ("ATR باز و فاصله از VWAP زیاد است؛ ریسک شکست فیک.",))
        return MarketStateResult("NOISY", ("بازار واضح نیست؛ فقط با TP/SL اقتصادی و بازه خوب قابل قبول است.",))
