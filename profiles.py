"""آپدیت روزانه پروفایل‌ها.
این فایل فقط در بک‌گراند/شروع روز اجرا می‌شود تا سرعت اصل ۱ و ۲ ربات کم نشود.
"""
from __future__ import annotations

import time
from statistics import median

import config
from okx_client import OKXClient
from storage import Storage
from strategy import analyze_symbol, pct_range
from symbols import SYMBOLS, SymbolMap


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    k = (len(xs) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return xs[f]
    return xs[f] + (xs[c] - xs[f]) * (k - f)

class ProfileBuilder:
    def __init__(self, okx: OKXClient, storage: Storage):
        self.okx = okx
        self.storage = storage

    def build_symbol_profile(self, sym: SymbolMap) -> dict[str, float | int]:
        candles = self.okx.get_candles(sym.okx, bar=config.OKX_PRIMARY_BAR, limit=min(1500, config.PROFILE_LOOKBACK_DAYS * 48))
        ranges = [pct_range(c) for c in candles if c.get("close", 0) > 0]
        noise_median = median(ranges) if ranges else 0.0
        noise_p70 = percentile(ranges, config.NOISE_PERCENTILE)
        min_sl_pct = noise_p70 * config.NOISE_SL_MULTIPLIER if noise_p70 > 0 else 0.35

        favorable_moves: list[float] = []
        # شبیه‌سازی سبک سیگنال‌های گذشته با همین روش اصلی 30-60M.
        horizon = 8  # حدود ۴ ساعت بعد از سیگنال روی 30m
        for i in range(80, max(80, len(candles) - horizon)):
            window = candles[: i + 1]
            sig = analyze_symbol(sym.id, sym.okx, sym.toobit, window)
            if not sig:
                continue
            future = candles[i + 1 : i + 1 + horizon]
            if not future:
                continue
            entry = sig.entry
            if sig.side == "LONG":
                move = (max(c["high"] for c in future) - entry) / entry * 100.0
            else:
                move = (entry - min(c["low"] for c in future)) / entry * 100.0
            if move > 0:
                favorable_moves.append(move)

        tp_median = median(favorable_moves) if favorable_moves else 0.0
        tp_p70 = percentile(favorable_moves, config.TP_PROFILE_PERCENTILE)
        return {
            "noise_median": round(noise_median, 6),
            "noise_p70": round(noise_p70, 6),
            "min_sl_pct": round(min_sl_pct, 6),
            "tp_median": round(tp_median, 6),
            "tp_p70": round(tp_p70, 6),
            "signal_count": len(favorable_moves),
            "updated_at": int(time.time()),
        }

    def update_all(self) -> None:
        for sym in SYMBOLS:
            try:
                data = self.build_symbol_profile(sym)
                self.storage.upsert_profile(sym.id, data)
            except Exception as exc:
                self.storage.add_health_event("profiles", "warning", f"profile update failed: {exc}", sym.id)
                continue
