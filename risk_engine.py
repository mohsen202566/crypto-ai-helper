from __future__ import annotations

from dataclasses import dataclass

from config import MIN_RISK_REWARD, WEIGHTS
from indicators import IndicatorSnapshot
from levels_engine import LevelsResult
from scorer import Direction


@dataclass(frozen=True)
class RiskResult:
    ok: bool
    tp: float
    sl: float
    risk_reward: float
    score: int
    reasons: tuple[str, ...]


class RiskEngine:
    def build_tp_sl(self, *, direction: Direction, entry: float, snapshot_15m: IndicatorSnapshot, levels: LevelsResult) -> RiskResult:
        atr = max(snapshot_15m.atr, entry * 0.001)
        buffer = atr * 0.18
        reasons: list[str] = []

        if direction == "LONG":
            raw_sl = min(levels.support - buffer, entry - atr * 0.55)
            max_sl_distance = entry - atr * 2.4
            sl = max(raw_sl, max_sl_distance)
            risk = entry - sl
            candidate_tp = max(levels.resistance, entry + risk * 1.45, entry + atr * 0.9)
            tp = candidate_tp
            reward = tp - entry
        else:
            raw_sl = max(levels.resistance + buffer, entry + atr * 0.55)
            max_sl_distance = entry + atr * 2.4
            sl = min(raw_sl, max_sl_distance)
            risk = sl - entry
            candidate_tp = min(levels.support, entry - risk * 1.45, entry - atr * 0.9)
            tp = candidate_tp
            reward = entry - tp

        if risk <= 0 or reward <= 0 or tp <= 0 or sl <= 0:
            return RiskResult(False, tp=float(tp), sl=float(sl), risk_reward=0.0, score=0, reasons=("TP/SL معتبر ساخته نشد.",))

        rr = reward / risk
        score = 0
        if rr >= MIN_RISK_REWARD:
            score += 7
        if rr >= 1.35:
            score += 3
        if rr >= 1.6:
            score += 2
        risk_pct = risk / entry if entry > 0 else 0.0
        if 0.002 <= risk_pct <= 0.025:
            score += 3
        elif risk_pct > 0.035:
            reasons.append("SL بیش از حد دور است.")
        else:
            reasons.append("SL خیلی نزدیک است و ممکن است با نویز بخورد.")

        ok = rr >= MIN_RISK_REWARD and risk_pct <= 0.04 and risk_pct > 0.001
        score = min(WEIGHTS.risk_reward_net, max(0, score))
        if ok:
            reasons.append("TP/SL با ATR و حمایت/مقاومت قابل قبول است.")
        else:
            reasons.append("ریسک/ریوارد یا فاصله SL قابل قبول نیست.")
        return RiskResult(ok=ok, tp=float(tp), sl=float(sl), risk_reward=float(rr), score=score, reasons=tuple(reasons))
