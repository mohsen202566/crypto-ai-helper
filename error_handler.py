from __future__ import annotations

"""
29 - error_handler.py

Global error handling layer for the locked Movement Hunter architecture.

Responsibilities:
- Normalize exceptions.
- Categorize errors.
- Prevent bot crashes from non-fatal failures.
- Generate user-safe and developer-safe error messages.
- Store structured error records.
- Integrate with logger.py and data_store.py.
- Support retry recommendations.

Strictly forbidden:
- No trading logic.
- No AI decisions.
- No Telegram sending.
- No Toobit order placement.
"""

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4
import traceback
import time

from logger import log_exception, error as log_error
from data_store import save_error


JsonDict = Dict[str, Any]

ERROR_UNKNOWN = "UNKNOWN"
ERROR_NETWORK = "NETWORK"
ERROR_EXCHANGE = "EXCHANGE"
ERROR_TELEGRAM = "TELEGRAM"
ERROR_DATA = "DATA"
ERROR_CONFIG = "CONFIG"
ERROR_VALIDATION = "VALIDATION"
ERROR_RUNTIME = "RUNTIME"


@dataclass(frozen=True)
class ErrorRecord:
    error_id: str
    timestamp: int
    category: str
    source: str
    message: str
    traceback_text: str = ""
    retryable: bool = False
    details: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


def now_ts() -> int:
    return int(time.time())


class ErrorClassifier:

    def classify(self, exc: Exception) -> Tuple[str, bool]:
        name = exc.__class__.__name__.lower()
        text = str(exc).lower()

        if any(x in text for x in ["timeout", "connection", "network"]):
            return ERROR_NETWORK, True

        if any(x in text for x in ["telegram", "chat", "message"]):
            return ERROR_TELEGRAM, True

        if any(x in text for x in ["exchange", "toobit", "order", "position"]):
            return ERROR_EXCHANGE, True

        if any(x in text for x in ["config", "setting"]):
            return ERROR_CONFIG, False

        if any(x in text for x in ["validation", "invalid"]):
            return ERROR_VALIDATION, False

        if any(x in name for x in ["valueerror", "keyerror", "typeerror"]):
            return ERROR_DATA, False

        return ERROR_RUNTIME, False


class ErrorHandler:

    def __init__(self):
        self.classifier = ErrorClassifier()

    def handle(
        self,
        source: str,
        exc: Exception,
        details: Optional[JsonDict] = None,
    ) -> ErrorRecord:

        category, retryable = self.classifier.classify(exc)

        record = ErrorRecord(
            error_id=f"err_{uuid4().hex}",
            timestamp=now_ts(),
            category=category,
            source=source,
            message=str(exc),
            traceback_text=traceback.format_exc(),
            retryable=retryable,
            details=details or {},
        )

        try:
            save_error(
                source,
                record.message,
                record.to_dict(),
            )
        except Exception:
            pass

        try:
            log_exception(source, exc, record.to_dict())
        except Exception:
            pass

        return record

    def safe_user_message(self, record: ErrorRecord) -> str:
        if record.retryable:
            return "⚠️ خطای موقت رخ داد. تلاش مجدد انجام خواهد شد."
        return "❌ خطا رخ داد. جزئیات ثبت شد."

    def safe_developer_message(self, record: ErrorRecord) -> str:
        return (
            f"[{record.category}] "
            f"{record.source} | "
            f"{record.message}"
        )


_default_handler: Optional[ErrorHandler] = None


def handler() -> ErrorHandler:
    global _default_handler
    if _default_handler is None:
        _default_handler = ErrorHandler()
    return _default_handler


def handle_error(
    source: str,
    exc: Exception,
    details: Optional[JsonDict] = None,
) -> ErrorRecord:
    return handler().handle(source, exc, details)
