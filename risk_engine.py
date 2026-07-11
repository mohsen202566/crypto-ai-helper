"""Smart TP/SL Engine.
کار لحظه‌ای این فایل فقط چند محاسبه سبک و خواندن پروفایل آماده است.
"""
from __future__ import annotations

from dataclasses import dataclass

import config
from storage import Storage
from strategy import StrategySignal

@dataclass
class RiskPlan:
    entry: float
    tp: float
    sl: float
    rr: float
    sl_pct: float
    tp_pct: float
    min_net_profit_ok: bool
    estimated_net_profit: float
    fee_estimate: float
    reason: str

def price_from_pct(entry: float, side: str, pct: float) -> float:
    if side.upper() == "LONG":
        return entry * (1.0 + pct / 100.0)
    return entry * (1.0 - pct / 100.0)

def sl_from_pct(entry: float, side: str, pct: float) -> float:
    if side.upper() == "LONG":
        return entry * (1.0 - pct / 100.0)
    return entry * (1.0 + pct / 100.0)

def estimate_net_profit(trade_usdt: float, leverage: int, tp_pct: float) -> tuple[float, float]:
    notional = float(trade_usdt) * float(leverage)
    gross = notional * (float(tp_pct) / 100.0)
    fee = notional * ((config.FALLBACK_FEE_PCT_PER_SIDE * 2.0) / 100.0)
    slip = notional * ((config.SLIPPAGE_PCT_PER_SIDE * 2.0) / 100.0)
    net = gross - fee - slip
    return net, fee + slip

def build_risk_plan(signal: StrategySignal, storage: Storage) -> RiskPlan | None:
    """ساخت TP/SL با RR ثابت تنظیم‌شده برای UEM یک‌ساعته.

    قانون مهم نسخه 1H UEM:
    - TP همیشه دقیقاً بر اساس SL * RISK_REWARD محاسبه می‌شود.
    - اگر حداقل سود خالص تأمین نشود، سیگنال رد می‌شود؛ RR دستکاری نمی‌شود.
    """
    entry = float(signal.entry)
    if entry <= 0:
        return None

    profile = storage.get_profile(signal.symbol_id) or {}
    if getattr(config, "REQUIRE_PROFILE_READY", True):
        if not profile or float(profile.get("min_sl_pct") or 0.0) <= 0:
            return None
        if int(profile.get("signal_count") or 0) < int(getattr(config, "PROFILE_MIN_SIGNALS", 6)):
            return None

    min_sl_pct = float(profile.get("min_sl_pct") or 0.0)
    if min_sl_pct <= 0:
        min_sl_pct = float(getattr(config, "RISK_FALLBACK_MIN_SL_PCT", 0.55))

    sl_pct = max(min_sl_pct, float(getattr(signal, "suggested_sl_pct", 0.0) or 0.0), 0.05)
    tp_pct = sl_pct * float(config.RISK_REWARD)

    trade_usdt = float(storage.get("trade_usdt", config.TRADE_USDT_DEFAULT))
    leverage = int(storage.get("leverage", config.LEVERAGE_DEFAULT))
    net, fee_est = estimate_net_profit(trade_usdt, leverage, tp_pct)

    tp_profile_p70 = float(profile.get("tp_p70") or 0.0)
    # پروفایل فقط واقع‌بینانه بودن TP ثابت config.RISK_REWARD را چک می‌کند؛ TP را تغییر نمی‌دهد.
    profile_ok = True
    if tp_profile_p70 > 0:
        profile_ok = tp_profile_p70 >= tp_pct * 0.82

    ok = bool(net >= config.MIN_NET_PROFIT_USDT and profile_ok)
    return RiskPlan(
        entry=entry,
        tp=price_from_pct(entry, signal.side, tp_pct),
        sl=sl_from_pct(entry, signal.side, sl_pct),
        rr=float(config.RISK_REWARD),
        sl_pct=sl_pct,
        tp_pct=tp_pct,
        min_net_profit_ok=ok,
        estimated_net_profit=net,
        fee_estimate=fee_est,
        reason=(
            "RR ثابت 1.5 + استاپ یک‌ساعته + حداقل سود خالص"
            if ok else
            "RR ثابت 1.5 حفظ شد؛ حداقل سود خالص یا پروفایل TP کافی نبود"
        ),
    )
