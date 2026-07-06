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


class Simple4HStrategy:
    """Simple score-based 4H strategy.

    Hard rules:
    - 1D and 4H must align.
    - score >= 70 emits a signal.
    - 70..84 => RR 1.5.
    - 85+ => RR 2.
    - SL is based on 4H swing/ATR, not 5m/1H.
    - No support/resistance filter, no AI, no DCA, no martingale.
    """

    def __init__(self) -> None:
        self.min_score = float(config.SIGNAL_SCORE_THRESHOLD)
        self.strong_score = float(config.STRONG_SCORE_THRESHOLD)

    def analyze(
        self,
        symbol: str,
        candles_1d: list[Candle],
        candles_4h: list[Candle],
        *,
        margin_usdt: float,
        leverage: int,
        toobit_symbol: str | None = None,
        round_trip_fee_usdt: float = config.ROUND_TRIP_FEE_USDT,
    ) -> SignalPlan | None:
        s1d = snapshot(candles_1d, swing_lookback=8)
        s4h = snapshot(candles_4h, swing_lookback=config.SWING_LOOKBACK_4H)

        d1 = self._direction_1d(s1d)
        d4 = self._direction_4h(s4h)
        if d1.direction is None or d4.direction is None:
            return None
        if d1.direction != d4.direction:
            return None

        direction: Direction = d1.direction
        score, reasons = self._score(direction, s1d, s4h)
        if score < self.min_score:
            return None

        rr = float(config.RR_STRONG if score >= self.strong_score else config.RR_NORMAL)
        strength = "قوی" if score >= self.strong_score else "معمولی"
        entry = s4h.close
        sl = self._make_sl(direction, s4h, entry)
        if sl <= 0 or sl == entry:
            return None
        risk = entry - sl if direction == "LONG" else sl - entry
        if risk <= 0:
            return None
        sl_pct = risk / entry
        # 4H guard rails: neither scalpy/tiny nor huge.
        if sl_pct > float(config.MAX_4H_SL_PCT):
            return None
        if sl_pct < float(config.MIN_4H_SL_PCT):
            risk = entry * float(config.MIN_4H_SL_PCT)
            sl = entry - risk if direction == "LONG" else entry + risk
            sl_pct = risk / entry

        tp = entry + risk * rr if direction == "LONG" else entry - risk * rr
        tp_pct = abs(tp - entry) / entry
        notional = max(0.0, float(margin_usdt)) * max(1, int(leverage))
        gross_profit = notional * tp_pct
        gross_loss = notional * sl_pct
        net_profit = gross_profit - float(round_trip_fee_usdt)

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
            risk_reward=rr,
            sl_pct=float(sl_pct),
            tp_pct=float(tp_pct),
            estimated_profit_usdt=float(gross_profit),
            estimated_loss_usdt=float(gross_loss),
            estimated_net_profit_usdt=float(net_profit),
            round_trip_fee_usdt=float(round_trip_fee_usdt),
            reasons=tuple(reasons),
        )

    def _direction_1d(self, s: Snapshot) -> DirectionScore:
        long_votes = 0
        short_votes = 0
        reasons: list[str] = []
        if s.close > s.ema200:
            long_votes += 1
            reasons.append("1D قیمت بالای EMA200")
        if s.ema50 > s.ema200:
            long_votes += 1
            reasons.append("1D EMA50 بالای EMA200")
        if s.rsi > 50:
            long_votes += 1
            reasons.append("1D RSI بالای 50")
        if s.close < s.ema200:
            short_votes += 1
            reasons.append("1D قیمت زیر EMA200")
        if s.ema50 < s.ema200:
            short_votes += 1
            reasons.append("1D EMA50 زیر EMA200")
        if s.rsi < 50:
            short_votes += 1
            reasons.append("1D RSI زیر 50")
        if long_votes >= 2 and long_votes > short_votes:
            return DirectionScore("LONG", 25.0, tuple(reasons))
        if short_votes >= 2 and short_votes > long_votes:
            return DirectionScore("SHORT", 25.0, tuple(reasons))
        return DirectionScore(None, 0.0, tuple(reasons))

    def _direction_4h(self, s: Snapshot) -> DirectionScore:
        long_votes = 0
        short_votes = 0
        reasons: list[str] = []
        if s.close > s.ema200:
            long_votes += 1
            reasons.append("4H قیمت بالای EMA200")
        if s.ema50 > s.ema200:
            long_votes += 1
            reasons.append("4H EMA50 بالای EMA200")
        if s.rsi > 50:
            long_votes += 1
            reasons.append("4H RSI بالای 50")
        if s.close < s.ema200:
            short_votes += 1
            reasons.append("4H قیمت زیر EMA200")
        if s.ema50 < s.ema200:
            short_votes += 1
            reasons.append("4H EMA50 زیر EMA200")
        if s.rsi < 50:
            short_votes += 1
            reasons.append("4H RSI زیر 50")
        if long_votes >= 2 and long_votes > short_votes:
            return DirectionScore("LONG", 25.0, tuple(reasons))
        if short_votes >= 2 and short_votes > long_votes:
            return DirectionScore("SHORT", 25.0, tuple(reasons))
        return DirectionScore(None, 0.0, tuple(reasons))

    def _score(self, direction: Direction, s1d: Snapshot, s4h: Snapshot) -> tuple[float, list[str]]:
        score = 0.0
        reasons: list[str] = []
        # 1D alignment: 25
        if direction == "LONG" and s1d.close > s1d.ema200 and s1d.ema50 > s1d.ema200:
            score += 25
            reasons.append("25 امتیاز: جهت روزانه صعودی")
        elif direction == "SHORT" and s1d.close < s1d.ema200 and s1d.ema50 < s1d.ema200:
            score += 25
            reasons.append("25 امتیاز: جهت روزانه نزولی")
        else:
            score += 18
            reasons.append("18 امتیاز: جهت روزانه نرم/قابل قبول")

        # 4H alignment: 25
        if direction == "LONG" and s4h.close > s4h.ema200 and s4h.ema50 > s4h.ema200:
            score += 25
            reasons.append("25 امتیاز: جهت 4H صعودی")
        elif direction == "SHORT" and s4h.close < s4h.ema200 and s4h.ema50 < s4h.ema200:
            score += 25
            reasons.append("25 امتیاز: جهت 4H نزولی")
        else:
            score += 18
            reasons.append("18 امتیاز: جهت 4H نرم/قابل قبول")

        # 4H EMA quality: 15
        if direction == "LONG":
            if s4h.ema20 > s4h.ema50 > s4h.ema200:
                score += 15
                reasons.append("15 امتیاز: EMA20/50/200 صعودی")
            elif s4h.ema50 > s4h.ema200:
                score += 10
                reasons.append("10 امتیاز: EMA50 بالای EMA200")
        else:
            if s4h.ema20 < s4h.ema50 < s4h.ema200:
                score += 15
                reasons.append("15 امتیاز: EMA20/50/200 نزولی")
            elif s4h.ema50 < s4h.ema200:
                score += 10
                reasons.append("10 امتیاز: EMA50 زیر EMA200")

        # RSI: 10
        if direction == "LONG" and s4h.rsi >= 55:
            score += 10
            reasons.append("10 امتیاز: RSI 4H قوی بالای 55")
        elif direction == "LONG" and s4h.rsi > 50:
            score += 7
            reasons.append("7 امتیاز: RSI 4H بالای 50")
        elif direction == "SHORT" and s4h.rsi <= 45:
            score += 10
            reasons.append("10 امتیاز: RSI 4H قوی زیر 45")
        elif direction == "SHORT" and s4h.rsi < 50:
            score += 7
            reasons.append("7 امتیاز: RSI 4H زیر 50")

        # MACD/momentum: 10
        if direction == "LONG" and s4h.macd_hist > 0 and s4h.macd_hist >= s4h.prev_macd_hist:
            score += 10
            reasons.append("10 امتیاز: MACD 4H مثبت/روبه‌رشد")
        elif direction == "LONG" and s4h.macd_hist > 0:
            score += 7
            reasons.append("7 امتیاز: MACD 4H مثبت")
        elif direction == "SHORT" and s4h.macd_hist < 0 and s4h.macd_hist <= s4h.prev_macd_hist:
            score += 10
            reasons.append("10 امتیاز: MACD 4H منفی/روبه‌رشد نزولی")
        elif direction == "SHORT" and s4h.macd_hist < 0:
            score += 7
            reasons.append("7 امتیاز: MACD 4H منفی")

        # Simple 4H structure: 10. No support/resistance filter.
        if direction == "LONG" and s4h.close > s4h.ema50 and s4h.ema50 >= s4h.prev_ema50:
            score += 10
            reasons.append("10 امتیاز: ساختار ساده 4H صعودی")
        elif direction == "SHORT" and s4h.close < s4h.ema50 and s4h.ema50 <= s4h.prev_ema50:
            score += 10
            reasons.append("10 امتیاز: ساختار ساده 4H نزولی")

        # 4H SL/ATR quality: 5
        atr_pct = s4h.atr / s4h.close if s4h.close > 0 else 0.0
        if float(config.MIN_4H_SL_PCT) <= max(atr_pct, float(config.MIN_4H_SL_PCT)) <= float(config.MAX_4H_SL_PCT):
            score += 5
            reasons.append("5 امتیاز: ATR/SL چهار ساعته منطقی")

        return clamp(score, 0, 100), reasons

    def _make_sl(self, direction: Direction, s: Snapshot, entry: float) -> float:
        atr_stop = float(s.atr) * float(config.ATR_SL_MULT)
        if direction == "LONG":
            swing_stop = max(0.0, entry - float(s.swing_low))
            risk = max(atr_stop, swing_stop, entry * float(config.MIN_4H_SL_PCT))
            risk = min(risk, entry * float(config.MAX_4H_SL_PCT))
            return entry - risk
        swing_stop = max(0.0, float(s.swing_high) - entry)
        risk = max(atr_stop, swing_stop, entry * float(config.MIN_4H_SL_PCT))
        risk = min(risk, entry * float(config.MAX_4H_SL_PCT))
        return entry + risk
