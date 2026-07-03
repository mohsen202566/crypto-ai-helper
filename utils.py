from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("spot_ai_range_profit_bot")

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_digits(text: str) -> str:
    return str(text or "").translate(_PERSIAN_DIGITS)


def parse_float(value: str) -> float:
    text = normalize_digits(value).replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        raise ValueError("عدد معتبر پیدا نشد.")
    return float(match.group(0))


def parse_int(value: str) -> int:
    return int(round(parse_float(value)))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def money(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.4f} USDT"


def pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.2f}%"


def duration_text(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    minutes = seconds // 60
    hours = minutes // 60
    days = hours // 24
    if days:
        return f"{days} روز و {hours % 24} ساعت"
    if hours:
        return f"{hours} ساعت و {minutes % 60} دقیقه"
    return f"{minutes} دقیقه"


def session_bucket(dt: datetime | None = None) -> str:
    dt = dt or now_utc()
    hour = dt.hour
    if 0 <= hour < 7:
        return "ASIA"
    if 7 <= hour < 13:
        return "EUROPE"
    if 13 <= hour < 21:
        return "AMERICA"
    return "LATE_US"


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("0")


def decimal_round_down(value: Any, step: str = "0.000001", digits: int = 8) -> str:
    val = _to_decimal(value)
    step_d = _to_decimal(step)
    if step_d <= 0:
        return f"{float(val):.{digits}f}".rstrip("0").rstrip(".") or "0"
    rounded = (val / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d
    fmt = f"{{0:.{digits}f}}".format(float(rounded))
    return fmt.rstrip("0").rstrip(".") or "0"


def decimal_to_api(value: Any) -> str:
    text = f"{safe_float(value):.12f}".rstrip("0").rstrip(".")
    return text or "0"


def round_price_to_tick(price: float, tick: str = "0.000001", direction: str = "nearest") -> float:
    value = _to_decimal(price)
    tick_d = _to_decimal(tick)
    if tick_d <= 0:
        return float(value)
    mode = ROUND_UP if direction == "up" else ROUND_DOWN if direction == "down" else ROUND_DOWN
    rounded = (value / tick_d).to_integral_value(rounding=mode) * tick_d
    return float(rounded)


def extract_filter(info: dict[str, Any], filter_type: str) -> dict[str, Any]:
    for key in ("filters", "filter", "symbolFilters"):
        filters = info.get(key)
        if isinstance(filters, list):
            for item in filters:
                if isinstance(item, dict) and str(item.get("filterType") or item.get("type") or "").upper() == filter_type.upper():
                    return item
    return {}


def net_profit_after_fees(entry: float, target: float, trade_usdt: float, buy_fee_rate: float, sell_fee_rate: float) -> tuple[float, float]:
    if entry <= 0 or target <= 0 or trade_usdt <= 0:
        return 0.0, 0.0
    qty = trade_usdt / entry
    gross = (target - entry) * qty
    fee = trade_usdt * buy_fee_rate + target * qty * sell_fee_rate
    return gross - fee, fee


def direction_word() -> str:
    return "BUY"
