# -*- coding: utf-8 -*-
"""
diagnostics.py

Lightweight diagnostic helpers for the crypto futures bot.

Purpose:
- Classify common runtime errors without crashing the bot.
- Produce short Persian reports for Telegram/logs.
- Include section/file/function/symbol context when available.
- Keep backward-compatible public functions:
    classify_error
    format_error_report
    log_exception

This module has no external dependencies and does not change trading logic.
"""

from __future__ import annotations

import json
import logging
import os
import traceback
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

try:
    from data_store import save_json, load_json
except Exception:  # keep diagnostics safe even if data_store is broken
    save_json = None
    load_json = None

LOGGER = logging.getLogger("crypto-ai-bot.diagnostics")
DIAGNOSTICS_FILE = "diagnostics_events.json"
MAX_DIAGNOSTIC_EVENTS = int(os.getenv("MAX_DIAGNOSTIC_EVENTS", "500") or "500")


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _safe_text(value: Any, limit: int = 500) -> str:
    try:
        text = str(value)
    except Exception:
        text = repr(value)
    return text[: max(1, int(limit))]


def _lower_msg(exc: BaseException) -> str:
    try:
        return str(exc).lower()
    except Exception:
        return ""


def classify_error(exc: BaseException) -> Tuple[str, str]:
    """Return (code, Persian probable cause) for common bot/VPS errors."""
    msg = _safe_text(exc, 1000)
    lower = msg.lower()
    name = exc.__class__.__name__

    # Exchange / Toobit / order safety
    if "-1202" in msg or "quantity too small" in lower or "sell quantity too small" in lower:
        return "ORDER_QUANTITY_TOO_SMALL", "حجم سفارش برای حداقل مقدار مجاز صرافی کوچک است"
    if "leverage" in lower and ("mismatch" in lower or "invalid" in lower or "not match" in lower):
        return "LEVERAGE_VERIFY_ERROR", "لوریج تنظیم/تأیید نشده یا با تنظیمات ربات هماهنگ نیست"
    if "isolated" in lower or "margin mode" in lower or "cross" in lower:
        return "MARGIN_MODE_ERROR", "حالت مارجین ایزوله تأیید نشده یا صرافی مقدار متفاوت برگردانده"
    if "tp" in lower and "sl" in lower:
        return "TPSL_ERROR", "ثبت یا تأیید TP/SL کامل نشده است"
    if "insufficient" in lower or "balance" in lower and "not enough" in lower:
        return "BALANCE_NOT_ENOUGH", "موجودی یا مارجین کافی برای سفارش وجود ندارد"

    # API / network
    if "too many requests" in lower or "429" in msg or "rate limit" in lower:
        return "API_RATE_LIMIT", "محدودیت درخواست API یا 429"
    if "timeout" in lower or "timed out" in lower or "read timed out" in lower:
        return "API_TIMEOUT", "تاخیر یا قطع ارتباط با API"
    if "connection" in lower or "network" in lower or "temporarily unavailable" in lower:
        return "NETWORK_ERROR", "اختلال ارتباط شبکه یا API"
    if any(x in lower for x in ["401", "403", "unauthorized", "invalid api", "signature"]):
        return "AUTH_API_ERROR", "کلید API، امضا، دسترسی یا مجوز صرافی مشکل دارد"

    # Market data / symbol / candles
    if "داده کافی" in msg or "ohlcv" in lower or "candle" in lower or "not enough data" in lower:
        return "DATA_NOT_ENOUGH", "داده کندل کافی نیست یا API دیتای کامل نداده"
    if "does not have market symbol" in lower or "bad symbol" in lower or "symbol" in lower and "not" in lower:
        return "SYMBOL_NOT_SUPPORTED", "نماد در صرافی یا دیتاپروایدر پشتیبانی نمی‌شود"
    if name in {"KeyError", "IndexError"}:
        return "DATA_FIELD_ERROR", "یکی از فیلدها یا ایندکس‌های موردنیاز در داده وجود ندارد"

    # Files / JSON / persistence
    if "json" in lower or "decode" in lower:
        return "JSON_ERROR", "خطا در خواندن/نوشتن یا خراب بودن فایل JSON"
    if name in {"FileNotFoundError", "PermissionError", "OSError"}:
        return "FILE_SYSTEM_ERROR", "مسیر فایل، سطح دسترسی یا فایل‌های data مشکل دارند"

    # Python runtime
    if name == "ImportError" or name == "ModuleNotFoundError":
        return "IMPORT_ERROR", "وابستگی یا فایل پایتون پیدا نشد؛ requirements یا نام فایل را چک کن"
    if name == "AttributeError":
        return "ATTRIBUTE_ERROR", "تابع/فیلد مورد انتظار در ماژول یا آبجکت وجود ندارد"
    if name in {"TypeError", "ValueError"}:
        return "VALUE_ERROR", "نوع یا مقدار داده نامعتبر است"

    return name or "UNKNOWN_ERROR", "خطای عمومی یا ناشناخته"


def format_error_report(
    section: str,
    exc: BaseException,
    file_name: Optional[str] = None,
    function_name: Optional[str] = None,
    symbol: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    include_traceback: bool = False,
) -> str:
    """Build compact Persian diagnostic text safe for Telegram."""
    code, cause = classify_error(exc)
    lines = [
        f"❌ خطا در بخش: {_safe_text(section, 80)}",
        f"نوع خطا: {code}",
        f"علت احتمالی: {cause}",
    ]
    if file_name:
        lines.append(f"فایل احتمالی: {_safe_text(file_name, 120)}")
    if function_name:
        lines.append(f"تابع: {_safe_text(function_name, 120)}")
    if symbol:
        lines.append(f"نماد: {_safe_text(symbol, 40)}")
    if extra and isinstance(extra, dict):
        compact = {k: _safe_text(v, 80) for k, v in list(extra.items())[:8]}
        lines.append(f"اطلاعات اضافه: {json.dumps(compact, ensure_ascii=False)}")
    lines.append(f"جزئیات: {_safe_text(exc, 500)}")
    if include_traceback:
        lines.append("Traceback:")
        lines.append(_safe_text(traceback.format_exc(), 1200))
    return "\n".join(lines)


def _event_dict(
    section: str,
    exc: BaseException,
    file_name: Optional[str] = None,
    function_name: Optional[str] = None,
    symbol: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    code, cause = classify_error(exc)
    return {
        "ts": int(datetime.utcnow().timestamp()),
        "time": _now_iso(),
        "section": section,
        "error_code": code,
        "cause": cause,
        "error_class": exc.__class__.__name__,
        "message": _safe_text(exc, 1000),
        "file_name": file_name,
        "function_name": function_name,
        "symbol": symbol,
        "extra": extra if isinstance(extra, dict) else {},
        "traceback": _safe_text(traceback.format_exc(), 4000),
    }


def save_diagnostic_event(event: Dict[str, Any]) -> bool:
    """Persist last diagnostic events without risking bot crash."""
    try:
        if load_json and save_json:
            data = load_json(DIAGNOSTICS_FILE, {"events": []})
            if not isinstance(data, dict):
                data = {"events": []}
            events = data.get("events") if isinstance(data.get("events"), list) else []
            events.append(event)
            data["events"] = events[-max(50, MAX_DIAGNOSTIC_EVENTS):]
            save_json(DIAGNOSTICS_FILE, data)
            return True
    except Exception:
        return False
    return False


def log_exception(
    section: str,
    exc: BaseException,
    file_name: Optional[str] = None,
    function_name: Optional[str] = None,
    symbol: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    persist: bool = True,
) -> str:
    """Log, optionally persist, and return compact Telegram-safe report."""
    report = format_error_report(section, exc, file_name, function_name, symbol, extra)
    event = _event_dict(section, exc, file_name, function_name, symbol, extra)

    try:
        LOGGER.error(report + "\n" + event.get("traceback", ""))
    except Exception:
        try:
            print(report)
            print(event.get("traceback", ""))
        except Exception:
            pass

    if persist:
        save_diagnostic_event(event)
    return report


# Backward/extra aliases for future modules
def diagnose_exception(*args: Any, **kwargs: Any) -> str:
    return log_exception(*args, **kwargs)


def build_error_report(*args: Any, **kwargs: Any) -> str:
    return format_error_report(*args, **kwargs)
