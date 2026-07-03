from __future__ import annotations

from dataclasses import dataclass

from indicators import IndicatorSnapshot


@dataclass(frozen=True)
class MarketContext:
    alignment: str
    bullish_count: int
    bearish_count: int
    risk: str
    long_ok: bool
    real_ok: bool
    reason: str


def _trend(snapshot: IndicatorSnapshot) -> str:
    if snapshot.close > snapshot.ema50 and snapshot.ema20 >= snapshot.ema50 and snapshot.rsi14 >= 50:
        return "UP"
    if snapshot.close < snapshot.ema50 and snapshot.ema20 <= snapshot.ema50 and snapshot.rsi14 <= 47:
        return "DOWN"
    return "NEUTRAL"


def analyze(symbol_tfs: dict[str, IndicatorSnapshot], btc_tfs: dict[str, IndicatorSnapshot] | None, eth_tfs: dict[str, IndicatorSnapshot] | None) -> MarketContext:
    states = {tf: _trend(s) for tf, s in symbol_tfs.items()}
    bullish = sum(1 for v in states.values() if v == "UP")
    bearish = sum(1 for v in states.values() if v == "DOWN")
    ctx_bad = 0
    for ctx in (btc_tfs, eth_tfs):
        if not ctx:
            continue
        c_bear = sum(1 for s in ctx.values() if _trend(s) == "DOWN")
        if c_bear >= 2:
            ctx_bad += 1
    long_ok = bearish < 2
    real_ok = bullish >= 2 and ctx_bad < 2
    risk = "LOW" if bullish >= 3 and ctx_bad == 0 else "MEDIUM" if long_ok else "HIGH"
    alignment = "/".join(f"{tf}:{states.get(tf, '-') }" for tf in ("1D", "4H", "1H"))
    reason = f"هم‌جهتی {alignment} | BTC/ETH ریسک {ctx_bad}"
    return MarketContext(alignment=alignment, bullish_count=bullish, bearish_count=bearish, risk=risk, long_ok=long_ok, real_ok=real_ok, reason=reason)
