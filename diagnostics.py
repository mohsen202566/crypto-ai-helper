from __future__ import annotations

"""
Diagnostics and error safety.

Responsibilities:
- structured error logging
- safe decorator for non-critical runtime paths
- health reports
- short Persian owner-facing messages
- no silent failures

Rules:
- Do not import bot.py here.
- Telegram sending is injected as a callback by bot.py when needed.
"""

import functools
import inspect
import logging
import traceback
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from config import LOG_DIR, ensure_directories


ensure_directories()
DIAGNOSTICS_LOG = LOG_DIR / "diagnostics.log"


def _logger() -> logging.Logger:
    logger = logging.getLogger("crypto_ai_diagnostics")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(str(DIAGNOSTICS_LOG), encoding="utf-8")
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(module)s | %(funcName)s | %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


log = _logger()


def now_ts() -> int:
    return int(time.time())


def short_error_fa(module: str, function: str, error: Exception) -> str:
    return (
        "⚠️ خطای داخلی ربات\n"
        f"بخش: {module}.{function}\n"
        f"نوع خطا: {type(error).__name__}\n"
        "جزئیات در لاگ VPS ذخیره شد."
    )


def record_error(
    error: Exception,
    module: Optional[str] = None,
    function: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    notify_callback: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    if module is None or function is None:
        frame = inspect.trace()[1] if len(inspect.trace()) > 1 else None
        module = module or (Path(frame.filename).stem if frame else "unknown")
        function = function or (frame.function if frame else "unknown")

    tb = traceback.format_exc()
    payload = {
        "ts": now_ts(),
        "module": module,
        "function": function,
        "error_type": type(error).__name__,
        "error": str(error),
        "context": context or {},
        "traceback": tb,
    }

    try:
        log.error(
            f"{payload['error_type']}: {payload['error']} | context={payload['context']}\n{tb}"
        )
    except Exception:
        pass

    if notify_callback:
        try:
            notify_callback(short_error_fa(module, function, error))
        except Exception:
            pass

    return payload


def info(message: str, context: Optional[Dict[str, Any]] = None) -> None:
    try:
        log.info(f"{message} | context={context or {}}")
    except Exception:
        pass


def warning(message: str, context: Optional[Dict[str, Any]] = None) -> None:
    try:
        log.warning(f"{message} | context={context or {}}")
    except Exception:
        pass


def safe(default: Any = None, notify_callback: Optional[Callable[[str], Any]] = None):
    """
    Decorator for runtime functions that should not crash the bot.
    It returns `default` on error and logs full traceback.
    """
    def deco(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                record_error(
                    e,
                    module=fn.__module__,
                    function=fn.__name__,
                    context={"args_count": len(args), "kwargs": list(kwargs.keys())},
                    notify_callback=notify_callback,
                )
                return default
        return wrapper
    return deco


async def async_safe_call(fn: Callable, *args, default: Any = None, **kwargs) -> Any:
    try:
        return await fn(*args, **kwargs)
    except Exception as e:
        record_error(
            e,
            module=getattr(fn, "__module__", "unknown"),
            function=getattr(fn, "__name__", "unknown"),
            context={"async": True},
        )
        return default


def health_report() -> Dict[str, Any]:
    ensure_directories()
    try:
        log_size = DIAGNOSTICS_LOG.stat().st_size if DIAGNOSTICS_LOG.exists() else 0
    except Exception:
        log_size = -1
    return {
        "ok": True,
        "diagnostics_log": str(DIAGNOSTICS_LOG),
        "log_size_bytes": log_size,
        "timestamp": now_ts(),
    }


def tail_log(lines: int = 50) -> str:
    if not DIAGNOSTICS_LOG.exists():
        return ""
    try:
        data = DIAGNOSTICS_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(data[-lines:])
    except Exception:
        return ""


class DiagnosticContext:
    """Context manager for logging protected sections."""

    def __init__(self, module: str, function: str, context: Optional[Dict[str, Any]] = None):
        self.module = module
        self.function = function
        self.context = context or {}

    def __enter__(self):
        info("enter", {"module": self.module, "function": self.function, **self.context})
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            record_error(exc, self.module, self.function, self.context)
            return True
        info("exit", {"module": self.module, "function": self.function, **self.context})
        return False
