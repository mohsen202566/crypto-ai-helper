"""موتور پویا و محتاط TP/SL بر پایه رفتار، نویز، هزینه و RR خالص."""
from __future__ import annotations
import config
from models import MarketSignal, RiskPlan


def _fee_rate(mode: str) -> float:
    pct = config.MAKER_FEE_PCT_PER_SIDE if str(mode).upper() == "MAKER" else config.TAKER_FEE_PCT_PER_SIDE
    return pct / 100.0


def _price(entry: float, side: str, pct: float, favorable: bool) -> float:
    direction = 1.0 if side == "LONG" else -1.0
    if not favorable:
        direction *= -1.0
    return entry * (1.0 + direction * pct / 100.0)


def _costs(notional: float, entry: float, exit_price: float) -> float:
    qty = notional / entry
    entry_fee = notional * _fee_rate(config.ENTRY_FEE_MODE)
    exit_notional = qty * exit_price
    exit_fee = exit_notional * _fee_rate(config.EXIT_FEE_MODE)
    slip = (notional + exit_notional) * (config.SLIPPAGE_PCT_PER_SIDE / 100.0)
    return entry_fee + exit_fee + slip


def _net_for_move(notional: float, entry: float, side: str, pct: float) -> tuple[float, float, float, float]:
    exit_price = _price(entry, side, pct, favorable=True)
    gross = notional * pct / 100.0
    fees = _costs(notional, entry, exit_price)
    return exit_price, gross, fees, gross - fees


def build_risk_plan_diagnostic(signal: MarketSignal, trade_usdt: float, leverage: int) -> tuple[RiskPlan | None, str, dict[str, float | int | str]]:
    entry = float(signal.entry)
    metrics: dict[str, float | int | str] = {"entry": entry, "trade_usdt": trade_usdt, "leverage": leverage}
    if entry <= 0 or trade_usdt <= 0 or leverage <= 0:
        return None, "ورودی مالی یا قیمت نامعتبر است", metrics

    notional = float(trade_usdt) * int(leverage)
    raw_invalidation_pct = abs(entry - float(signal.invalidation_price)) / entry * 100.0
    noise = max(float(signal.noise_pct), float(signal.spread_pct) * 2.0)
    if signal.strength == "بسیار قوی":
        noise_fraction = config.NOISE_BUFFER_MIN_FRACTION
        tp_fraction = config.TP_CAUTION_STRONG_FRACTION
    elif signal.strength == "متوسط":
        noise_fraction = config.NOISE_BUFFER_HIGH_FRACTION
        tp_fraction = config.TP_CAUTION_MIN_FRACTION
    else:
        noise_fraction = config.NOISE_BUFFER_NORMAL_FRACTION
        tp_fraction = config.TP_CAUTION_NORMAL_FRACTION

    # استاپ پشت ابطال واقعی و سپس بیرون نویز؛ هرگز برای ساخت RR مصنوعی کوچک نمی‌شود.
    sl_pct = raw_invalidation_pct + noise * noise_fraction
    max_sl = float(signal.expected_move_pct) * config.MAX_STOP_EXPECTED_MOVE_FRACTION
    metrics.update({"notional": notional, "raw_invalidation_pct": raw_invalidation_pct, "noise_pct": noise,
                    "noise_buffer_fraction": noise_fraction, "sl_pct": sl_pct, "max_sl_pct": max_sl})
    if sl_pct <= 0 or sl_pct > max_sl:
        return None, "استاپ منطقیِ خارج نویز با ظرفیت حرکت سازگار نیست", metrics

    sl = _price(entry, signal.side, sl_pct, favorable=False)
    sl_gross = notional * sl_pct / 100.0
    sl_fees = _costs(notional, entry, sl)
    sl_net_loss = sl_gross + sl_fees
    required_net = sl_net_loss * config.RISK_REWARD

    # TP کمی قبل از ظرفیت تخمینی قرار می‌گیرد تا احتمال برخورد بالا برود.
    cautious_cap = float(signal.expected_move_pct) * tp_fraction
    lo, hi = 0.0, max(cautious_cap, sl_pct * 2.0)
    for _ in range(50):
        mid = (lo + hi) / 2.0
        _, gross, fees, net = _net_for_move(notional, entry, signal.side, mid)
        required = max(required_net, config.MIN_NET_PROFIT_USDT)
        if net < required or gross < config.MIN_GROSS_PROFIT_USDT:
            lo = mid
        else:
            hi = mid
    tp_pct = hi
    metrics.update({"tp_required_pct": tp_pct, "cautious_capacity_pct": cautious_cap, "tp_fraction": tp_fraction})
    if tp_pct > cautious_cap:
        return None, "TP محتاطانه سود خالص و RR لازم را تأمین نمی‌کند", metrics

    tp, tp_gross, tp_fees, tp_net = _net_for_move(notional, entry, signal.side, tp_pct)
    rr_net = tp_net / sl_net_loss if sl_net_loss > 0 else 0.0
    if tp_gross + 1e-9 < config.MIN_GROSS_PROFIT_USDT:
        return None, "سود ناخالص کمتر از کف ۱۰ سنت است", metrics
    if tp_net + 1e-9 < config.MIN_NET_PROFIT_USDT:
        return None, "سود خالص کمتر از کف ۵ سنت است", metrics
    if rr_net + 1e-9 < config.RISK_REWARD:
        return None, "RR خالص کمتر از ۱.۵ است", metrics

    qty = notional / entry
    reason = (f"TP روی بخش محتاطانه ظرفیت حرکت ({tp_fraction:.0%}) | "
              f"SL پشت ابطال + حاشیه نویز ({noise_fraction:.0%}) | RR خالص {rr_net:.3f}")
    plan = RiskPlan(entry, tp, sl, rr_net, sl_pct, tp_pct, notional, qty, tp_gross, tp_fees, tp_net,
                    sl_gross, sl_fees, sl_net_loss, True, reason)
    return plan, "برنامه TP/SL تأیید شد", metrics


def build_risk_plan(signal: MarketSignal, trade_usdt: float, leverage: int) -> RiskPlan | None:
    return build_risk_plan_diagnostic(signal, trade_usdt, leverage)[0]
