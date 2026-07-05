from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any

from config import (
    STOP_GUARD_BASE_COOLDOWN_MINUTES,
    STOP_GUARD_CAUTION_MIN_CONFIDENCE,
    STOP_GUARD_CONSECUTIVE_SL,
    STOP_GUARD_ENABLED,
    STOP_GUARD_LEARNED_RISK_MIN_SAMPLES,
    STOP_GUARD_MAX_COOLDOWN_MINUTES,
    STOP_GUARD_WINDOW_MINUTES,
)
from fundamental_guard import FundamentalGuard
from guard_types import GuardVerdict
from guard_utils import decision_market_is_calm, result_time, session_info, signal_time, parse_utc
from utils import now_utc


class StopLossGuard:
    """Learns from stop-loss clusters and blocks repeated mistakes externally.

    The AI entry/TP/SL logic is not modified. This guard watches closed results, explains SL clusters,
    stores bad hour/session/day profiles, and later downgrades or blocks signals in similar conditions.
    """

    def __init__(self, storage) -> None:
        self.storage = storage

    def evaluate(self, symbol_name: str, decision) -> GuardVerdict:
        if not STOP_GUARD_ENABLED:
            return GuardVerdict()
        now = now_utc()
        cooldown = self.storage.guard_cooldown()
        until = parse_utc(cooldown.get("until"))
        reason = cooldown.get("reason") or ""
        if until and now < until:
            payload = {"until": until.isoformat(), "reason": reason}
            return GuardVerdict("BLOCK", "STOP_GUARD", f"ترمز درمان استاپ فعال است تا {until.strftime('%H:%M UTC')}: {reason}؛ سیگنال جدید در این پنجره Reject می‌شود.", STOP_GUARD_CAUTION_MIN_CONFIDENCE, payload)
        profile = self.storage.get_time_risk_profile(symbol_name=symbol_name, direction=getattr(decision, "direction", None))
        if profile and int(profile.get("samples") or 0) >= STOP_GUARD_LEARNED_RISK_MIN_SAMPLES:
            action = str(profile.get("action") or "ALLOW")
            risk_score = int(profile.get("risk_score") or 0)
            cause = str(profile.get("main_cause") or "BAD_HOUR_LEARNED")
            calm = decision_market_is_calm(decision)
            payload = {"profile": profile, "calm": calm}
            if action == "BLOCK" and not calm:
                return GuardVerdict("BLOCK", "STOP_GUARD", f"حافظه AI این ساعت/سشن را پرریسک می‌داند ({cause}, risk={risk_score}) و بازار آرام نیست؛ سیگنال Reject می‌شود.", STOP_GUARD_CAUTION_MIN_CONFIDENCE, payload)
            if action == "BLOCK":
                return GuardVerdict("BLOCK", "STOP_GUARD", f"حافظه AI این ساعت/سشن را بد می‌شناسد ({cause}); سیگنال Reject می‌شود.", STOP_GUARD_CAUTION_MIN_CONFIDENCE, payload)
            if action == "REAL_BLOCK":
                return GuardVerdict("REAL_BLOCK", "STOP_GUARD", f"حافظه SLهای قبلی هشدار می‌دهد ({cause}, risk={risk_score}); Real بسته است.", STOP_GUARD_CAUTION_MIN_CONFIDENCE, payload)
            if action == "CAUTION":
                return GuardVerdict("CAUTION", "STOP_GUARD", f"این ساعت/سشن قبلاً SL بیشتری داده ({cause}); فقط سیگنال قوی مجاز است.", STOP_GUARD_CAUTION_MIN_CONFIDENCE, payload)
        return GuardVerdict()

    def learn_from_closed_signal(self, signal_id: int, failure_reason: str) -> None:
        if not STOP_GUARD_ENABLED:
            return
        signal = self.storage.signal_dict(signal_id)
        if not signal:
            return
        status = str(signal.get("status") or "").upper()
        if status not in {"TP", "SL"}:
            return
        cause = failure_reason if status == "SL" else "TP_OK"
        self.storage.update_time_risk_profile(signal=signal, result=status, cause=cause)
        if status != "SL":
            return
        cluster = self._recent_sl_cluster()
        if len(cluster) < STOP_GUARD_CONSECUTIVE_SL:
            return
        diagnosis = self._diagnose_cluster(cluster)
        cooldown_minutes = self._cooldown_minutes(len(cluster), diagnosis["severity"])
        until = now_utc() + timedelta(minutes=cooldown_minutes)
        reason = diagnosis["message"]
        self.storage.set_guard_cooldown(until.isoformat(), reason)
        self.storage.record_guard_event("STOP_GUARD", diagnosis["severity"], reason, "BLOCK", diagnosis)
        # Save the cluster cause into each recent losing profile so future similar hours/sessions are avoided.
        for row in cluster:
            self.storage.update_time_risk_profile(signal=row, result="SL", cause=diagnosis["main_cause"])

    def _recent_sl_cluster(self) -> list[dict[str, Any]]:
        rows = self.storage.recent_closed_signals(limit=20, minutes=STOP_GUARD_WINDOW_MINUTES)
        cluster: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("status") or "").upper() == "SL":
                cluster.append(row)
            else:
                break
        return list(reversed(cluster))

    def _diagnose_cluster(self, cluster: list[dict[str, Any]]) -> dict[str, Any]:
        symbols = [str(r.get("symbol_name") or "?") for r in cluster]
        directions = [str(r.get("direction") or "?") for r in cluster]
        market_states = [str(r.get("market_state") or "") for r in cluster]
        reasons = [str(r.get("reason") or "") for r in cluster]
        failure_reasons = []
        for r in cluster:
            # range_observations contains the exact learning failure reason, but signal.reason also helps.
            m = str(r.get("market_state") or "")
            if m in {"CLIMAX", "FAKE_BREAKOUT_RISK"}:
                failure_reasons.append("FAKE_BREAKOUT_OR_CLIMAX")
            if "BTC" in str(r.get("reason") or "") or "ETH" in str(r.get("reason") or ""):
                failure_reasons.append("BTC_ETH_CONFLICT")
        infos = [session_info(signal_time(r)) for r in cluster]
        same_session = Counter(i.name for i in infos).most_common(1)[0]
        same_hour = Counter(i.hour_bucket for i in infos).most_common(1)[0]
        distinct_symbols = len(set(symbols))
        distinct_dirs = len(set(directions))

        start = min(signal_time(r) for r in cluster) - timedelta(minutes=10)
        end = max(result_time(r) for r in cluster) + timedelta(minutes=10)
        news_near = FundamentalGuard(self.storage).has_event_near(start, end, high_only=False)

        cause_scores: Counter[str] = Counter()
        if news_near:
            cause_scores["NEWS_RISK"] += 5
        if same_session[1] >= max(2, len(cluster) - 1) and any(i.is_open_watch for i in infos):
            cause_scores["SESSION_OPEN_RISK"] += 4
        if same_hour[1] >= max(2, len(cluster) - 1):
            cause_scores["BAD_HOUR_LEARNED"] += 3
        if distinct_symbols >= 3:
            cause_scores["MARKET_WIDE_REVERSAL"] += 4
        if distinct_dirs == 1 and distinct_symbols >= 2:
            cause_scores["MARKET_WIDE_REVERSAL"] += 2
        if any(m in {"CLIMAX", "FAKE_BREAKOUT_RISK"} for m in market_states):
            cause_scores["FAKE_BREAKOUT_OR_CLIMAX"] += 4
        if any(m in {"NOISY", "BREAKOUT"} for m in market_states):
            cause_scores["HIGH_VOLATILITY"] += 2
        if any("BTC" in r or "ETH" in r for r in reasons):
            cause_scores["BTC_ETH_CONFLICT"] += 3
        if distinct_symbols == 1:
            cause_scores["SYMBOL_SPECIFIC"] += 3
        if not cause_scores:
            cause_scores["UNKNOWN_CLUSTER"] = 1

        main_cause, score = cause_scores.most_common(1)[0]
        severity = "high" if len(cluster) >= STOP_GUARD_CONSECUTIVE_SL + 1 or score >= 5 else "medium"
        session_text = f"{same_session[0]} / {same_hour[0]} UTC"
        symbol_text = ", ".join(symbols[-5:])
        message = (
            f"{len(cluster)} استاپ پشت‌سرهم تشخیص داده شد. علت غالب: {main_cause}. "
            f"سشن/ساعت: {session_text}. ارزها: {symbol_text}. "
            "ربات Real را می‌بندد و اگر ریسک قابل کنترل نباشد سیگنال را Reject می‌کند. این ساعت/الگو در حافظه ریسک ذخیره شد."
        )
        return {
            "main_cause": main_cause,
            "all_causes": dict(cause_scores),
            "severity": severity,
            "session": same_session[0],
            "hour_bucket": same_hour[0],
            "symbols": symbols,
            "directions": directions,
            "market_states": market_states,
            "message": message,
        }

    @staticmethod
    def _cooldown_minutes(sl_count: int, severity: str) -> int:
        base = STOP_GUARD_BASE_COOLDOWN_MINUTES
        extra = max(0, sl_count - STOP_GUARD_CONSECUTIVE_SL) * 5
        if severity == "high":
            extra += 5
        return max(base, min(STOP_GUARD_MAX_COOLDOWN_MINUTES, base + extra))
