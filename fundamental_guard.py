from __future__ import annotations

import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from config import (
    DATA_DIR,
    FUNDAMENTAL_ALERT_BEFORE_MINUTES,
    FUNDAMENTAL_BLOCK_AFTER_MINUTES,
    FUNDAMENTAL_BLOCK_BEFORE_MINUTES,
    FUNDAMENTAL_FEED_TIMEOUT_SECONDS,
    FUNDAMENTAL_FEED_URLS,
    FUNDAMENTAL_GUARD_ENABLED,
    FUNDAMENTAL_LOCAL_EVENTS_PATH,
    FUNDAMENTAL_REFRESH_MINUTES,
    FUNDAMENTAL_STRICT_AFTER_MINUTES,
)
from session_guard import GuardDecision
from storage import Storage
from utils import now_utc


@dataclass(frozen=True)
class FundamentalEvent:
    event_id: str
    title: str
    source: str
    event_time: datetime
    severity: str
    category: str
    url: str = ""


class FundamentalGuard:
    """Market-wide news guard.

    Only market-wide macro/crypto shock events affect trading. Coin-specific listings,
    delistings, token burns/unlocks, and ordinary project updates are ignored unless
    the text clearly indicates a system-wide exchange/stablecoin/regulatory crisis.
    """

    HIGH_KEYWORDS = (
        "cpi", "ppi", "fomc", "fed rate", "federal funds", "interest rate", "rate decision",
        "powell", "fed chair", "pce", "nonfarm", "nfp", "employment situation", "jobless claims",
        "gdp", "inflation", "sec", "cftc", "etf", "bitcoin etf", "ethereum etf",
        "stablecoin", "usdt", "usdc", "depeg", "bank crisis", "liquidity crisis",
        "binance", "coinbase", "exchange hack", "halt withdrawals", "withdrawal halt",
        "war", "sanction", "bank run",
    )
    IGNORE_KEYWORDS = (
        "listing", "listed", "delisting", "delisted", "token unlock", "unlock", "burn",
        "partnership", "airdrop", "mainnet", "testnet", "ama", "giveaway", "staking campaign",
    )

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._last_refresh: datetime | None = None

    def refresh_if_due(self) -> None:
        if not FUNDAMENTAL_GUARD_ENABLED:
            return
        now = now_utc()
        if self._last_refresh and now - self._last_refresh < timedelta(minutes=FUNDAMENTAL_REFRESH_MINUTES):
            return
        self._last_refresh = now
        for event in self._load_local_events() + self._load_env_events() + self._load_feed_events():
            self.storage.upsert_fundamental_event(event)

    def evaluate(self) -> GuardDecision:
        if not FUNDAMENTAL_GUARD_ENABLED:
            return GuardDecision.ok("fundamental")
        now = now_utc()
        active = self.storage.active_fundamental_events(
            now_iso=now.isoformat(),
            before_minutes=max(FUNDAMENTAL_BLOCK_BEFORE_MINUTES, FUNDAMENTAL_ALERT_BEFORE_MINUTES),
            after_minutes=max(FUNDAMENTAL_STRICT_AFTER_MINUTES, FUNDAMENTAL_BLOCK_AFTER_MINUTES),
        )
        if not active:
            return GuardDecision.ok("fundamental")
        # Most severe first, nearest first.
        active.sort(key=lambda row: (0 if row.get("severity") == "HIGH" else 1, abs(self._minutes_to_event(row, now))))
        event = active[0]
        severity = str(event.get("severity") or "MEDIUM").upper()
        minutes = self._minutes_to_event(event, now)
        title = str(event.get("title") or "خبر مهم")
        source = str(event.get("source") or "source")
        if severity == "HIGH":
            if -FUNDAMENTAL_BLOCK_AFTER_MINUTES <= minutes <= FUNDAMENTAL_BLOCK_BEFORE_MINUTES:
                return GuardDecision("fundamental", "MARKET_WIDE_HIGH", False, False, 100, f"FUNDAMENTAL_BLOCK: {title} ({source}) زمان خبر نزدیک/فعال است.", hard_block=True, caution=True)
            return GuardDecision("fundamental", "MARKET_WIDE_HIGH_STRICT", True, False, 30, f"FUNDAMENTAL_STRICT: بازار هنوز بعد از/قبل از خبر {title} با احتیاط بررسی می‌شود.", caution=True)
        return GuardDecision("fundamental", "MARKET_WIDE_MEDIUM", True, False, 20, f"FUNDAMENTAL_CAUTION: خبر کل‌مارکتی {title} نزدیک است.", caution=True)

    async def send_due_alerts(self, ui) -> None:
        if not FUNDAMENTAL_GUARD_ENABLED:
            return
        now = now_utc()
        events = self.storage.due_fundamental_alerts(now_iso=now.isoformat(), before_minutes=FUNDAMENTAL_ALERT_BEFORE_MINUTES)
        for event in events:
            title = str(event.get("title") or "خبر مهم")
            when = str(event.get("event_time") or "")
            severity = str(event.get("severity") or "MEDIUM")
            source = str(event.get("source") or "")
            text = (
                "⚠️ هشدار فاندامنتال کل‌مارکتی\n"
                "━━━━━━━━━━━━━━\n"
                f"خبر: {title}\n"
                f"منبع: {source}\n"
                f"زمان انتشار: {when}\n"
                f"شدت اثر: {severity}\n\n"
                "اقدام ربات:\n"
                "Real موقتاً محدود/خاموش می‌شود و سیگنال‌های جدید با گارد خبر بررسی می‌شوند."
            )
            ok = await ui.send_guard_alert(text)
            if ok:
                self.storage.mark_fundamental_alert_sent(str(event.get("event_id")), "pre_5m")

    def _load_local_events(self) -> list[FundamentalEvent]:
        configured_path = Path(FUNDAMENTAL_LOCAL_EVENTS_PATH)
        candidate_paths = [configured_path]
        if not configured_path.is_absolute():
            # Prefer the GitHub/project root for manual editing.
            candidate_paths = [configured_path, DATA_DIR / configured_path]
        for path in candidate_paths:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return self._events_from_json(data, source_default="local_calendar")
            except Exception:
                return []
        return []

    def _load_env_events(self) -> list[FundamentalEvent]:
        raw = os.getenv("FUNDAMENTAL_EVENTS_JSON", "").strip()
        if not raw:
            return []
        try:
            return self._events_from_json(json.loads(raw), source_default="env_calendar")
        except Exception:
            return []

    def _load_feed_events(self) -> list[FundamentalEvent]:
        events: list[FundamentalEvent] = []
        for url in [u.strip() for u in FUNDAMENTAL_FEED_URLS.split(",") if u.strip()]:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "AI-Range-Bot/1.0"})
                with urllib.request.urlopen(req, timeout=FUNDAMENTAL_FEED_TIMEOUT_SECONDS) as resp:
                    body = resp.read(800_000)
                events.extend(self._parse_feed(body, url))
            except Exception:
                continue
        return events

    def _events_from_json(self, data: Any, source_default: str) -> list[FundamentalEvent]:
        if isinstance(data, dict):
            items = data.get("events") or data.get("data") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []
        out: list[FundamentalEvent] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("name") or "").strip()
            when = item.get("time") or item.get("event_time") or item.get("date")
            if not title or not when:
                continue
            classified = self.classify(title, item.get("category"))
            if classified == "IGNORE":
                continue
            try:
                event_time = self._parse_datetime(str(when))
            except Exception:
                continue
            severity = str(item.get("severity") or ("HIGH" if classified == "MARKET_WIDE_HIGH" else "MEDIUM")).upper()
            source = str(item.get("source") or source_default)
            url = str(item.get("url") or "")
            event_id = str(item.get("id") or self._event_id(source, title, event_time))
            out.append(FundamentalEvent(event_id, title, source, event_time, severity, classified, url))
        return out

    def _parse_feed(self, body: bytes, source_url: str) -> list[FundamentalEvent]:
        out: list[FundamentalEvent] = []
        try:
            root = ET.fromstring(body)
        except Exception:
            return out
        now = now_utc()
        # RSS/Atom items are immediate news shocks, not scheduled macro releases.
        candidates = root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for item in candidates[:50]:
            title = self._find_text(item, "title")
            if not title:
                continue
            classified = self.classify(title, None)
            if classified == "IGNORE":
                continue
            date_text = self._find_text(item, "pubDate") or self._find_text(item, "updated") or self._find_text(item, "published")
            event_time = now
            if date_text:
                try:
                    event_time = parsedate_to_datetime(date_text).astimezone(timezone.utc)
                except Exception:
                    try:
                        event_time = self._parse_datetime(date_text)
                    except Exception:
                        event_time = now
            if abs((now - event_time).total_seconds()) > 12 * 3600:
                continue
            link = self._find_text(item, "link") or ""
            severity = "HIGH" if classified == "MARKET_WIDE_HIGH" else "MEDIUM"
            event_id = self._event_id(source_url, title, event_time)
            out.append(FundamentalEvent(event_id, title, source_url, event_time, severity, classified, link))
        return out

    @classmethod
    def classify(cls, title: str, category: Any = None) -> str:
        text = f"{title} {category or ''}".lower()
        high = any(k in text for k in cls.HIGH_KEYWORDS)
        ignored = any(k in text for k in cls.IGNORE_KEYWORDS)
        # A listing/delisting is ignored unless it mentions a system-wide shock.
        systemic = any(k in text for k in ("sec", "cftc", "stablecoin", "depeg", "binance", "coinbase", "hack", "halt withdrawals", "bank run"))
        if ignored and not systemic:
            return "IGNORE"
        if high:
            if any(k in text for k in ("fomc", "cpi", "ppi", "nfp", "nonfarm", "pce", "rate decision", "depeg", "etf", "sec", "cftc", "halt withdrawals", "exchange hack")):
                return "MARKET_WIDE_HIGH"
            return "MARKET_WIDE_MEDIUM"
        return "IGNORE"

    @staticmethod
    def _find_text(item: ET.Element, tag: str) -> str:
        node = item.find(tag)
        if node is None:
            node = item.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
        if node is None:
            return ""
        return (node.text or node.attrib.get("href") or "").strip()

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            dt = parsedate_to_datetime(text)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    @staticmethod
    def _event_id(source: str, title: str, event_time: datetime) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")[:80]
        return f"{source}|{event_time.isoformat()}|{slug}"

    @staticmethod
    def _minutes_to_event(event: dict[str, Any], now: datetime) -> float:
        try:
            event_time = datetime.fromisoformat(str(event.get("event_time"))).astimezone(timezone.utc)
        except Exception:
            return 0.0
        return (event_time - now).total_seconds() / 60.0
