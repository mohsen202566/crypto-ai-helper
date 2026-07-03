from __future__ import annotations

import config
from storage import Storage, StoredSignal
from utils import net_profit_after_fees


class LearningEngine:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def learn_result(self, signal: StoredSignal, result: str, exit_price: float, net_profit: float, mfe_pct: float, mae_pct: float, reason: str) -> None:
        target_distance = (signal.target_price - signal.entry_price) / signal.entry_price if signal.entry_price > 0 else 0.0
        self.storage.record_observation(source=signal.signal_type, signal_id=signal.id, features_key=signal.features_key, symbol_name=signal.symbol_name, result=result, net_profit=net_profit, mfe_pct=mfe_pct, mae_pct=mae_pct, target_distance_pct=target_distance, reason=reason)
        self._maybe_capital_suggestion()

    def learn_warning(self, signal: StoredSignal, reason: str, current_price: float) -> None:
        distance = (signal.target_price - current_price) / current_price if current_price > 0 else 0.0
        self.storage.record_warning(signal.id, reason, current_price, distance)

    def estimated_normal_pnl(self, signal: StoredSignal, exit_price: float) -> float:
        return net_profit_after_fees(signal.entry_price, exit_price, signal.trade_usdt, config.SPOT_TAKER_FEE_RATE, config.SPOT_TAKER_FEE_RATE)[0]

    def _maybe_capital_suggestion(self) -> None:
        stats = self.storage.all_stats()
        if stats["total"] < 20:
            return
        ai = self.storage.ai_summary()
        confidence = float(ai.get("confidence") or 0)
        pnl = float(stats.get("pnl") or 0)
        current = self.storage.trade_usdt()
        if confidence < 35 and current > 5:
            self.storage.add_capital_suggestion("risk", f"اعتماد AI هنوز {confidence:.1f}% است؛ بازار/نمونه‌ها ریسکی‌اند. پیشنهاد: دلار هر پوزیشن نزدیک 5 USDT باشد.")
        elif confidence > 65 and pnl > 0 and current < 20:
            self.storage.add_capital_suggestion("growth", f"اعتماد AI {confidence:.1f}% و سود کلی مثبت است. برای سود بیشتر می‌توانی دلار هر پوزیشن را تا حدود 20 USDT بررسی کنی.")
