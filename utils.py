from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from config import MIN_NET_PROFIT_USDT, SLIPPAGE_BUFFER_RATE, TAKER_FEE_RATE

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_digits(text: str) -> str:
    return text.translate(_DIGITS)


def parse_float(text: str) -> float:
    return float(normalize_digits(text).replace(",", ".").strip())


def parse_int(text: str) -> int:
    return int(float(normalize_digits(text).strip()))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def pct(value: float) -> str:
    return f"{value * 100:.3f}%"


def money(value: float | None) -> str:
    if value is None:
        return "نامشخص"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.4f} USDT"


def session_bucket(dt: datetime | None = None) -> str:
    dt = dt or now_utc()
    return f"{dt.hour:02d}:{0 if dt.minute < 30 else 30:02d}"


def round_price(value: float, decimals: int = 8) -> float:
    q = Decimal("1") / (Decimal("10") ** decimals)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def total_round_trip_cost_rate() -> float:
    return (TAKER_FEE_RATE * 2.0) + SLIPPAGE_BUFFER_RATE


def net_profit_for_move(margin_usdt: float, leverage: int, move_pct: float) -> float:
    notional = margin_usdt * leverage
    gross = notional * move_pct
    costs = notional * total_round_trip_cost_rate()
    return gross - costs


def required_move_for_min_profit(margin_usdt: float, leverage: int, min_profit: float = MIN_NET_PROFIT_USDT) -> float:
    notional = max(margin_usdt * leverage, 0.000001)
    return total_round_trip_cost_rate() + (min_profit / notional)


def direction_profit_pct(direction: str, entry: float, exit_price: float) -> float:
    if entry <= 0:
        return 0.0
    if direction == "LONG":
        return (exit_price - entry) / entry
    return (entry - exit_price) / entry


def json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return str(value)
