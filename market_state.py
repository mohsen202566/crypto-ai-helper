from __future__ import annotations

from indicators import IndicatorSnapshot


def classify(snapshot_5m: IndicatorSnapshot, snapshot_15m: IndicatorSnapshot) -> str:
    if snapshot_5m.volume_ratio < 0.35 or snapshot_5m.adx14 < 8:
        return "DEAD_MARKET"
    if snapshot_5m.volume_ratio > 6.0 and snapshot_5m.rsi14 > 72:
        return "CLIMAX_RISK"
    if snapshot_5m.atr_pct > 0.03:
        return "NOISY"
    if snapshot_5m.close > snapshot_5m.vwap and snapshot_5m.ema20 >= snapshot_5m.ema50 and snapshot_5m.adx14 >= 16 and snapshot_5m.di_plus >= snapshot_5m.di_minus:
        return "TREND_UP"
    if snapshot_5m.close >= snapshot_5m.ema50 and abs(snapshot_5m.dist_vwap_pct) <= 0.012 and snapshot_15m.rsi14 >= 48:
        return "PULLBACK_BUY"
    if snapshot_5m.adx14 < 14 and abs(snapshot_5m.dist_vwap_pct) < 0.008:
        return "RANGE"
    if snapshot_5m.close > snapshot_5m.swing_high * 0.998 and snapshot_5m.volume_ratio > 1.2:
        return "BREAKOUT"
    return "NORMAL"
