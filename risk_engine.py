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
    expected_move_pct: float
    reasons: tuple[str, ...]


class RiskEngine:
    def build_tp_sl(self, *, direction: Direction, entry: float, snapshot_15m: IndicatorSnapshot, levels: LevelsResult, learned_expected_pct: float | None = None) -> RiskResult:
        atr = max(snapshot_15m.atr, entry * 0.001)
        buffer = atr * 0.18
        reasons: list[str] = []
        if direction == "LONG":
            raw_sl = min(levels.support - buffer, entry - atr * 0.55)
            sl = max(raw_sl, entry - atr * 2.2)
            risk = entry - sl
            candidate_tp = max(levels.resistance, entry + risk * 1.35, entry + atr * 0.85)
            if learned_expected_pct and learned_expected_pct > 0:
                candidate_tp = min(candidate_tp, entry * (1.0 + learned_expected_pct * 1.15))
            tp = candidate_tp
            reward = tp - entry
        else:
            raw_sl = max(levels.resistance + buffer, entry + atr * 0.55)
            sl = min(raw_sl, entry + atr * 2.2)
            risk = sl - entry
            candidate_tp = min(levels.support, entry - risk * 1.35, entry - atr * 0.85)
            if learned_expected_pct and learned_expected_pct > 0:
                candidate_tp = max(candidate_tp, entry * (1.0 - learned_expected_pct * 1.15))
            tp = candidate_tp
            reward = entry - tp
        if risk <= 0 or reward <= 0 or tp <= 0 or sl <= 0:
            return RiskResult(False, float(tp), float(sl), 0.0, 0, 0.0, ("TP/SL معتبر ساخته نشد.",))
        rr = reward / risk
        risk_pct = risk / entry if entry > 0 else 0.0
        expected_move_pct = reward / entry if entry > 0 else 0.0
        score = 0
        if rr >= MIN_RISK_REWARD:
            score += 6
        if rr >= 1.30:
            score += 3
        if expected_move_pct >= 0.001:
            score += 2
        if 0.001 <= risk_pct <= 0.035:
            score += 2
        if risk_pct > 0.04:
            reasons.append("SL بیش از حد دور است.")
        elif risk_pct < 0.001:
            reasons.append("SL خیلی نزدیک است.")
        ok = rr >= MIN_RISK_REWARD and 0.001 <= risk_pct <= 0.04
        if ok:
            reasons.append("TP/SL با ATR و سطوح قابل قبول است.")
        else:
            reasons.append("ریسک/ریوارد یا فاصله SL قابل قبول نیست.")
        return RiskResult(ok, float(tp), float(sl), float(rr), min(WEIGHTS.risk_net, max(0, score)), float(expected_move_pct), tuple(reasons))
