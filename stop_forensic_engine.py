from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fundamental_guard import FundamentalGuard
from guard_utils import session_info, signal_time, result_time
from utils import now_utc


@dataclass(frozen=True)
class StopForensicReport:
    result: str
    primary_cause: str
    secondary_causes: tuple[str, ...]
    cause_scores: dict[str, int]
    fix_policy: str
    action: str
    treatment_level: int
    message: str
    indicator_suggestion: str | None = None


class StopForensicEngine:
    """Single-stop forensic investigator.

    Every closed signal is treated as a case.  The purpose is not to reject signals
    blindly; it is to discover why a result happened and create a practical fix that
    can be tested on future similar signals.
    """

    def __init__(self, storage) -> None:
        self.storage = storage
        self.news = FundamentalGuard(storage)

    def analyze(self, signal: dict[str, Any]) -> StopForensicReport:
        status = str(signal.get("status") or "").upper()
        if status == "TP":
            return StopForensicReport(
                result="TP",
                primary_cause="TP_OK",
                secondary_causes=(),
                cause_scores={"TP_OK": 100},
                fix_policy="REINFORCE_SUCCESSFUL_CONTEXT",
                action="ALLOW",
                treatment_level=0,
                message="این الگو به TP رسید؛ درمان‌های فعال مشابه ضعیف‌تر یا تایید موفقیت قوی‌تر می‌شود.",
            )

        scores = self._score_loss_causes(signal)
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        primary = ranked[0][0] if ranked else "UNKNOWN_SL_REASON"
        secondary = tuple(k for k, v in ranked[1:5] if v >= 25)
        policy, action, level = self._fix_for_cause(primary, secondary, signal, scores)
        suggestion = self._indicator_suggestion(primary, secondary, signal, scores)
        msg = self._message(signal, primary, secondary, policy, action, scores, suggestion)
        return StopForensicReport(
            result="SL",
            primary_cause=primary,
            secondary_causes=secondary,
            cause_scores=dict(ranked),
            fix_policy=policy,
            action=action,
            treatment_level=level,
            message=msg,
            indicator_suggestion=suggestion,
        )

    def _score_loss_causes(self, signal: dict[str, Any]) -> dict[str, int]:
        scores: dict[str, int] = {}

        def add(name: str, points: int) -> None:
            scores[name] = max(0, min(100, scores.get(name, 0) + int(points)))

        entry = float(signal.get("entry") or 0.0)
        tp = float(signal.get("tp") or 0.0)
        sl = float(signal.get("sl") or 0.0)
        mfe = float(signal.get("mfe_pct") or 0.0)
        mae = float(signal.get("mae_pct") or 0.0)
        tp_dist = float(signal.get("tp_distance_pct") or 0.0)
        sl_dist = float(signal.get("sl_distance_pct") or 0.0)
        rr = float(signal.get("risk_reward") or 0.0)
        net_expected = float(signal.get("estimated_net_profit_usdt") or 0.0)
        cost_pct = float(signal.get("estimated_cost_pct") or 0.0)
        market_state = str(signal.get("market_state") or "").upper()
        alignment = str(signal.get("alignment") or "").upper()
        reason = str(signal.get("reason") or "")
        indicator = str(signal.get("indicator_profile") or "")
        created = signal_time(signal)
        result_at = result_time(signal)
        info = session_info(created)
        minutes_to_sl = max(0.0, (result_at - created).total_seconds() / 60.0)

        # Economic edge: when the expected net is tiny, even TP is not worth the fee risk.
        if net_expected < 0.011:
            add("ECONOMIC_EDGE_TOO_SMALL", 65)
        if cost_pct > 0 and tp_dist > 0 and cost_pct >= tp_dist * 0.45:
            add("FEE_TOO_HEAVY_FOR_TARGET", 55)
        if rr and rr < 1.25:
            add("RR_TOO_WEAK", 35)

        # Session and news.  These are the only areas that can later cause temporary signal pause.
        start = created - timedelta(minutes=20)
        end = result_at + timedelta(minutes=20)
        if self.news.has_event_near(start, end, high_only=False) or "NEWS" in reason.upper() or "خبر" in reason:
            add("NEWS_RISK", 85)
        if info.is_open_watch:
            add("SESSION_OPEN_NOISE", 60 if info.name in {"EUROPE", "AMERICA"} else 42)
            if minutes_to_sl <= 20:
                add("SESSION_OPEN_NOISE", 18)

        # Market mode / volatility / fake movement.
        if market_state in {"CLIMAX", "FAKE_BREAKOUT_RISK"}:
            add("FAKE_BREAKOUT_OR_CLIMAX", 82)
        if market_state in {"RANGE", "NOISY", "DEAD_MARKET"}:
            add("MARKET_NOISE_OR_RANGE", 72)
        if market_state == "BREAKOUT" and minutes_to_sl <= 20:
            add("FAKE_BREAKOUT_OR_CLIMAX", 35)

        if "BTC" in reason or "ETH" in reason:
            add("BTC_ETH_CONFLICT", 68)
        if alignment == "BAD" or "خلاف جهت" in reason:
            add("HTF_ALIGNMENT_WEAKNESS", 62)

        # Price-path clues from MFE/MAE.
        if sl_dist > 0 and mae >= sl_dist * 0.90 and mfe < max(tp_dist * 0.22, 0.00001):
            add("WRONG_DIRECTION_OR_CONTEXT", 65)
        if tp_dist > 0 and mfe >= tp_dist * 0.58:
            add("TP_TOO_FAR_OR_REVERSAL", 74)
        if sl_dist > 0 and mae <= sl_dist * 1.10 and mfe >= tp_dist * 0.18:
            add("STOP_TOO_TIGHT", 62)
        if sl_dist > 0 and sl_dist < max(cost_pct * 1.30, 0.0012):
            add("STOP_TOO_TIGHT", 35)
        if sl_dist > 0 and sl_dist > tp_dist * 1.05:
            add("STOP_TOO_WIDE_OR_RR_DAMAGE", 42)

        # Indicator-context clues from stored indicator profile.  No new indicator is used for signal issuance.
        upper = indicator.upper()
        if "ADX" in upper:
            try:
                # Pattern in current text: "ADX 14.5".
                adx_val = float(upper.split("ADX", 1)[1].strip().split()[0])
                if adx_val < 14:
                    add("INDICATOR_CONTEXT_BAD", 54)
                    add("MARKET_NOISE_OR_RANGE", 18)
                elif adx_val > 34 and market_state in {"CLIMAX", "BREAKOUT"}:
                    add("VOLATILITY_OVERHEAT", 40)
            except Exception:
                pass
        if "VOL" in upper:
            try:
                vol_val = float(upper.rsplit("VOL", 1)[1].strip().split()[0])
                if vol_val > 3.8:
                    add("VOLATILITY_OVERHEAT", 60)
                elif vol_val < 0.55:
                    add("MARKET_NOISE_OR_RANGE", 35)
            except Exception:
                pass
        if "ATR" in upper:
            try:
                atr_text = upper.split("ATR", 1)[1].strip().split("%", 1)[0]
                atr_pct = float(atr_text) / 100.0
                if atr_pct > 0.014:
                    add("VOLATILITY_OVERHEAT", 56)
                if atr_pct > 0 and sl_dist > 0 and sl_dist < atr_pct * 0.70:
                    add("STOP_TOO_TIGHT", 45)
            except Exception:
                pass

        if not scores:
            add("UNKNOWN_SL_REASON", 30)
        return scores

    @staticmethod
    def _fix_for_cause(primary: str, secondary: tuple[str, ...], signal: dict[str, Any], scores: dict[str, int]) -> tuple[str, str, int]:
        # action meanings: ALLOW, CAUTION, REAL_BLOCK, WATCH_ONLY, SESSION_PAUSE, NEWS_PAUSE.
        if primary == "NEWS_RISK":
            return "PAUSE_NEWS_15M_THEN_NORMAL_OR_WATCH_UNTIL_CALM", "NEWS_PAUSE", 4
        if primary == "SESSION_OPEN_NOISE":
            return "PAUSE_SESSION_10M_THEN_NORMAL_OR_WATCH_UNTIL_CALM", "SESSION_PAUSE", 3
        if primary == "ECONOMIC_EDGE_TOO_SMALL" or primary == "FEE_TOO_HEAVY_FOR_TARGET":
            return "REQUIRE_NET_PROFIT_AFTER_FEE_OR_KEEP_AS_WATCH", "WATCH_ONLY", 3
        if primary == "STOP_TOO_TIGHT":
            return "TEST_STRUCTURAL_WIDER_SL_ONLY_IF_RR_AND_NET_STAY_VALID", "CAUTION", 2
        if primary == "TP_TOO_FAR_OR_REVERSAL":
            return "TEST_MORE_REALISTIC_TP_FROM_PREVIOUS_MFE", "CAUTION", 2
        if primary == "STOP_TOO_WIDE_OR_RR_DAMAGE":
            return "KEEP_TP_SL_ONLY_IF_RR_AND_NET_ARE_VALID_ELSE_WATCH", "WATCH_ONLY", 3
        if primary in {"MARKET_NOISE_OR_RANGE", "FAKE_BREAKOUT_OR_CLIMAX", "VOLATILITY_OVERHEAT"}:
            return "NORMAL_OR_WATCH_AND_REQUIRE_CALMER_MARKET_CONTEXT", "REAL_BLOCK", 3
        if primary in {"HTF_ALIGNMENT_WEAKNESS", "BTC_ETH_CONFLICT", "WRONG_DIRECTION_OR_CONTEXT", "INDICATOR_CONTEXT_BAD"}:
            return "DOWNGRADE_REAL_AND_TEST_STRONGER_CONFIRMATION_FOR_SAME_CONTEXT", "REAL_BLOCK", 3
        return "FORENSIC_CAUTION_AND_TEST_NEXT_SIMILAR_SIGNALS", "CAUTION", 1

    @staticmethod
    def _indicator_suggestion(primary: str, secondary: tuple[str, ...], signal: dict[str, Any], scores: dict[str, int]) -> str | None:
        causes = {primary, *secondary}
        if "MARKET_NOISE_OR_RANGE" in causes and scores.get("MARKET_NOISE_OR_RANGE", 0) >= 70:
            return "پیشنهاد فقط برای تحلیل استاپ: Choppiness Index یا Bollinger/Keltner Squeeze برای تشخیص بهتر رنج/نویز."
        if "FAKE_BREAKOUT_OR_CLIMAX" in causes:
            return "پیشنهاد فقط برای تحلیل استاپ: Donchian/Breakout Retest یا Bollinger Width برای تشخیص شکست فیک/کلایمکس."
        if "VOLATILITY_OVERHEAT" in causes:
            return "پیشنهاد فقط برای تحلیل استاپ: ATR Percentile برای تشخیص نوسان غیرعادی قبل از ورود."
        if "INDICATOR_CONTEXT_BAD" in causes:
            return "پیشنهاد فقط برای تحلیل استاپ: بررسی وزن RSI/MACD/ADX در همان شرایط؛ اندیکاتور جدید فقط با تایید شما اضافه شود."
        return None

    @staticmethod
    def _message(signal: dict[str, Any], primary: str, secondary: tuple[str, ...], policy: str, action: str, scores: dict[str, int], suggestion: str | None) -> str:
        symbol = signal.get("symbol_name", "-")
        direction = signal.get("direction", "-")
        sec = ", ".join(secondary) if secondary else "-"
        compact_scores = ", ".join(f"{k}:{v}" for k, v in sorted(scores.items(), key=lambda x: x[1], reverse=True)[:5])
        text = (
            f"پرونده استاپ {symbol} {direction}: علت اصلی {primary}، علت‌های فرعی {sec}. "
            f"امتیاز علت‌ها: {compact_scores}. درمان فعال: {policy} ({action})."
        )
        if suggestion:
            text += " " + suggestion
        return text[:1200]
