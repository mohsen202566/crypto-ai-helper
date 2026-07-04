from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import (
    MAX_ATR_PCT,
    MAX_VOLUME_RATIO_SOFT,
    SESSION_OPEN_MAX_WATCH_MINUTES,
)
from utils import now_utc


@dataclass(frozen=True)
class SessionInfo:
    name: str
    label: str
    start_minute_utc: int
    minutes_from_open: int
    is_open_watch: bool
    hour_bucket: str
    weekday: str


SESSION_STARTS_UTC: tuple[tuple[str, str, int], ...] = (
    ("ASIA", "شروع سشن آسیا", 0),       # 00:00 UTC
    ("EUROPE", "شروع سشن اروپا", 7 * 60),  # 07:00 UTC
    ("AMERICA", "شروع سشن آمریکا", 13 * 60 + 30),  # 13:30 UTC
)


def parse_utc(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def half_hour_bucket(dt: datetime | None = None) -> str:
    dt = (dt or now_utc()).astimezone(timezone.utc)
    return f"{dt.hour:02d}:{0 if dt.minute < 30 else 30:02d}"


def weekday_key(dt: datetime | None = None) -> str:
    dt = (dt or now_utc()).astimezone(timezone.utc)
    return dt.strftime("%a").upper()


def session_info(dt: datetime | None = None) -> SessionInfo:
    dt = (dt or now_utc()).astimezone(timezone.utc)
    minute = dt.hour * 60 + dt.minute
    best_name = "OFF_SESSION"
    best_label = "خارج از شروع سشن"
    best_start = -1
    best_diff = 10_000
    for name, label, start in SESSION_STARTS_UTC:
        diff = minute - start
        if diff < 0:
            diff += 24 * 60
        if diff < best_diff:
            best_name, best_label, best_start, best_diff = name, label, start, diff
    return SessionInfo(
        name=best_name,
        label=best_label,
        start_minute_utc=best_start,
        minutes_from_open=int(best_diff),
        is_open_watch=0 <= best_diff <= SESSION_OPEN_MAX_WATCH_MINUTES,
        hour_bucket=half_hour_bucket(dt),
        weekday=weekday_key(dt),
    )


def decision_market_is_calm(decision: Any) -> bool:
    market_state = str(getattr(decision, "market_state", "") or "").upper()
    atr_pct = float(getattr(decision, "atr_pct", 0.0) or 0.0)
    volume_ratio = float(getattr(decision, "volume_ratio", 0.0) or 0.0)
    adx = float(getattr(decision, "adx", 0.0) or 0.0)
    if market_state in {"CLIMAX", "FAKE_BREAKOUT_RISK", "NOISY"}:
        return False
    if volume_ratio >= MAX_VOLUME_RATIO_SOFT:
        return False
    if atr_pct >= max(MAX_ATR_PCT * 0.85, 0.012):
        return False
    if market_state == "BREAKOUT" and (volume_ratio > 2.7 or adx > 30):
        return False
    return True


def signal_time(signal: dict[str, Any]) -> datetime:
    return parse_utc(str(signal.get("created_at") or "")) or now_utc()


def result_time(signal: dict[str, Any]) -> datetime:
    return parse_utc(str(signal.get("result_at") or signal.get("created_at") or "")) or now_utc()
