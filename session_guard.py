from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time, timezone
from zoneinfo import ZoneInfo

from config import (
    BOT_TIMEZONE,
    SESSION_GUARD_ENABLED,
    SESSION_CAUTION_MIN_CONFIDENCE,
    SESSION_WINDOWS,
)
from utils import now_utc, session_bucket


@dataclass(frozen=True)
class GuardDecision:
    name: str
    level: str
    normal_allowed: bool
    real_allowed: bool
    confidence_penalty: int
    reason: str
    hard_block: bool = False
    caution: bool = False

    @staticmethod
    def ok(name: str = "guard") -> "GuardDecision":
        return GuardDecision(name, "OK", True, True, 0, "")


@dataclass(frozen=True)
class SessionWindow:
    start: dt_time
    end: dt_time
    level: str
    label: str
    reason: str


class SessionGuard:
    """Time/session risk layer.

    The guard is intentionally conservative around repeated bad transition windows.
    It does not replace AIBrain; it only blocks Real, adds confidence penalties,
    or hard-blocks new signals during very risky windows.
    """

    def __init__(self) -> None:
        self.tz = ZoneInfo(BOT_TIMEZONE)
        self.windows = self._parse_windows(SESSION_WINDOWS)

    def evaluate(self, now: datetime | None = None) -> GuardDecision:
        if not SESSION_GUARD_ENABLED:
            return GuardDecision.ok("session")
        local = (now or now_utc()).astimezone(self.tz)
        current = local.time().replace(second=0, microsecond=0)
        bucket = session_bucket(local)
        for window in self.windows:
            if self._contains(window.start, window.end, current):
                level = window.level.upper()
                if level == "BLOCK":
                    return GuardDecision(
                        "session", level, False, False, 100,
                        f"SESSION_BLOCK {bucket} {window.label}: {window.reason}",
                        hard_block=True,
                        caution=True,
                    )
                if level == "REAL_BLOCK":
                    return GuardDecision(
                        "session", level, True, False, max(10, SESSION_CAUTION_MIN_CONFIDENCE // 2),
                        f"SESSION_REAL_BLOCK {bucket} {window.label}: {window.reason}",
                        caution=True,
                    )
                if level == "CAUTION":
                    return GuardDecision(
                        "session", level, True, False, 15,
                        f"SESSION_CAUTION {bucket} {window.label}: {window.reason}",
                        caution=True,
                    )
        return GuardDecision.ok("session")

    @staticmethod
    def _contains(start: dt_time, end: dt_time, current: dt_time) -> bool:
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end

    @staticmethod
    def _parse_time(value: str) -> dt_time:
        hh, mm = value.strip().split(":", 1)
        return dt_time(hour=int(hh), minute=int(mm))

    def _parse_windows(self, raw: str) -> tuple[SessionWindow, ...]:
        windows: list[SessionWindow] = []
        for item in (raw or "").split(";"):
            item = item.strip()
            if not item:
                continue
            try:
                time_range, level, label, reason = (part.strip() for part in item.split("|", 3))
                start_s, end_s = (part.strip() for part in time_range.split("-", 1))
                windows.append(SessionWindow(self._parse_time(start_s), self._parse_time(end_s), level.upper(), label, reason))
            except Exception:
                continue
        return tuple(windows)
