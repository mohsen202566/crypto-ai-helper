"""مدیریت سفارش‌های واقعی Spot در Toobit."""
from __future__ import annotations

import asyncio
from typing import Any

import config
from models import BotSettings, CloseResult, RealOpenResult, Signal
from toobit_client import ToobitClient, ToobitError
from utils import make_id, now_ms, pct_change, target_price_from_entry


class OrderManager:
    def __init__(self, toobit: ToobitClient, settings: BotSettings):
        self.toobit = toobit
        self.settings = settings
        self._exchange_symbols: dict[str, dict[str, Any]] | None = None

    def update_settings(self, settings: BotSettings) -> None:
        self.settings = settings

    async def open_real_spot_position(self, signal: Signal) -> RealOpenResult:
        return await asyncio.to_thread(self._open_real_spot_position_sync, signal)

    def _open_real_spot_position_sync(self, signal: Signal) -> RealOpenResult:
        if not self.toobit.has_credentials:
            return RealOpenResult(opened=False, reason="کلید API توبیت تنظیم نشده است")

        try:
            if self._exchange_symbols is None:
                self._exchange_symbols = self.toobit.get_spot_symbols()
            symbol, symbol_info = self.toobit.validate_spot_symbol(signal.toobit_symbol, self._exchange_symbols)

            buy_client_id = make_id("spot_buy")
            buy = self.toobit.place_spot_market_buy(symbol, signal.amount_usdt, buy_client_id)
            buy_order_id = buy.get("order_id")

            filled_buy = self.toobit.wait_spot_order_fill(
                symbol,
                buy_order_id,
                timeout_seconds=config.BUY_FILL_TIMEOUT_SECONDS,
                poll_seconds=config.BUY_FILL_POLL_SECONDS,
            )
            if not filled_buy:
                if buy_order_id:
                    try:
                        self.toobit.cancel_spot_order(symbol, order_id=buy_order_id)
                    except Exception:
                        pass
                return RealOpenResult(
                    opened=False,
                    buy_order_id=buy_order_id,
                    reason=f"خرید تا {config.BUY_FILL_TIMEOUT_SECONDS} ثانیه پر نشد؛ اسلات آزاد شد",
                    raw={"buy": buy},
                )

            buy_fill = self.toobit.parse_order_fill(filled_buy, fallback_fee_pct=self.settings.taker_fee_pct)
            qty = buy_fill["qty"]
            avg_buy = buy_fill["avg_price"]
            if qty <= 0 or avg_buy <= 0:
                return RealOpenResult(
                    opened=False,
                    buy_order_id=buy_order_id,
                    reason="خرید پاسخ پرشده داد ولی مقدار یا میانگین قیمت معتبر نبود",
                    raw={"buy": buy, "filled_buy": filled_buy},
                )

            target_price = target_price_from_entry(avg_buy, signal.target_percent)
            sell_client_id = make_id("spot_sell")
            sell = self.toobit.place_spot_limit_sell(symbol, qty, target_price, sell_client_id, symbol_info=symbol_info)

            return RealOpenResult(
                opened=True,
                reason="خرید واقعی پر شد و سفارش فروش هدف روی Toobit ثبت شد",
                buy_order_id=buy_order_id,
                sell_order_id=sell.get("order_id"),
                avg_buy_price=avg_buy,
                target_price=float(sell.get("price") or target_price),
                filled_qty=float(sell.get("quantity") or qty),
                buy_fee_usdt=buy_fill["fee_usdt"],
                raw={"buy": buy, "filled_buy": filled_buy, "sell": sell},
            )
        except Exception as exc:
            return RealOpenResult(opened=False, reason=f"خطا در اجرای خرید واقعی Toobit: {exc}")

    async def check_real_close(self, signal: Signal) -> CloseResult:
        return await asyncio.to_thread(self._check_real_close_sync, signal)

    def _check_real_close_sync(self, signal: Signal) -> CloseResult:
        if not signal.sell_order_id:
            return CloseResult(closed=False, reason="سفارش فروش هدف ثبت نشده است")
        try:
            end_ms = now_ms()
            start_ms = max(0, int(signal.created_at_ms) - 120_000)
            sell_item = self.toobit.find_filled_order(
                symbol=signal.toobit_symbol,
                order_id=signal.sell_order_id,
                side="SELL",
                start_ms=start_ms,
                end_ms=end_ms,
            )
            if not sell_item:
                return CloseResult(closed=False, reason="سفارش فروش هنوز پر نشده است")

            sell_fill = self.toobit.parse_order_fill(sell_item, fallback_fee_pct=self.settings.maker_fee_pct)
            avg_sell = sell_fill["avg_price"]
            qty = min(signal.filled_qty or sell_fill["qty"], sell_fill["qty"] or signal.filled_qty or 0.0)
            avg_buy = signal.avg_buy_price or signal.entry_price
            if qty <= 0 or avg_buy <= 0 or avg_sell <= 0:
                return CloseResult(closed=False, reason="اطلاعات فروش پرشده کامل نیست")

            gross = (avg_sell - avg_buy) * qty
            total_fee = float(signal.buy_fee_usdt or 0.0) + sell_fill["fee_usdt"]
            net = gross - total_fee
            move_pct = pct_change(avg_buy, avg_sell)
            return CloseResult(
                closed=True,
                reason="سفارش فروش هدف در Toobit پر شد",
                close_price=avg_sell,
                move_percent=move_pct,
                gross_profit_usdt=gross,
                fee_usdt=total_fee,
                net_profit_usdt=net,
                sell_fee_usdt=sell_fill["fee_usdt"],
                raw={"sell_item": sell_item, "sell_fill": sell_fill},
            )
        except Exception as exc:
            return CloseResult(closed=False, reason=f"چک هیستوری Toobit ناموفق بود: {exc}")
