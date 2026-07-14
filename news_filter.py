"""Optional high-impact news blackout filter.

The feed is deliberately provider-neutral. Accepted JSON shapes are a top-level list or
an object containing ``events``/``data``. Event time may be epoch seconds, epoch
milliseconds, or ISO-8601. Only high-impact events are used.

When no feed URL is configured the component is explicitly disabled; it never invents
news or blocks signals from stale assumptions.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

import config
from utils import now_ms, safe_int

logger = logging.getLogger("adaptive_bot")


class NewsFilter:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.url = config.NEWS_CALENDAR_URL
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._last_refresh_ms = 0
        self._last_error = ""
        self.refresh_seconds = 60
        self.extension_minutes = 30

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @staticmethod
    def _timestamp_ms(value: Any) -> int:
        if value in (None, ""):
            return 0
        if isinstance(value, (int, float)):
            raw = int(value)
            return raw if raw > 10_000_000_000 else raw * 1000
        text = str(value).strip()
        if text.isdigit():
            raw = int(text)
            return raw if raw > 10_000_000_000 else raw * 1000
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return 0

    @staticmethod
    def _high_impact(item: dict[str, Any]) -> bool:
        raw = str(item.get("impact") or item.get("importance") or item.get("severity") or item.get("priority") or "").strip().lower()
        if raw in {"high", "3", "red", "major", "critical", "important"}:
            return True
        numeric = safe_int(item.get("importance") or item.get("priority"), 0)
        return numeric >= 3

    @classmethod
    def _parse(cls, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("events") or payload.get("data") or payload.get("results") or []
            if isinstance(rows, dict):
                rows = rows.get("events") or rows.get("items") or []
        else:
            rows = []
        out: list[dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict) or not cls._high_impact(item):
                continue
            ts = cls._timestamp_ms(
                item.get("timestamp") or item.get("time") or item.get("start") or item.get("datetime") or item.get("date")
            )
            if ts <= 0:
                continue
            out.append({
                "timestamp_ms": ts,
                "title": str(item.get("title") or item.get("name") or item.get("event") or "خبر مهم")[:200],
                "raw": item,
            })
        return sorted(out, key=lambda x: x["timestamp_ms"])

    def refresh_if_due(self) -> None:
        if not self.enabled:
            return
        now = now_ms()
        with self._lock:
            if now - self._last_refresh_ms < self.refresh_seconds * 1000:
                return
            self._last_refresh_ms = now
        try:
            response = self.session.get(self.url, timeout=config.REQUEST_TIMEOUT)
            response.raise_for_status()
            events = self._parse(response.json())
            # Keep only a practical window to bound memory.
            lower = now - 24 * 60 * 60 * 1000
            upper = now + 7 * 24 * 60 * 60 * 1000
            events = [event for event in events if lower <= int(event["timestamp_ms"]) <= upper]
            with self._lock:
                self._events = events
                self._last_error = ""
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)[:300]
            logger.warning("NEWS_FEED_ERROR | %s", str(exc)[:200])

    def is_blocked(self, market_abnormal: bool = False, at_ms: int | None = None) -> tuple[bool, str]:
        if not self.enabled:
            return False, "NEWS_FEED_DISABLED"
        self.refresh_if_due()
        now = int(at_ms or now_ms())
        before = config.NEWS_BLOCK_BEFORE_MINUTES * 60_000
        after = config.NEWS_BLOCK_AFTER_MINUTES * 60_000
        extended = self.extension_minutes * 60_000
        with self._lock:
            events = list(self._events)
        for event in events:
            ts = int(event["timestamp_ms"])
            if ts - before <= now <= ts + after:
                return True, f"{event['title']} | پنجره خبر مهم"
            if market_abnormal and ts + after < now <= ts + extended:
                return True, f"{event['title']} | تمدید به‌علت نوسان غیرعادی"
        return False, ""

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "events": len(self._events),
                "last_refresh_ms": self._last_refresh_ms,
                "last_error": self._last_error,
            }
