"""مدیریت و محاسبه آمار ربات."""
from __future__ import annotations

from models import Signal, TradeStats


class StatsManager:
    @staticmethod
    def register_signal(stats: TradeStats, signal: Signal) -> TradeStats:
        stats.total_signals += 1
        if signal.execution_mode == "real":
            stats.real_signals += 1
        else:
            stats.normal_signals += 1
        return stats

    @staticmethod
    def register_close(stats: TradeStats, signal: Signal) -> TradeStats:
        stats.closed_total += 1
        if signal.execution_mode == "real":
            stats.closed_real += 1
        else:
            stats.closed_normal += 1

        stats.gross_profit_usdt += float(signal.gross_profit_usdt or 0.0)
        stats.total_fee_usdt += float(signal.fee_usdt or 0.0)
        stats.net_profit_usdt += float(signal.net_profit_usdt or 0.0)
        if float(signal.net_profit_usdt or 0.0) > 0:
            stats.wins_count += 1
        else:
            stats.losses_count += 1
        return stats

    @staticmethod
    def reset() -> TradeStats:
        return TradeStats()
