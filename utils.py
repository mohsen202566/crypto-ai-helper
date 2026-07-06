from __future__ import annotations

import logging
import math
import os
import re
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from typing import Any

import config

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("crypto_4h_bot")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def decimal_round_down(value: float | Decimal, step: str | float | Decimal | None = None, digits: int = 8) -> str:
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        dec = Decimal("0")
    if step not in (None, "", 0, "0"):
        step_dec = Decimal(str(step))
        if step_dec > 0:
            dec = (dec / step_dec).to_integral_value(rounding=ROUND_DOWN) * step_dec
            return format(dec.normalize(), "f")
    quant = Decimal("1") / (Decimal("10") ** int(digits))
    return format(dec.quantize(quant, rounding=ROUND_DOWN).normalize(), "f")


def extract_filter(info: dict[str, Any], filter_type: str) -> dict[str, Any]:
    filters = info.get("filters") or info.get("filter") or []
    if isinstance(filters, dict):
        filters = list(filters.values())
    if isinstance(filters, list):
        for item in filters:
            if not isinstance(item, dict):
                continue
            raw = str(item.get("filterType") or item.get("type") or item.get("name") or "").upper()
            if raw == filter_type.upper():
                return item
    return {}


def normalize_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    s = s.replace("-USDT-SWAP", "USDT").replace("-USDT", "USDT")
    s = s.replace("/", "").replace("-", "").replace("_", "")
    return s


def okx_swap_symbol(internal_symbol: str) -> str:
    s = normalize_symbol(internal_symbol)
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}-USDT-SWAP"
    return s


def toobit_symbol_candidates(internal_symbol: str) -> list[str]:
    s = normalize_symbol(internal_symbol)
    candidates = [s]
    if s.endswith("USDT"):
        base = s[:-4]
        candidates.extend([f"{base}USDT", f"{base}-USDT", f"{base}_USDT", f"{base}USDT_PERP"])
    # keep order, unique
    out: list[str] = []
    for item in candidates:
        item = item.upper()
        if item and item not in out:
            out.append(item)
    return out


def side_to_toobit_open(side: str) -> str:
    raw = str(side or "").upper()
    if raw in {"LONG", "BUY", "BUY_OPEN"}:
        return "BUY_OPEN"
    if raw in {"SHORT", "SELL", "SELL_OPEN"}:
        return "SELL_OPEN"
    raise ValueError(f"Unknown side: {side}")


def side_to_toobit_position(side: str | None) -> str:
    raw = str(side or "").upper()
    if raw in {"LONG", "BUY", "BUY_OPEN"}:
        return "LONG"
    if raw in {"SHORT", "SELL", "SELL_OPEN"}:
        return "SHORT"
    return raw


def side_to_order_side(direction: str) -> str:
    return "BUY" if str(direction).upper() == "LONG" else "SELL"


def fmt_price(value: Any) -> str:
    n = safe_float(value, 0.0)
    if n >= 100:
        return f"{n:.3f}".rstrip("0").rstrip(".")
    if n >= 1:
        return f"{n:.5f}".rstrip("0").rstrip(".")
    return f"{n:.8f}".rstrip("0").rstrip(".")


def fmt_money(value: Any) -> str:
    return f"{safe_float(value):.2f} USDT"


def now_ms() -> int:
    import time
    return int(time.time() * 1000)
