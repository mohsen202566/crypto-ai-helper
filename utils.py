"""
utils.py
Level 4 / 1H Smart Scalp Bot

Small, safe, dependency-light utility helpers shared by the Level 4 modules.

Architecture lock:
- This file must stay lightweight.
- No market analysis, AI decision logic, order execution, Telegram sending, or JSON storage ownership here.
- Allowed project import: constants.py only.
"""

from __future__ import annotations

import math
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation
from typing import Any, Iterable, Mapping, Optional

from constants import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    SYSTEM_VERSION,
    TOOBIT_SPECIAL_SYMBOL_MAP,
)


# ---------------------------------------------------------------------------
# Version contract
# ---------------------------------------------------------------------------

UTILS_VERSION: str = SYSTEM_VERSION


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utc_now_ts() -> int:
    """Return current UTC timestamp in seconds."""
    return int(time.time())


def utc_now_ms() -> int:
    """Return current UTC timestamp in milliseconds."""
    return int(time.time() * 1000)


def utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ms_to_iso(ms: Any) -> Optional[str]:
    """Convert milliseconds timestamp to UTC ISO string. Return None on invalid input."""
    value = safe_int(ms, default=None)
    if value is None or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Safe conversion helpers
# ---------------------------------------------------------------------------

def is_none_like(value: Any) -> bool:
    """Return True for values that should be treated as empty/missing."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in {"", "none", "null", "nan", "na", "n/a"}:
        return True
    return False


def safe_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    """Safely convert value to float, returning default on failure or non-finite values."""
    if is_none_like(value):
        return default
    try:
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: Optional[int] = 0) -> Optional[int]:
    """Safely convert value to int, returning default on failure."""
    if is_none_like(value):
        return default
    try:
        if isinstance(value, bool):
            return int(value)
        return int(float(value))
    except (TypeError, ValueError):
        return default


def safe_bool(value: Any, default: bool = False) -> bool:
    """Safely convert common truthy/falsy values to bool."""
    if isinstance(value, bool):
        return value
    if is_none_like(value):
        return default
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on", "enable", "enabled", "فعال"}:
            return True
        if text in {"0", "false", "no", "n", "off", "disable", "disabled", "خاموش"}:
            return False
    return default


def safe_str(value: Any, default: str = "") -> str:
    """Safely convert value to stripped string."""
    if value is None:
        return default
    try:
        text = str(value).strip()
        return text if text else default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Decimal / rounding helpers
# ---------------------------------------------------------------------------

def to_decimal(value: Any, default: str = "0") -> Decimal:
    """Convert value to Decimal safely."""
    if is_none_like(value):
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def decimal_places_from_step(step: Any) -> int:
    """Infer decimal places from an exchange step size such as 0.001."""
    d = to_decimal(step, "1").normalize()
    if d == 0:
        return 0
    exponent = d.as_tuple().exponent
    return max(0, -exponent)


def round_to_step(value: Any, step: Any, rounding: str = "down") -> float:
    """
    Round value to exchange step.

    rounding:
    - "down": never exceed requested size/price.
    - "nearest": normal half-up rounding.
    """
    value_d = to_decimal(value)
    step_d = to_decimal(step, "1")
    if step_d <= 0:
        return float(value_d)

    try:
        units = value_d / step_d
        if rounding == "nearest":
            units = units.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        else:
            units = units.quantize(Decimal("1"), rounding=ROUND_DOWN)
        return float(units * step_d)
    except (InvalidOperation, ZeroDivisionError):
        return float(value_d)


def round_price(value: Any, tick_size: Any = "0.0001") -> float:
    """Round price to tick size, defaulting to 4 decimals."""
    return round_to_step(value, tick_size, rounding="nearest")


def round_quantity(value: Any, step_size: Any = "0.001") -> float:
    """Round quantity down to exchange step size."""
    return round_to_step(value, step_size, rounding="down")


def clamp(value: Any, min_value: float, max_value: float, default: Optional[float] = None) -> float:
    """Clamp numeric value between min and max."""
    v = safe_float(value, default=default if default is not None else min_value)
    if v is None:
        v = min_value
    return max(min_value, min(max_value, v))


# ---------------------------------------------------------------------------
# Numeric trading helpers
# ---------------------------------------------------------------------------

def percent_change(old: Any, new: Any, default: float = 0.0) -> float:
    """Return percentage change from old to new."""
    old_f = safe_float(old, default=None)
    new_f = safe_float(new, default=None)
    if old_f is None or new_f is None or old_f == 0:
        return default
    return ((new_f - old_f) / abs(old_f)) * 100.0


def pct_distance(a: Any, b: Any, default: float = 0.0) -> float:
    """Return absolute percentage distance between two prices."""
    a_f = safe_float(a, default=None)
    b_f = safe_float(b, default=None)
    if a_f is None or b_f is None or a_f == 0:
        return default
    return abs((b_f - a_f) / a_f) * 100.0


def direction_price_move(direction: str, entry: Any, current: Any) -> float:
    """Return signed percent move in favor of direction."""
    entry_f = safe_float(entry, default=None)
    current_f = safe_float(current, default=None)
    if entry_f is None or current_f is None or entry_f == 0:
        return 0.0

    raw = ((current_f - entry_f) / entry_f) * 100.0
    return raw if normalize_direction(direction) == DIRECTION_LONG else -raw


def profit_usdt(direction: str, entry: Any, exit_price: Any, quantity: Any) -> float:
    """Estimate gross PnL in USDT."""
    entry_f = safe_float(entry, default=None)
    exit_f = safe_float(exit_price, default=None)
    qty_f = safe_float(quantity, default=None)
    if entry_f is None or exit_f is None or qty_f is None:
        return 0.0
    if normalize_direction(direction) == DIRECTION_LONG:
        return (exit_f - entry_f) * qty_f
    return (entry_f - exit_f) * qty_f


def notional_value(price: Any, quantity: Any) -> float:
    """Return notional value = price * quantity."""
    p = safe_float(price, default=None)
    q = safe_float(quantity, default=None)
    if p is None or q is None:
        return 0.0
    return p * q


def fee_estimate(notional: Any, fee_rate: Any, sides: int = 2) -> float:
    """Estimate trading fees using notional * fee_rate * sides."""
    n = safe_float(notional, default=0.0) or 0.0
    r = safe_float(fee_rate, default=0.0) or 0.0
    s = max(1, safe_int(sides, default=2) or 2)
    return n * r * s


def net_profit_after_fee(gross_profit: Any, notional: Any, fee_rate: Any, sides: int = 2) -> float:
    """Estimate net PnL after fee."""
    gp = safe_float(gross_profit, default=0.0) or 0.0
    return gp - fee_estimate(notional, fee_rate, sides=sides)


def progress_to_target(direction: str, entry: Any, current: Any, target: Any) -> float:
    """
    Return progress from entry to target as 0..1+.

    For LONG: (current-entry)/(target-entry)
    For SHORT: (entry-current)/(entry-target)
    """
    d = normalize_direction(direction)
    e = safe_float(entry, default=None)
    c = safe_float(current, default=None)
    t = safe_float(target, default=None)
    if e is None or c is None or t is None:
        return 0.0

    if d == DIRECTION_LONG:
        denom = t - e
        num = c - e
    else:
        denom = e - t
        num = e - c

    if denom <= 0:
        return 0.0
    return num / denom


# ---------------------------------------------------------------------------
# Direction / side helpers
# ---------------------------------------------------------------------------

def normalize_direction(direction: Any) -> str:
    """Normalize direction to LONG or SHORT."""
    text = safe_str(direction).upper()
    if text in {"LONG", "BUY", "BULL", "BULLISH", "UP", "صعودی", "لانگ"}:
        return DIRECTION_LONG
    if text in {"SHORT", "SELL", "BEAR", "BEARISH", "DOWN", "نزولی", "شورت"}:
        return DIRECTION_SHORT
    return ""


def opposite_direction(direction: Any) -> str:
    """Return opposite direction."""
    d = normalize_direction(direction)
    if d == DIRECTION_LONG:
        return DIRECTION_SHORT
    if d == DIRECTION_SHORT:
        return DIRECTION_LONG
    return ""


def is_long(direction: Any) -> bool:
    return normalize_direction(direction) == DIRECTION_LONG


def is_short(direction: Any) -> bool:
    return normalize_direction(direction) == DIRECTION_SHORT


def direction_to_order_side(direction: Any, action: str = "open") -> str:
    """
    Convert trade direction to order side.

    For opening:
    - LONG -> BUY
    - SHORT -> SELL

    For closing:
    - LONG -> SELL
    - SHORT -> BUY
    """
    d = normalize_direction(direction)
    close_action = safe_str(action).lower() in {"close", "exit", "reduce"}
    if d == DIRECTION_LONG:
        return "SELL" if close_action else "BUY"
    if d == DIRECTION_SHORT:
        return "BUY" if close_action else "SELL"
    return ""


# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------

_SYMBOL_CLEAN_RE = re.compile(r"[^A-Z0-9]")


def normalize_symbol(symbol: Any, quote: str = "USDT") -> str:
    """
    Normalize common symbol formats to compact form, e.g.
    doge-usdt, DOGE/USDT, DOGE_USDT -> DOGEUSDT.
    """
    raw = safe_str(symbol).upper()
    if not raw:
        return ""

    cleaned = _SYMBOL_CLEAN_RE.sub("", raw)
    quote = quote.upper()

    if cleaned.endswith(quote):
        return cleaned

    # If user passed only base symbol such as DOGE, append quote.
    return f"{cleaned}{quote}"


def symbol_base(symbol: Any, quote: str = "USDT") -> str:
    """Return base asset from a normalized symbol."""
    normalized = normalize_symbol(symbol, quote=quote)
    quote = quote.upper()
    if normalized.endswith(quote):
        return normalized[: -len(quote)]
    return normalized


def to_okx_inst_id(symbol: Any, market_type: str = "SWAP") -> str:
    """
    Convert compact symbol to OKX instrument id.

    DOGEUSDT -> DOGE-USDT-SWAP
    """
    normalized = normalize_symbol(symbol)
    base = symbol_base(normalized)
    if not base:
        return ""
    if market_type.upper() == "SPOT":
        return f"{base}-USDT"
    return f"{base}-USDT-SWAP"


def to_tobit_symbol(symbol: Any) -> str:
    """
    Convert a regular compact symbol to Toobit futures symbol.

    Special mappings are defined in constants.py, e.g. PEPEUSDT -> 1000PEPEUSDT.
    """
    normalized = normalize_symbol(symbol)
    return TOOBIT_SPECIAL_SYMBOL_MAP.get(normalized, normalized)


def from_tobit_symbol(symbol: Any) -> str:
    """Convert known Toobit special symbol back to internal compact symbol."""
    normalized = normalize_symbol(symbol)
    reverse_map = {v: k for k, v in TOOBIT_SPECIAL_SYMBOL_MAP.items()}
    return reverse_map.get(normalized, normalized)


def is_usdt_linear_symbol(symbol: Any) -> bool:
    """Return True when symbol looks like a USDT linear contract."""
    return normalize_symbol(symbol).endswith("USDT")


def market_symbol_key(symbol: Any, direction: Any = "", level: Any = "") -> str:
    """Stable key for symbol/direction/level grouped learning and stats."""
    parts = [normalize_symbol(symbol)]
    d = normalize_direction(direction)
    if d:
        parts.append(d)
    if not is_none_like(level):
        parts.append(str(level))
    return ":".join(parts)


# ---------------------------------------------------------------------------
# ID / key helpers
# ---------------------------------------------------------------------------

def make_id(prefix: str = "id") -> str:
    """Create a short unique id suitable for signals, positions, and events."""
    clean_prefix = safe_str(prefix, "id").lower().replace(" ", "_")
    return f"{clean_prefix}_{utc_now_ms()}_{uuid.uuid4().hex[:8]}"


def make_signal_id(symbol: Any, direction: Any, level: Any = 4) -> str:
    """Create a stable-looking signal id."""
    return make_id(f"sig_l{level}_{normalize_symbol(symbol).lower()}_{normalize_direction(direction).lower()}")


def make_position_id(symbol: Any, direction: Any, level: Any = 4) -> str:
    """Create a stable-looking position id."""
    return make_id(f"pos_l{level}_{normalize_symbol(symbol).lower()}_{normalize_direction(direction).lower()}")


def make_event_id(event_type: Any = "event") -> str:
    """Create an event id."""
    return make_id(safe_str(event_type, "event").lower())


# ---------------------------------------------------------------------------
# Dict/list helpers
# ---------------------------------------------------------------------------

def get_nested(data: Mapping[str, Any], path: Iterable[str], default: Any = None) -> Any:
    """Safely read nested mapping value."""
    current: Any = data
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return default
        current = current[key]
    return current


def clean_none_values(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy without None values."""
    return {k: v for k, v in dict(data).items() if v is not None}


def ensure_list(value: Any) -> list[Any]:
    """Return value as list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def unique_keep_order(values: Iterable[Any]) -> list[Any]:
    """Return unique values while preserving order."""
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        key = str(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_float(value: Any, digits: int = 4, default: str = "-") -> str:
    """Format a number safely."""
    v = safe_float(value, default=None)
    if v is None:
        return default
    return f"{v:.{max(0, digits)}f}"


def fmt_price(value: Any, default: str = "-") -> str:
    """Format price with adaptive decimals."""
    v = safe_float(value, default=None)
    if v is None:
        return default
    if abs(v) >= 100:
        return f"{v:.2f}"
    if abs(v) >= 1:
        return f"{v:.4f}"
    return f"{v:.8f}".rstrip("0").rstrip(".")


def fmt_percent(value: Any, digits: int = 2, signed: bool = False, default: str = "-") -> str:
    """Format percentage safely."""
    v = safe_float(value, default=None)
    if v is None:
        return default
    sign = "+" if signed and v > 0 else ""
    return f"{sign}{v:.{max(0, digits)}f}%"


def fmt_usdt(value: Any, digits: int = 2, signed: bool = False, default: str = "-") -> str:
    """Format USDT amount safely."""
    v = safe_float(value, default=None)
    if v is None:
        return default
    sign = "+" if signed and v > 0 else ""
    return f"{sign}{v:.{max(0, digits)}f}$"


# ---------------------------------------------------------------------------
# Lightweight validation helpers
# ---------------------------------------------------------------------------

def is_valid_price(value: Any) -> bool:
    v = safe_float(value, default=None)
    return v is not None and v > 0


def is_valid_quantity(value: Any) -> bool:
    v = safe_float(value, default=None)
    return v is not None and v > 0


def is_valid_direction(direction: Any) -> bool:
    return normalize_direction(direction) in {DIRECTION_LONG, DIRECTION_SHORT}


def validate_system_version(version: Any) -> bool:
    """Return True if version matches current locked system version."""
    return safe_str(version) == SYSTEM_VERSION


def validate_market_type(market_type: Any) -> bool:
    """Return True if market type is supported by this bot architecture."""
    return safe_str(market_type).upper() in {"LINEAR_USDT", "SWAP"}


__all__ = [
    "UTILS_VERSION",
    "utc_now_ts",
    "utc_now_ms",
    "utc_now_iso",
    "ms_to_iso",
    "is_none_like",
    "safe_float",
    "safe_int",
    "safe_bool",
    "safe_str",
    "to_decimal",
    "decimal_places_from_step",
    "round_to_step",
    "round_price",
    "round_quantity",
    "clamp",
    "percent_change",
    "pct_distance",
    "direction_price_move",
    "profit_usdt",
    "notional_value",
    "fee_estimate",
    "net_profit_after_fee",
    "progress_to_target",
    "normalize_direction",
    "opposite_direction",
    "is_long",
    "is_short",
    "direction_to_order_side",
    "normalize_symbol",
    "symbol_base",
    "to_okx_inst_id",
    "to_tobit_symbol",
    "from_tobit_symbol",
    "is_usdt_linear_symbol",
    "market_symbol_key",
    "make_id",
    "make_signal_id",
    "make_position_id",
    "make_event_id",
    "get_nested",
    "clean_none_values",
    "ensure_list",
    "unique_keep_order",
    "fmt_float",
    "fmt_price",
    "fmt_percent",
    "fmt_usdt",
    "is_valid_price",
    "is_valid_quantity",
    "is_valid_direction",
    "validate_system_version",
    "validate_market_type",
]
