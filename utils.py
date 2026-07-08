from __future__ import annotations

import logging
import math
import re
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from typing import Any

import config

logging.basicConfig(
    level=getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("crypto_5m_ice")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def normalize_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    s = s.replace("-USDT-SWAP", "USDT").replace("-USDT", "USDT").replace("/", "").replace("_", "")
    s = re.sub(r"[^A-Z0-9]", "", s)
    if s and not s.endswith("USDT"):
        s += "USDT"
    return s


def base_asset(symbol: str) -> str:
    s = normalize_symbol(symbol)
    return s[:-4] if s.endswith("USDT") else s


def okx_swap_symbol(symbol: str) -> str:
    return f"{base_asset(symbol)}-USDT-SWAP"


def toobit_symbol_candidates(symbol: str) -> list[str]:
    s = normalize_symbol(symbol)
    b = base_asset(s)
    return [s, f"{b}USDT", f"{b}-USDT", f"{b}_USDT"]


def side_to_order_side(direction: str) -> str:
    return "BUY" if str(direction).upper() == "LONG" else "SELL"


def side_to_toobit_open(side: str) -> str:
    s = str(side).upper()
    if s in {"LONG", "BUY"}:
        return "BUY_OPEN"
    if s in {"SHORT", "SELL"}:
        return "SELL_OPEN"
    return s


def side_to_toobit_position(side: str) -> str:
    s = str(side).upper()
    if s in {"LONG", "BUY", "BUY_OPEN"}:
        return "LONG"
    if s in {"SHORT", "SELL", "SELL_OPEN"}:
        return "SHORT"
    return s


def decimal_round_down(value: float | Decimal, step: float | Decimal, digits: int | None = None) -> Decimal:
    try:
        v = value if isinstance(value, Decimal) else Decimal(str(value))
        st = step if isinstance(step, Decimal) else Decimal(str(step))
        if st <= 0:
            return v
        out = (v / st).to_integral_value(rounding=ROUND_DOWN) * st
        if digits is not None:
            q = Decimal("1") / (Decimal("10") ** int(digits))
            out = out.quantize(q, rounding=ROUND_DOWN)
        return out
    except (InvalidOperation, Exception):
        return Decimal("0")


def extract_filter(symbol_info: dict[str, Any] | None, *names: str) -> Any:
    if not symbol_info:
        return None
    for name in names:
        if name in symbol_info:
            return symbol_info.get(name)
    filters = symbol_info.get("filters") if isinstance(symbol_info, dict) else None
    if isinstance(filters, list):
        lowered = {str(n).lower() for n in names}
        for f in filters:
            if not isinstance(f, dict):
                continue
            ftype = str(f.get("filterType") or f.get("filter_type") or "").lower()
            if ftype in lowered:
                return f
            for n in names:
                if n in f:
                    return f.get(n)
    return None
