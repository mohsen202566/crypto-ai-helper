from __future__ import annotations

import asyncio
import time
from datetime import datetime

import config
from learning_engine import LearningEngine
from okx_data import OkxDataClient
from storage import Storage, StoredSignal
from toobit_client import ToobitClient
from utils import duration_text, logger, net_profit_after_fees, now_utc


class SignalMonitor:
    """Monitor active signals without wasting Toobit API calls.

    Normal signals are tracked only with OKX prices.
    Real signals read the final SELL fill from Toobit, but the expensive Toobit
    history check is throttled per signal. Price/risk warnings use OKX so active
    real positions do not spam Toobit just for warning calculations.
    """

    def __init__(self, storage: Storage, okx: OkxDataClient, toobit: ToobitClient, ui) -> None:
        self.storage = storage
        self.okx = okx
        self.toobit = toobit
        self.ui = ui
        self.learning = LearningEngine(storage)
        self._last_real_toobit_check: dict[int, float] = {}

    async def run_once(self) -> None:
        for signal in self.storage.open_signals():
            try:
                if signal.signal_type == "real":
                    await self._check_real_signal(signal)
                else:
                    await self._check_normal_signal(signal)
            except Exception as exc:
                logger.warning("monitor error %s %s: %s", signal.signal_type, signal.symbol_name, exc)

    async def _check_normal_signal(self, signal: StoredSignal) -> None:
        """Normal signals must never touch Toobit; OKX is the only source."""
        price = await asyncio.to_thread(self.okx.get_last_price, signal.okx_symbol)
        mfe, mae = self.storage.update_excursions(signal, price)
        if price >= signal.target_price:
            pnl, _ = net_profit_after_fees(
                signal.entry_price,
                signal.target_price,
                signal.trade_usdt,
                config.SPOT_TAKER_FEE_RATE,
                config.SPOT_TAKER_FEE_RATE,
            )
            msg_id = await self.ui.send_result(signal, "TARGET", signal.target_price, pnl, None, "okx_normal")
            if self.storage.finish_signal(
                signal.id,
                status="TARGET",
                exit_price=signal.target_price,
                approx_pnl=pnl,
                real_pnl=None,
                sell_fee_usdt=0.0,
                result_message_id=msg_id,
                result_source="okx_normal",
                mfe_pct=mfe,
                mae_pct=mae,
            ):
                self.learning.learn_result(signal, "TARGET", signal.target_price, pnl, mfe, mae, "هدف عادی فقط با OKX رسید.")
            return
        await self._maybe_warn(signal, price)

    async def _check_real_signal(self, signal: StoredSignal) -> None:
        """Real signals use Toobit only for real order/result status, with throttling."""
        await self._check_real_result_from_toobit(signal)

        current = self.storage.signal_by_id(signal.id)
        if not current or current.status != "OPEN":
            return

        # Warnings for active real signals use OKX price to avoid extra Toobit calls.
        try:
            price = await asyncio.to_thread(self.okx.get_last_price, signal.okx_symbol)
            self.storage.update_excursions(signal, price)
            await self._maybe_warn(signal, price)
        except Exception as exc:
            logger.warning("real warning price check failed %s: %s", signal.symbol_name, exc)

    async def _check_real_result_from_toobit(self, signal: StoredSignal) -> None:
        if not signal.sell_order_id:
            return

        now = time.monotonic()
        last = self._last_real_toobit_check.get(signal.id, 0.0)
        interval = max(15, int(config.REAL_TOOBIT_MONITOR_SECONDS))
        if now - last < interval:
            return
        self._last_real_toobit_check[signal.id] = now

        filled = await asyncio.to_thread(
            self.toobit.find_filled_order,
            signal.toobit_symbol,
            signal.sell_order_id,
            "SELL",
        )
        if not filled:
            return

        parsed = self.toobit.parse_order_fill(filled, fallback_fee_pct=config.SPOT_MAKER_FEE_RATE * 100)
        exit_price = float(parsed.get("avg_price") or signal.target_price)
        sell_fee = float(parsed.get("fee_usdt") or 0.0)
        sell_value = float(parsed.get("value_usdt") or (signal.quantity * exit_price))
        buy_value = signal.quantity * signal.entry_price
        real_pnl = sell_value - buy_value - signal.buy_fee_usdt - sell_fee
        mfe, mae = self.storage.update_excursions(signal, exit_price)
        msg_id = await self.ui.send_result(signal, "TARGET", exit_price, real_pnl, real_pnl, "toobit_real")
        if self.storage.finish_signal(
            signal.id,
            status="TARGET",
            exit_price=exit_price,
            approx_pnl=real_pnl,
            real_pnl=real_pnl,
            sell_fee_usdt=sell_fee,
            result_message_id=msg_id,
            result_source="toobit_real",
            mfe_pct=mfe,
            mae_pct=mae,
        ):
            self.learning.learn_result(signal, "TARGET", exit_price, real_pnl, mfe, mae, "فروش Limit واقعی از Toobit پر شد.")

    async def _maybe_warn(self, signal: StoredSignal, price: float) -> None:
        created = datetime.fromisoformat(signal.created_at)
        age_seconds = (now_utc() - created).total_seconds()
        last_warn_ok = True
        if signal.last_warning_at:
            try:
                last = datetime.fromisoformat(signal.last_warning_at)
                last_warn_ok = (now_utc() - last).total_seconds() >= config.WARNING_COOLDOWN_SECONDS
            except Exception:
                last_warn_ok = True
        if not last_warn_ok:
            return

        distance_to_target = (signal.target_price - price) / price if price > 0 else 0.0
        original_target_distance = (signal.target_price / signal.entry_price - 1.0) if signal.entry_price > 0 else 0.0
        reasons: list[str] = []

        if age_seconds > config.MAX_SIGNAL_HOURS_BEFORE_WARNING * 3600:
            reasons.append(f"مدت باز بودن زیاد شده: {duration_text(age_seconds)}")
        if distance_to_target > original_target_distance and age_seconds > 3600:
            reasons.append("قیمت از مسیر تارگت دور شده است.")
        if signal.mae_pct > max(0.012, signal.mfe_pct * 1.8):
            reasons.append("حرکت منفی بعد ورود بیشتر از حرکت مثبت بوده است.")

        if not reasons:
            return
        reason = " | ".join(reasons)
        self.learning.learn_warning(signal, reason, price)
        await self.ui.send_warning(signal, price, reason)
