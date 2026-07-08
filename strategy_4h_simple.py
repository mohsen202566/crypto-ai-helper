from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import config
from indicators import Snapshot, snapshot
from okx_data import Candle
from utils import clamp, okx_swap_symbol

Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class SignalPlan:
    symbol: str
    okx_symbol: str
    toobit_symbol: str
    direction: Direction
    score: float
    strength: str
    entry_price: float
    tp_price: float
    sl_price: float
    risk_reward: float
    sl_pct: float
    tp_pct: float
    estimated_profit_usdt: float
    estimated_loss_usdt: float
    estimated_net_profit_usdt: float
    round_trip_fee_usdt: float
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_legacy_dict(self) -> dict[str, object]:
        return {
            "coin": self.symbol,
            "symbol": self.symbol,
            "okx_symbol": self.okx_symbol,
            "toobit_symbol": self.toobit_symbol,
            "direction": self.direction,
            "side": "BUY" if self.direction == "LONG" else "SELL",
            "score": self.score,
            "confidence": self.score,
            "entry": self.entry_price,
            "entry_price": self.entry_price,
            "tp": self.tp_price,
            "tp_price": self.tp_price,
            "sl": self.sl_price,
            "sl_price": self.sl_price,
            "risk_reward": self.risk_reward,
            "tp_percent": self.tp_pct,
            "sl_percent": self.sl_pct,
            "estimated_profit_usdt": self.estimated_profit_usdt,
            "estimated_loss_usdt": self.estimated_loss_usdt,
            "estimated_net_profit_usdt": self.estimated_net_profit_usdt,
            "round_trip_fee_usdt": self.round_trip_fee_usdt,
            "strength": self.strength,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class DirectionScore:
    direction: Direction | None
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RejectInfo:
    symbol: str
    stage: str
    reason: str
    details: str = ""

    def text(self) -> str:
        if self.details:
            return f"{self.reason} | {self.details}"
        return self.reason


class Simple4HStrategy:
    """1H trend-pullback strategy, kept under the old class/file name for compatibility.

    Hard rules:
    - Direction must align on 4H and 1H.
    - Entry, SL and TP are calculated from 1H structure/ATR.
    - Range is filtered with ADX, EMA50 slope, and price location.
    - Late entries are rejected when price is too far from EMA20/EMA50.
    - Default RR is 1.5; very strong scores may use RR 2.
    - No support/resistance filter, no AI, no DCA, no martingale, no trailing.
    """

    def __init__(self) -> None:
        self.min_score = float(config.SIGNAL_SCORE_THRESHOLD)
        self.strong_score = float(config.STRONG_SCORE_THRESHOLD)
        self.last_reject = RejectInfo("", "", "")

    def _set_reject(self, symbol: str, stage: str, reason: str, details: str = "") -> None:
        self.last_reject = RejectInfo(symbol.upper(), stage, reason, details)

    def get_last_reject_text(self) -> str:
        return self.last_reject.text()

    @staticmethod
    def _dir_label(direction: Direction | None) -> str:
        return direction if direction is not None else "نامشخص"

    @staticmethod
    def _snapshot_summary(prefix: str, s: Snapshot) -> str:
        ema_slope = s.ema50 - s.ema50_lookback
        return (
            f"{prefix}: close={s.close:g}, EMA50={s.ema50:g}, EMA200={s.ema200:g}, "
            f"EMA50_slope={ema_slope:g}, ADX={s.adx:.1f}, +DI={s.plus_di:.1f}, -DI={s.minus_di:.1f}, ATR={s.atr:g}"
        )

    def analyze(
        self,
        symbol: str,
        candles_4h: list[Candle],
        candles_1h: list[Candle],
        *,
        margin_usdt: float,
        leverage: int,
        toobit_symbol: str | None = None,
        round_trip_fee_usdt: float = config.ROUND_TRIP_FEE_USDT,
    ) -> SignalPlan | None:
        self.last_reject = RejectInfo(symbol.upper(), "", "")
        try:
            s4h = snapshot(candles_4h, swing_lookback=config.SWING_LOOKBACK_4H, slope_lookback=config.EMA_SLOPE_LOOKBACK)
            s1h = snapshot(candles_1h, swing_lookback=config.SWING_LOOKBACK_1H, slope_lookback=config.EMA_SLOPE_LOOKBACK)
        except Exception as exc:
            self._set_reject(symbol, "INDICATORS", "اندیکاتورها/کندل‌ها کامل نیستند", str(exc))
            return None

        d4 = self._direction_4h(s4h)
        d1 = self._direction_1h(s1h)
        if d4.direction is None or d1.direction is None:
            details = (
                f"4H={self._dir_label(d4.direction)}, 1H={self._dir_label(d1.direction)} | "
                f"{self._snapshot_summary('4H', s4h)} | {self._snapshot_summary('1H', s1h)}"
            )
            self._set_reject(symbol, "DIRECTION", "جهت 4H یا 1H واضح نیست", details)
            return None
        if d4.direction != d1.direction:
            details = (
                f"4H={self._dir_label(d4.direction)} ولی 1H={self._dir_label(d1.direction)} | "
                "شرط ورود: 4H و 1H باید هم‌جهت باشند"
            )
            self._set_reject(symbol, "DIRECTION", "4H و 1H هم‌جهت نیستند", details)
            return None

        direction: Direction = d1.direction
        reject_reason = self._hard_reject_reason(direction, s1h, candles_1h)
        if reject_reason:
            self._set_reject(symbol, "FILTER", reject_reason, self._snapshot_summary("1H", s1h))
            return None

        score, reasons = self._score(direction, s4h, s1h, candles_1h)
        if score < self.min_score:
            self._set_reject(symbol, "SCORE", "امتیاز کمتر از حد ورود است", f"score={score:.1f} < min={self.min_score:.1f}")
            return None

        entry = s1h.close
        sl = self._make_sl_1h(direction, s1h, entry)
        if sl <= 0 or sl == entry:
            self._set_reject(symbol, "SL", "استاپ 1H معتبر ساخته نشد", f"entry={entry:g}, sl={sl:g}")
            return None
        risk = entry - sl if direction == "LONG" else sl - entry
        if risk <= 0:
            self._set_reject(symbol, "SL", "ریسک/استاپ منفی یا صفر است", f"direction={direction}, entry={entry:g}, sl={sl:g}, risk={risk:g}")
            return None
        if s1h.atr <= 0:
            self._set_reject(symbol, "ATR", "ATR فعال/معتبر نیست", f"ATR={s1h.atr:g}")
            return None

        risk_atr = risk / s1h.atr
        if risk_atr < float(config.MIN_1H_RISK_ATR):
            self._set_reject(symbol, "SL", "استاپ 1H زیادی تنگ است", f"risk={risk_atr:.2f}ATR < min={float(config.MIN_1H_RISK_ATR):.2f}ATR")
            return None
        if risk_atr > float(config.MAX_1H_RISK_ATR):
            self._set_reject(symbol, "SL", "استاپ 1H زیادی بزرگ است / ورود دیر است", f"risk={risk_atr:.2f}ATR > max={float(config.MAX_1H_RISK_ATR):.2f}ATR")
            return None

        sl_pct = risk / entry
        if sl_pct > float(config.MAX_1H_SL_PCT):
            self._set_reject(symbol, "SL", "فاصله SL درصدی بیش از حد مجاز است", f"sl_pct={sl_pct*100:.2f}% > max={float(config.MAX_1H_SL_PCT)*100:.2f}%")
            return None

        rr = self._risk_reward(score, direction, s1h)
        strength = "قوی" if score >= self.strong_score else "معمولی"
        tp = entry + risk * rr if direction == "LONG" else entry - risk * rr
        tp_pct = abs(tp - entry) / entry

        notional = max(0.0, float(margin_usdt)) * max(1, int(leverage))
        gross_profit = notional * tp_pct
        gross_loss = notional * sl_pct
        net_profit = gross_profit - float(round_trip_fee_usdt)

        final_reasons = list(reasons)
        final_reasons.append(f"SL 1H: پشت ساختار با بافر ATR | risk={risk_atr:.2f} ATR")
        final_reasons.append(f"TP: {rr:g}R بر اساس استاپ 1H")

        return SignalPlan(
            symbol=symbol.upper(),
            okx_symbol=okx_swap_symbol(symbol),
            toobit_symbol=(toobit_symbol or symbol).upper(),
            direction=direction,
            score=round(score, 2),
            strength=strength,
            entry_price=float(entry),
            tp_price=float(tp),
            sl_price=float(sl),
            risk_reward=float(rr),
            sl_pct=float(sl_pct),
            tp_pct=float(tp_pct),
            estimated_profit_usdt=float(gross_profit),
            estimated_loss_usdt=float(gross_loss),
            estimated_net_profit_usdt=float(net_profit),
            round_trip_fee_usdt=float(round_trip_fee_usdt),
            reasons=tuple(final_reasons),
        )

    def _direction_4h(self, s: Snapshot) -> DirectionScore:
        reasons: list[str] = []
        if s.close > s.ema200 and s.ema50 > s.ema200 and s.ema50 >= s.ema50_lookback:
            reasons.append("4H جهت مادر صعودی: قیمت و EMA50 بالای EMA200")
            return DirectionScore("LONG", 25.0, tuple(reasons))
        if s.close < s.ema200 and s.ema50 < s.ema200 and s.ema50 <= s.ema50_lookback:
            reasons.append("4H جهت مادر نزولی: قیمت و EMA50 زیر EMA200")
            return DirectionScore("SHORT", 25.0, tuple(reasons))
        return DirectionScore(None, 0.0, tuple(reasons))

    def _direction_1h(self, s: Snapshot) -> DirectionScore:
        reasons: list[str] = []
        if s.close > s.ema200 and s.ema50 > s.ema200 and s.ema50 > s.ema50_lookback and s.plus_di > s.minus_di:
            reasons.append("1H جهت صعودی: EMAها و DMI همسو")
            return DirectionScore("LONG", 25.0, tuple(reasons))
        if s.close < s.ema200 and s.ema50 < s.ema200 and s.ema50 < s.ema50_lookback and s.minus_di > s.plus_di:
            reasons.append("1H جهت نزولی: EMAها و DMI همسو")
            return DirectionScore("SHORT", 25.0, tuple(reasons))
        return DirectionScore(None, 0.0, tuple(reasons))

    def _hard_reject_reason(self, direction: Direction, s: Snapshot, candles: list[Candle]) -> str | None:
        if s.atr <= 0:
            return f"ATR فعال/معتبر نیست | ATR={s.atr:g}"
        if s.adx < float(config.MIN_TREND_ADX):
            return f"ADX پایین است / بازار روند کافی ندارد | ADX={s.adx:.1f} < {float(config.MIN_TREND_ADX):.1f}"
        ema_slope_atr = abs(s.ema50 - s.ema50_lookback) / s.atr if s.atr > 0 else 0.0
        if ema_slope_atr < float(config.FLAT_EMA_ATR_MULT):
            return f"EMA50 صاف است / احتمال رنج | EMA50_slope={ema_slope_atr:.2f}ATR < {float(config.FLAT_EMA_ATR_MULT):.2f}ATR"
        if min(s.ema50, s.ema200) <= s.close <= max(s.ema50, s.ema200):
            return f"قیمت بین EMA50 و EMA200 است / ناحیه رنج یا وسط بازار | close={s.close:g}"
        late_reason = self._late_reason(direction, s, candles)
        if late_reason:
            return late_reason
        pullback_reason = self._pullback_reject_reason(direction, s, candles)
        if pullback_reason:
            return pullback_reason
        return None

    def _late_reason(self, direction: Direction, s: Snapshot, candles: list[Candle]) -> str | None:
        if s.atr <= 0:
            return "ATR فعال/معتبر نیست"
        recent = candles[-6:]
        bullish = sum(1 for c in recent if c.close > c.open)
        bearish = sum(1 for c in recent if c.close < c.open)
        if direction == "LONG":
            dist20 = (s.close - s.ema20) / s.atr
            dist50 = (s.close - s.ema50) / s.atr
            if dist20 > float(config.MAX_DISTANCE_EMA20_ATR):
                return f"ورود دیر است: فاصله قیمت از EMA20 زیاد است | dist20={dist20:.2f}ATR"
            if dist50 > float(config.MAX_DISTANCE_EMA50_ATR):
                return f"ورود دیر است: فاصله قیمت از EMA50 زیاد است | dist50={dist50:.2f}ATR"
            if bullish >= 5:
                return f"ورود دیر است: {bullish}/6 کندل اخیر صعودی بوده"
            if s.adx >= float(config.EXHAUSTION_ADX) and s.adx < s.prev_adx:
                return f"احتمال انتهای موج: ADX بالا ولی رو به افت | ADX={s.adx:.1f}, prev={s.prev_adx:.1f}"
        else:
            dist20 = (s.ema20 - s.close) / s.atr
            dist50 = (s.ema50 - s.close) / s.atr
            if dist20 > float(config.MAX_DISTANCE_EMA20_ATR):
                return f"ورود دیر است: فاصله قیمت از EMA20 زیاد است | dist20={dist20:.2f}ATR"
            if dist50 > float(config.MAX_DISTANCE_EMA50_ATR):
                return f"ورود دیر است: فاصله قیمت از EMA50 زیاد است | dist50={dist50:.2f}ATR"
            if bearish >= 5:
                return f"ورود دیر است: {bearish}/6 کندل اخیر نزولی بوده"
            if s.adx >= float(config.EXHAUSTION_ADX) and s.adx < s.prev_adx:
                return f"احتمال انتهای موج: ADX بالا ولی رو به افت | ADX={s.adx:.1f}, prev={s.prev_adx:.1f}"
        return None

    def _pullback_reject_reason(self, direction: Direction, s: Snapshot, candles: list[Candle]) -> str | None:
        lookback = max(2, int(config.PULLBACK_LOOKBACK_1H))
        recent = candles[-lookback:]
        buffer = float(config.PULLBACK_ATR_BUFFER) * s.atr
        if direction == "LONG":
            touched_zone = any(c.low <= s.ema20 + buffer for c in recent) or any(c.low <= s.ema50 + buffer for c in recent)
            respected_ema50 = min(c.close for c in recent) >= s.ema50 - buffer
            trigger = s.close >= s.ema20 and candles[-1].close > candles[-1].open
            if not touched_zone:
                return "پولبک معتبر نداریم: قیمت به محدوده EMA20/EMA50 برنگشته"
            if not respected_ema50:
                return "پولبک خراب شد: کلوز زیر EMA50 آمده"
            if not trigger:
                return "تریگر ورود لانگ کامل نیست: کندل برگشتی/کلوز بالای EMA20 نداریم"
            return None
        touched_zone = any(c.high >= s.ema20 - buffer for c in recent) or any(c.high >= s.ema50 - buffer for c in recent)
        respected_ema50 = max(c.close for c in recent) <= s.ema50 + buffer
        trigger = s.close <= s.ema20 and candles[-1].close < candles[-1].open
        if not touched_zone:
            return "پولبک معتبر نداریم: قیمت به محدوده EMA20/EMA50 برنگشته"
        if not respected_ema50:
            return "پولبک خراب شد: کلوز بالای EMA50 آمده"
        if not trigger:
            return "تریگر ورود شورت کامل نیست: کندل برگشتی/کلوز زیر EMA20 نداریم"
        return None

    def _score(self, direction: Direction, s4h: Snapshot, s1h: Snapshot, candles_1h: list[Candle]) -> tuple[float, list[str]]:
        score = 0.0
        reasons: list[str] = []

        # 25: multi-timeframe direction alignment.
        if direction == "LONG" and s4h.close > s4h.ema200 and s1h.close > s1h.ema200:
            score += 25
            reasons.append("25 امتیاز: جهت 4H و 1H صعودی همسو")
        elif direction == "SHORT" and s4h.close < s4h.ema200 and s1h.close < s1h.ema200:
            score += 25
            reasons.append("25 امتیاز: جهت 4H و 1H نزولی همسو")

        # 20: trend strength.
        if direction == "LONG" and s1h.plus_di > s1h.minus_di:
            if s1h.adx >= float(config.STRONG_TREND_ADX) and s1h.adx >= s1h.prev_adx:
                score += 20
                reasons.append("20 امتیاز: ADX/DMI روند صعودی قوی")
            elif s1h.adx >= float(config.MIN_TREND_ADX):
                score += 16
                reasons.append("16 امتیاز: ADX/DMI روند صعودی قابل قبول")
        elif direction == "SHORT" and s1h.minus_di > s1h.plus_di:
            if s1h.adx >= float(config.STRONG_TREND_ADX) and s1h.adx >= s1h.prev_adx:
                score += 20
                reasons.append("20 امتیاز: ADX/DMI روند نزولی قوی")
            elif s1h.adx >= float(config.MIN_TREND_ADX):
                score += 16
                reasons.append("16 امتیاز: ADX/DMI روند نزولی قابل قبول")

        # 15: no-range quality.
        ema_slope_atr = abs(s1h.ema50 - s1h.ema50_lookback) / s1h.atr if s1h.atr > 0 else 0.0
        if s1h.adx >= float(config.MIN_TREND_ADX) and ema_slope_atr >= float(config.FLAT_EMA_ATR_MULT):
            score += 15
            reasons.append("15 امتیاز: فیلتر رنج پاس شد")

        # 15: pullback quality.
        if self._has_pullback_trigger(direction, s1h, candles_1h):
            if direction == "LONG" and s1h.close >= s1h.ema20:
                score += 15
                reasons.append("15 امتیاز: پولبک به EMA20/EMA50 و برگشت صعودی")
            elif direction == "SHORT" and s1h.close <= s1h.ema20:
                score += 15
                reasons.append("15 امتیاز: پولبک به EMA20/EMA50 و برگشت نزولی")
            else:
                score += 11
                reasons.append("11 امتیاز: پولبک معتبر ولی تریگر نرم")

        # 10: entry candle quality.
        last = candles_1h[-1]
        candle_range = max(last.high - last.low, 1e-12)
        body = abs(last.close - last.open)
        body_ratio = body / candle_range
        if direction == "LONG" and last.close > last.open and body_ratio >= float(config.MIN_ENTRY_BODY_RATIO):
            score += 10
            reasons.append("10 امتیاز: کندل ورود صعودی و بدنه کافی")
        elif direction == "SHORT" and last.close < last.open and body_ratio >= float(config.MIN_ENTRY_BODY_RATIO):
            score += 10
            reasons.append("10 امتیاز: کندل ورود نزولی و بدنه کافی")
        elif body_ratio >= 0.35:
            score += 6
            reasons.append("6 امتیاز: کندل ورود قابل قبول")

        # 10: not-late filter.
        if not self._is_late(direction, s1h, candles_1h):
            score += 10
            reasons.append("10 امتیاز: ورود دیر نیست")

        # 5: ATR/SL quality.
        preliminary_sl = self._make_sl_1h(direction, s1h, s1h.close)
        risk = (s1h.close - preliminary_sl) if direction == "LONG" else (preliminary_sl - s1h.close)
        risk_atr = risk / s1h.atr if s1h.atr > 0 else 99.0
        if float(config.MIN_1H_RISK_ATR) <= risk_atr <= float(config.MAX_1H_RISK_ATR):
            score += 5
            reasons.append("5 امتیاز: استاپ 1H از نظر ATR منطقی")

        return clamp(score, 0, 100), reasons

    def _has_pullback_trigger(self, direction: Direction, s: Snapshot, candles: list[Candle]) -> bool:
        lookback = max(2, int(config.PULLBACK_LOOKBACK_1H))
        recent = candles[-lookback:]
        buffer = float(config.PULLBACK_ATR_BUFFER) * s.atr
        if direction == "LONG":
            touched_zone = any(c.low <= s.ema20 + buffer for c in recent) or any(c.low <= s.ema50 + buffer for c in recent)
            respected_ema50 = min(c.close for c in recent) >= s.ema50 - buffer
            trigger = s.close >= s.ema20 and candles[-1].close > candles[-1].open
            return touched_zone and respected_ema50 and trigger
        touched_zone = any(c.high >= s.ema20 - buffer for c in recent) or any(c.high >= s.ema50 - buffer for c in recent)
        respected_ema50 = max(c.close for c in recent) <= s.ema50 + buffer
        trigger = s.close <= s.ema20 and candles[-1].close < candles[-1].open
        return touched_zone and respected_ema50 and trigger

    def _is_late(self, direction: Direction, s: Snapshot, candles: list[Candle]) -> bool:
        if s.atr <= 0:
            return True
        recent = candles[-6:]
        bullish = sum(1 for c in recent if c.close > c.open)
        bearish = sum(1 for c in recent if c.close < c.open)
        if direction == "LONG":
            too_far_20 = (s.close - s.ema20) > float(config.MAX_DISTANCE_EMA20_ATR) * s.atr
            too_far_50 = (s.close - s.ema50) > float(config.MAX_DISTANCE_EMA50_ATR) * s.atr
            too_many = bullish >= 5
            adx_exhaustion = s.adx >= float(config.EXHAUSTION_ADX) and s.adx < s.prev_adx
            return too_far_20 or too_far_50 or too_many or adx_exhaustion
        too_far_20 = (s.ema20 - s.close) > float(config.MAX_DISTANCE_EMA20_ATR) * s.atr
        too_far_50 = (s.ema50 - s.close) > float(config.MAX_DISTANCE_EMA50_ATR) * s.atr
        too_many = bearish >= 5
        adx_exhaustion = s.adx >= float(config.EXHAUSTION_ADX) and s.adx < s.prev_adx
        return too_far_20 or too_far_50 or too_many or adx_exhaustion

    def _make_sl_1h(self, direction: Direction, s: Snapshot, entry: float) -> float:
        buffer = float(config.ATR_SL_BUFFER_MULT) * float(s.atr)
        if direction == "LONG":
            return max(0.0, min(float(s.swing_low), float(s.ema50)) - buffer)
        return max(float(s.swing_high), float(s.ema50)) + buffer

    def _risk_reward(self, score: float, direction: Direction, s: Snapshot) -> float:
        if score >= self.strong_score and s.adx >= float(config.STRONG_TREND_ADX) and s.adx >= s.prev_adx:
            return float(config.RR_STRONG)
        return float(config.RR_NORMAL)
