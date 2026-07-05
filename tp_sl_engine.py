from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from config import BotSettings
from indicators import recent_resistance, recent_support, snapshot
from okx_client import Candle

Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class TPSLResult:
    passed: bool
    entry: float
    tp: float
    sl: float
    estimated_move_percent: float
    estimated_gross_profit: float
    estimated_net_profit: float
    estimated_loss_after_fee: float
    rr: float
    estimated_hold_time: str
    reason: str


def calculate_tp_sl(
    *,
    direction: Direction,
    entry: float,
    h1: list[Candle],
    m15: list[Candle],
    m5: list[Candle],
    settings: BotSettings,
    strength: str,
) -> TPSLResult:
    h1s = snapshot(h1)
    m15s = snapshot(m15)
    m5s = snapshot(m5)
    notional = settings.trade_amount_usdt * settings.leverage
    qty = notional / entry

    strength_mult = {"ضعیف": 1.1, "معمولی": 1.4, "قوی": 1.8, "خیلی قوی": 2.2}.get(strength, 1.4)
    atr_target = h1s.atr14 * strength_mult
    stop_buffer = max(m5s.atr14 * 0.35, entry * 0.0008)

    if direction == "LONG":
        natural_sl = min(recent_support(m5, 30), recent_support(m15, 20)) - stop_buffer
        sr_target = min(x for x in [recent_resistance(h1, 60), recent_resistance(m15, 60)] if x > entry)
        tp = min(entry + atr_target, sr_target * 0.998)
        sl = natural_sl
        if sl >= entry:
            sl = entry - max(m15s.atr14 * 0.8, entry * 0.003)
        gross_profit = (tp - entry) * qty
        gross_loss = (entry - sl) * qty
        move_percent = (tp - entry) / entry * 100
    else:
        natural_sl = max(recent_resistance(m5, 30), recent_resistance(m15, 20)) + stop_buffer
        sr_target = max(x for x in [recent_support(h1, 60), recent_support(m15, 60)] if x < entry)
        tp = max(entry - atr_target, sr_target * 1.002)
        sl = natural_sl
        if sl <= entry:
            sl = entry + max(m15s.atr14 * 0.8, entry * 0.003)
        gross_profit = (entry - tp) * qty
        gross_loss = (sl - entry) * qty
        move_percent = (entry - tp) / entry * 100

    net_profit = gross_profit - settings.fee_usdt
    net_loss = gross_loss + settings.fee_usdt
    rr = net_profit / max(net_loss, 1e-9)

    if strength in {"خیلی قوی", "قوی"}:
        hold = "۴ تا ۱۲ ساعت"
    else:
        hold = "۲ تا ۶ ساعت"

    if tp <= 0 or sl <= 0 or entry <= 0:
        return _fail(entry, tp, sl, "قیمت تیپی یا استاپ معتبر نیست.")
    if direction == "LONG" and not (tp > entry > sl):
        return _fail(entry, tp, sl, "چیدمان تیپی و استاپ برای لانگ درست نیست.")
    if direction == "SHORT" and not (tp < entry < sl):
        return _fail(entry, tp, sl, "چیدمان تیپی و استاپ برای شورت درست نیست.")
    if net_profit < settings.min_net_profit_usdt:
        return _fail(entry, tp, sl, "سود خالص بعد از کارمزد کافی نیست.", move_percent, gross_profit, net_profit, net_loss, rr, hold)
    if rr < settings.min_rr:
        return _fail(entry, tp, sl, "ریسک به ریوارد ارزش معامله ندارد.", move_percent, gross_profit, net_profit, net_loss, rr, hold)

    return TPSLResult(True, entry, tp, sl, move_percent, gross_profit, net_profit, net_loss, rr, hold, "تیپی و استاپ تایید شد.")


def _fail(
    entry: float,
    tp: float,
    sl: float,
    reason: str,
    move: float = 0.0,
    gross: float = 0.0,
    net: float = 0.0,
    loss: float = 0.0,
    rr: float = 0.0,
    hold: str = "نامشخص",
) -> TPSLResult:
    return TPSLResult(False, entry, tp, sl, move, gross, net, loss, rr, hold, reason)
