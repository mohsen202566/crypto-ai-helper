from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import config
from indicators import Snapshot, snapshot
from okx_data import Candle
from utils import clamp, okx_swap_symbol

Direction = Literal["LONG", "SHORT"]
MarketState = Literal["LONG", "SHORT", "RANGE"]


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
    direction: MarketState
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

    دقیق‌سازی نسخه فعلی:
    - 1H تایم اصلی ورود است.
    - 4H فقط فیلتر مادر است؛ اگر خلاف 1H باشد رد کامل، اگر رنج باشد با 1H قوی اجازه سیگنال می‌دهد.
    - Real و Normal هیچ تفاوتی در منطق سیگنال ندارند؛ فقط اجرای واقعی جداگانه تصمیم‌گیری می‌شود.
    - SL/TP فقط از ساختار و ATR تایم 1H ساخته می‌شود.
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
    def _dir_label(direction: MarketState | str | None) -> str:
        if direction == "LONG":
            return "صعودی"
        if direction == "SHORT":
            return "نزولی"
        if direction == "RANGE":
            return "رنج/خنثی"
        return "نامشخص"

    @staticmethod
    def _snapshot_summary(prefix: str, s: Snapshot) -> str:
        ema_slope = s.ema50 - s.ema50_lookback
        return (
            f"{prefix}: close={s.close:g}, EMA20={s.ema20:g}, EMA50={s.ema50:g}, EMA200={s.ema200:g}, "
            f"EMA50_slope={ema_slope:g}, ADX={s.adx:.1f}, prevADX={s.prev_adx:.1f}, "
            f"+DI={s.plus_di:.1f}, -DI={s.minus_di:.1f}, ATR={s.atr:g}"
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

        # 1H تایم ورود است؛ اگر خود 1H رنج باشد سیگنال نمی‌دهیم.
        if d1.direction == "RANGE":
            details = (
                f"4H={self._dir_label(d4.direction)}, 1H={self._dir_label(d1.direction)} | "
                f"1H_reason={'; '.join(d1.reasons) or 'بدون توضیح'} | "
                f"{self._snapshot_summary('1H', s1h)}"
            )
            self._set_reject(symbol, "DIRECTION", "1H رنج/خنثی است؛ ورود 1H نداریم", details)
            return None

        direction: Direction = d1.direction  # type: ignore[assignment]

        # 4H فقط نباید خلاف جهت 1H باشد. 4H رنج/انتقالی قابل قبول است ولی امتیاز کمتری می‌گیرد.
        if d4.direction != "RANGE" and d4.direction != direction:
            details = (
                f"4H={self._dir_label(d4.direction)} ولی 1H={self._dir_label(d1.direction)} | "
                "قانون: معامله خلاف جهت مادر 4H ممنوع است"
            )
            self._set_reject(symbol, "DIRECTION", "4H خلاف جهت 1H است", details)
            return None

        reject_reason = self._hard_reject_reason(direction, s1h, candles_1h)
        if reject_reason:
            self._set_reject(symbol, "FILTER", reject_reason, self._snapshot_summary("1H", s1h))
            return None

        score, reasons = self._score(direction, d4.direction, s4h, s1h, candles_1h)
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
        if min(s.ema50, s.ema200) <= s.close <= max(s.ema50, s.ema200):
            reasons.append("4H رنج/انتقالی: قیمت بین EMA50 و EMA200")
            return DirectionScore("RANGE", 0.0, tuple(reasons))
        if s.close > s.ema200 and s.ema50 > s.ema200:
            reasons.append("4H صعودی: قیمت و EMA50 بالای EMA200")
            return DirectionScore("LONG", 25.0, tuple(reasons))
        if s.close < s.ema200 and s.ema50 < s.ema200:
            reasons.append("4H نزولی: قیمت و EMA50 زیر EMA200")
            return DirectionScore("SHORT", 25.0, tuple(reasons))
        reasons.append("4H رنج/خنثی: قیمت و EMA50 نسبت به EMA200 همسو نیستند")
        return DirectionScore("RANGE", 0.0, tuple(reasons))

    def _direction_1h(self, s: Snapshot) -> DirectionScore:
        reasons: list[str] = []
        if min(s.ema50, s.ema200) <= s.close <= max(s.ema50, s.ema200):
            reasons.append("1H قیمت بین EMA50 و EMA200 است؛ محدوده رنج/انتقالی")
            return DirectionScore("RANGE", 0.0, tuple(reasons))

        ema50_up = s.ema50 > s.ema50_lookback
        ema50_down = s.ema50 < s.ema50_lookback
        early_long = s.close > s.ema200 and s.ema20 > s.ema50 and ema50_up
        early_short = s.close < s.ema200 and s.ema20 < s.ema50 and ema50_down

        if s.close > s.ema200 and (s.ema50 > s.ema200 or early_long):
            if s.ema50 > s.ema200:
                reasons.append("1H صعودی: قیمت و EMA50 بالای EMA200")
            else:
                reasons.append("1H صعودی زودهنگام: قیمت بالای EMA200، EMA20 بالای EMA50 و شیب EMA50 مثبت")
            return DirectionScore("LONG", 25.0, tuple(reasons))
        if s.close < s.ema200 and (s.ema50 < s.ema200 or early_short):
            if s.ema50 < s.ema200:
                reasons.append("1H نزولی: قیمت و EMA50 زیر EMA200")
            else:
                reasons.append("1H نزولی زودهنگام: قیمت زیر EMA200، EMA20 زیر EMA50 و شیب EMA50 منفی")
            return DirectionScore("SHORT", 25.0, tuple(reasons))
        reasons.append("1H رنج/خنثی: ساختار EMAها برای ورود روندی کامل نیست")
        return DirectionScore("RANGE", 0.0, tuple(reasons))

    def _adx_is_acceptable(self, s: Snapshot) -> bool:
        if s.adx >= 20.0:
            return True
        return s.adx >= float(config.MIN_TREND_ADX) and s.adx >= s.prev_adx

    def _hard_reject_reason(self, direction: Direction, s: Snapshot, candles: list[Candle]) -> str | None:
        if s.atr <= 0:
            return f"ATR فعال/معتبر نیست | ATR={s.atr:g}"
        if s.adx < float(getattr(config, "HARD_RANGE_ADX", 14.0)):
            return f"ADX خیلی پایین است / بازار رنج و نویزی است | ADX={s.adx:.1f} < {float(getattr(config, 'HARD_RANGE_ADX', 14.0)):.1f}"
        if not self._adx_is_acceptable(s):
            return f"ADX هنوز قدرت کافی ندارد | ADX={s.adx:.1f}, prevADX={s.prev_adx:.1f}"
        if direction == "LONG" and s.plus_di <= s.minus_di:
            return f"DMI با جهت صعودی همسو نیست | +DI={s.plus_di:.1f} <= -DI={s.minus_di:.1f}"
        if direction == "SHORT" and s.minus_di <= s.plus_di:
            return f"DMI با جهت نزولی همسو نیست | -DI={s.minus_di:.1f} <= +DI={s.plus_di:.1f}"
        ema_slope_atr = abs(s.ema50 - s.ema50_lookback) / s.atr if s.atr > 0 else 0.0
        if ema_slope_atr < float(config.FLAT_EMA_ATR_MULT) and s.adx < 20.0:
            return f"EMA50 صاف است و ADX هم قوی نیست | EMA50_slope={ema_slope_atr:.2f}ATR, ADX={s.adx:.1f}"
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
        max_same = max(5, int(getattr(config, "MAX_SAME_DIRECTION_CANDLES", 6)))
        if direction == "LONG":
            dist20 = (s.close - s.ema20) / s.atr
            dist50 = (s.close - s.ema50) / s.atr
            if dist20 > float(config.MAX_DISTANCE_EMA20_ATR):
                return f"ورود دیر است: فاصله قیمت از EMA20 زیاد است | dist20={dist20:.2f}ATR"
            if dist50 > float(config.MAX_DISTANCE_EMA50_ATR):
                return f"ورود دیر است: فاصله قیمت از EMA50 زیاد است | dist50={dist50:.2f}ATR"
            if bullish >= max_same:
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
            if bearish >= max_same:
                return f"ورود دیر است: {bearish}/6 کندل اخیر نزولی بوده"
            if s.adx >= float(config.EXHAUSTION_ADX) and s.adx < s.prev_adx:
                return f"احتمال انتهای موج: ADX بالا ولی رو به افت | ADX={s.adx:.1f}, prev={s.prev_adx:.1f}"
        return None

    def _pullback_state(self, direction: Direction, s: Snapshot, candles: list[Candle]) -> tuple[bool, str]:
        lookback = max(2, int(config.PULLBACK_LOOKBACK_1H))
        recent = candles[-lookback:]
        last = candles[-1]
        prev = candles[-2] if len(candles) >= 2 else last
        buffer = float(config.PULLBACK_ATR_BUFFER) * s.atr

        if direction == "LONG":
            touched_zone = any(c.low <= s.ema20 + buffer for c in recent) or any(c.low <= s.ema50 + buffer for c in recent)
            near_ema20 = abs(s.close - s.ema20) <= buffer and s.close >= s.ema20 - buffer
            shallow_pullback = any(c.close < c.open for c in recent[:-1]) and s.close >= s.ema20
            respected_ema50 = min(c.close for c in recent) >= s.ema50 - buffer
            trigger = (last.close > last.open and last.close >= s.ema20 - buffer) or (last.close > prev.high and s.close >= s.ema20 - buffer)
            if not (touched_zone or near_ema20 or shallow_pullback):
                return False, "پولبک معتبر نداریم: قیمت به محدوده EMA20/EMA50 یا نزدیکی EMA20 برنگشته"
            if not respected_ema50:
                return False, "پولبک خراب شد: کلوز زیر EMA50 آمده"
            if not trigger:
                return False, "تریگر ورود لانگ کامل نیست: کندل برگشتی یا شکست high نداریم"
            return True, "پولبک/اصلاح 1H معتبر و تریگر لانگ فعال است"

        touched_zone = any(c.high >= s.ema20 - buffer for c in recent) or any(c.high >= s.ema50 - buffer for c in recent)
        near_ema20 = abs(s.close - s.ema20) <= buffer and s.close <= s.ema20 + buffer
        shallow_pullback = any(c.close > c.open for c in recent[:-1]) and s.close <= s.ema20
        respected_ema50 = max(c.close for c in recent) <= s.ema50 + buffer
        trigger = (last.close < last.open and last.close <= s.ema20 + buffer) or (last.close < prev.low and s.close <= s.ema20 + buffer)
        if not (touched_zone or near_ema20 or shallow_pullback):
            return False, "پولبک معتبر نداریم: قیمت به محدوده EMA20/EMA50 یا نزدیکی EMA20 برنگشته"
        if not respected_ema50:
            return False, "پولبک خراب شد: کلوز بالای EMA50 آمده"
        if not trigger:
            return False, "تریگر ورود شورت کامل نیست: کندل برگشتی یا شکست low نداریم"
        return True, "پولبک/اصلاح 1H معتبر و تریگر شورت فعال است"

    def _pullback_reject_reason(self, direction: Direction, s: Snapshot, candles: list[Candle]) -> str | None:
        ok, reason = self._pullback_state(direction, s, candles)
        return None if ok else reason

    def _score(self, direction: Direction, mother_state: MarketState, s4h: Snapshot, s1h: Snapshot, candles_1h: list[Candle]) -> tuple[float, list[str]]:
        score = 0.0
        reasons: list[str] = []

        # 25/18: 4H is mother filter; RANGE is allowed but with lower confidence.
        if mother_state == direction:
            score += 25
            reasons.append(f"25 امتیاز: 4H و 1H هر دو {self._dir_label(direction)} هستند")
        elif mother_state == "RANGE":
            score += 18
            reasons.append(f"18 امتیاز: 4H رنج/انتقالی است ولی خلاف 1H نیست؛ 1H {self._dir_label(direction)} است")

        # 20: trend strength on 1H.
        di_aligned = (direction == "LONG" and s1h.plus_di > s1h.minus_di) or (direction == "SHORT" and s1h.minus_di > s1h.plus_di)
        if di_aligned:
            if s1h.adx >= float(config.STRONG_TREND_ADX) and s1h.adx >= s1h.prev_adx:
                score += 20
                reasons.append("20 امتیاز: ADX/DMI روند 1H قوی و همسو")
            elif s1h.adx >= 20.0:
                score += 18
                reasons.append("18 امتیاز: ADX/DMI روند 1H قابل قبول")
            elif s1h.adx >= float(config.MIN_TREND_ADX) and s1h.adx >= s1h.prev_adx:
                score += 15
                reasons.append("15 امتیاز: شروع روند 1H؛ ADX پایین‌تر ولی رو به افزایش و DMI همسو")

        # 15: no-range quality.
        ema_slope_atr = abs(s1h.ema50 - s1h.ema50_lookback) / s1h.atr if s1h.atr > 0 else 0.0
        if s1h.adx >= 20.0 and ema_slope_atr >= float(config.FLAT_EMA_ATR_MULT):
            score += 15
            reasons.append("15 امتیاز: فیلتر رنج پاس شد")
        elif s1h.adx >= float(config.MIN_TREND_ADX) and s1h.adx >= s1h.prev_adx and ema_slope_atr >= float(config.FLAT_EMA_ATR_MULT) * 0.70:
            score += 12
            reasons.append("12 امتیاز: فیلتر رنج نرم پاس شد؛ ADX رو به رشد و EMA50 کافی")
        elif ema_slope_atr >= float(config.FLAT_EMA_ATR_MULT) * 0.50 and di_aligned:
            score += 8
            reasons.append("8 امتیاز: بازار کاملاً فلت نیست و DMI همسو است")

        # 15: pullback quality.
        pullback_ok, pullback_reason = self._pullback_state(direction, s1h, candles_1h)
        if pullback_ok:
            score += 15
            reasons.append(f"15 امتیاز: {pullback_reason}")

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
        elif body_ratio >= 0.30:
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
        ok, _reason = self._pullback_state(direction, s, candles)
        return ok

    def _is_late(self, direction: Direction, s: Snapshot, candles: list[Candle]) -> bool:
        return self._late_reason(direction, s, candles) is not None

    def _make_sl_1h(self, direction: Direction, s: Snapshot, entry: float) -> float:
        buffer = float(config.ATR_SL_BUFFER_MULT) * float(s.atr)
        if direction == "LONG":
            return max(0.0, min(float(s.swing_low), float(s.ema50)) - buffer)
        return max(float(s.swing_high), float(s.ema50)) + buffer

    def _risk_reward(self, score: float, direction: Direction, s: Snapshot) -> float:
        if score >= self.strong_score and s.adx >= float(config.STRONG_TREND_ADX) and s.adx >= s.prev_adx:
            return float(config.RR_STRONG)
        return float(config.RR_NORMAL)
