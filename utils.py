from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from config import BOT_TIMEZONE, FEE_SIDE_MODE, FUTURES_MAKER_FEE_RATE, FUTURES_TAKER_FEE_RATE, MIN_NET_PROFIT_USDT, SLIPPAGE_BUFFER_RATE, TAKER_FEE_RATE

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


def local_time(dt: datetime | None = None) -> datetime:
    base = dt or now_utc()
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    try:
        return base.astimezone(ZoneInfo(BOT_TIMEZONE))
    except Exception:
        return base.astimezone(timezone.utc)


def session_bucket(dt: datetime | None = None) -> str:
    local = local_time(dt)
    return f"{local.hour:02d}:{0 if local.minute < 30 else 30:02d}"


def round_price(value: float, decimals: int = 8) -> float:
    q = Decimal("1") / (Decimal("10") ** decimals)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def futures_fee_rate() -> float:
    if FEE_SIDE_MODE == "maker":
        return FUTURES_MAKER_FEE_RATE
    return FUTURES_TAKER_FEE_RATE


def total_round_trip_cost_rate(*, include_slippage: bool = True) -> float:
    cost = futures_fee_rate() * 2.0
    if include_slippage:
        cost += SLIPPAGE_BUFFER_RATE
    return cost


def futures_fee_usdt(margin_usdt: float, leverage: int, *, round_trip: bool = True) -> float:
    notional = margin_usdt * leverage
    sides = 2.0 if round_trip else 1.0
    return notional * futures_fee_rate() * sides


def pnl_breakdown_for_move(margin_usdt: float, leverage: int, move_pct: float) -> dict[str, float]:
    notional = margin_usdt * leverage
    gross = notional * move_pct
    fee = futures_fee_usdt(margin_usdt, leverage, round_trip=True)
    # Slippage stays in planning/minimum-profit checks, but the user-facing realized result
    # reports exchange futures fee separately and net after fee.
    net = gross - fee
    return {"gross_pnl": gross, "fee_usdt": fee, "net_pnl": net, "notional": notional}


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
