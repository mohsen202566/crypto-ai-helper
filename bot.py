"""موتور ربات — اتصال استراتژی، هستهٔ ریاضی، صرافی و دیتابیس.

جریان کار:
  1. اگر چرخهٔ بازی نیست  → تحلیل کن، اگر سیگنال معتبر بود چرخه بساز و پلهٔ اول را بزن.
  2. اگر چرخهٔ باز هست   → قیمت را بپا؛ پلهٔ بعدی را در ماشه‌اش بزن،
                            حد سود/ضرر را چک کن، و در آخرین پله هشدار بده.
"""
from __future__ import annotations

import time
from typing import Any

import config
import risk_engine
import strategy
from storage import Storage
from telegram_bot import position_panel, result_panel
from toobit_client import ToobitClient, ToobitError
from utils import json_loads, logger, now_ms, safe_float, safe_int, toobit_contract_symbol


class BotEngine:
    def __init__(self, storage: Storage, toobit: ToobitClient):
        self.storage = storage
        self.toobit = toobit
        self.symbol = config.TARGET_SYMBOL
        self._contract_info: dict[str, Any] = {}
        self._contract_ts = 0.0

    # ------------------------------------------------------------------
    #  راه‌اندازی
    # ------------------------------------------------------------------
    def startup(self) -> None:
        self.storage.set_setting("startup_phase", "اتصال به صرافی")
        contracts = self.toobit.get_contracts()
        key = None
        for candidate in (self.symbol, toobit_contract_symbol(self.symbol)):
            if candidate in contracts:
                key = candidate
                break
        if key is None:
            # جست‌وجوی انعطاف‌پذیر بر پایهٔ نام پایه
            from utils import canonical_base
            base = canonical_base(self.symbol)
            for name in contracts:
                if canonical_base(name) == base:
                    key = name
                    break
        if key is None:
            raise ToobitError(f"نماد {self.symbol} در فهرست قراردادهای صرافی پیدا نشد")
        self._contract_info = contracts[key]
        self._contract_ts = time.monotonic()
        self.symbol = key

        if self.toobit.has_credentials:
            self.refresh_balance(force=True)
            self.storage.set_setting("startup_phase", "آماده")
        else:
            self.storage.set_setting("startup_phase", "بدون کلید API — فقط حالت مجازی")

        self.storage.set_setting("startup_ready", True)
        self.storage.set_health("startup", "ok", f"نماد {self.symbol} آماده است")
        logger.info("STARTUP_OK | symbol=%s", self.symbol)

    def contract_info(self) -> dict[str, Any]:
        if not self._contract_info or (time.monotonic() - self._contract_ts) > config.CONTRACT_REFRESH_SECONDS:
            try:
                contracts = self.toobit.get_contracts()
                if self.symbol in contracts:
                    self._contract_info = contracts[self.symbol]
                    self._contract_ts = time.monotonic()
            except Exception as exc:
                logger.warning("CONTRACT_REFRESH_FAIL | %s", exc)
        return self._contract_info

    # ------------------------------------------------------------------
    #  موجودی
    # ------------------------------------------------------------------
    def refresh_balance(self, force: bool = False) -> float:
        """موجودی زنده را از صرافی می‌گیرد و کش می‌کند."""
        if not force and self.storage.balance_is_fresh():
            balance, _ = self.storage.cached_balance()
            return balance
        if not self.toobit.has_credentials:
            return 0.0
        try:
            summary = self.toobit.get_usdt_balance_summary()
            balance = safe_float(summary.get("equity") or summary.get("balance"))
            self.storage.cache_balance(balance)
            self.storage.set_health("balance", "ok", f"{balance:.2f} USDT")
            return balance
        except Exception as exc:
            self.storage.set_health("balance", "warning", str(exc))
            logger.warning("BALANCE_FAIL | %s", exc)
            balance, _ = self.storage.cached_balance()
            return balance

    def effective_capital(self, *, real_mode: bool) -> float:
        """سرمایهٔ مبنا — همیشه زنده، هرگز عدد ثابت."""
        if real_mode:
            live = self.refresh_balance()
        else:
            live = 0.0
        virtual = safe_float(self.storage.get_setting("virtual_balance", config.VIRTUAL_START_CAPITAL_USDT))
        capital = risk_engine.available_capital(
            live_balance=live, virtual=not real_mode, virtual_balance=virtual
        )
        cap_override = safe_float(self.storage.get_setting("capital_cap", 0.0))
        if cap_override > 0:
            capital = min(capital, cap_override)
        return capital

    # ------------------------------------------------------------------
    #  دادهٔ بازار
    # ------------------------------------------------------------------
    def market_snapshot(self) -> dict[str, Any]:
        price = self.toobit.get_mark_price(self.symbol)
        book: dict[str, float] = {}
        try:
            tickers = self.toobit.get_all_book_tickers()
            book = tickers.get(self.symbol) or {}
        except Exception as exc:
            logger.debug("BOOK_TICKER_SKIP | %s", exc)
        trend_candles = {}
        for tf in config.TREND_TIMEFRAMES:
            trend_candles[tf] = self.toobit.get_klines(self.symbol, interval=tf, limit=120)
        entry_candles = self.toobit.get_klines(
            self.symbol, interval=config.ENTRY_TIMEFRAME, limit=120
        )
        return {
            "price": safe_float(price),
            "best_bid": safe_float(book.get("bid") or book.get("bidPrice")),
            "best_ask": safe_float(book.get("ask") or book.get("askPrice")),
            "trend_candles": trend_candles,
            "entry_candles": entry_candles,
        }

    # ------------------------------------------------------------------
    #  حلقهٔ اصلی
    # ------------------------------------------------------------------
    def tick(self) -> None:
        """یک تکرار کامل: یا چرخهٔ باز را مدیریت کن، یا دنبال ورود جدید بگرد."""
        cycle = self.storage.open_cycle()
        if cycle:
            self.manage_open_cycle(cycle)
        else:
            self.look_for_entry()

    # --- ورود جدید ----------------------------------------------------
    def look_for_entry(self) -> None:
        snapshot = self.market_snapshot()
        signal = strategy.build_signal(
            trend_candles=snapshot["trend_candles"],
            entry_candles=snapshot["entry_candles"],
            price=snapshot["price"],
            best_bid=snapshot["best_bid"],
            best_ask=snapshot["best_ask"],
        )
        if not signal.ok:
            self.storage.set_health("strategy", "ok", f"صبر: {signal.reason}")
            return

        real_mode = bool(self.storage.get_setting("real_trading_enabled", False))
        capital = self.effective_capital(real_mode=real_mode)
        max_steps = safe_int(self.storage.get_setting("max_steps", config.MAX_ENTRY_STEPS))

        info = self.contract_info()
        try:
            _, _, min_qty, min_notional = self.toobit.get_symbol_rules(info)
        except Exception:
            min_qty, min_notional = 0.0, 0.0

        plan = strategy.plan_for_signal(
            signal=signal,
            capital_usdt=capital,
            max_steps=max_steps,
            min_qty=min_qty,
            min_notional=min_notional,
        )
        if not plan.ok:
            self.storage.set_health("risk", "ok", f"ورود رد شد: {plan.reason}")
            logger.info("ENTRY_REJECTED | %s", plan.reason)
            return

        mode = "real" if real_mode else "virtual"
        cycle_id = self.storage.create_cycle(
            symbol=self.symbol,
            side=plan.side,
            mode=mode,
            leverage=plan.leverage,
            planned_steps=plan.step_count,
            capital_at_open=capital,
            plan=plan.to_dict(),
            take_profit_price=plan.take_profit_price,
            hard_stop_price=plan.hard_stop_price,
        )
        self.storage.log_event("cycle_opened", {"id": cycle_id, "reason": signal.reason})
        logger.info(
            "CYCLE_OPEN | id=%s side=%s lev=%s steps=%s capital=%.2f",
            cycle_id, plan.side, plan.leverage, plan.step_count, capital,
        )

        # پلهٔ اول بلافاصله اجرا می‌شود.
        first_step = self.storage.cycle_steps(cycle_id)[0]
        self.execute_step(cycle_id, first_step, snapshot["price"])

        cycle = self.storage.get_cycle(cycle_id) or {}
        text = position_panel(cycle, plan.to_dict())
        text += f"\n\n🧭 دلیل ورود: {signal.reason}"
        self.storage.queue_message(text, cycle_id=cycle_id)

    # --- مدیریت چرخهٔ باز ----------------------------------------------
    def manage_open_cycle(self, cycle: dict[str, Any]) -> None:
        cycle_id = safe_int(cycle.get("id"))
        side = str(cycle.get("side"))
        price = safe_float(self.toobit.get_mark_price(self.symbol))
        if price <= 0:
            return

        steps = self.storage.cycle_steps(cycle_id)
        filled = [s for s in steps if s.get("status") == "filled"]
        planned = [s for s in steps if s.get("status") == "planned"]

        # --- آیا پلهٔ بعدی باید اجرا شود؟ ---
        due = strategy.next_step_due(side=side, steps=steps, current_price=price)
        if due is not None:
            self.execute_step(cycle_id, due, price)
            cycle = self.storage.get_cycle(cycle_id) or cycle
            steps = self.storage.cycle_steps(cycle_id)
            filled = [s for s in steps if s.get("status") == "filled"]
            planned = [s for s in steps if s.get("status") == "planned"]

            self.storage.queue_message(
                f"📉 پلهٔ {safe_int(due.get('step_index'))} در {price:.6f} اجرا شد.\n"
                f"میانگین ورود: {safe_float(cycle.get('avg_entry_price')):.6f}\n"
                f"پله‌های مصرف‌شده: {len(filled)}/{len(steps)}",
                reply_to=cycle.get("tg_message_id"),
                cycle_id=cycle_id,
            )

            # هشدار آخرین پله
            if config.WARN_ON_FINAL_STEP and not planned and not cycle.get("final_step_warned"):
                self.storage.mark_final_step_warned(cycle_id)
                self.storage.queue_message(
                    "⚠️ هشدار: آخرین پله مصرف شد.\n"
                    "پلهٔ جدیدی باز نمی‌شود؛ از این پس فقط حد سود یا حد ضرر سخت عمل می‌کند.\n"
                    f"حد ضرر: {safe_float(cycle.get('hard_stop_price')):.6f}",
                    reply_to=cycle.get("tg_message_id"),
                    cycle_id=cycle_id,
                )

        # --- آیا وقت خروج است؟ ---
        avg_entry = safe_float(cycle.get("avg_entry_price"))
        quantity = safe_float(cycle.get("total_quantity"))
        reason, gross = strategy.exit_decision(
            side=side,
            avg_entry=avg_entry,
            quantity=quantity,
            current_price=price,
            take_profit=safe_float(cycle.get("take_profit_price")),
            hard_stop=safe_float(cycle.get("hard_stop_price")),
            all_steps_filled=not planned,
        )
        if reason:
            self.close_cycle(cycle, exit_price=price, exit_reason=reason, gross_pnl=gross)

    # --- اجرای یک پله --------------------------------------------------
    def execute_step(self, cycle_id: int, step: dict[str, Any], price: float) -> None:
        cycle = self.storage.get_cycle(cycle_id)
        if not cycle:
            return
        side = str(cycle.get("side"))
        margin = safe_float(step.get("margin"))
        leverage = safe_int(cycle.get("leverage"))
        step_index = safe_int(step.get("step_index"))
        quantity = (margin * leverage) / price if price > 0 else 0.0
        order_id = None

        if str(cycle.get("mode")) == "real":
            try:
                result = self.toobit.place_market_order(
                    symbol=self.symbol,
                    side=side,
                    entry_price=price,
                    margin_usdt=margin,
                    leverage=leverage,
                    tp_price=safe_float(cycle.get("take_profit_price")),
                    sl_price=safe_float(cycle.get("hard_stop_price")),
                    client_order_id=f"staged-{cycle_id}-{step_index}-{now_ms()}",
                    symbol_info=self.contract_info(),
                )
                quantity = safe_float(result.get("quantity")) or quantity
                margin = safe_float(result.get("actual_margin_usdt")) or margin
                order_id = result.get("order_id")
            except Exception as exc:
                logger.exception("ORDER_FAIL | step=%s", step_index)
                self.storage.set_health("order", "warning", str(exc))
                self.storage.queue_message(
                    f"❌ اجرای پلهٔ {step_index} ناموفق بود:\n{exc}",
                    reply_to=cycle.get("tg_message_id"),
                    cycle_id=cycle_id,
                )
                return

        self.storage.mark_step_filled(
            cycle_id=cycle_id,
            step_index=step_index,
            fill_price=price,
            quantity=quantity,
            margin=margin,
            order_id=order_id,
        )
        filled = self.storage.filled_steps(cycle_id)
        snapshot = risk_engine.recompute_after_fill(
            side=side,
            filled_steps=[
                {
                    "price": safe_float(s.get("fill_price")),
                    "quantity": safe_float(s.get("quantity")),
                    "margin": safe_float(s.get("margin")),
                }
                for s in filled
            ],
            leverage=leverage,
        )
        self.storage.update_cycle_position(cycle_id, snapshot)
        logger.info(
            "STEP_FILLED | cycle=%s step=%s price=%.6f avg=%.6f liq=%.6f",
            cycle_id, step_index, price,
            snapshot["avg_entry"], snapshot["liquidation_price"],
        )

    # --- بستن چرخه -----------------------------------------------------
    def close_cycle(self, cycle: dict[str, Any], *, exit_price: float,
                    exit_reason: str, gross_pnl: float) -> None:
        cycle_id = safe_int(cycle.get("id"))
        notional = safe_float(cycle.get("total_notional"))
        fees = notional * risk_engine.round_trip_cost_rate()
        net = gross_pnl - fees

        if str(cycle.get("mode")) == "real":
            try:
                self.toobit.flash_close(self.symbol, str(cycle.get("side")))
            except Exception as exc:
                logger.warning("CLOSE_FAIL | %s", exc)
                self.storage.set_health("close", "warning", str(exc))
        else:
            self.storage.adjust_virtual_balance(net)

        self.storage.close_cycle(
            cycle_id,
            exit_price=exit_price,
            exit_reason=exit_reason,
            gross_pnl=gross_pnl,
            net_pnl=net,
            fees=fees,
        )
        closed = self.storage.get_cycle(cycle_id) or {}
        self.storage.queue_message(
            result_panel(closed),
            reply_to=cycle.get("tg_message_id"),
            cycle_id=cycle_id,
        )
        self.storage.log_event("cycle_closed", {"id": cycle_id, "reason": exit_reason, "net": net})
        logger.info("CYCLE_CLOSED | id=%s reason=%s net=%.2f", cycle_id, exit_reason, net)

    # --- همگام‌سازی با صرافی -------------------------------------------
    def monitor_real(self) -> None:
        """چک می‌کند پوزیشن واقعی روی صرافی هنوز باز است یا نه.

        اگر صرافی خودش پوزیشن را بسته باشد (حد سود/ضرر روی خود صرافی)، چرخه
        در دیتابیس هم بسته می‌شود تا آمار از واقعیت جدا نیفتد.
        """
        if not self.toobit.has_credentials:
            return
        cycle = self.storage.open_cycle(mode="real")
        if not cycle:
            return
        try:
            still_open = self.toobit.has_open_position(self.symbol)
        except Exception as exc:
            self.storage.set_health("monitor", "warning", str(exc))
            return
        if still_open:
            self.storage.set_health("monitor", "ok", "پوزیشن واقعی باز است")
            return

        price = safe_float(self.toobit.get_mark_price(self.symbol))
        gross = risk_engine.unrealized_pnl(
            side=str(cycle.get("side")),
            avg_entry=safe_float(cycle.get("avg_entry_price")),
            quantity=safe_float(cycle.get("total_quantity")),
            current_price=price,
        )
        self.close_cycle(cycle, exit_price=price, exit_reason="tp" if gross > 0 else "stop",
                         gross_pnl=gross)
