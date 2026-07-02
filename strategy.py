"""استراتژی Spot Long Hunter.

قانون اصلی:
- فقط LONG
- ورود بعد از ریزش/پولبک سالم
- همه تایم‌فریم‌ها باید همسو یا سالم رو به بالا باشند
- ورود وسط پامپ ممنوع
"""
from __future__ import annotations

from typing import Any

import pandas as pd

import config
from indicators import (
    above_emas,
    bullish_candle,
    ema_bullish,
    last,
    last_n_change_pct,
    near_ema_support,
    pct_from_high,
    pct_to_recent_high,
    strong_bullish_candle,
    volume_ratio,
)
from models import BotSettings, Signal
from utils import net_profit_estimate, safe_float


class SpotLongStrategy:
    def __init__(self, settings: BotSettings):
        self.settings = settings

    def update_settings(self, settings: BotSettings) -> None:
        self.settings = settings

    def evaluate(self, market_pack: dict[str, Any]) -> Signal | None:
        frames: dict[str, pd.DataFrame] = market_pack.get("frames") or {}
        required = ["1D", "4H", "1H", "15M", "5M"]
        if any(frames.get(tf) is None or frames.get(tf).empty or len(frames.get(tf)) < 60 for tf in required):
            return None

        df1d = frames["1D"]
        df4h = frames["4H"]
        df1h = frames["1H"]
        df15 = frames["15M"]
        df5 = frames["5M"]

        entry = float(last(df5)["close"])
        if entry <= 0:
            return None

        score = 0
        confirmations: dict[str, str] = {}
        reasons: list[str] = []

        day_ok, day_text, day_score = self._higher_tf_ok(df1d, max_score=20, name="1D")
        h4_ok, h4_text, h4_score = self._higher_tf_ok(df4h, max_score=25, name="4H")
        h1_ok, h1_text, h1_score = self._h1_ok(df1h)
        m15_ok, m15_text, m15_score = self._entry_tf_ok(df15, name="15M", max_score=15)
        m5_ok, m5_text, m5_score = self._entry_tf_ok(df5, name="5M", max_score=10)

        confirmations.update({
            "1D": day_text,
            "4H": h4_text,
            "1H": h1_text,
            "15M": m15_text,
            "5M": m5_text,
        })
        score += day_score + h4_score + h1_score + m15_score + m5_score

        quality_score, quality_text, reject_reason = self._market_quality(df1h, df15, df5, entry)
        confirmations["کیفیت"] = quality_text
        score += quality_score

        if reject_reason:
            return None
        if not (day_ok and h4_ok and h1_ok and m15_ok and m5_ok):
            return None

        potential = max(
            pct_to_recent_high(df1h, 80),
            pct_to_recent_high(df4h, 80),
        )
        if potential < self.settings.target_percent:
            return None
        confirmations["فضای حرکت"] = f"حدود {potential:.2f}٪ فضای احتمالی تا سقف اخیر"

        if score < config.MIN_SIGNAL_SCORE:
            return None

        estimate = net_profit_estimate(
            self.settings.trade_amount_usdt,
            self.settings.target_percent,
            self.settings.taker_fee_pct,
            self.settings.maker_fee_pct,
        )
        if estimate["net_profit_usdt"] <= 0:
            return None

        reasons.append("جهت‌ها همسو هستند")
        reasons.append("ورود بعد از اصلاح سالم تایید شد")
        reasons.append(f"فضای حرکت حداقل {self.settings.target_percent:.2f}٪ دیده شد")
        reasons.append("ورود وسط پامپ رد شد")

        return Signal.new(
            base_symbol=str(market_pack.get("base_symbol") or "").upper(),
            entry_price=entry,
            target_percent=self.settings.target_percent,
            amount_usdt=self.settings.trade_amount_usdt,
            score=score,
            reason="؛ ".join(reasons),
            confirmations=confirmations,
        )

    def _higher_tf_ok(self, df: pd.DataFrame, max_score: int, name: str) -> tuple[bool, str, int]:
        row = last(df)
        close = safe_float(row.get("close"))
        rsi = safe_float(row.get("rsi14"))
        macd_hist = safe_float(row.get("macd_hist"))
        ema_ok = ema_bullish(row) and close > safe_float(row.get("ema50"))
        soft_ok = close > safe_float(row.get("ema20")) and rsi >= 45

        if ema_ok and 45 <= rsi <= 78 and macd_hist >= -abs(close) * 0.002:
            return True, f"لانگ سالم | RSI {rsi:.1f}", max_score
        if soft_ok and name == "1D":
            return True, f"خنثی رو به بالا | RSI {rsi:.1f}", int(max_score * 0.75)
        return False, f"تایید لانگ کافی نیست | RSI {rsi:.1f}", 0

    def _h1_ok(self, df: pd.DataFrame) -> tuple[bool, str, int]:
        row = last(df)
        pullback = pct_from_high(df, 24)
        rsi = safe_float(row.get("rsi14"))
        hist = safe_float(row.get("macd_hist"))
        price_ok = above_emas(row) or near_ema_support(row, 2.2)
        pullback_ok = config.MIN_PULLBACK_PCT <= pullback <= config.MAX_PULLBACK_PCT
        if price_ok and pullback_ok and rsi >= 42 and hist > safe_float(df.iloc[-2].get("macd_hist"), hist - 1):
            return True, f"برگشت/پولبک سالم | اصلاح {pullback:.2f}٪", 25
        return False, f"پولبک یا برگشت کافی نیست | اصلاح {pullback:.2f}٪", 0

    def _entry_tf_ok(self, df: pd.DataFrame, name: str, max_score: int) -> tuple[bool, str, int]:
        row = last(df)
        prev = df.iloc[-2]
        rsi = safe_float(row.get("rsi14"))
        vr = volume_ratio(row)
        candle_ok = bullish_candle(row) or (safe_float(row.get("close")) > safe_float(prev.get("close")))
        support_ok = near_ema_support(row, 2.0) or above_emas(row)
        hist_up = safe_float(row.get("macd_hist")) >= safe_float(prev.get("macd_hist"))
        if candle_ok and support_ok and rsi >= 42 and hist_up and vr >= config.MIN_VOLUME_RATIO:
            return True, f"تریگر لانگ تایید شد | حجم {vr:.2f}x", max_score
        return False, f"تریگر کافی نیست | حجم {vr:.2f}x", 0

    def _market_quality(self, df1h: pd.DataFrame, df15: pd.DataFrame, df5: pd.DataFrame, entry: float) -> tuple[int, str, str | None]:
        last_hour = last_n_change_pct(df5, 12)
        if last_hour > config.MAX_LAST_HOUR_PUMP_PCT:
            return 0, f"رد: پامپ اخیر {last_hour:.2f}٪", "ورود وسط پامپ"

        last3 = df15.tail(3)
        if len(last3) == 3 and all(strong_bullish_candle(row) for _, row in last3.iterrows()):
            return 0, "رد: سه کندل 15M خیلی سبز پشت سر هم", "ورود دیرهنگام"

        pullback = pct_from_high(df1h, 24)
        if pullback < config.MIN_PULLBACK_PCT:
            return 0, f"رد: اصلاح کافی نیست {pullback:.2f}٪", "اصلاح کافی نیست"
        if pullback > config.MAX_PULLBACK_PCT:
            return 0, f"رد: اصلاح خیلی عمیق {pullback:.2f}٪", "ریزش سنگین"

        return 5, f"کیفیت مناسب | اصلاح {pullback:.2f}٪ و ورود دیر نیست", None
