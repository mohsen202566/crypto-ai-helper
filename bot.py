"""موتور ربات — اسکن چندارزی، امتیازدهی، و مدیریت پوزیشن‌های هم‌زمان.

جریان کار در هر چرخه:
  ۱. پوزیشن‌های باز را بپا (حد سود، حد ضرر، برگشت مومنتوم، عمر پوزیشن).
  ۲. اگر اسلات خالی داری، همهٔ ارزهای فهرست را امتیاز بده.
  ۳. نامزدها را بر اساس امتیاز مرتب کن و از بالا به پایین اسلات‌ها را پر کن.

نکتهٔ مهم: امتیاز بالا یعنی «ساختار فعلی بازار در این جهت است»، نه «قیمت
حتماً بالا می‌رود». برد و باخت هر دو رخ می‌دهد؛ چیزی که سیستم را سرپا نگه
می‌دارد نسبت ریسک به سود است، نه دقت پیش‌بینی.
"""
from __future__ import annotations

import time
from typing import Any

import config
import risk_engine
import strategy
from storage import Storage
from telegram_bot import live_panel, position_panel, result_panel, summary_panel
from toobit_client import ToobitClient, ToobitError
from utils import canonical_base, logger, now_ms, safe_float, safe_int


class BotEngine:
    def __init__(self, storage: Storage, toobit: ToobitClient):
        self.storage = storage
        self.toobit = toobit
        self._contracts: dict[str, dict[str, Any]] = {}
        self._contracts_ts = 0.0
        self._universe: list[str] = []
        self._universe_ts = 0.0
        self._last_scan = 0.0
        self._last_live_report = 0.0
        self._last_summary_day = ""

    # ------------------------------------------------------------------
    #  راه‌اندازی
    # ------------------------------------------------------------------
    def startup(self) -> None:
        self.storage.set_setting("startup_phase", "اتصال به صرافی")
        self._refresh_contracts(force=True)
        universe = self.refresh_universe(force=True)
        if not universe:
            raise ToobitError("هیچ ارز قابل معامله‌ای پیدا نشد")

        if self.toobit.has_credentials:
            self.refresh_balance(force=True)
            self.storage.set_setting("startup_phase", "آماده")
        else:
            self.storage.set_setting("startup_phase", "بدون کلید API — فقط حالت مجازی")

        self.storage.set_setting("startup_ready", True)
        self.storage.set_health("startup", "ok", f"{len(universe)} ارز آمادهٔ اسکن")
        logger.info("STARTUP_OK | symbols=%s", len(universe))

    def _refresh_contracts(self, force: bool = False) -> dict[str, dict[str, Any]]:
        stale = (time.monotonic() - self._contracts_ts) > config.CONTRACT_REFRESH_SECONDS
        if force or not self._contracts or stale:
            try:
                self._contracts = self.toobit.get_contracts()
                self._contracts_ts = time.monotonic()
            except Exception as exc:
                logger.warning("CONTRACT_REFRESH_FAIL | %s", exc)
        return self._contracts

    # ------------------------------------------------------------------
    #  فهرست ارزها
    # ------------------------------------------------------------------
    def refresh_universe(self, force: bool = False) -> list[str]:
        """فهرست ارزهای قابل اسکن؛ یا دستی از تنظیمات، یا پرحجم‌ترین‌های صرافی.

        نقدینگی مهم است: ارز کم‌حجم اسپرد بزرگ دارد و همان اسپرد، سود یک
        معاملهٔ کوچک را می‌بلعد.
        """
        stale = (time.monotonic() - self._universe_ts) > config.SYMBOL_REFRESH_SECONDS
        if self._universe and not force and not stale:
            return self._universe

        contracts = self._refresh_contracts()
        available = list(contracts.keys())
        blacklist = {b.upper() for b in config.SYMBOL_BLACKLIST}

        chosen: list[str] = []
        if config.SYMBOL_LIST:
            wanted = {canonical_base(x) for x in config.SYMBOL_LIST}
            chosen = [s for s in available if canonical_base(s) in wanted]
        else:
            volumes: dict[str, float] = {}
            try:
                for row in self.toobit.get_24h_tickers():
                    sym = str(row.get("s") or row.get("symbol") or "")
                    if not sym:
                        continue
                    vol = safe_float(
                        row.get("qv") or row.get("quoteVolume") or row.get("v") or 0
                    )
                    volumes[sym] = vol
            except Exception as exc:
                logger.warning("TICKER_24H_FAIL | %s", exc)

            ranked = sorted(
                (s for s in available if canonical_base(s) not in blacklist),
                key=lambda s: volumes.get(s, 0.0),
                reverse=True,
            )
            if volumes:
                ranked = [
                    s for s in ranked
                    if volumes.get(s, 0.0) >= config.MIN_24H_QUOTE_VOLUME
                ] or ranked
            chosen = ranked[: config.SCAN_SYMBOL_COUNT]

        chosen = [s for s in chosen if canonical_base(s) not in blacklist]
        if chosen:
            self._universe = chosen
            self._universe_ts = time.monotonic()
            self.storage.set_setting("universe", chosen)
            self.storage.set_health("universe", "ok", f"{len(chosen)} ارز در فهرست اسکن")
        return self._universe

    # ------------------------------------------------------------------
    #  موجودی
    # ------------------------------------------------------------------
    def refresh_balance(self, force: bool = False) -> float:
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
        """سرمایهٔ مبنا — همیشه زنده از صرافی، هرگز عدد ثابت."""
        live = self.refresh_balance()
        virtual = safe_float(
            self.storage.get_setting("virtual_balance", config.VIRTUAL_START_CAPITAL_USDT)
        )
        capital = risk_engine.available_capital(
            live_balance=live, virtual=not real_mode, virtual_balance=virtual
        )
        cap_override = safe_float(self.storage.get_setting("capital_cap", 0.0))
        if cap_override > 0:
            capital = min(capital, cap_override)
        return capital

    # ------------------------------------------------------------------
    #  تنظیمات کاربر
    # ------------------------------------------------------------------
    def max_positions(self) -> int:
        value = safe_int(
            self.storage.get_setting("max_positions", config.MAX_CONCURRENT_POSITIONS)
        )
        return max(1, min(value, config.MAX_CONCURRENT_LIMIT))

    def score_threshold(self) -> float:
        value = safe_float(self.storage.get_setting("score_threshold", config.SCORE_THRESHOLD))
        return max(config.SCORE_THRESHOLD_MIN, min(value, config.SCORE_THRESHOLD_MAX))

    def position_size(self) -> float:
        """مارجین ثابت هر پوزیشن؛ صفر یعنی تقسیم خودکار سرمایه."""
        return max(0.0, safe_float(self.storage.get_setting("position_size", config.POSITION_SIZE_USDT)))

    def live_report_minutes(self) -> int:
        value = safe_int(self.storage.get_setting("live_report_minutes", config.LIVE_REPORT_MINUTES))
        return max(config.LIVE_REPORT_MIN, min(value, config.LIVE_REPORT_MAX))

    def leverage(self) -> int:
        value = safe_int(self.storage.get_setting("leverage", config.DEFAULT_LEVERAGE))
        return max(config.LEVERAGE_MIN, min(value, config.LEVERAGE_MAX))

    def mode(self) -> str | None:
        """حالت فعلی؛ None یعنی هر دو خاموش‌اند و فقط اسکن انجام می‌شود."""
        if bool(self.storage.get_setting("real_trading_enabled", False)):
            return "real"
        if bool(self.storage.get_setting("virtual_trading_enabled", True)):
            return "virtual"
        return None

    # ------------------------------------------------------------------
    #  حلقهٔ اصلی
    # ------------------------------------------------------------------
    def tick(self) -> None:
        """هر تیک: پوزیشن‌های باز، اسکن ارزها، و گزارش‌های دوره‌ای."""
        self.manage_open_positions()
        if (time.monotonic() - self._last_scan) >= config.SCAN_INTERVAL_SECONDS:
            self._last_scan = time.monotonic()
            self.scan_for_entries()
        self.maybe_send_live_report()
        self.maybe_send_daily_summary()

    # --- گزارش‌های دوره‌ای -------------------------------------------------
    def live_report_text(self) -> str:
        """متن گزارش لحظه‌ای با قیمت‌های زنده."""
        cycles = self.storage.open_cycles()
        if not cycles:
            return "هیچ پوزیشن بازی نیست."
        try:
            prices = self.toobit.get_all_prices()
        except Exception as exc:
            logger.warning("LIVE_PRICE_FAIL | %s", exc)
            prices = {}
        for cycle in cycles:
            symbol = str(cycle.get("symbol"))
            if safe_float(prices.get(symbol)) <= 0:
                try:
                    prices[symbol] = safe_float(self.toobit.get_mark_price(symbol))
                except Exception:
                    continue
        return live_panel(cycles, prices)

    def maybe_send_live_report(self) -> None:
        """گزارش خودکار پوزیشن‌های باز — فقط وقتی پوزیشنی هست."""
        minutes = self.live_report_minutes()
        if minutes <= 0:
            return
        if (time.monotonic() - self._last_live_report) < minutes * 60:
            return
        if not self.storage.open_cycles():
            self._last_live_report = time.monotonic()
            return
        self._last_live_report = time.monotonic()
        self.storage.queue_message(self.live_report_text())

    def maybe_send_daily_summary(self) -> None:
        """خلاصهٔ روز قبل، یک بار در ابتدای هر روز."""
        if not config.DAILY_SUMMARY_ENABLED:
            return
        today = time.strftime("%Y-%m-%d")
        stored = str(self.storage.get_setting("last_summary_day", "") or "")
        if not stored:
            self.storage.set_setting("last_summary_day", today)
            return
        if stored == today:
            return
        day_seconds = time.time() - (time.time() % 86400)
        start_ms = int((day_seconds - 86400) * 1000)
        rows = [
            c for c in self.storage.closed_since(start_ms)
            if safe_int(c.get("closed_at")) < int(day_seconds * 1000)
        ]
        self.storage.set_setting("last_summary_day", today)
        if rows:
            self.storage.queue_message(summary_panel(rows, "📅 خلاصهٔ دیروز"))

    # --- مدیریت پوزیشن‌های باز -------------------------------------------
    def manage_open_positions(self) -> None:
        cycles = self.storage.open_cycles()
        if not cycles:
            return
        try:
            prices = self.toobit.get_all_prices()
        except Exception as exc:
            logger.warning("PRICE_FETCH_FAIL | %s", exc)
            return

        for cycle in cycles:
            symbol = str(cycle.get("symbol"))
            price = safe_float(prices.get(symbol))
            if price <= 0:
                try:
                    price = safe_float(self.toobit.get_mark_price(symbol))
                except Exception:
                    continue
            if price <= 0:
                continue

            opened_at = safe_int(cycle.get("opened_at"))
            age_minutes = max(0.0, (now_ms() - opened_at) / 60000.0) if opened_at else 0.0

            reason, gross = strategy.exit_decision(
                side=str(cycle.get("side")),
                entry_price=safe_float(cycle.get("avg_entry_price")),
                quantity=safe_float(cycle.get("total_quantity")),
                current_price=price,
                take_profit=safe_float(cycle.get("take_profit_price")),
                hard_stop=safe_float(cycle.get("hard_stop_price")),
                age_minutes=age_minutes,
            )
            if reason:
                self.close_position(cycle, exit_price=price, exit_reason=reason, gross_pnl=gross)

    # --- اسکن و ورود ------------------------------------------------------
    def scan_for_entries(self) -> None:
        mode = self.mode()
        if mode is None:
            self.storage.set_health(
                "scan", "ok",
                "ترید واقعی و مجازی هر دو خاموش‌اند — با «ترید مجازی فعال» روشن کنید",
            )
            return
        max_positions = self.max_positions()
        open_count = self.storage.open_position_count()
        free_slots = max_positions - open_count
        if free_slots <= 0:
            self.storage.set_health(
                "scan", "ok", f"همهٔ {max_positions} اسلات پر است — منتظر بسته شدن"
            )
            return

        universe = self.refresh_universe()
        if not universe:
            self.storage.set_health("scan", "warning", "فهرست ارزها خالی است")
            return

        capital = self.effective_capital(real_mode=(mode == "real"))
        if capital < config.MIN_CAPITAL_TO_TRADE_USDT:
            self.storage.set_health(
                "scan", "warning",
                f"سرمایه ({capital:.2f}$) کمتر از حداقل "
                f"({config.MIN_CAPITAL_TO_TRADE_USDT:.2f}$) است",
            )
            return

        busy = self.storage.open_symbols()
        threshold = self.score_threshold()
        candidates: list[strategy.SymbolScore] = []
        best_rejected: strategy.SymbolScore | None = None
        scanned = 0

        for symbol in universe:
            if config.ONE_POSITION_PER_SYMBOL and symbol in busy:
                continue
            try:
                entry_candles = self.toobit.get_klines(
                    symbol, interval=config.ENTRY_TIMEFRAME, limit=config.ENTRY_CANDLE_LIMIT
                )
                trend_candles = self.toobit.get_klines(
                    symbol, interval=config.TREND_TIMEFRAME, limit=config.TREND_CANDLE_LIMIT
                )
            except Exception as exc:
                logger.debug("KLINE_SKIP | %s | %s", symbol, exc)
                continue

            scanned += 1
            result = strategy.score_symbol(
                symbol=symbol,
                entry_candles=entry_candles,
                trend_candles=trend_candles,
                threshold=threshold,
            )
            if result.ok:
                candidates.append(result)
            elif best_rejected is None or result.score > best_rejected.score:
                best_rejected = result

        if not candidates:
            detail = (
                f"{scanned} ارز اسکن شد — هیچ‌کدام به آستانهٔ {threshold:.0f} نرسید"
            )
            if best_rejected and best_rejected.symbol:
                detail += (
                    f" | بهترین: {canonical_base(best_rejected.symbol)} "
                    f"{best_rejected.score:.0f}"
                )
            self.storage.set_health("scan", "ok", detail)
            return

        # بالاترین امتیاز اول — اسلات کمیاب است، پس به بهترین سیگنال می‌رسد.
        candidates.sort(key=lambda c: c.score, reverse=True)
        self.storage.set_health(
            "scan", "ok",
            f"{len(candidates)} نامزد از {scanned} ارز | {free_slots} اسلات خالی",
        )

        for candidate in candidates[:free_slots]:
            self.open_position(candidate, mode=mode, capital=capital)

    # --- باز کردن پوزیشن --------------------------------------------------
    def open_position(self, candidate: strategy.SymbolScore, *, mode: str, capital: float) -> None:
        symbol = candidate.symbol
        contracts = self._refresh_contracts()
        info = contracts.get(symbol, {})
        try:
            _, _, min_qty, min_notional = self.toobit.get_symbol_rules(info)
        except Exception:
            min_qty, min_notional = 0.0, 0.0

        try:
            price = safe_float(self.toobit.get_mark_price(symbol)) or candidate.price
        except Exception:
            price = candidate.price
        if price <= 0:
            return

        margin = risk_engine.slot_margin(
            capital_usdt=capital,
            max_positions=self.max_positions(),
            open_margin_usdt=self.storage.open_margin_total(),
            fixed_size_usdt=self.position_size(),
        )
        if margin <= 0:
            self.storage.set_health("risk", "ok", "سقف درگیری سرمایه پر است")
            return

        plan = risk_engine.best_leverage_for_entry(
            symbol=symbol,
            side=candidate.side or "LONG",
            entry_price=price,
            atr_value=candidate.atr_value,
            slot_margin_usdt=margin,
            max_leverage=self.leverage(),
            min_qty=min_qty,
            min_notional=min_notional,
        )
        if not plan.ok:
            self.storage.set_health("risk", "ok", f"{canonical_base(symbol)}: {plan.reason}")
            logger.info("ENTRY_REJECTED | %s | %s", symbol, plan.reason)
            return

        cycle_id = self.storage.create_cycle(
            symbol=symbol,
            side=plan.side,
            mode=mode,
            leverage=plan.leverage,
            capital_at_open=capital,
            plan=plan.to_dict(),
            take_profit_price=plan.take_profit_price,
            hard_stop_price=plan.stop_price,
            entry_score=candidate.score,
            entry_reason=candidate.reason,
        )

        order_id = None
        quantity = plan.quantity
        actual_margin = plan.margin_usdt
        if mode == "real":
            try:
                result = self.toobit.place_market_order(
                    symbol=symbol,
                    side=plan.side,
                    entry_price=price,
                    margin_usdt=plan.margin_usdt,
                    leverage=plan.leverage,
                    tp_price=plan.take_profit_price,
                    sl_price=plan.stop_price,
                    client_order_id=f"scan-{cycle_id}-{now_ms()}",
                    symbol_info=info,
                )
                quantity = safe_float(result.get("quantity")) or quantity
                actual_margin = safe_float(result.get("actual_margin_usdt")) or actual_margin
                order_id = result.get("order_id")
            except Exception as exc:
                logger.exception("ORDER_FAIL | %s", symbol)
                self.storage.set_health("order", "warning", str(exc))
                self.storage.close_cycle(
                    cycle_id, exit_price=price, exit_reason="failed",
                    gross_pnl=0.0, net_pnl=0.0, fees=0.0,
                )
                self.storage.queue_message(
                    f"❌ ارسال سفارش {canonical_base(symbol)} ناموفق بود:\n{exc}"
                )
                return

        self.storage.mark_step_filled(
            cycle_id=cycle_id, step_index=1, fill_price=price,
            quantity=quantity, margin=actual_margin, order_id=order_id,
        )
        snapshot = risk_engine.position_snapshot(
            side=plan.side,
            fills=[{"price": price, "quantity": quantity, "margin": actual_margin}],
            leverage=plan.leverage,
        )
        self.storage.update_cycle_position(cycle_id, snapshot)

        cycle = self.storage.get_cycle(cycle_id) or {}
        self.storage.queue_message(position_panel(cycle, plan.to_dict()), cycle_id=cycle_id)
        self.storage.log_event("position_opened", {
            "id": cycle_id, "symbol": symbol, "score": candidate.score,
        })
        logger.info(
            "POSITION_OPEN | %s %s score=%.0f lev=%sx margin=%.2f",
            symbol, plan.side, candidate.score, plan.leverage, actual_margin,
        )

    # --- بستن پوزیشن ------------------------------------------------------
    def close_position(self, cycle: dict[str, Any], *, exit_price: float,
                       exit_reason: str, gross_pnl: float) -> None:
        cycle_id = safe_int(cycle.get("id"))
        symbol = str(cycle.get("symbol"))
        notional = safe_float(cycle.get("total_notional"))
        fees = notional * risk_engine.round_trip_cost_rate()
        net = gross_pnl - fees

        if str(cycle.get("mode")) == "real":
            try:
                self.toobit.flash_close(symbol, str(cycle.get("side")))
            except Exception as exc:
                logger.warning("CLOSE_FAIL | %s | %s", symbol, exc)
                self.storage.set_health("close", "warning", str(exc))
        else:
            self.storage.adjust_virtual_balance(net)

        self.storage.close_cycle(
            cycle_id, exit_price=exit_price, exit_reason=exit_reason,
            gross_pnl=gross_pnl, net_pnl=net, fees=fees,
        )
        closed = self.storage.get_cycle(cycle_id) or {}
        self.storage.queue_message(
            result_panel(closed), reply_to=cycle.get("tg_message_id"), cycle_id=cycle_id
        )
        self.storage.log_event("position_closed", {
            "id": cycle_id, "symbol": symbol, "reason": exit_reason, "net": net,
        })
        logger.info("POSITION_CLOSED | %s reason=%s net=%.2f", symbol, exit_reason, net)

    # --- همگام‌سازی با صرافی ----------------------------------------------
    def monitor_real(self) -> None:
        """اگر صرافی خودش پوزیشن را بسته باشد، دیتابیس هم به‌روز می‌شود.

        بدون این، آمار ربات از واقعیت حساب جدا می‌افتد.
        """
        if not self.toobit.has_credentials:
            return
        for cycle in self.storage.open_cycles_for_mode("real"):
            symbol = str(cycle.get("symbol"))
            try:
                if self.toobit.has_open_position(symbol):
                    continue
                price = safe_float(self.toobit.get_mark_price(symbol))
            except Exception as exc:
                self.storage.set_health("monitor", "warning", str(exc))
                continue
            if price <= 0:
                continue
            gross = risk_engine.unrealized_pnl(
                side=str(cycle.get("side")),
                avg_entry=safe_float(cycle.get("avg_entry_price")),
                quantity=safe_float(cycle.get("total_quantity")),
                current_price=price,
            )
            self.close_position(
                cycle, exit_price=price,
                exit_reason="tp" if gross > 0 else "stop", gross_pnl=gross,
            )
        self.storage.set_health("monitor", "ok", "همگام با صرافی")
