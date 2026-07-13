from __future__ import annotations

import threading
import time

import config


class ExecutionEngine:
    def __init__(self, storage, toobit, okx, health, exchange_lock: threading.RLock | None = None):
        self.storage = storage
        self.toobit = toobit
        self.okx = okx
        self.health = health
        self.lock = exchange_lock or threading.RLock()

    @staticmethod
    def _deviation_pct(reference: float, current: float) -> float:
        return abs(current - reference) / reference * 100.0 if reference > 0 and current > 0 else float("inf")

    @staticmethod
    def _position_matches_side(position, side: str) -> bool:
        side_u = side.upper()
        position_side = str(position.get("positionSide") or position.get("side") or position.get("position_side") or "").upper()
        if position_side in {"LONG", "SHORT"}:
            return position_side == side_u
        try:
            amount = float(position.get("positionAmt") or position.get("size") or position.get("qty") or position.get("quantity") or 0)
        except (TypeError, ValueError):
            return False
        return amount > 0 if side_u == "LONG" else amount < 0

    def execute(self, symbol, signal_id, side, risk):
        if not self.storage.get("trading_enabled", False):
            return {"status": "VIRTUAL_ONLY", "reason": "ترید واقعی خاموش است"}
        if not self.toobit.has_credentials:
            return {"status": "VIRTUAL_ONLY", "reason": "کلیدهای API توبیت تنظیم نشده‌اند"}

        order_attempted = False
        with self.lock:
            try:
                db_open = self.storage.count_real_open()
                exchange_open = len(self.toobit.get_open_positions())
                max_positions = int(self.storage.get("max_positions", config.MAX_POSITIONS_DEFAULT))
                if max(db_open, exchange_open) >= max_positions:
                    return {"status": "VIRTUAL_ONLY", "reason": "اسلات واقعی خالی نیست"}

                balance = self.toobit.get_futures_balance()
                required_free = float(risk.trade_usdt) * (1.0 + config.MIN_FREE_MARGIN_BUFFER_PCT / 100.0)
                if float(balance.get("available") or 0.0) < required_free:
                    return {"status": "VIRTUAL_ONLY", "reason": "موجودی آزاد توبیت برای مارجین و حاشیه ایمنی کافی نیست"}

                # A last public-price check prevents sending a stale market entry after Telegram/network delay.
                current_price = float(self.okx.get_last_price(symbol.okx))
                allowed_deviation = min(
                    float(config.EXECUTION_MAX_DEVIATION_PCT),
                    max(0.05, float(risk.sl_pct) * float(config.EXECUTION_MAX_DEVIATION_SL_FRACTION)),
                )
                deviation = self._deviation_pct(float(risk.entry), current_price)
                if deviation > allowed_deviation:
                    return {
                        "status": "VIRTUAL_ONLY",
                        "reason": f"قیمت اجرای زنده {deviation:.3f}% از Entry فاصله گرفته و ورود دیر شده است",
                    }

                execution_risk = risk.rebased(current_price) if config.REAL_ENTRY_REBASE_ENABLED else risk
                if side.upper() == "LONG" and not (execution_risk.sl < execution_risk.entry < execution_risk.tp):
                    return {"status": "VIRTUAL_ONLY", "reason": "چیدمان اجرایی TP/SL برای LONG نامعتبر است"}
                if side.upper() == "SHORT" and not (execution_risk.tp < execution_risk.entry < execution_risk.sl):
                    return {"status": "VIRTUAL_ONLY", "reason": "چیدمان اجرایی TP/SL برای SHORT نامعتبر است"}

                order_attempted = True
                result = self.toobit.open_futures_position_with_tpsl(
                    symbol.toobit,
                    side,
                    execution_risk.trade_usdt,
                    execution_risk.leverage,
                    execution_risk.entry,
                    execution_risk.tp,
                    execution_risk.sl,
                    f"bot_{signal_id}_{int(time.time())}",
                )
                order_id = result.get("order_id")
                self.storage.update_signal(
                    signal_id,
                    is_real=1,
                    trade_mode="real",
                    status="pending",
                    order_id=order_id,
                )
                if not order_id:
                    self.storage.add_health_event(
                        "toobit_order",
                        "warning",
                        "سفارش پذیرفته شد اما Order ID از پاسخ توبیت استخراج نشد",
                        symbol.id,
                    )
                self.health.mark("toobit")
                return {"status": "REAL_PENDING", "order_id": order_id}
            except Exception as exc:
                # A POST timeout can be ambiguous. Check the exchange before falling back to virtual.
                try:
                    positions = self.toobit.get_open_positions(symbol.toobit) if order_attempted else []
                    if any(self._position_matches_side(position, side) for position in positions):
                        self.storage.update_signal(signal_id, is_real=1, trade_mode="real", status="pending")
                        self.storage.add_health_event(
                            "toobit_order",
                            "critical",
                            f"پاسخ سفارش نامشخص بود اما پوزیشن روی توبیت دیده شد: {exc}",
                            symbol.id,
                        )
                        return {"status": "REAL_PENDING", "reason": "پوزیشن روی صرافی دیده شد؛ پاسخ سفارش نامشخص است"}
                except Exception:
                    pass
                self.storage.add_health_event("toobit_order", "warning", str(exc), symbol.id)
                return {"status": "VIRTUAL_ONLY", "reason": str(exc)}
