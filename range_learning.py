from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from config import BOOT_NORMAL_SAMPLE_LIMIT, INITIAL_SOFT_MODE, REAL_MIN_SAMPLES
from indicators import IndicatorSnapshot
from market_context import MarketContextResult
from market_state import MarketStateResult
from utils import clamp, session_bucket

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
    volume_bin: str
    vwap_bin: str
    ema_gap_bin: str
    di_bin: str
    raw: dict[str, float | str]

    @property
    def key(self) -> str:
        return "|".join((self.symbol_name, self.direction, self.session, self.market_state, self.alignment, self.rsi_bin, self.adx_bin, self.atr_bin, self.volume_bin, self.vwap_bin, self.ema_gap_bin, self.di_bin))


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
        raw = {
            "rsi": snapshot.rsi,
            "adx": snapshot.adx,
            "atr_pct": snapshot.atr_pct,
            "volume_ratio": snapshot.volume_ratio,
            "price_vs_vwap_pct": snapshot.price_vs_vwap_pct,
            "ema20_50_gap_pct": snapshot.ema20_50_gap_pct,
            "di_edge": di_edge,
        }
        return RangeFeatures(
            symbol_name=symbol_name,
            direction=direction,
            session=session_bucket(),
            market_state=state.state,
            alignment=context.alignment,
            rsi_bin=self._bin(snapshot.rsi, 4),
            adx_bin=self._bin(snapshot.adx, 4),
            atr_bin=self._bin(snapshot.atr_pct * 100.0, 0.10),
            volume_bin=self._bin(snapshot.volume_ratio, 0.35),
            vwap_bin=self._bin(snapshot.price_vs_vwap_pct * 100.0, 0.25),
            ema_gap_bin=self._bin(snapshot.ema20_50_gap_pct * 100.0, 0.20),
            di_bin=self._bin(di_edge, 4),
            raw=raw,
        )

    def evaluate(self, storage: Any, features: RangeFeatures, snapshot: IndicatorSnapshot, context: MarketContextResult) -> RangeVerdict:
        profile = storage.get_range_profile(features.key)
        samples = int(profile.get("samples", 0)) if profile else 0
        wins = int(profile.get("tp", 0)) if profile else 0
        win_rate = (wins / samples * 100.0) if samples else 0.0
        net_profit = float(profile.get("net_profit", 0.0)) if profile else 0.0
        avg_mfe = float(profile.get("avg_mfe_pct", 0.0)) if profile else 0.0
        avg_mae = float(profile.get("avg_mae_pct", 0.0)) if profile else 0.0
        reasons: list[str] = []
        soft_ok, soft_reasons = self._soft_gate(features.direction, snapshot)
        reasons.extend(soft_reasons)
        if not soft_ok:
            return RangeVerdict(False, False, 0, samples, win_rate, net_profit, 0.0, 0.70, 1.15, tuple(reasons))
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
            reasons.append("بازه جدید است؛ برای یادگیری Normal نرم مجاز است.")
        elif samples < BOOT_NORMAL_SAMPLE_LIMIT:
            confidence = min(35, 8 + samples)
            reasons.append("نمونه هنوز کم است؛ Normal برای یادگیری مجاز است.")
            if win_rate >= 55 and net_profit > 0 and samples >= REAL_MIN_SAMPLES and context.real_ok:
                real_allowed = True
        else:
            expected_ok = net_profit > 0 and (win_rate >= 45 or avg_mfe > avg_mae * 1.35)
            confidence = int(clamp((win_rate * 0.55) + min(samples, 150) * 0.25 + (15 if net_profit > 0 else -10), 0, 100))
            normal_allowed = normal_allowed and expected_ok
            real_allowed = context.real_ok and expected_ok and confidence >= 45
            safe_tp_fraction = 0.68 if win_rate < 50 else 0.76
            sl_atr_mult = 1.35 if avg_mae > snapshot.atr_pct else 1.10
            reasons.append("بازه با حافظه قبلی ارزیابی شد.")
        if not context.normal_ok:
            normal_allowed = False
            real_allowed = False
            reasons.append("کانتکست تایم‌های بالا برای صدور سیگنال مناسب نیست.")
        return RangeVerdict(normal_allowed, real_allowed, confidence, samples, win_rate, net_profit, predicted, safe_tp_fraction, sl_atr_mult, tuple(reasons))

    @staticmethod
    def _bin(value: float, step: float) -> str:
        if step <= 0:
            return str(round(value, 3))
        low = int(value / step) * step
        high = low + step
        return f"{low:.3f}:{high:.3f}"

    @staticmethod
    def _soft_gate(direction: Direction, snapshot: IndicatorSnapshot) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if snapshot.volume_ratio < 0.45:
            return False, ["حجم کمتر از حد یادگیری است."]
        if snapshot.volume_ratio > 5.5 and snapshot.body_pct > 0.60:
            return False, ["کلایمکس حجمی شدید است."]
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
