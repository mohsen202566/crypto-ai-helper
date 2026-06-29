from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from config import (
    AI_EXIT_BREAKEVEN_BUFFER_PCT,
    AI_EXIT_DAMAGE_CONTROL_ADVERSE_RATIO,
    AI_EXIT_ENABLED,
    AI_EXIT_GIVEBACK_RATIO,
    AI_EXIT_MIN_ACTIVE_SECONDS,
    AI_EXIT_MIN_GIVEBACK_PCT,
    AI_EXIT_MIN_PROFIT_PCT,
    AI_EXIT_NOISE_ATR_MULTIPLIER,
    AI_EXIT_REVERSAL_TICKS,
    AI_EXIT_RISKY_GIVEBACK_RATIO,
    AI_EXIT_TARGET_ZONE_RATIO,
    AI_EXIT_WEAKNESS_CONFIRMATIONS,
)
from storage import StoredSignal


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str
    status: str | None = None
    exit_price: float | None = None
    exit_score: int = 0
    giveback_pct: float = 0.0
    target_zone_reached: bool = False


@dataclass(frozen=True)
class _Pulse:
    against_ticks: int
    trend_against_pct: float
    pullback_from_local_extreme_pct: float


class ExitEngine:
    """Slow 1H AI exit brain.

    This is intentionally less sensitive than the 5m bot:
    - no AI exit before the minimum active time,
    - one weakness is not enough,
    - small pullbacks inside the symbol's ATR noise are ignored,
    - TP is treated as a target zone and the wave can keep running.
    """

    _RISKY_ENTRY_QUALITIES = {"PRECISION_WAIT", "EXHAUSTION_RISK", "NOISE_RISK", "WEAK_MOVEMENT", "NO_ENTRY"}
    _RISKY_MARKET_MODES = {"CLIMAX_RISK", "NOISY"}

    def analyze(
        self,
        signal: StoredSignal,
        price: float,
        *,
        mfe_pct: float | None = None,
        mae_pct: float | None = None,
        recent_prices: tuple[float, ...] = (),
    ) -> ExitDecision:
        if not AI_EXIT_ENABLED:
            return ExitDecision(False, "")
        if signal.entry <= 0 or price <= 0:
            return ExitDecision(False, "")
        if self._age_seconds(signal.created_at) < AI_EXIT_MIN_ACTIVE_SECONDS:
            return ExitDecision(False, "")

        entry = float(signal.entry)
        reward_abs = abs(float(signal.tp) - entry)
        risk_abs = abs(entry - float(signal.sl))
        if reward_abs <= 0 or risk_abs <= 0:
            return ExitDecision(False, "")

        signed_profit_pct = self._signed_profit_pct(signal.direction, entry, price)
        current_profit_pct = max(0.0, signed_profit_pct)
        current_loss_pct = max(0.0, -signed_profit_pct)
        mfe = max(float(signal.mfe_pct or 0.0), float(mfe_pct or 0.0), current_profit_pct)
        mae = max(float(signal.mae_pct or 0.0), float(mae_pct or 0.0), current_loss_pct)

        progress = self._progress_to_target(signal.direction, entry, price, reward_abs)
        adverse = self._adverse_to_sl(signal.direction, entry, price, risk_abs)
        target_zone = progress >= AI_EXIT_TARGET_ZONE_RATIO

        atr_pct = max(float(getattr(signal, "atr_pct_15m", 0.0) or 0.0), risk_abs / entry, reward_abs / entry * 0.35)
        noise_pct = max(AI_EXIT_MIN_GIVEBACK_PCT, atr_pct * AI_EXIT_NOISE_ATR_MULTIPLIER)

        risky_context = (signal.entry_quality or "") in self._RISKY_ENTRY_QUALITIES or (getattr(signal, "market_mode", "") or "") in self._RISKY_MARKET_MODES
        giveback_ratio_limit = AI_EXIT_RISKY_GIVEBACK_RATIO if risky_context else AI_EXIT_GIVEBACK_RATIO
        from_peak_pct = max(0.0, mfe - current_profit_pct)
        giveback_pct = from_peak_pct / max(mfe, 1e-9) if mfe > 0 else 0.0

        recent = tuple(float(x) for x in recent_prices if float(x) > 0)
        if not recent or abs(recent[-1] - price) > max(entry * 0.000001, 1e-12):
            recent = (*recent, price)
        pulse = self._pulse(signal.direction, entry, recent)

        weakness_score = 0
        reasons: list[str] = []

        # Weakness #1: real giveback from MFE, beyond adaptive ATR noise.
        if from_peak_pct >= noise_pct and giveback_pct >= giveback_ratio_limit:
            weakness_score += 1
            reasons.append(f"giveback واقعی از موج: {giveback_pct * 100:.2f}% از سود بازگشته؛ نویز مجاز {noise_pct * 100:.3f}%")

        # Weakness #2: target zone reached and then price gives back more than noise.
        if target_zone and from_peak_pct >= noise_pct:
            weakness_score += 1
            reasons.append("قیمت به Target Zone ذهنی رسیده و بعد از آن برگشت معنادار داده است.")

        # Weakness #3: consecutive adverse ticks / monitor pulses.
        if pulse.against_ticks >= AI_EXIT_REVERSAL_TICKS:
            weakness_score += 1
            reasons.append(f"{pulse.against_ticks} حرکت پشت‌سرهم خلاف جهت دیده شد.")

        # Weakness #4: recent local move is against the position beyond normal noise.
        if pulse.trend_against_pct >= noise_pct * 0.75:
            weakness_score += 1
            reasons.append(f"روند کوتاه اخیر خلاف پوزیشن است: {pulse.trend_against_pct * 100:.3f}%")

        # Weakness #5: local pullback from recent extreme is not just a tiny fluctuation.
        if pulse.pullback_from_local_extreme_pct >= noise_pct:
            weakness_score += 1
            reasons.append(f"برگشت از سقف/کف اخیر از نویز طبیعی بیشتر شد: {pulse.pullback_from_local_extreme_pct * 100:.3f}%")

        enough_weakness = weakness_score >= max(1, AI_EXIT_WEAKNESS_CONFIRMATIONS)
        profit_good_enough = current_profit_pct >= AI_EXIT_MIN_PROFIT_PCT

        if enough_weakness and (target_zone or profit_good_enough):
            return ExitDecision(
                True,
                " | ".join(reasons),
                status="AI_EXIT_PROFIT",
                exit_price=price,
                exit_score=weakness_score,
                giveback_pct=giveback_pct,
                target_zone_reached=target_zone,
            )

        # Breakeven protection: only after the trade had real profit and multiple weaknesses.
        if enough_weakness and mfe >= max(AI_EXIT_MIN_PROFIT_PCT, noise_pct * 1.35) and signed_profit_pct <= AI_EXIT_BREAKEVEN_BUFFER_PCT:
            return ExitDecision(
                True,
                " | ".join(reasons + ["سود قبلی تقریباً برگشته؛ خروج نزدیک سربه‌سر برای حفظ موج."]),
                status="AI_EXIT_BREAKEVEN",
                exit_price=price,
                exit_score=weakness_score,
                giveback_pct=giveback_pct,
                target_zone_reached=target_zone,
            )

        # Damage control: before hard SL, but only when adverse path is clearly confirmed.
        if enough_weakness and adverse >= AI_EXIT_DAMAGE_CONTROL_ADVERSE_RATIO and mae >= noise_pct:
            return ExitDecision(
                True,
                " | ".join(reasons + [f"damage control قبل از SL؛ adverse={adverse:.2f}"]),
                status="AI_EXIT_DAMAGE_CONTROL",
                exit_price=price,
                exit_score=weakness_score,
                giveback_pct=giveback_pct,
                target_zone_reached=target_zone,
            )

        return ExitDecision(False, "")

    @staticmethod
    def _age_seconds(created_at: str) -> float:
        try:
            created = datetime.fromisoformat(str(created_at))
        except ValueError:
            return 0.0
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - created).total_seconds())

    @staticmethod
    def _signed_profit_pct(direction: str, entry: float, price: float) -> float:
        if direction == "LONG":
            return (price - entry) / entry
        return (entry - price) / entry

    @staticmethod
    def _progress_to_target(direction: str, entry: float, price: float, reward_abs: float) -> float:
        if reward_abs <= 0:
            return 0.0
        move = price - entry if direction == "LONG" else entry - price
        return move / reward_abs

    @staticmethod
    def _adverse_to_sl(direction: str, entry: float, price: float, risk_abs: float) -> float:
        if risk_abs <= 0:
            return 0.0
        adverse = entry - price if direction == "LONG" else price - entry
        return max(0.0, adverse / risk_abs)

    @staticmethod
    def _pulse(direction: str, entry: float, recent: tuple[float, ...]) -> _Pulse:
        if len(recent) < 3 or entry <= 0:
            return _Pulse(0, 0.0, 0.0)

        against_ticks = 0
        for previous, current in zip(reversed(recent[:-1]), reversed(recent[1:])):
            if direction == "LONG":
                against = current < previous
            else:
                against = current > previous
            if against:
                against_ticks += 1
            else:
                break

        first = recent[0]
        last = recent[-1]
        if direction == "LONG":
            trend_against = max(0.0, (first - last) / entry)
            pullback = max(0.0, (max(recent) - last) / entry)
        else:
            trend_against = max(0.0, (last - first) / entry)
            pullback = max(0.0, (last - min(recent)) / entry)
        return _Pulse(against_ticks, trend_against, pullback)
