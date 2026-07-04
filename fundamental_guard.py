from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import (
    FUNDAMENTAL_ALERT_BEFORE_MINUTES,
    FUNDAMENTAL_BLOCK_AFTER_MINUTES,
    FUNDAMENTAL_BLOCK_BEFORE_MINUTES,
    FUNDAMENTAL_EVENTS_FILE,
    FUNDAMENTAL_GUARD_ENABLED,
    FUNDAMENTAL_STRICT_AFTER_MINUTES,
)
from guard_types import GuardVerdict
from guard_utils import parse_utc
from utils import now_utc

_MARKET_WIDE_CATEGORIES = {
    "MACRO",
    "FOMC",
    "FED",
    "CPI",
    "PPI",
    "NFP",
    "PCE",
    "GDP",
    "RATES",
    "ETF",
    "SEC",
    "CFTC",
    "REGULATION",
    "STABLECOIN",
    "EXCHANGE_CRISIS",
    "BANKING",
    "LIQUIDITY",
    "WAR",
}

_HIGH_IMPACT = {"HIGH", "CRITICAL", "VERY_HIGH", "RED"}
_MEDIUM_IMPACT = {"MEDIUM", "YELLOW", "NORMAL"}


@dataclass(frozen=True)
class FundamentalEvent:
    event_id: str
    title: str
    time_utc: Any
    impact: str
    category: str
    market_wide: bool
    source: str = "manual"

    @property
    def dt(self):
        return parse_utc(self.time_utc)


class FundamentalGuard:
    """Reads manually curated market-wide news/events and acts as an external safety brake.

    This guard intentionally ignores normal coin-specific listings, delistings, unlocks and project news.
    It only reacts to market-wide macro/regulatory/liquidity/stablecoin/exchange-crisis events.
    """

    def __init__(self, storage) -> None:
        self.storage = storage
        self.path = Path(FUNDAMENTAL_EVENTS_FILE)

    def _load_events(self) -> list[FundamentalEvent]:
        if not FUNDAMENTAL_GUARD_ENABLED:
            return []
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []
        items = raw.get("events", raw) if isinstance(raw, dict) else raw
        events: list[FundamentalEvent] = []
        if not isinstance(items, list):
            return events
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("name") or "Fundamental Event").strip()
            time_utc = item.get("time_utc") or item.get("datetime_utc") or item.get("at") or item.get("time")
            impact = str(item.get("impact") or item.get("severity") or "MEDIUM").upper()
            category = str(item.get("category") or item.get("type") or "MACRO").upper()
            market_wide = bool(item.get("market_wide", category in _MARKET_WIDE_CATEGORIES))
            event_id = str(item.get("id") or f"{title}|{time_utc}|{idx}")
            source = str(item.get("source") or "manual")
            ev = FundamentalEvent(event_id, title, time_utc, impact, category, market_wide, source)
            if ev.dt is None:
                continue
            if not ev.market_wide and ev.category not in _MARKET_WIDE_CATEGORIES:
                continue
            # Explicitly ignore low-value coin-specific events unless the user marks market_wide=true.
            if ev.category in {"LISTING", "DELISTING", "UNLOCK", "BURN", "PARTNERSHIP", "PROJECT"} and not ev.market_wide:
                continue
            events.append(ev)
        return events

    def evaluate(self) -> GuardVerdict:
        events = self._load_events()
        now = now_utc()
        active: list[tuple[float, FundamentalEvent]] = []
        for ev in events:
            dt = ev.dt
            if dt is None:
                continue
            minutes = (dt - now).total_seconds() / 60.0
            if -FUNDAMENTAL_STRICT_AFTER_MINUTES <= minutes <= FUNDAMENTAL_BLOCK_BEFORE_MINUTES:
                active.append((minutes, ev))
        if not active:
            return GuardVerdict()
        minutes, ev = min(active, key=lambda x: abs(x[0]))
        when = f"{minutes:.0f} دقیقه مانده" if minutes >= 0 else f"{abs(minutes):.0f} دقیقه بعد خبر"
        impact = ev.impact.upper()
        reason = f"خبر کل‌مارکتی {ev.title} ({ev.category}/{impact})؛ {when}."
        payload = {"event_id": ev.event_id, "title": ev.title, "minutes": minutes, "impact": impact, "category": ev.category}
        if impact in _HIGH_IMPACT and -FUNDAMENTAL_BLOCK_AFTER_MINUTES <= minutes <= FUNDAMENTAL_BLOCK_BEFORE_MINUTES:
            return GuardVerdict("BLOCK", "NEWS_GUARD", reason + " سیگنال جدید تا آرام‌شدن خبر بسته است.", 0, payload)
        if impact in _HIGH_IMPACT:
            return GuardVerdict("REAL_BLOCK", "NEWS_GUARD", reason + " Real موقتاً بسته و Normal سخت‌گیر می‌شود.", 64, payload)
        if impact in _MEDIUM_IMPACT:
            return GuardVerdict("REAL_BLOCK", "NEWS_GUARD", reason + " اثر متوسط؛ Real محتاطانه بسته می‌شود.", 60, payload)
        return GuardVerdict("CAUTION", "NEWS_GUARD", reason + " احتیاط خبری فعال است.", 58, payload)

    def pending_alert_messages(self) -> list[str]:
        if not FUNDAMENTAL_GUARD_ENABLED:
            return []
        out: list[str] = []
        now = now_utc()
        for ev in self._load_events():
            dt = ev.dt
            if dt is None:
                continue
            minutes = (dt - now).total_seconds() / 60.0
            if not (0 <= minutes <= FUNDAMENTAL_ALERT_BEFORE_MINUTES):
                continue
            alert_key = f"fundamental:{ev.event_id}:minus_{FUNDAMENTAL_ALERT_BEFORE_MINUTES}m"
            if self.storage.guard_alert_sent(alert_key):
                continue
            self.storage.mark_guard_alert_sent(alert_key, "fundamental", ev.title)
            action = "سیگنال‌دهی/Real موقتاً محدود می‌شود" if ev.impact.upper() in _HIGH_IMPACT else "Real محتاط می‌شود"
            out.append(
                "⚠️ هشدار فاندامنتال کل‌مارکتی\n"
                "━━━━━━━━━━━━━━\n"
                f"خبر: {ev.title}\n"
                f"زمان UTC: {dt.strftime('%Y-%m-%d %H:%M')}\n"
                f"شدت: {ev.impact.upper()} | دسته: {ev.category}\n"
                f"منبع: {ev.source}\n\n"
                f"اقدام ربات: {action}."
            )
        return out

    def has_event_near(self, start, end, high_only: bool = False) -> bool:
        start_dt = parse_utc(start)
        end_dt = parse_utc(end)
        if not start_dt or not end_dt:
            return False
        for ev in self._load_events():
            dt = ev.dt
            if dt is None:
                continue
            if high_only and ev.impact.upper() not in _HIGH_IMPACT:
                continue
            if start_dt <= dt <= end_dt:
                return True
        return False
