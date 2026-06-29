from __future__ import annotations

from dataclasses import dataclass

from config import WEIGHTS
from indicators import IndicatorSnapshot
from scorer import Direction


@dataclass(frozen=True)
class EntryPrecisionResult:
    state: str
    precision_pct: float
    score: int
    confidence: int
    reasons: tuple[str, ...]


class EntryPrecisionEngine:
    """Soft 30m entry precision for 1H signals.

    Precision is adaptive information for AI. It does not hard reject Normal signals;
    Real can be stricter through MetaBrain learning.
    """

    def analyze(self, snapshot: IndicatorSnapshot, direction: Direction) -> EntryPrecisionResult:
        atr = max(snapshot.atr, snapshot.close * 0.0001)
        if direction == "LONG":
            base = min(snapshot.ema20, snapshot.vwap, snapshot.bb_mid, snapshot.recent_low)
            distance = max(0.0, snapshot.close - base)
            flow_ok = snapshot.rsi_delta > -0.15 and snapshot.macd_hist_slope >= 0
            position_ok = snapshot.bb_position >= 0.38
        else:
            base = max(snapshot.ema20, snapshot.vwap, snapshot.bb_mid, snapshot.recent_high)
            distance = max(0.0, base - snapshot.close)
            flow_ok = snapshot.rsi_delta < 0.15 and snapshot.macd_hist_slope <= 0
            position_ok = snapshot.bb_position <= 0.62
        precision = max(0.0, 100.0 - min(100.0, (distance / max(atr * 3.8, snapshot.close * 0.0016)) * 100.0))
        if position_ok:
            precision = min(100.0, precision + 4.0)
        reasons: list[str] = [f"Entry Precision 30m={precision:.1f}%"]
        if precision >= 80:
            return EntryPrecisionResult("READY", precision, WEIGHTS.entry_precision, 90, tuple(reasons + ["AI محدوده ورود 30m برای سیگنال 1H را تایید کرد."]))
        if precision >= 62:
            return EntryPrecisionResult("READY", precision, max(7, WEIGHTS.entry_precision - 2), 76, tuple(reasons + ["AI محدوده ورود را قابل قبول می‌داند."]))
        if precision >= 38 and flow_ok:
            return EntryPrecisionResult("WATCH", precision, max(4, WEIGHTS.entry_precision - 5), 58, tuple(reasons + ["AI هنوز دنبال تایید 30m بهتر است، ولی قفل نیست."]))
        return EntryPrecisionResult("WAIT", precision, 1, 35, tuple(reasons + ["AI ورود دقیق 30m را هنوز کامل تایید نکرده است."]))
