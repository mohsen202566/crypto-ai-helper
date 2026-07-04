from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from config import (
    RISK_GUARD_ENABLED,
    STOP_GUARD_LOOKBACK_MINUTES,
    STOP_GUARD_MIN_STREAK,
    STOP_GUARD_MIN_COUNT,
    STOP_GUARD_COOLDOWN_MINUTES,
    STOP_GUARD_EXTENDED_COOLDOWN_MINUTES,
    STOP_PROFILE_MIN_SAMPLES,
    STOP_PROFILE_BAD_SL_RATE,
    STOP_PROFILE_HARD_SL_RATE,
)
from session_guard import GuardDecision
from storage import Storage
from utils import now_utc, session_bucket


@dataclass(frozen=True)
class StopRiskSnapshot:
    streak_sl: int
    sl_count: int
    tp_count: int
    reason: str


class RiskGuard:
    """Protects the bot after stop-loss clusters and remembers repeated SL causes."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def evaluate(self, symbol_name: str | None = None, direction: str | None = None) -> GuardDecision:
        if not RISK_GUARD_ENABLED:
            return GuardDecision.ok("risk")
        now = now_utc()
        state = self.storage.get_guard_state("stop_guard")
        cooldown_until = self._parse_dt(state.get("cooldown_until")) if state else None
        if cooldown_until and cooldown_until > now:
            minutes_left = max(1, int((cooldown_until - now).total_seconds() // 60) + 1)
            if state.get("level") == "cooldown":
                return GuardDecision(
                    "risk", "COOLDOWN", False, False, 100,
                    f"STOP_GUARD_COOLDOWN: بعد از چند SL، سیگنال جدید تا حدود {minutes_left} دقیقه دیگر بسته است. علت: {state.get('reason','')}",
                    hard_block=True,
                    caution=True,
                )
            return GuardDecision(
                "risk", "CAUTION", True, False, 25,
                f"STOP_GUARD_CAUTION: بعد از چند SL، تا حدود {minutes_left} دقیقه Real بسته و Normal سخت‌گیر است. علت: {state.get('reason','')}",
                caution=True,
            )

        snapshot = self._recent_stop_snapshot()
        if snapshot.streak_sl >= STOP_GUARD_MIN_STREAK + 2 or snapshot.sl_count >= STOP_GUARD_MIN_COUNT + 2:
            until = now + timedelta(minutes=STOP_GUARD_EXTENDED_COOLDOWN_MINUTES)
            self.storage.set_guard_state("stop_guard", "cooldown", until.isoformat(), snapshot.reason)
            return GuardDecision("risk", "COOLDOWN", False, False, 100, f"STOP_GUARD_COOLDOWN: {snapshot.reason}", hard_block=True, caution=True)
        if snapshot.streak_sl >= STOP_GUARD_MIN_STREAK or snapshot.sl_count >= STOP_GUARD_MIN_COUNT:
            # First step is caution. If the pattern continues the next scan will hard-cooldown.
            self.storage.set_guard_state("stop_guard", "caution", (now + timedelta(minutes=STOP_GUARD_COOLDOWN_MINUTES)).isoformat(), snapshot.reason)
            return GuardDecision("risk", "CAUTION", True, False, 25, f"STOP_GUARD_CAUTION: {snapshot.reason}", caution=True)

        if symbol_name and direction:
            profile_decision = self._profile_risk(symbol_name, direction)
            if profile_decision:
                return profile_decision

        if state and state.get("level") in {"caution", "cooldown"}:
            # Keep a mild caution after old state until a few TPs arrive.
            if snapshot.tp_count >= 2 and snapshot.sl_count == 0:
                self.storage.clear_guard_state("stop_guard")
            elif state.get("level") == "caution":
                return GuardDecision("risk", "CAUTION", True, False, 15, f"STOP_GUARD_RECOVERY: هنوز بعد از حالت احتیاط هستیم. {state.get('reason','')}", caution=True)

        return GuardDecision.ok("risk")

    def _recent_stop_snapshot(self) -> StopRiskSnapshot:
        rows = self.storage.recent_closed_signals(minutes=STOP_GUARD_LOOKBACK_MINUTES, limit=30)
        streak = 0
        for row in rows:
            if str(row.get("status")) == "SL":
                streak += 1
            else:
                break
        sl_count = sum(1 for row in rows if str(row.get("status")) == "SL")
        tp_count = sum(1 for row in rows if str(row.get("status")) == "TP")
        reasons = [str(row.get("stop_reason") or row.get("result_reason") or row.get("reason") or "") for row in rows if str(row.get("status")) == "SL"]
        common = self._common_reason(reasons)
        reason = f"{streak} SL پشت‌سرهم / {sl_count} SL در {STOP_GUARD_LOOKBACK_MINUTES} دقیقه اخیر"
        if common:
            reason += f" | دلیل مشترک: {common}"
        return StopRiskSnapshot(streak, sl_count, tp_count, reason)

    def _profile_risk(self, symbol_name: str, direction: str) -> GuardDecision | None:
        bucket = session_bucket()
        profiles = [
            self.storage.get_stop_reason_profile(symbol_name=symbol_name, direction=direction, session_bucket=bucket),
            self.storage.get_stop_reason_profile(symbol_name="ALL", direction=direction, session_bucket=bucket),
            self.storage.get_stop_reason_profile(symbol_name="ALL", direction="ALL", session_bucket=bucket),
        ]
        for profile in profiles:
            if not profile:
                continue
            samples = int(profile.get("samples") or 0)
            sl = int(profile.get("sl_count") or 0)
            if samples < STOP_PROFILE_MIN_SAMPLES:
                continue
            sl_rate = sl / max(samples, 1) * 100.0
            reason = str(profile.get("common_reason") or profile.get("last_reason") or "تکرار SL در همین ساعت/شرایط")
            owner = f"{profile.get('symbol_name')} {profile.get('direction')} {profile.get('session_bucket')}"
            if sl_rate >= STOP_PROFILE_HARD_SL_RATE:
                return GuardDecision("risk", "PROFILE_BLOCK", False, False, 100, f"STOP_PROFILE_BLOCK {owner}: SL rate {sl_rate:.1f}% | {reason}", hard_block=True, caution=True)
            if sl_rate >= STOP_PROFILE_BAD_SL_RATE:
                return GuardDecision("risk", "PROFILE_CAUTION", True, False, 20, f"STOP_PROFILE_CAUTION {owner}: SL rate {sl_rate:.1f}% | {reason}", caution=True)
        return None

    @staticmethod
    def _common_reason(reasons: list[str]) -> str:
        counts: dict[str, int] = {}
        for reason in reasons:
            key = reason.split("|")[0].strip()
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
        if not counts:
            return ""
        return max(counts.items(), key=lambda item: item[1])[0]

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value))
            return dt if dt.tzinfo else dt.replace(tzinfo=now_utc().tzinfo)
        except Exception:
            return None
