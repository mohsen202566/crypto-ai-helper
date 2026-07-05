from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from indicators import IndicatorSnapshot

MarketState = Literal[
    "TREND",
    "RANGE",
    "NOISE",
    "BREAKOUT",
    "FAKE_BREAKOUT",
    "CLIMAX",
    "REVERSAL",
    "HIGH_VOLATILITY",
    "LOW_VOLUME",
]


@dataclass(frozen=True)
class MarketStateResult:
    state: MarketState
    reasons: tuple[str, ...]


class MarketStateEngine:
    """Classify the market before judging the signal.

    This layer is intentionally deterministic and lightweight. It does not create
    entries by itself; it labels the current market behavior so the existing AI,
    risk guards, and learning engine can decide NORMAL / REAL / REJECT correctly.
    """

    def analyze(self, snapshot: IndicatorSnapshot, direction: str) -> MarketStateResult:
        reasons: list[str] = []
        direction = str(direction or "").upper()

        atr_pct = float(snapshot.atr_pct or 0.0)
        volume_ratio = float(snapshot.volume_ratio or 0.0)
        adx = float(snapshot.adx or 0.0)
        vwap_distance = abs(float(snapshot.price_vs_vwap_pct or 0.0))
        ema_gap = abs(float(snapshot.ema20_50_gap_pct or 0.0))
        chop = float(getattr(snapshot, "choppiness", 50.0) or 50.0)
        atrp = float(getattr(snapshot, "atr_percentile", 50.0) or 50.0)
        squeeze = float(getattr(snapshot, "keltner_squeeze_ratio", 1.0) or 1.0)
        donch_breakout = str(getattr(snapshot, "donchian_breakout", "NONE") or "NONE").upper()
        donch_pos = float(getattr(snapshot, "donchian_position_pct", 50.0) or 50.0)

        # 1) Hard behavior states first.
        if volume_ratio < 0.55 and adx < 14:
            return MarketStateResult("LOW_VOLUME", ("حجم و ADX پایین است؛ بازار کم‌جان/کم‌اعتبار است.",))

        if volume_ratio > 4.5 and snapshot.body_pct > 0.65:
            return MarketStateResult("CLIMAX", ("ولوم و بدنه کندل کلایمکس است؛ ورود دیر یا برگشت ناگهانی محتمل است.",))

        if atr_pct >= 0.018 or atrp >= 92:
            if volume_ratio >= 2.8 or snapshot.body_pct > 0.58:
                return MarketStateResult("CLIMAX", ("ATR/ATRP و ولوم بیش‌ازحد داغ است؛ ریسک پامپ/دامپ یا کلایمکس.",))
            return MarketStateResult("HIGH_VOLATILITY", ("نوسان غیرعادی بالاست؛ سیگنال فقط با کنترل خروج و ریسک معتبر است.",))

        # 2) Breakout / fake breakout based on Donchian and squeeze helpers.
        if donch_breakout in {"UP", "DOWN"}:
            breakout_matches_direction = (direction == "LONG" and donch_breakout == "UP") or (direction == "SHORT" and donch_breakout == "DOWN")
            if not breakout_matches_direction:
                return MarketStateResult("FAKE_BREAKOUT", ("شکست Donchian خلاف جهت سیگنال است؛ ریسک شکست فیک/دام زیاد است.",))
            if squeeze <= 0.85 and volume_ratio < 1.25:
                return MarketStateResult("FAKE_BREAKOUT", ("شکست از فشردگی بدون تایید حجم کافی دیده شد؛ احتمال فیک‌اوت.",))
            if adx >= 17 and volume_ratio >= 1.20:
                return MarketStateResult("BREAKOUT", ("شکست Donchian با ADX/حجم قابل بررسی است؛ ورود باید کنترل‌شده باشد.",))

        # 3) Reversal risk at channel extremes with weak trend force.
        if direction == "LONG" and donch_pos >= 92 and adx < 22:
            return MarketStateResult("REVERSAL", ("قیمت نزدیک سقف کانال و قدرت روند کافی نیست؛ ریسک برگشت برای لانگ.",))
        if direction == "SHORT" and donch_pos <= 8 and adx < 22:
            return MarketStateResult("REVERSAL", ("قیمت نزدیک کف کانال و قدرت روند کافی نیست؛ ریسک برگشت برای شورت.",))

        # 4) Range / chop / trend.
        if chop >= 61 or (adx < 16 and vwap_distance < 0.004):
            return MarketStateResult("RANGE", ("چاپ/ADX/VWAP حالت رنج یا نوسان داخل محدوده را نشان می‌دهد.",))

        if adx >= 19 and ema_gap > 0.0008 and chop <= 58:
            reasons.append("ADX، EMAها و Choppiness روند قابل استفاده نشان می‌دهند.")
            return MarketStateResult("TREND", tuple(reasons))

        if volume_ratio > 1.35 and adx >= 17:
            return MarketStateResult("BREAKOUT", ("حجم و ADX برای شکست قابل بررسی است؛ Retest/کنترل خروج مهم است.",))

        if atr_pct > 0.014 and vwap_distance > 0.018:
            return MarketStateResult("FAKE_BREAKOUT", ("ATR باز و فاصله از VWAP زیاد است؛ ریسک شکست فیک/تعقیب دیرهنگام.",))

        return MarketStateResult("NOISE", ("بازار واضح نیست؛ فقط اگر Context و خروج خالص قابل کنترل باشد Normal مجاز است.",))
