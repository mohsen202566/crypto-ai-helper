from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Literal

import config
from indicators import snapshot
from okx_data import Candle
from utils import clamp, okx_swap_symbol, safe_float

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
            "timeframe": "5M-ICE",
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class FlowSnapshot:
    spread_pct: float
    bid_depth_usdt: float
    ask_depth_usdt: float
    book_imbalance: float
    trade_delta_ratio: float
    cvd_slope: float
    reasons: tuple[str, ...]


class ICE5MStrategy:
    """Imbalance + Compression + Explosion strategy.

    The bot does not wait for a classic pullback and does not use a second TP.
    It waits for 5M compression, reads public OKX order-flow pressure, then enters
    on the first 1M explosion only when the breakout is supported by volume/delta.
    """

    def __init__(self) -> None:
        self.min_score = float(config.SIGNAL_SCORE_THRESHOLD)
        self.strong_score = float(config.STRONG_SCORE_THRESHOLD)
        self.last_reject_reason = ""

    def _reject(self, reason: str) -> None:
        self.last_reject_reason = str(reason)[:500]
        return None

    def analyze(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_5m: list[Candle],
        candles_1m: list[Candle],
        order_book: dict[str, Any] | None,
        trades: list[dict[str, Any]] | None,
        *,
        margin_usdt: float,
        leverage: int,
        min_net_profit_usdt: float,
        toobit_symbol: str | None = None,
        round_trip_fee_usdt: float = config.ROUND_TRIP_FEE_USDT,
    ) -> SignalPlan | None:
        self.last_reject_reason = ""
        if len(candles_5m) < 80:
            return self._reject("رد شد: دیتای 5M کافی نیست")
        if len(candles_1m) < 30:
            return self._reject("رد شد: دیتای 1M کافی نیست")

        s15 = snapshot(candles_15m, swing_lookback=8) if len(candles_15m) >= 80 else None
        s5 = snapshot(candles_5m, swing_lookback=12)
        s1 = snapshot(candles_1m, swing_lookback=8)

        comp = self._compression_box(candles_5m)
        if not comp["ok"]:
            return self._reject("رد شد: فشردگی ICE کامل نیست - " + comp["reason"])

        trigger = self._explosion_trigger(candles_1m, comp)
        if trigger["direction"] is None:
            return self._reject("رد شد: انفجار 1M معتبر نیست - " + trigger["reason"])
        direction: Direction = trigger["direction"]

        anti_late = self._anti_late_reject(direction, candles_1m, comp)
        if anti_late:
            return self._reject("رد شد: ورود دیر شده - " + anti_late)

        flow = self._flow_snapshot(order_book or {}, trades or [], candles_1m)
        flow_gate = self._flow_gate(direction, flow)
        if flow_gate:
            return self._reject("رد شد: اوردرفلو تأیید نکرد - " + flow_gate)

        score, reasons = self._score(direction, comp, trigger, flow, s5, s15)
        if score < self.min_score:
            return self._reject(f"رد شد: امتیاز ICE کم است ({score:.1f}/{self.min_score:g})")

        entry = float(trigger["entry"])
        sl = self._make_sl(direction, entry, comp, candles_1m, s1.atr)
        risk = entry - sl if direction == "LONG" else sl - entry
        if risk <= 0:
            return self._reject("رد شد: ریسک/SL نامعتبر است")
        sl_pct = risk / entry
        if sl_pct < float(config.MIN_5M_SL_PCT):
            risk = entry * float(config.MIN_5M_SL_PCT)
            sl = entry - risk if direction == "LONG" else entry + risk
            sl_pct = risk / entry
        if sl_pct > float(config.MAX_5M_SL_PCT):
            return self._reject(f"رد شد: SL برای ICE زیاد است ({sl_pct * 100:.2f}%)")

        rr = max(1.0, float(config.ICE_RR))
        tp = entry + risk * rr if direction == "LONG" else entry - risk * rr
        tp_pct = abs(tp - entry) / entry
        notional = max(0.0, float(margin_usdt)) * max(1, int(leverage))
        gross_profit = notional * tp_pct
        gross_loss = notional * sl_pct
        net_profit = gross_profit - float(round_trip_fee_usdt)
        if rr < 1.0:
            return self._reject("رد شد: RR زیر 1 مجاز نیست")
        if net_profit < float(min_net_profit_usdt):
            return self._reject(f"رد شد: سود خالص بعد کارمزد کم است ({net_profit:.4f} USDT)")

        strength = "خیلی قوی" if score >= self.strong_score else "قابل اجرا"
        reasons.append(f"فقط یک TP | RR={rr:.2f} | TP={tp_pct * 100:.2f}% | SL={sl_pct * 100:.2f}%")
        reasons.append(f"سود خالص تخمینی بعد کارمزد: {net_profit:.4f} USDT")

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
            reasons=tuple(reasons),
        )

    def _compression_box(self, candles_5m: list[Candle]) -> dict[str, Any]:
        n = int(config.COMPRESSION_LOOKBACK_5M)
        box = candles_5m[-n-1:-1]
        if len(box) < n:
            return {"ok": False, "reason": "تعداد کندل فشردگی کم است"}
        close = candles_5m[-2].close
        high = max(c.high for c in box)
        low = min(c.low for c in box)
        width_pct = (high - low) / close if close > 0 else 999
        atrs = []
        for c in box:
            atrs.append((c.high - c.low) / c.close if c.close > 0 else 0.0)
        recent_atr = mean(atrs) if atrs else 999
        prev = candles_5m[-(n * 3 + 1):-(n + 1)]
        prev_ranges = [((c.high - c.low) / c.close) for c in prev if c.close > 0]
        prev_atr = mean(prev_ranges) if prev_ranges else recent_atr
        atr_ratio = recent_atr / prev_atr if prev_atr > 0 else 1.0
        if width_pct > float(config.COMPRESSION_MAX_RANGE_PCT):
            return {"ok": False, "reason": f"باکس فشرده نیست width={width_pct*100:.2f}%"}
        if width_pct < float(config.COMPRESSION_MIN_BOX_PCT):
            return {"ok": False, "reason": f"باکس بیش از حد مرده/باریک است width={width_pct*100:.2f}%"}
        if atr_ratio > float(config.COMPRESSION_MAX_ATR_RATIO):
            return {"ok": False, "reason": f"ATR هنوز فشرده نشده ratio={atr_ratio:.2f}"}
        return {
            "ok": True,
            "high": high,
            "low": low,
            "mid": (high + low) / 2,
            "width_pct": width_pct,
            "atr_ratio": atr_ratio,
            "reason": "ok",
        }

    def _explosion_trigger(self, candles_1m: list[Candle], comp: dict[str, Any]) -> dict[str, Any]:
        last = candles_1m[-1]
        prev = candles_1m[-21:-1]
        vol_avg = mean([max(0.0, c.volume) for c in prev]) if prev else last.volume
        vol_ratio = last.volume / vol_avg if vol_avg > 0 else 0.0
        rng = max(0.0, last.high - last.low)
        body_ratio = abs(last.close - last.open) / rng if rng > 0 else 0.0
        high = float(comp["high"])
        low = float(comp["low"])
        direction: Direction | None = None
        if last.close > high and last.close > last.open:
            direction = "LONG"
            entry = last.close
        elif last.close < low and last.close < last.open:
            direction = "SHORT"
            entry = last.close
        else:
            return {"direction": None, "reason": "کندل 1M بیرون باکس بسته نشد"}
        if vol_ratio < float(config.TRIGGER_VOLUME_RATIO):
            return {"direction": None, "reason": f"حجم انفجار کم است {vol_ratio:.2f}x"}
        if body_ratio < float(config.TRIGGER_BODY_MIN_RATIO):
            return {"direction": None, "reason": f"کندل انفجار بدنه کافی ندارد body={body_ratio:.2f}"}
        return {"direction": direction, "entry": entry, "vol_ratio": vol_ratio, "body_ratio": body_ratio}

    def _anti_late_reject(self, direction: Direction, candles_1m: list[Candle], comp: dict[str, Any]) -> str | None:
        last = candles_1m[-1]
        edge = float(comp["high"] if direction == "LONG" else comp["low"])
        extension = abs(last.close - edge) / last.close if last.close > 0 else 999
        if extension > float(config.MAX_ENTRY_EXTENSION_PCT):
            return f"قیمت از لبه باکس دور شده extension={extension*100:.2f}%"
        if len(candles_1m) >= 3:
            a, b = candles_1m[-3], candles_1m[-1]
            if direction == "LONG":
                move = max(0.0, (b.close - a.open) / a.open) if a.open > 0 else 0.0
            else:
                move = max(0.0, (a.open - b.close) / a.open) if a.open > 0 else 0.0
            if move > float(config.MAX_TWO_CANDLE_MOVE_PCT):
                return f"دو کندل اخیر حرکت زیادی کرده‌اند move={move*100:.2f}%"
        return None

    def _flow_snapshot(self, order_book: dict[str, Any], trades: list[dict[str, Any]], candles_1m: list[Candle]) -> FlowSnapshot:
        bids = order_book.get("bids") or []
        asks = order_book.get("asks") or []
        bid_depth = 0.0
        ask_depth = 0.0
        best_bid = safe_float(bids[0][0]) if bids else 0.0
        best_ask = safe_float(asks[0][0]) if asks else 0.0
        levels = int(config.ORDERBOOK_DEPTH_LEVELS)
        for row in bids[:levels]:
            p, q = safe_float(row[0]), safe_float(row[1])
            bid_depth += p * q
        for row in asks[:levels]:
            p, q = safe_float(row[0]), safe_float(row[1])
            ask_depth += p * q
        total = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total if total > 0 else 0.0
        mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else candles_1m[-1].close
        spread_pct = (best_ask - best_bid) / mid if mid > 0 and best_ask >= best_bid else 0.0

        buy_vol = sell_vol = 0.0
        for t in trades[-100:]:
            side = str(t.get("side") or "").lower()
            px = safe_float(t.get("px") or t.get("price"))
            sz = safe_float(t.get("sz") or t.get("size"))
            notional = px * sz if px > 0 else sz
            if side == "buy":
                buy_vol += notional
            elif side == "sell":
                sell_vol += notional
        if buy_vol + sell_vol <= 0:
            # Candle-based fallback: green volume = buy pressure, red volume = sell pressure.
            look = candles_1m[-int(config.CVD_LOOKBACK_1M):]
            for c in look:
                notional = c.close * max(0.0, c.volume)
                if c.close >= c.open:
                    buy_vol += notional
                else:
                    sell_vol += notional
        delta_ratio = (buy_vol - sell_vol) / (buy_vol + sell_vol) if buy_vol + sell_vol > 0 else 0.0
        look = candles_1m[-int(config.CVD_LOOKBACK_1M):]
        signed = [(c.close - c.open) * max(0.0, c.volume) for c in look]
        cvd_slope = sum(signed) / (sum(abs(x) for x in signed) or 1.0)
        reasons = (
            f"Spread={spread_pct*100:.3f}%",
            f"Depth bid/ask={bid_depth:.0f}/{ask_depth:.0f} USDT",
            f"Book imbalance={imbalance:.2f}",
            f"Delta ratio={delta_ratio:.2f}",
            f"CVD slope={cvd_slope:.2f}",
        )
        return FlowSnapshot(spread_pct, bid_depth, ask_depth, imbalance, delta_ratio, cvd_slope, reasons)

    def _flow_gate(self, direction: Direction, f: FlowSnapshot) -> str | None:
        if f.spread_pct > float(config.MAX_SPREAD_PCT):
            return f"اسپرد زیاد است {f.spread_pct*100:.3f}%"
        if min(f.bid_depth_usdt, f.ask_depth_usdt) < float(config.MIN_DEPTH_USDT):
            return f"عمق سفارش کافی نیست bid={f.bid_depth_usdt:.0f} ask={f.ask_depth_usdt:.0f}"
        min_imb = float(config.IMBALANCE_MIN_ABS)
        min_delta = float(config.DELTA_MIN_RATIO)
        if direction == "LONG":
            if f.book_imbalance < -min_imb:
                return f"دفتر سفارش خلاف لانگ است imbalance={f.book_imbalance:.2f}"
            if f.trade_delta_ratio < min_delta and f.cvd_slope < min_delta:
                return f"دلتا/CVD لانگ کافی نیست delta={f.trade_delta_ratio:.2f} cvd={f.cvd_slope:.2f}"
        else:
            if f.book_imbalance > min_imb:
                return f"دفتر سفارش خلاف شورت است imbalance={f.book_imbalance:.2f}"
            if f.trade_delta_ratio > -min_delta and f.cvd_slope > -min_delta:
                return f"دلتا/CVD شورت کافی نیست delta={f.trade_delta_ratio:.2f} cvd={f.cvd_slope:.2f}"
        return None

    def _score(self, direction: Direction, comp: dict[str, Any], trigger: dict[str, Any], flow: FlowSnapshot, s5: Any, s15: Any | None) -> tuple[float, list[str]]:
        score = 0.0
        reasons: list[str] = []
        # Compression 20
        score += 20
        reasons.append(f"20 امتیاز: فشردگی 5M پاس شد width={comp['width_pct']*100:.2f}% atr_ratio={comp['atr_ratio']:.2f}")
        # Explosion 20
        vr = float(trigger.get("vol_ratio") or 0.0)
        br = float(trigger.get("body_ratio") or 0.0)
        score += 15 + clamp((vr - 1.0) * 8, 0, 5)
        reasons.append(f"{15 + clamp((vr - 1.0) * 8, 0, 5):.0f} امتیاز: انفجار 1M با حجم {vr:.2f}x و بدنه {br:.2f}")
        # Orderbook 20
        if direction == "LONG":
            book_points = 12 + clamp(flow.book_imbalance * 40, 0, 8)
        else:
            book_points = 12 + clamp((-flow.book_imbalance) * 40, 0, 8)
        score += book_points
        reasons.append(f"{book_points:.0f} امتیاز: عمق/اسپرد مناسب | " + " | ".join(flow.reasons[:3]))
        # Delta/CVD 20
        if direction == "LONG":
            dpoints = 10 + clamp(max(flow.trade_delta_ratio, flow.cvd_slope) * 35, 0, 10)
        else:
            dpoints = 10 + clamp(max(-flow.trade_delta_ratio, -flow.cvd_slope) * 35, 0, 10)
        score += dpoints
        reasons.append(f"{dpoints:.0f} امتیاز: دلتا/CVD هم‌جهت | " + " | ".join(flow.reasons[3:]))
        # Not late / 5M context 10
        score += 10
        reasons.append("10 امتیاز: ورود روی اولین انفجار است، نه پولبک و نه وسط روند")
        # 15M danger filter 10 but not mandatory trend-following.
        context_points = 0.0
        if s15 is not None:
            if direction == "LONG" and s15.close >= s15.ema50 * 0.997:
                context_points = 10
                reasons.append("10 امتیاز: 15M خلاف جهت خطرناک نیست")
            elif direction == "SHORT" and s15.close <= s15.ema50 * 1.003:
                context_points = 10
                reasons.append("10 امتیاز: 15M خلاف جهت خطرناک نیست")
            else:
                context_points = 4
                reasons.append("4 امتیاز: 15M کمی خلاف است ولی هسته ICE اجازه بررسی داده")
        else:
            context_points = 5
            reasons.append("5 امتیاز: دیتای 15M کافی نبود؛ فیلتر خطر خنثی شد")
        score += context_points
        return clamp(score, 0, 100), reasons

    def _make_sl(self, direction: Direction, entry: float, comp: dict[str, Any], candles_1m: list[Candle], atr_1m: float) -> float:
        last = candles_1m[-1]
        buffer = max(float(atr_1m) * 0.18, entry * 0.00025)
        if direction == "LONG":
            # Invalidation = breakout lost / price returns inside compression.
            return min(float(comp["high"]) - buffer, last.low - buffer * 0.30)
        return max(float(comp["low"]) + buffer, last.high + buffer * 0.30)


# Backward-compatible class name for older bot imports.
Simple5MScalperStrategy = ICE5MStrategy
