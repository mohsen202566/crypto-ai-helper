from __future__ import annotations

from dataclasses import dataclass

from config import SLIPPAGE_BUFFER_RATE, TAKER_FEE_RATE
from utils import direction_profit_pct


@dataclass(frozen=True)
class NetPnl:
    gross_usdt: float
    cost_usdt: float
    net_usdt: float
    move_pct: float
    cost_pct: float


def round_trip_cost_pct() -> float:
    """Estimated entry + exit cost as a fraction of position notional."""
    return 2.0 * (TAKER_FEE_RATE + SLIPPAGE_BUFFER_RATE)


def notional_usdt(margin_usdt: float, leverage: int | float) -> float:
    return max(0.0, float(margin_usdt)) * max(1.0, float(leverage or 1))


def estimate_round_trip_cost_usdt(margin_usdt: float, leverage: int | float) -> float:
    return notional_usdt(margin_usdt, leverage) * round_trip_cost_pct()


def net_profit_for_move(margin_usdt: float, leverage: int | float, move_pct: float) -> NetPnl:
    notional = notional_usdt(margin_usdt, leverage)
    gross = notional * float(move_pct)
    cost = estimate_round_trip_cost_usdt(margin_usdt, leverage)
    return NetPnl(
        gross_usdt=gross,
        cost_usdt=cost,
        net_usdt=gross - cost,
        move_pct=float(move_pct),
        cost_pct=round_trip_cost_pct(),
    )


def net_profit_for_exit(direction: str, entry: float, exit_price: float, margin_usdt: float, leverage: int | float) -> NetPnl:
    move_pct = direction_profit_pct(direction, entry, exit_price)
    return net_profit_for_move(margin_usdt, leverage, move_pct)


def learning_bucket_key(symbol_name: str, direction: str, features_key: str) -> str:
    """Keep range learning isolated per coin, side and range/context."""
    symbol = str(symbol_name or "UNKNOWN").strip()
    side = str(direction or "UNKNOWN").strip().upper()
    key = str(features_key or "UNKNOWN_RANGE").strip()
    prefix = f"{symbol}|{side}|"
    return key if key.startswith(prefix) else f"{prefix}{key}"
