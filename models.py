"""مدل‌های داده مشترک معماری رفتارمحور پنج‌دقیقه‌ای."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BehaviorState:
    """حافظه کوتاه هر ارز؛ واچ نیست و فقط برای مقایسه رفتار لحظه‌ای نگه‌داری می‌شود."""
    symbol_id: str
    prices: list[float] = field(default_factory=list)
    trade_imbalances: list[float] = field(default_factory=list)
    book_imbalances: list[float] = field(default_factory=list)
    micro_biases: list[float] = field(default_factory=list)
    spreads: list[float] = field(default_factory=list)
    updated_at: float = 0.0
    last_control: str = "RANGE"

    def append(self, snapshot: "MicroSnapshot", maxlen: int) -> None:
        self.prices.append(float(snapshot.last))
        self.trade_imbalances.append(float(snapshot.trade_imbalance))
        self.book_imbalances.append(float(snapshot.book_imbalance))
        self.micro_biases.append(float(snapshot.microprice_bias_pct))
        self.spreads.append(float(snapshot.spread_pct))
        for seq in (self.prices, self.trade_imbalances, self.book_imbalances, self.micro_biases, self.spreads):
            if len(seq) > maxlen:
                del seq[:-maxlen]


@dataclass(frozen=True)
class MarketSignal:
    symbol_id: str
    okx_symbol: str
    toobit_symbol: str
    side: str
    entry: float
    invalidation_price: float
    noise_pct: float
    expected_move_pct: float
    strength: str
    direction_reason: str
    strength_reason: str
    entry_reason: str
    spread_pct: float
    trade_imbalance: float
    book_imbalance: float
    microprice_bias_pct: float


@dataclass(frozen=True)
class RiskPlan:
    entry: float
    tp: float
    sl: float
    rr_net: float
    sl_pct: float
    tp_pct: float
    notional: float
    quantity_estimate: float
    estimated_tp_gross: float
    estimated_tp_fees: float
    estimated_tp_net: float
    estimated_sl_gross_loss: float
    estimated_sl_fees: float
    estimated_sl_net_loss: float
    min_net_profit_ok: bool
    reason: str


@dataclass(frozen=True)
class MicroSnapshot:
    last: float
    bid: float
    ask: float
    spread_pct: float
    trade_imbalance: float
    book_imbalance: float
    microprice: float
    microprice_bias_pct: float
    trade_count: int
    raw: dict[str, Any]
