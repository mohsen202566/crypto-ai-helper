"""مدل‌های داده‌ای ربات Spot Hunter."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import config
from utils import make_id, now_ms, okx_inst_id, toobit_symbol


@dataclass
class BotSettings:
    trading_enabled: bool = config.DEFAULT_TRADING_ENABLED
    trade_amount_usdt: float = config.DEFAULT_TRADE_AMOUNT_USDT
    max_real_positions: int = config.DEFAULT_MAX_REAL_POSITIONS
    target_percent: float = config.DEFAULT_TARGET_PERCENT
    active_symbol_count: int = config.DEFAULT_ACTIVE_SYMBOL_COUNT
    history_check_minutes: int = config.DEFAULT_HISTORY_CHECK_MINUTES
    maker_fee_pct: float = config.DEFAULT_MAKER_FEE_PCT
    taker_fee_pct: float = config.DEFAULT_TAKER_FEE_PCT

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BotSettings":
        if not isinstance(data, dict):
            return cls()
        base = cls()
        for key in asdict(base):
            if key in data:
                setattr(base, key, data[key])
        return base

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Signal:
    id: str
    base_symbol: str
    okx_symbol: str
    toobit_symbol: str
    entry_price: float
    target_price: float
    target_percent: float
    amount_usdt: float
    score: int
    reason: str
    confirmations: dict[str, str] = field(default_factory=dict)
    execution_mode: str = config.MODE_NORMAL
    status: str = config.STATUS_OPEN
    created_at_ms: int = field(default_factory=now_ms)
    closed_at_ms: int | None = None
    telegram_message_id: int | None = None

    # سفارش واقعی Toobit
    buy_order_id: str | None = None
    sell_order_id: str | None = None
    avg_buy_price: float | None = None
    avg_sell_price: float | None = None
    filled_qty: float | None = None
    buy_fee_usdt: float = 0.0
    sell_fee_usdt: float = 0.0

    # نتیجه
    close_price: float | None = None
    move_percent: float = 0.0
    gross_profit_usdt: float = 0.0
    fee_usdt: float = 0.0
    net_profit_usdt: float = 0.0
    close_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        base_symbol: str,
        entry_price: float,
        target_percent: float,
        amount_usdt: float,
        score: int,
        reason: str,
        confirmations: dict[str, str] | None = None,
    ) -> "Signal":
        base = base_symbol.upper()
        target_price = entry_price * (1.0 + target_percent / 100.0)
        return cls(
            id=make_id("sig"),
            base_symbol=base,
            okx_symbol=okx_inst_id(base),
            toobit_symbol=toobit_symbol(base),
            entry_price=float(entry_price),
            target_price=float(target_price),
            target_percent=float(target_percent),
            amount_usdt=float(amount_usdt),
            score=int(score),
            reason=reason,
            confirmations=confirmations or {},
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Signal":
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TradeStats:
    total_signals: int = 0
    normal_signals: int = 0
    real_signals: int = 0
    closed_total: int = 0
    closed_normal: int = 0
    closed_real: int = 0
    wins_count: int = 0
    losses_count: int = 0
    gross_profit_usdt: float = 0.0
    total_fee_usdt: float = 0.0
    net_profit_usdt: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TradeStats":
        if not isinstance(data, dict):
            return cls()
        base = cls()
        for key in asdict(base):
            if key in data:
                setattr(base, key, data[key])
        return base

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def win_rate_count_pct(self) -> float:
        if self.closed_total <= 0:
            return 0.0
        return self.wins_count / self.closed_total * 100.0

    @property
    def avg_net_per_trade(self) -> float:
        if self.closed_total <= 0:
            return 0.0
        return self.net_profit_usdt / self.closed_total


@dataclass
class RealOpenResult:
    opened: bool
    reason: str
    buy_order_id: str | None = None
    sell_order_id: str | None = None
    avg_buy_price: float | None = None
    target_price: float | None = None
    filled_qty: float | None = None
    buy_fee_usdt: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CloseResult:
    closed: bool
    reason: str
    close_price: float | None = None
    move_percent: float = 0.0
    gross_profit_usdt: float = 0.0
    fee_usdt: float = 0.0
    net_profit_usdt: float = 0.0
    sell_fee_usdt: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)
