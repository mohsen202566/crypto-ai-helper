"""لاگ ساختاریافته روی stdout برای مشاهده مستقیم در VPS/journalctl."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from typing import Any

import config


class RejectLogger:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._logger = logging.getLogger("rejections")

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {str(k): RejectLogger._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [RejectLogger._json_safe(v) for v in value]
        return str(value)

    def write(
        self,
        stage: str,
        symbol_id: str,
        reason: str,
        metrics: dict[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> None:
        now = time.time()
        key = (stage, symbol_id)
        previous_reason, previous_ts = self._cache.get(key, ("", 0.0))
        if not force and reason == previous_reason and now - previous_ts < config.REJECT_LOG_REPEAT_SECONDS:
            return
        self._cache[key] = (reason, now)
        row = {
            "event": "signal_rejected",
            "ts": int(now * 1000),
            "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "stage": stage,
            "symbol": symbol_id,
            "reason": reason,
            "metrics": self._json_safe(metrics or {}),
        }
        with self._lock:
            self._logger.info(json.dumps(row, ensure_ascii=False, separators=(",", ":")))


def configure_application_logging() -> None:
    """فقط stdout؛ مناسب systemd/journalctl و بدون ساخت پوشه یا فایل جانبی."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(threadName)s | %(name)s | %(message)s"
    ))
    root.addHandler(handler)
