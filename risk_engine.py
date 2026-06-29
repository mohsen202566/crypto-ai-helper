from __future__ import annotations

from dataclasses import dataclass

from config import MAX_SCALP_SL_PCT, MIN_RISK_REWARD, MIN_SCALP_SL_PCT, MIN_SCALP_TP_PCT, WEIGHTS
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
    def build_tp_sl(self, *, direction: Direction, entry: float, snapshot_15m: IndicatorSnapshot, levels: LevelsResult, learned_expected_pct: float | None = None, learned_mae_pct: float | None = None) -> RiskResult:
        # compatibility name: snapshot_15m means 1H in this project.
        if entry <= 0:
            return RiskResult(False, 0.0, 0.0, 0.0, 0, 0.0, ("قیمت ورود برای ساخت TP/SL نامعتبر است.",))
        atr = max(snapshot_15m.atr, entry * 0.0025)
        buffer = atr * 0.18
        min_sl_distance = max(entry * MIN_SCALP_SL_PCT, atr * 0.34)
        min_tp_distance = max(entry * MIN_SCALP_TP_PCT, atr * 0.58)
        learned_sl_distance = entry * learned_mae_pct * 1.45 if learned_mae_pct and learned_mae_pct > 0 else min_sl_distance
        max_sl_distance = max(min_sl_distance * 1.35, entry * MAX_SCALP_SL_PCT)
        rr_floor = max(MIN_RISK_REWARD, 1.18)
        reasons: list[str] = ["TP/SL یک‌ساعته بر اساس قیمت زنده، ATR 1H، سطح و حافظه AI ساخته شد."]
        if direction == "LONG":
            raw_sl = min(levels.support - buffer, entry - atr * 0.55)
            sl_distance = entry - raw_sl
            sl_distance = min(max(sl_distance, min_sl_distance, learned_sl_distance), max_sl_distance)
            sl = entry - sl_distance
            raw_tp = max(levels.resistance, entry + sl_distance * rr_floor, entry + atr * 0.82)
            reward_floor = max(sl_distance * rr_floor, min_tp_distance)
            reward_distance = max(raw_tp - entry, reward_floor)
            if learned_expected_pct and learned_expected_pct > 0:
                learned_cap = max(min_tp_distance, entry * learned_expected_pct * 1.20)
                if learned_cap > reward_floor:
                    reward_distance = min(reward_distance, learned_cap)
            tp = entry + reward_distance
            risk = entry - sl
            reward = tp - entry
        else:
            raw_sl = max(levels.resistance + buffer, entry + atr * 0.55)
            sl_distance = raw_sl - entry
            sl_distance = min(max(sl_distance, min_sl_distance, learned_sl_distance), max_sl_distance)
            sl = entry + sl_distance
            raw_tp = min(levels.support, entry - sl_distance * rr_floor, entry - atr * 0.82)
            reward_floor = max(sl_distance * rr_floor, min_tp_distance)
            reward_distance = max(entry - raw_tp, reward_floor)
            if learned_expected_pct and learned_expected_pct > 0:
                learned_cap = max(min_tp_distance, entry * learned_expected_pct * 1.20)
                if learned_cap > reward_floor:
                    reward_distance = min(reward_distance, learned_cap)
            tp = entry - reward_distance
            risk = sl - entry
            reward = entry - tp
        if risk <= 0 or reward <= 0 or tp <= 0 or sl <= 0:
            return RiskResult(False, float(tp), float(sl), 0.0, 0, 0.0, ("TP/SL معتبر ساخته نشد.",))
        rr = reward / risk
        risk_pct = risk / entry
        expected_move_pct = reward / entry
        ok = rr >= MIN_RISK_REWARD and risk_pct >= MIN_SCALP_SL_PCT and expected_move_pct >= MIN_SCALP_TP_PCT and risk_pct <= MAX_SCALP_SL_PCT
        score = WEIGHTS.tp_sl if ok else max(0, WEIGHTS.tp_sl - 7)
        reasons.append("TP/SL برای اسکالپ 1H قابل اجراست." if ok else "TP/SL فقط برای Watch/یادگیری مناسب است و AI می‌تواند بعداً تنظیم کند.")
        return RiskResult(ok, float(tp), float(sl), float(rr), score, float(expected_move_pct), tuple(reasons))
