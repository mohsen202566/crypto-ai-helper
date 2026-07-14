"""راه‌اندازی لاگ کم‌حجم و Rate-limited برای VPS."""
from __future__ import annotations

import logging
import sys
import threading
import time
from collections import defaultdict
from typing import Callable

import config


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("adaptive_bot")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


class RejectLogger:
    """فقط وقتی تنظیم «لاگ رد فعال» باشد، یک خط کوتاه چاپ می‌کند.

    خطاهای یکسان در بازه کوتاه تکرار نمی‌شوند و تعدادشان جمع می‌شود.
    """

    def __init__(self, enabled_getter: Callable[[], bool], logger: logging.Logger | None = None):
        self.enabled_getter = enabled_getter
        self.logger = logger or logging.getLogger("adaptive_bot")
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._counts: defaultdict[str, int] = defaultdict(int)

    def reject(self, canonical: str, tier: str, reason: str, detail: str = "") -> None:
        if not self.enabled_getter():
            return
        key = f"{canonical}|{tier}|{reason}|{detail}"
        now = time.monotonic()
        with self._lock:
            self._counts[key] += 1
            last = self._last.get(key, 0.0)
            if now - last < config.REJECT_LOG_RATE_SECONDS:
                return
            count = self._counts.pop(key, 1)
            self._last[key] = now
        suffix = f" | {detail}" if detail else ""
        mult = f" x{count}" if count > 1 else ""
        self.logger.info("REJECT | %s | %s | %s%s%s", canonical, tier, reason, suffix, mult)

    def event(self, kind: str, canonical: str, reason: str, detail: str = "") -> None:
        if not self.enabled_getter() and kind not in {"ERROR", "CRITICAL"}:
            return
        suffix = f" | {detail}" if detail else ""
        self.logger.info("%s | %s | %s%s", kind, canonical, reason, suffix)
