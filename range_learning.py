from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from config import BOOT_NORMAL_SAMPLE_LIMIT, INITIAL_SOFT_MODE, REAL_MIN_SAMPLES
from indicators import IndicatorSnapshot
from market_context import MarketContextResult
from market_state import MarketStateResult
from utils import clamp

Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class RangeFeatures:
    symbol_name: str
    direction: Direction
    session: str
    market_state: str
    alignment: str
    rsi_bin: str
    adx_bin: str
    atr_bin: str
    vwap_bin: str
    ema20_dist_bin: str
    ema50_dist_bin: str
    ema200_dist_bin: str
    ema_gap_bin: str
    ema20_slope_bin: str
    ema50_slope_bin: str
    di_bin: str
    recent_pos_bin: str
    swing_pos_bin: str
    raw: dict[str, float | str]

    @property
    def key(self) -> str:
        """Learning bucket: only coin + side + 5m indicator ranges.

        Session, market-state and HTF alignment are deliberately excluded so the
        learning memory stays indicator-range based, while context remains a
        separate gate before any signal is accepted.
        """
        return "|".join((
            self.symbol_name,
            self.direction,
            self.rsi_bin,
            self.adx_bin,
            self.atr_bin,
            self.vwap_bin,
            self.ema20_dist_bin,
            self.ema50_dist_bin,
            self.ema200_dist_bin,
            self.ema_gap_bin,
            self.ema20_slope_bin,
            self.ema50_slope_bin,
            self.di_bin,
            self.recent_pos_bin,
            self.swing_pos_bin,
        ))


@dataclass(frozen=True)
class RangeVerdict:
    normal_allowed: bool
    real_allowed: bool
    confidence: int
    samples: int
    win_rate: float
    net_profit: float
    predicted_move_pct: float
    safe_tp_fraction: float
    sl_atr_mult: float
    reasons: tuple[str, ...]


class RangeLearningEngine:
    def build_features(self, symbol_name: str, direction: Direction, snapshot: IndicatorSnapshot, context: MarketContextResult, state: MarketStateResult) -> RangeFeatures:
        di_edge = snapshot.plus_di - snapshot.minus_di if direction == "LONG" else snapshot.minus_di - snapshot.plus_di
        recent_pos = self._range_position(snapshot.close, snapshot.recent_low, snapshot.recent_high)
        swing_pos = self._range_position(snapshot.close, snapshot.swing_low, snapshot.swing_high)
        raw = {
            "rsi": snapshot.rsi,
            "adx": snapshot.adx,
            "atr_pct": snapshot.atr_pct,
            "price_vs_vwap_pct": snapshot.price_vs_vwap_pct,
            "price_vs_ema20_pct": snapshot.price_vs_ema20_pct,
            "price_vs_ema50_pct": snapshot.price_vs_ema50_pct,
            "price_vs_ema200_pct": snapshot.price_vs_ema200_pct,
            "ema20_50_gap_pct": snapshot.ema20_50_gap_pct,
            "ema20_slope_pct": snapshot.ema20_slope_pct,
            "ema50_slope_pct": snapshot.ema50_slope_pct,
            "di_edge": di_edge,
            "recent_position": recent_pos,
            "swing_position": swing_pos,
        }
        return RangeFeatures(
            symbol_name=symbol_name,
            direction=direction,
            session="INDICATOR_ONLY",
            market_state=state.state,
            alignment=context.alignment,
            rsi_bin=self._bin(snapshot.rsi, 4),
            adx_bin=self._bin(snapshot.adx, 4),
            atr_bin=self._bin(snapshot.atr_pct * 100.0, 0.10),
            vwap_bin=self._bin(snapshot.price_vs_vwap_pct * 100.0, 0.25),
            ema20_dist_bin=self._bin(snapshot.price_vs_ema20_pct * 100.0, 0.20),
            ema50_dist_bin=self._bin(snapshot.price_vs_ema50_pct * 100.0, 0.25),
            ema200_dist_bin=self._bin(snapshot.price_vs_ema200_pct * 100.0, 0.40),
            ema_gap_bin=self._bin(snapshot.ema20_50_gap_pct * 100.0, 0.20),
            ema20_slope_bin=self._bin(snapshot.ema20_slope_pct * 100.0, 0.05),
            ema50_slope_bin=self._bin(snapshot.ema50_slope_pct * 100.0, 0.05),
            di_bin=self._bin(di_edge, 4),
            recent_pos_bin=self._bin(recent_pos, 0.10),
            swing_pos_bin=self._bin(swing_pos, 0.10),
            raw=raw,
        )

    def evaluate(self, storage: Any, features: RangeFeatures, snapshot: IndicatorSnapshot, context: MarketContextResult) -> RangeVerdict:
        profile = storage.get_range_profile(features.key)
        exact_samples = int(profile.get("samples", 0)) if profile else 0

        fallback = None
        if exact_samples == 0 and hasattr(storage, "get_symbol_direction_profile"):
            fallback = storage.get_symbol_direction_profile(features.symbol_name, features.direction)
        active_profile = profile if exact_samples > 0 else fallback

        samples = int(active_profile.get("samples", 0)) if active_profile else 0
        wins = int(active_profile.get("tp", 0)) if active_profile else 0
        win_rate = (wins / samples * 100.0) if samples else 0.0
        net_profit = float(active_profile.get("net_profit", 0.0)) if active_profile else 0.0
        avg_mfe = float(active_profile.get("avg_mfe_pct", 0.0)) if active_profile else 0.0
        avg_mae = float(active_profile.get("avg_mae_pct", 0.0)) if active_profile else 0.0
        if fallback is active_profile and exact_samples == 0:
            avg_mfe = 0.0
            avg_mae = 0.0

        reasons: list[str] = []
        soft_ok, soft_reasons = self._soft_gate(features.direction, snapshot)
        reasons.extend(soft_reasons)
        if not soft_ok:
            return RangeVerdict(False, False, 0, samples, win_rate, net_profit, 0.0, 0.70, 1.15, tuple(reasons))

        # Fast-entry expectation: use recent volatility and VWAP distance as a base,
        # then let exact range MFE override it when enough closed samples exist.
        base_move = max(snapshot.atr_pct * 2.8, abs(snapshot.price_vs_vwap_pct) * 0.7, 0.0035)
        predicted = avg_mfe * 0.85 if samples >= 10 and avg_mfe > 0 else base_move
        predicted = clamp(predicted, 0.0035, 0.035)
        safe_tp_fraction = 0.72
        sl_atr_mult = 1.15
        confidence = 0
        normal_allowed = context.normal_ok
        real_allowed = False

        if samples == 0 and INITIAL_SOFT_MODE:
            confidence = 5
            reasons.append("بازه اندیکاتوری جدید است؛ فقط Normal برای جمع‌کردن نمونه مجاز است.")
        elif exact_samples == 0 and fallback is active_profile:
            confidence = min(32, 6 + samples // 2)
            reasons.append(f"نمونه دقیق بازه اندیکاتوری صفر است؛ از حافظه ارز/جهت با {samples} نمونه فقط برای احتیاط استفاده شد.")
            real_allowed = False
            if net_profit < 0 or win_rate < 42:
                normal_allowed = normal_allowed and INITIAL_SOFT_MODE
                reasons.append("حافظه ارز/جهت هنوز ضعیف است؛ Real ممنوع و Normal فقط برای یادگیری است.")
        elif samples < BOOT_NORMAL_SAMPLE_LIMIT:
            confidence = min(35, 8 + samples)
            reasons.append("نمونه بازه اندیکاتوری هنوز کم است؛ Normal برای یادگیری مجاز است.")
            if win_rate >= 55 and net_profit > 0 and samples >= REAL_MIN_SAMPLES and context.real_ok:
                real_allowed = True
        else:
            expected_ok = net_profit > 0 and (win_rate >= 45 or avg_mfe > avg_mae * 1.35)
            confidence = int(clamp((win_rate * 0.55) + min(samples, 150) * 0.25 + (15 if net_profit > 0 else -10), 0, 100))
            normal_allowed = normal_allowed and expected_ok
            real_allowed = context.real_ok and expected_ok and confidence >= 45
            safe_tp_fraction = 0.68 if win_rate < 50 else 0.76
            sl_atr_mult = 1.35 if avg_mae > snapshot.atr_pct else 1.10
            reasons.append("بازه اندیکاتوری با حافظه قبلی ارزیابی شد.")

        if not context.normal_ok:
            normal_allowed = False
            real_allowed = False
            reasons.append("Direction Gate: 4H/1H یا BTC/ETH برای این جهت اوکی نیست.")
        return RangeVerdict(normal_allowed, real_allowed, confidence, samples, win_rate, net_profit, predicted, safe_tp_fraction, sl_atr_mult, tuple(reasons))

    @staticmethod
    def _bin(value: float, step: float) -> str:
        if step <= 0:
            return str(round(value, 3))
        low = int(value / step) * step
        high = low + step
        return f"{low:.3f}:{high:.3f}"

    @staticmethod
    def _range_position(close: float, low: float, high: float) -> float:
        width = high - low
        if width <= 0:
            return 0.50
        return clamp((close - low) / width, 0.0, 1.0)

    @staticmethod
    def _soft_gate(direction: Direction, snapshot: IndicatorSnapshot) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if snapshot.adx < 10:
            return False, ["ADX خیلی پایین است."]
        if snapshot.atr_pct < 0.0004 or snapshot.atr_pct > 0.030:
            return False, ["ATR بیش از حد مرده یا انفجاری است."]
        if direction == "LONG":
            if not (44 <= snapshot.rsi <= 76):
                return False, ["RSI برای لانگ خارج از بازه نرم شروع است."]
            if snapshot.plus_di < snapshot.minus_di * 0.82:
                reasons.append("DI لانگ ضعیف است؛ فقط اگر حافظه کمک کند قابل قبول است.")
        else:
            if not (24 <= snapshot.rsi <= 56):
                return False, ["RSI برای شورت خارج از بازه نرم شروع است."]
            if snapshot.minus_di < snapshot.plus_di * 0.82:
                reasons.append("DI شورت ضعیف است؛ فقط اگر حافظه کمک کند قابل قبول است.")
        return True, reasons
