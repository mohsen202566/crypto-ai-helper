from __future__ import annotations

"""
28 - logger.py

Central logging system for the locked Movement Hunter architecture.

Responsibilities:
- Single logging entry point for all modules.
- Structured JSON-safe logging.
- Console logging.
- File logging.
- Rotating log files.
- Error logging helpers.
- Performance timing helpers.
- VPS-safe operation.
- Never interrupt trading flow because of logging failures.

Strictly forbidden:
- No trading logic.
- No Telegram sending.
- No Toobit calls.
- No AI decisions.
"""

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional
import json
import logging
import logging.handlers
import os
import time
import traceback
from pathlib import Path


JsonDict = Dict[str, Any]

LOG_DEBUG = "DEBUG"
LOG_INFO = "INFO"
LOG_WARNING = "WARNING"
LOG_ERROR = "ERROR"
LOG_CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class LogRecord:
    timestamp: int
    level: str
    source: str
    message: str
    data: JsonDict

    def to_dict(self) -> JsonDict:
        return asdict(self)


def now_ts() -> int:
    return int(time.time())


def safe_json(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": int(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        extra_data = getattr(record, "extra_data", None)
        if extra_data is not None:
            payload["data"] = safe_json(extra_data)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


class MovementHunterLogger:
    def __init__(
        self,
        log_dir: str = "logs",
        level: str = LOG_INFO,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 10,
    ):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.logger = logging.getLogger("movement_hunter")
        self.logger.setLevel(getattr(logging, level.upper(), logging.INFO))

        if self.logger.handlers:
            return

        console = logging.StreamHandler()
        console.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(levelname)s | %(name)s | %(message)s"
            )
        )

        file_handler = logging.handlers.RotatingFileHandler(
            self.log_dir / "bot.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter())

        error_handler = logging.handlers.RotatingFileHandler(
            self.log_dir / "errors.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(JsonFormatter())

        self.logger.addHandler(console)
        self.logger.addHandler(file_handler)
        self.logger.addHandler(error_handler)

    def log(
        self,
        level: str,
        source: str,
        message: str,
        data: Optional[JsonDict] = None,
    ) -> None:
        try:
            payload = {"source": source}
            if data:
                payload["data"] = safe_json(data)

            record_level = getattr(logging, level.upper(), logging.INFO)

            self.logger.log(
                record_level,
                f"[{source}] {message}",
                extra={"extra_data": payload},
            )
        except Exception:
            pass

    def debug(self, source: str, message: str, data: Optional[JsonDict] = None) -> None:
        self.log(LOG_DEBUG, source, message, data)

    def info(self, source: str, message: str, data: Optional[JsonDict] = None) -> None:
        self.log(LOG_INFO, source, message, data)

    def warning(self, source: str, message: str, data: Optional[JsonDict] = None) -> None:
        self.log(LOG_WARNING, source, message, data)

    def error(self, source: str, message: str, data: Optional[JsonDict] = None) -> None:
        self.log(LOG_ERROR, source, message, data)

    def critical(self, source: str, message: str, data: Optional[JsonDict] = None) -> None:
        self.log(LOG_CRITICAL, source, message, data)

    def exception(
        self,
        source: str,
        exception: Exception,
        data: Optional[JsonDict] = None,
    ) -> None:
        try:
            self.logger.error(
                f"[{source}] {str(exception)}",
                exc_info=True,
                extra={"extra_data": data or {}},
            )
        except Exception:
            pass


class PerformanceTimer:
    """
    Measure execution times for critical sections.
    """

    def __init__(self, name: str):
        self.name = name
        self.started = time.perf_counter()

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000.0

    def finish(self) -> float:
        return self.elapsed_ms()


_default_logger: Optional[MovementHunterLogger] = None


def logger() -> MovementHunterLogger:
    global _default_logger
    if _default_logger is None:
        _default_logger = MovementHunterLogger()
    return _default_logger


def debug(source: str, message: str, data: Optional[JsonDict] = None) -> None:
    logger().debug(source, message, data)


def info(source: str, message: str, data: Optional[JsonDict] = None) -> None:
    logger().info(source, message, data)


def warning(source: str, message: str, data: Optional[JsonDict] = None) -> None:
    logger().warning(source, message, data)


def error(source: str, message: str, data: Optional[JsonDict] = None) -> None:
    logger().error(source, message, data)


def critical(source: str, message: str, data: Optional[JsonDict] = None) -> None:
    logger().critical(source, message, data)


def log_exception(
    source: str,
    exception: Exception,
    data: Optional[JsonDict] = None,
) -> None:
    logger().exception(source, exception, data)
