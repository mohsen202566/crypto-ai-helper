"""موتور ربات V3 — Extreme Pump Fade (Short-Only).

دو حلقهٔ کاملاً جدا:

  ۱. Watchlist Scanner (هر ۱۵ دقیقه)
     نمادهایی که 24h Change >= آستانه دارند وارد Watchlist می‌شوند.
     این فقط «زیر نظر گرفتن» است، نه معامله.

  ۲. Monitoring (هر ۵ دقیقه)
     فقط اعضای Watchlist بررسی می‌شوند: آیا همین الان شرایط ورود SHORT
     برقرار است؟ ورود intra-candle است و منتظر بسته‌شدن کندل نمی‌ماند.

و مدیریت پوزیشن‌های باز که در هر تیک اجرا می‌شود: حد ضرر سخت (فوری،
بدون تأیید تکنیکال) یا برگشت ساختاری تأییدشده.

اصل روش‌شناختی: این یک آزمون است، نه یک سیستم اثبات‌شده. هدف جمع‌آوری
دادهٔ causal و قابل‌ممیزی است تا بعداً بتوان فهمید آیا این پدیده در زمان
واقعی قابل شکار هست یا نه. هیچ قانونی در طول جمع‌آوری داده تغییر نمی‌کند.
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
from utils import canonical_base, canonical_symbol, logger, now_ms, safe_float, safe_int, timeframe_seconds


class BotEngine:
    def __init__(self, storage: Storage, toobit: ToobitClient):
        self.storage = storage
        self.toobit = toobit
        self._contracts: dict[str, dict[str, Any]] = {}
        self._contracts_ts = 0.0
        self._last_watchlist_scan = 0.0
        self._last_monitor = 0.0
        self._last_peak_pullback = 0.0
        self._last_gain_refresh = 0.0
        self._last_live_report = 0.0
        self._last_summary_day = ""

    # ------------------------------------------------------------------
    #  راه‌اندازی
    # ------------------------------------------------------------------
    def startup(self) -> None:
        self.storage.set_setting("startup_phase", "اتصال به صرافی")
        contracts = self._refresh_contracts(force=True)
        if not contracts:
            raise ToobitError("هیچ قرارداد قابل معامله‌ای پیدا نشد")

        if self.toobit.has_credentials:
            self.refresh_balance(force=True)
            self.storage.set_setting("startup_phase", "آماده")
        else:
            self.storage.set_setting("startup_phase", "بدون کلید API — فقط حالت مجازی")

        self.storage.set_setting("startup_ready", True)
        self.storage.set_setting("tradable_count", len(contracts))
        self.storage.set_health(
            "startup", "ok",
            f"{len(contracts)} قرارداد قابل معامله | اسکن کل بازار برای پامپ",
        )
        logger.info("STARTUP_OK | tradable=%s", len(contracts))

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
    def max_positions(self, capital: float | None = None) -> int:
        """سقف پوزیشن همزمان.

        دو محدودیت هم‌زمان اعمال می‌شود و کوچک‌تر برنده است:
        ۱. عددی که کاربر دستی تعیین کرده (مثلاً «۱۰ تا مجاز است»).
        ۲. ظرفیت واقعی سرمایه — وقتی هر پوزیشن اندازهٔ ثابت دارد
           (``position_size`` > 0)، تعداد پوزیشنی که موجودی فعلی واقعاً
           جواب می‌دهد: ``floor(capital / margin_per_position)``. با رشد
           موجودی (سود) این عدد بالا می‌رود، با افت آن (ضرر) پایین می‌آید —
           بدون این‌که مقدار دستی کاربر تغییر کند.
        در حالت خودکار (``position_size`` صفر) این محدودیت دوم اعمال نمی‌شود،
        چون آنجا اندازهٔ هر اسلات خودش با تعداد اسلات تنظیم می‌شود.
        """
        value = safe_int(
            self.storage.get_setting("max_positions", config.MAX_CONCURRENT_POSITIONS)
        )
        value = max(1, min(value, config.MAX_CONCURRENT_LIMIT))

        fixed_margin = self.position_size()
        if fixed_margin > 0 and capital is not None:
            capacity = int(capital // fixed_margin)
            value = min(value, max(0, capacity))
        return value

    def cooldown_hours(self) -> float:
        """مدت استراحت هر نماد بعد از خروج (ساعت). از پنل: «استراحت ۲»."""
        value = safe_float(self.storage.get_setting("cooldown_hours", config.COOLDOWN_HOURS))
        return max(config.COOLDOWN_HOURS_MIN, min(value, config.COOLDOWN_HOURS_MAX))

    def max_hold_seconds(self) -> float:
        """شبکهٔ ایمنی برای پوزیشن فراموش‌شده؛ صفر یعنی بدون مهلت.

        این بخشی از استراتژی نیست — خروج واقعی فقط با حد ضرر یا برگشت
        ساختاری انجام می‌شود.
        """
        return max(0.0, config.MAX_HOLD_HOURS) * 3600.0

    def position_size(self) -> float:
        """مارجین ثابت هر پوزیشن؛ صفر یعنی تقسیم خودکار سرمایه."""
        return max(0.0, safe_float(self.storage.get_setting("position_size", config.POSITION_SIZE_USDT)))

    def live_report_minutes(self) -> int:
        value = safe_int(self.storage.get_setting("live_report_minutes", config.LIVE_REPORT_MINUTES))
        return max(config.LIVE_REPORT_MIN, min(value, config.LIVE_REPORT_MAX))

    def leverage(self) -> int:
        value = safe_int(self.storage.get_setting("leverage", config.DEFAULT_LEVERAGE))
        return max(config.LEVERAGE_MIN, min(value, config.LEVERAGE_MAX))

    def watchlist_threshold(self) -> float:
        """آستانهٔ رشد ۲۴ساعته برای ورود به واچ‌لیست (٪). از پنل: «واچ ۱۵».

        فقط این عدد آستانه قابل‌تنظیمه؛ خود الگوریتم ورود دست‌نخورده است.
        """
        value = safe_float(
            self.storage.get_setting("watchlist_threshold", config.WATCHLIST_MIN_GAIN_PCT)
        )
        return max(config.WATCHLIST_THRESHOLD_MIN, min(value, config.WATCHLIST_THRESHOLD_MAX))

    def reserve_threshold(self) -> float:
        """آستانهٔ رزرو اسلات برای شکار پامپ‌های قوی. از پنل: «رزرو ۵۰»."""
        value = safe_float(
            self.storage.get_setting("reserve_threshold", config.RESERVE_THRESHOLD_PCT)
        )
        return max(config.RESERVE_THRESHOLD_MIN, min(value, config.RESERVE_THRESHOLD_MAX))

    def top_n_count(self) -> int:
        """فقط N تای برتر واچ‌لیست (بیشترین پامپ ۲۴ساعته) معامله می‌شوند؛
        ۰ یعنی خاموش (بدون محدودیت). از پنل: «تاپ ۳» یا «تاپ ۰»."""
        value = safe_int(self.storage.get_setting("top_n_count", config.TOP_N_COUNT))
        return max(config.TOP_N_MIN, min(value, config.TOP_N_MAX))

    def pullback_entry_pct(self) -> float:
        """درصد برگشت از سقف که ورود Peak-Pullback رو تریگر می‌کنه. از پنل: «برگشت N»."""
        value = safe_float(
            self.storage.get_setting("pullback_entry_pct", config.PULLBACK_ENTRY_PCT)
        )
        return max(config.PULLBACK_ENTRY_MIN, min(value, config.PULLBACK_ENTRY_MAX))

    def staleness_minutes(self) -> float:
        """حداقل زمان (دقیقه) از آخرین رکورد قیمتی سقف تا مجاز شدن ورود.
        از پنل: «کهنگی N» (۱ تا ۵۰۰ دقیقه، پیش‌فرض ۳۰)."""
        value = safe_float(
            self.storage.get_setting("staleness_minutes", config.STALENESS_MINUTES_DEFAULT)
        )
        return max(config.STALENESS_MINUTES_MIN, min(value, config.STALENESS_MINUTES_MAX))

    def trail_usd(self) -> float:
        """بعد از لمس کف «تیپی»، اگه سود از بالاترین نقطه‌ی لمس‌شده به این
        مقدار (دلار) برگرده، می‌بندیم. از پنل: «تریل N» (۱ تا ۱۰۰، پیش‌فرض ۱)."""
        value = safe_float(
            self.storage.get_setting("trail_usd", config.TRAIL_USD_DEFAULT)
        )
        return max(config.TRAIL_USD_MIN, min(value, config.TRAIL_USD_MAX))

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
        """هر تیک: پوزیشن‌های باز، اسکن Watchlist، ورود Peak-Pullback، گزارش‌ها.

        سه فرکانس متفاوت و عمدی:
          * Watchlist هر ۱۵ دقیقه — کشف کاندید کافی است همین‌قدر کند باشد.
          * Peak-Pullback هر چند ثانیه — چون کل هدفش نبستن به کندل یا
            تأیید چندلحظه‌ای است؛ صبر کردن یعنی از دست دادن دقیقاً همون
            برگشتی که دنبالشیم.
        """
        self.manage_open_positions()

        if (time.monotonic() - self._last_watchlist_scan) >= config.WATCHLIST_SCAN_SECONDS:
            self._last_watchlist_scan = time.monotonic()
            self.scan_watchlist()

        if (time.monotonic() - self._last_peak_pullback) >= config.PEAK_PULLBACK_CHECK_SECONDS:
            self._last_peak_pullback = time.monotonic()
            self.monitor_peak_pullback()

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
        """پایش پوزیشن‌های باز -- فقط دو راه خروج، هیچ‌کدام دیگر به کندل یا
        زمان نیاز ندارد؛ پوزیشن تا برخورد به یکی از این دو باز می‌ماند،
        حتی اگر چند روز طول بکشد:

        ۱. حد ضرر (سخت یا دلاری دستی) -- محافظت مطلق حساب، بدون تأیید.
        ۲. تیپی+تریل شناور: به محض رسیدن سود به آستانه‌ی «تیپی N» (دلار)،
           همون سطح قفل و محافظت می‌شه (هیچ‌وقت پایین‌تر از اون نمی‌بندیم)؛
           بعدش سود می‌تونه هرجا بره، فقط وقتی از بالاترین سودِ لمس‌شده به
           اندازه‌ی «تریل N» (دلار) برگرده، می‌بندیم. قبل از لمس تیپی، این
           لایه اصلاً کاری نمی‌کنه -- فقط حد ضرر فعاله.

        برگشت ساختاری و مهلت ایمنی زمانی (MAX_HOLD) دیگر بخشی از خروج
        نیستند -- طبق تصمیم صریح: پوزیشن تا استاپ یا تیپی نخورده باز می‌ماند.
        """
        if bool(self.storage.get_setting("close_all_execute", False)):
            self.storage.set_setting("close_all_execute", False)
            self._close_all_open_positions()
            return

        cycles = self.storage.open_cycles()
        if not cycles:
            return
        try:
            prices = self.toobit.get_all_prices()
        except Exception as exc:
            logger.warning("PRICE_FETCH_FAIL | %s", exc)
            return

        trail_usd = self.trail_usd()

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

            entry_price = safe_float(cycle.get("avg_entry_price"))
            quantity = safe_float(cycle.get("total_quantity"))
            notional = safe_float(cycle.get("total_notional"))
            hard_stop = safe_float(cycle.get("hard_stop_price"))

            # بهترین قیمت (کمترین برای شورت) -- هم برای گزارش MFE، هم برای
            # تعیین اینکه کف «تیپی» لمس شده یا نه و تریل چقدر تنگ شده.
            best_price = safe_float(cycle.get("best_price"))
            new_best = min(best_price, price) if best_price > 0 else price

            # حد ضرر سخت (یا دلاری دستی -- هر دو تو hard_stop_price ذخیره
            # می‌شن) را بدون نیاز به کندل می‌سنجیم (سریع‌ترین و مطمئن‌ترین مسیر).
            if hard_stop > 0 and price >= hard_stop:
                gross = risk_engine.unrealized_pnl(
                    side="SHORT", avg_entry=entry_price,
                    quantity=quantity, current_price=price,
                )
                self.close_position(
                    cycle, exit_price=price, exit_reason="HARD_STOP", gross_pnl=gross,
                )
                continue

            # تیپی+تریل شناور -- فقط وقتی «تیپی» روشن باشه (take_profit_price>0).
            floor_price = safe_float(cycle.get("take_profit_price"))
            dynamic_stop = 0.0
            if floor_price > 0 and notional > 0 and new_best <= floor_price:
                gross_best = risk_engine.unrealized_pnl(
                    side="SHORT", avg_entry=entry_price,
                    quantity=quantity, current_price=new_best,
                )
                net_best_usd = risk_engine.net_pnl_after_costs(gross_best, notional)
                gross_floor = risk_engine.unrealized_pnl(
                    side="SHORT", avg_entry=entry_price,
                    quantity=quantity, current_price=floor_price,
                )
                floor_usd = risk_engine.net_pnl_after_costs(gross_floor, notional)
                trail_target_usd = net_best_usd - trail_usd
                dynamic_stop = floor_price
                if trail_target_usd > floor_usd:
                    trail_price = risk_engine.dollar_target_price(
                        entry_price=entry_price, notional_usdt=notional,
                        target_usd=trail_target_usd, favorable=True,
                    )
                    if trail_price > 0:
                        dynamic_stop = trail_price

                if price >= dynamic_stop:
                    reason = "TRAIL_FLOAT" if dynamic_stop < floor_price else "TAKE_PROFIT"
                    gross = risk_engine.unrealized_pnl(
                        side="SHORT", avg_entry=entry_price,
                        quantity=quantity, current_price=price,
                    )
                    self.close_position(
                        cycle, exit_price=price, exit_reason=reason, gross_pnl=gross,
                    )
                    continue

            self.storage.update_trailing(
                safe_int(cycle.get("id")),
                best_price=new_best, active_stop=hard_stop,
            )

    # --- اسکن و ورود ------------------------------------------------------
    def scan_watchlist(self) -> None:
        """اسکن هر ۱۵ دقیقه: چه نمادهایی شرط کاندید را دارند؟

        تنها شرط: 24h Change >= WATCHLIST_MIN_GAIN_PCT.

        ورود به Watchlist هیچ ربطی به ورود به معامله ندارد؛ فقط یعنی این
        نماد از حالا با فرکانس بالاتر (هر ۵ دقیقه) زیر نظر گرفته می‌شود.

        نمادی که زیر آستانه برمی‌گردد فوراً حذف نمی‌شود — ممکن است دقیقاً
        همان لحظه وارد فاز برگشت شده باشد که هدف اصلی ماست. حذف فقط بر
        اساس گذر زمان (WATCHLIST_RETENTION_HOURS) انجام می‌شود.
        """
        try:
            tickers = self.toobit.get_24h_tickers()
        except Exception as exc:
            self.storage.set_health("watchlist", "warning", f"دریافت تیکر ناموفق: {exc}")
            logger.warning("WATCHLIST_TICKER_FAIL | %s", exc)
            return

        contracts = self._refresh_contracts()
        blacklist = {b.upper() for b in config.SYMBOL_BLACKLIST}
        threshold = self.watchlist_threshold()
        now = now_ms()
        added = 0
        scanned = 0

        for row in tickers:
            symbol = str(row.get("s") or row.get("symbol") or "")
            if not symbol or symbol not in contracts:
                continue
            if canonical_base(symbol) in blacklist:
                continue

            change = self._ticker_change_pct(row)
            price = safe_float(row.get("c") or row.get("lastPrice") or row.get("close"))
            volume = safe_float(row.get("qv") or row.get("quoteVolume") or 0)
            scanned += 1

            if change < threshold:
                continue
            if volume > 0 and volume < config.MIN_24H_QUOTE_VOLUME:
                # نماد کم‌حجم: اسپرد و لغزش، هر سودی را می‌بلعد.
                continue

            existing = self.storage.watchlist_get(symbol)
            self.storage.watchlist_upsert(
                symbol=symbol, gain_pct=change, price=price, now_ts=now
            )
            if existing is None or not existing.get("active"):
                added += 1
                self.storage.log_event("watchlist_add", {
                    "symbol": symbol, "change_24h": round(change, 2), "price": price,
                })

        # انقضای اعضای قدیمی
        cutoff = now - int(config.WATCHLIST_RETENTION_HOURS * 3600 * 1000)
        expired = self.storage.watchlist_expire(cutoff)

        active = self.storage.watchlist_active()
        self.storage.set_setting("watchlist_size", len(active))
        self.storage.set_health(
            "watchlist", "ok",
            f"{len(active)} نماد زیر نظر | {added} جدید | {expired} منقضی "
            f"(از {scanned} نماد بررسی‌شده، آستانه {threshold:.0f}%)",
        )

    @staticmethod
    def _ticker_change_pct(row: dict[str, Any]) -> float:
        """درصد تغییر ۲۴ ساعته از ردیف تیکر.

        بعضی endpointها درصد می‌دهند و بعضی نسبت خام؛ هر دو پوشش داده
        می‌شوند تا یک نماد به‌اشتباه از قلم نیفتد.
        """
        for key in ("pcp", "priceChangePercent", "changeRate", "rose"):
            if key in row:
                value = safe_float(row.get(key))
                if value == 0:
                    continue
                # نسبت خام (مثلاً 0.18) در برابر درصد (مثلاً 18.0)
                return value * 100.0 if abs(value) <= 1.5 else value
        open_price = safe_float(row.get("o") or row.get("openPrice"))
        last_price = safe_float(row.get("c") or row.get("lastPrice"))
        if open_price > 0 and last_price > 0:
            return (last_price - open_price) / open_price * 100.0
        return 0.0

    # ------------------------------------------------------------------
    #  مانیتور و ورود
    # ------------------------------------------------------------------
    def _refresh_watchlist_gains(self, watch: list[dict[str, Any]]) -> None:
        """تازه‌سازی سریع last_gain_pct/peak برای نمادهای فعلاً در واچ‌لیست --
        بدون اسکن کل بازار، فقط همون نمادهایی که از قبل زیر نظرن."""
        try:
            tickers = self.toobit.get_24h_tickers()
        except Exception as exc:
            logger.debug("GAIN_REFRESH_FAIL | %s", exc)
            return

        by_symbol = {str(row.get("s") or row.get("symbol") or ""): row for row in tickers}
        now = now_ms()
        for row in watch:
            symbol = str(row.get("symbol"))
            ticker = by_symbol.get(symbol)
            if not ticker:
                continue
            change = self._ticker_change_pct(ticker)
            price = safe_float(ticker.get("c") or ticker.get("lastPrice") or ticker.get("close"))
            if price <= 0:
                continue
            self.storage.watchlist_upsert(symbol=symbol, gain_pct=change, price=price, now_ts=now)

    def monitor_peak_pullback(self) -> None:
        """ورود لحظه‌ای Peak-Pullback -- جایگزین کامل منطق قبلی.

        برخلاف monitor_watchlist قدیمی که هر ۵ دقیقه اجرا می‌شد، این تابع
        با تیک سریع (هر چند ثانیه، مستقل از بسته‌شدن هر کندلی) صدا زده
        می‌شود، چون کل هدف Peak-Pullback همینه: صبر نکردن.

        برای ارزان ماندن، هر چرخه فقط یک فراخوانی get_all_prices (وزن ۱)
        برای همهٔ نمادها می‌زند؛ کندل (get_klines) فقط دقیقاً همون لحظه‌ای
        گرفته می‌شود که یک نماد واقعاً تریگر بزنه (برای محاسبهٔ ATR/استاپ).

        درصد پامپ (last_gain_pct) -- که رتبه‌بندی «تاپ» رویش انجام می‌شه --
        جدا و هر GAIN_REFRESH_SECONDS (نه هر ۵ ثانیه، چون گرفتن تیکر کل بازار
        سنگین‌تره) با یه فراخوانی get_24h_tickers تازه می‌شه؛ وگرنه رتبه‌بندی
        می‌تونه تا ۱۵ دقیقه (فاصله‌ی اسکن قدیمی) عقب بیفته و نمادی که دیگه
        تو صدر نیست هنوز رتبه‌ی بالا نشون بده.
        """
        mode = self.mode()
        if mode is None:
            return

        watch = self.storage.watchlist_active()
        if not watch:
            return

        if (time.monotonic() - self._last_gain_refresh) >= config.GAIN_REFRESH_SECONDS:
            self._last_gain_refresh = time.monotonic()
            self._refresh_watchlist_gains(watch)
            watch = self.storage.watchlist_active()

        watch = sorted(watch, key=lambda r: safe_float(r.get("last_gain_pct")), reverse=True)
        focus_n = self.top_n_count()
        if focus_n > 0:
            watch = watch[:focus_n]

        capital = self.effective_capital(real_mode=(mode == "real"))
        if capital < config.MIN_CAPITAL_TO_TRADE_USDT:
            return

        max_positions = self.max_positions(capital=capital)
        open_count = self.storage.open_position_count()
        free_slots = max_positions - open_count
        reserve_th = self.reserve_threshold()
        has_reserved_candidate = any(
            safe_float(r.get("last_gain_pct")) >= reserve_th for r in watch
        )
        if free_slots <= 0 and not has_reserved_candidate:
            return

        try:
            prices = self.toobit.get_all_prices()
        except Exception as exc:
            logger.debug("PEAK_PULLBACK_PRICE_FAIL | %s", exc)
            return

        busy = self.storage.open_symbols()
        pullback_pct = self.pullback_entry_pct()
        staleness_req_min = self.staleness_minutes()
        watch_th = self.watchlist_threshold()
        now = now_ms()
        opened = 0
        checked = 0
        setups = 0
        rejects: dict[str, int] = {}

        for row in watch:
            symbol = str(row.get("symbol"))
            gain_pct = safe_float(row.get("last_gain_pct"))
            is_reserved_tier = gain_pct >= reserve_th

            if free_slots <= 0 and not is_reserved_tier:
                break

            # نماد ممکنه از وقتی وارد واچ‌لیست شده (وقتی بالای آستانه بوده)
            # تا الان افت کرده باشه و زیر آستانه رفته باشه؛ RETENTION هنوز
            # نگهش داشته برای دید، ولی نباید معامله بشه مگه الان هم واقعاً
            # بالای آستانه‌ی واچ باشه.
            if gain_pct < watch_th:
                rejects["below_watch_threshold_now"] = rejects.get("below_watch_threshold_now", 0) + 1
                continue

            if config.ONE_POSITION_PER_SYMBOL and symbol in busy:
                continue

            cooling, _until = self.storage.in_cooldown(symbol)
            if cooling:
                rejects["cooldown"] = rejects.get("cooldown", 0) + 1
                continue

            price = safe_float(prices.get(canonical_symbol(symbol)))
            if price <= 0:
                try:
                    price = safe_float(self.toobit.get_mark_price(symbol))
                except Exception:
                    price = 0.0
            if price <= 0:
                continue

            checked += 1

            # سقف رو با قیمت لحظه‌ای آپدیت کن -- بدون کندل، بدون تأخیر.
            # peak_price_time فقط وقتی جلو می‌ره که رکورد واقعاً جدید باشه.
            peak_price = safe_float(row.get("peak_price"))
            peak_price_time = safe_int(row.get("peak_price_time")) or now
            if price > peak_price:
                peak_price = price
                peak_price_time = now
                self.storage.watchlist_bump_peak(symbol, price, now)

            trigger_price = peak_price * (1 - pullback_pct / 100.0)
            if price > trigger_price:
                rejects["pullback_not_reached"] = rejects.get("pullback_not_reached", 0) + 1
                continue

            # کهنگی سقف: از آخرین رکورد جدید این نماد، حداقل این‌قدر (دقیقه)
            # باید گذشته باشه -- یعنی پامپ واقعاً نفس بریده، نه یه مکث موقت.
            staleness_min = (now - peak_price_time) / 60000.0
            if staleness_min < staleness_req_min:
                rejects["peak_too_fresh"] = rejects.get("peak_too_fresh", 0) + 1
                continue

            try:
                candles = self.toobit.get_klines(
                    symbol, interval=config.ENTRY_TIMEFRAME, limit=config.ENTRY_CANDLE_LIMIT
                )
            except Exception as exc:
                logger.debug("PEAK_PULLBACK_KLINES_FAIL | %s | %s", symbol, exc)
                continue
            if not candles:
                continue

            signal = strategy.evaluate_peak_pullback_entry(
                symbol=symbol, candles=candles, current_price=price,
                peak_price=peak_price, pullback_pct=pullback_pct,
                change_24h=gain_pct, trigger_time_ms=now,
            )
            if not signal.ok:
                code = signal.reject_code or "unknown"
                rejects[code] = rejects.get(code, 0) + 1
                continue

            setups += 1
            self.storage.watchlist_touch_counter(symbol, "setup_count")

            if free_slots <= 0:
                freed = self._preempt_for_reserved(symbol=symbol, candidate_gain_pct=gain_pct)
                if not freed:
                    rejects["no_slot_reserved"] = rejects.get("no_slot_reserved", 0) + 1
                    continue
                free_slots += 1

            cycle_id = self.open_position(
                symbol=symbol, signal=signal, mode=mode, capital=capital,
                max_positions=max_positions,
            )
            if cycle_id:
                opened += 1
                free_slots -= 1
                busy.add(symbol)
                self.storage.watchlist_touch_counter(symbol, "trade_count")

        self.storage.set_setting("last_monitor_report", {
            "ts": now,
            "watchlist": len(watch),
            "checked": checked,
            "setups": setups,
            "opened": opened,
            "free_slots": free_slots,
            "rejects": rejects,
        })
        top_reject = max(rejects.items(), key=lambda kv: kv[1])[0] if rejects else "-"
        self.storage.set_health(
            "monitor", "ok",
            f"{checked} نماد بررسی شد | {setups} ستاپ | {opened} ورود | "
            f"برگشت={pullback_pct:.1f}٪ | تاپ={focus_n or 'خاموش'} | بیشترین دلیل رد: {top_reject}",
        )

    def monitor_watchlist(self) -> None:
        """بررسی هر ۵ دقیقه: آیا شرایط ورود SHORT برقرار است؟

        فقط اعضای Watchlist بررسی می‌شوند. برای هر نماد، عکس لحظه‌ای کامل
        شرایط ذخیره می‌شود — چه ورود انجام شود چه نشود — تا بعداً بتوان
        قیف را تحلیل کرد (از چند کاندید، چند Setup، چند معامله).
        """
        mode = self.mode()
        if mode is None:
            self.storage.set_health(
                "monitor", "ok",
                "ترید واقعی و مجازی هر دو خاموش‌اند — با «ترید مجازی فعال» روشن کنید",
            )
            return

        watch = self.storage.watchlist_active()
        if not watch:
            self.storage.set_health("monitor", "ok", "Watchlist خالی است")
            return

        # اولویت با نمادی‌ست که همین الان بیشتر پمپ کرده (صدر لیست ۲۴ساعته) --
        # وقتی اسلات محدوده، این‌ها زودتر بررسی و در صورت واجد شرایط بودن باز
        # می‌شوند؛ بقیه فقط برای همین دور رد می‌شوند و دور بعد دوباره دیده می‌شوند.
        watch = sorted(watch, key=lambda r: safe_float(r.get("last_gain_pct")), reverse=True)

        capital = self.effective_capital(real_mode=(mode == "real"))
        if capital < config.MIN_CAPITAL_TO_TRADE_USDT:
            self.storage.set_health(
                "monitor", "warning",
                f"سرمایه ({capital:.2f}$) کمتر از حداقل "
                f"({config.MIN_CAPITAL_TO_TRADE_USDT:.2f}$) است",
            )
            return

        max_positions = self.max_positions(capital=capital)
        open_count = self.storage.open_position_count()
        free_slots = max_positions - open_count
        reserve_th = self.reserve_threshold()
        has_reserved_candidate = any(
            safe_float(r.get("last_gain_pct")) >= reserve_th for r in watch
        )
        if free_slots <= 0 and not has_reserved_candidate:
            self.storage.set_health(
                "monitor", "ok",
                f"همهٔ {max_positions} اسلات پر است — منتظر بسته شدن",
            )
            return

        busy = self.storage.open_symbols()
        bar_seconds = float(timeframe_seconds(config.ENTRY_TIMEFRAME))
        now = now_ms()
        checked = 0
        setups = 0
        opened = 0
        rejects: dict[str, int] = {}

        for row in watch:
            symbol = str(row.get("symbol"))
            gain_pct = safe_float(row.get("last_gain_pct"))
            is_reserved_tier = gain_pct >= reserve_th

            if free_slots <= 0 and not is_reserved_tier:
                # چون watch نزولی مرتبه، از اینجا به بعد هیچ نماد رزرو-سطحی
                # نمونده -- امن می‌شه کل بقیهٔ حلقه رو رد کرد.
                break

            if config.ONE_POSITION_PER_SYMBOL and symbol in busy:
                continue

            # استراحت: مطلق است. حتی سیگنال قوی هم نادیده گرفته می‌شود.
            cooling, until = self.storage.in_cooldown(symbol)
            if cooling:
                rejects["cooldown"] = rejects.get("cooldown", 0) + 1
                continue

            try:
                candles = self.toobit.get_klines(
                    symbol, interval=config.ENTRY_TIMEFRAME, limit=config.ENTRY_CANDLE_LIMIT
                )
                price = safe_float(self.toobit.get_mark_price(symbol))
            except Exception as exc:
                logger.debug("MONITOR_SKIP | %s | %s", symbol, exc)
                continue
            if not candles or price <= 0:
                continue

            checked += 1

            # کندل جاری (ناقص) — برای ارزیابی intra-candle لازم است.
            live = candles[-1] if candles else {}
            live_open_ms = safe_int(live.get("ts"))  # کلید صحیح از get_klines: "ts" نه "time"
            elapsed = max(0.0, (now - live_open_ms) / 1000.0) if live_open_ms else 0.0
            live_high = safe_float(live.get("high"))
            live_low = safe_float(live.get("low"))
            live_volume = safe_float(live.get("volume"))
            volume_ok = live_volume > 0

            signal = strategy.evaluate_entry(
                symbol=symbol,
                candles=candles,
                current_price=price,
                change_24h=safe_float(row.get("last_gain_pct")),
                current_high=live_high,
                current_low=live_low,
                current_volume=live_volume,
                elapsed_seconds=elapsed,
                volume_data_available=volume_ok,
                bar_seconds=bar_seconds,
                trigger_time_ms=now,
            )

            if not signal.ok:
                code = signal.reject_code or "unknown"
                rejects[code] = rejects.get(code, 0) + 1
                self.storage.watchlist_set_reject(symbol, code)
                # فقط ردهای «نزدیک به ورود» ذخیره می‌شوند تا دیتابیس پر
                # از ردهای بی‌اهمیت (مثل نبود کندل) نشود.
                if code in {"no_structure_break", "no_deceleration",
                            "cascade_no_volume_expansion", "cascade_no_range_expansion",
                            "spread_too_wide", "stop_unavailable"}:
                    self.storage.save_snapshot(
                        symbol=symbol, trigger_ts=now, accepted=False,
                        payload=signal.snapshot.as_dict(), reject_code=code,
                    )
                continue

            setups += 1
            self.storage.watchlist_touch_counter(symbol, "setup_count")

            if free_slots <= 0:
                # فقط نمادهای رزرو-سطح به اینجا با اسلات خالی صفر می‌رسند.
                freed = self._preempt_for_reserved(
                    symbol=symbol, candidate_gain_pct=gain_pct,
                )
                if not freed:
                    rejects["no_slot_reserved"] = rejects.get("no_slot_reserved", 0) + 1
                    continue
                free_slots += 1

            cycle_id = self.open_position(
                symbol=symbol, signal=signal, mode=mode, capital=capital,
                max_positions=max_positions,
            )
            if cycle_id:
                opened += 1
                free_slots -= 1
                busy.add(symbol)
                self.storage.watchlist_touch_counter(symbol, "trade_count")

        self.storage.set_setting("last_monitor_report", {
            "ts": now,
            "watchlist": len(watch),
            "checked": checked,
            "setups": setups,
            "opened": opened,
            "free_slots": free_slots,
            "rejects": rejects,
        })
        top_reject = max(rejects.items(), key=lambda kv: kv[1])[0] if rejects else "-"
        self.storage.set_health(
            "monitor", "ok",
            f"{checked} نماد بررسی شد | {setups} ستاپ | {opened} ورود | "
            f"بیشترین دلیل رد: {top_reject}",
        )

    def open_position(
        self,
        *,
        symbol: str,
        signal: strategy.EntrySignal,
        mode: str,
        capital: float,
        max_positions: int = 0,
    ) -> int | None:
        """پوزیشن SHORT را باز می‌کند. خروجی: شناسهٔ چرخه یا None.

        حد ضرر از خود سیگنال می‌آید (سقف تأییدشده + بافر ATR) و همان‌جا
        فریز می‌شود؛ اینجا هیچ استاپی ساخته یا حدس زده نمی‌شود.
        """
        contracts = self._refresh_contracts()
        info = contracts.get(symbol, {})
        try:
            _, _, min_qty, min_notional = self.toobit.get_symbol_rules(info)
        except Exception:
            min_qty, min_notional = 0.0, 0.0

        # قیمت ورود همان قیمت لحظهٔ تریگر است؛ اگر نشد، قیمت تازه گرفته می‌شود.
        price = signal.price
        if price <= 0:
            try:
                price = safe_float(self.toobit.get_mark_price(symbol))
            except Exception:
                price = 0.0
        if price <= 0:
            return None

        margin = risk_engine.slot_margin(
            capital_usdt=capital,
            max_positions=max_positions or self.max_positions(),
            open_margin_usdt=self.storage.open_margin_total(),
            fixed_size_usdt=self.position_size(),
        )
        if margin <= 0:
            fixed = self.position_size()
            if fixed > 0:
                free = capital - self.storage.open_margin_total()
                detail = (
                    f"هر پوزیشن {fixed:,.2f}$ تنظیم شده ولی فقط {free:,.2f}$ آزاد است — "
                    "یا «دلار» را کم کنید یا «پوزیشن» را"
                )
            else:
                detail = "سقف درگیری سرمایه پر است"
            self.storage.set_health("risk", "ok", detail)
            return None

        # لوریج دقیقاً همانی است که کاربر تعیین کرده — نه بیشتر، نه کمتر.
        plan = risk_engine.plan_entry(
            symbol=symbol,
            side="SHORT",
            entry_price=price,
            stop_price=signal.stop_price,
            slot_margin_usdt=margin,
            leverage=self.leverage(),
            min_qty=min_qty,
            min_notional=min_notional,
        )
        if not plan.ok:
            # «معامله نکردن» یک خروجی معتبر سیستم است — مثلاً وقتی فاصلهٔ
            # استاپ ساختاری با مارجین موجود، لیکوئید را نزدیک می‌کند.
            self.storage.set_health("risk", "ok", f"{canonical_base(symbol)}: {plan.reason}")
            logger.info("ENTRY_REJECTED | %s | %s", symbol, plan.reason)
            self.storage.save_snapshot(
                symbol=symbol, trigger_ts=signal.snapshot.trigger_time_ms,
                accepted=False, payload=signal.snapshot.as_dict(),
                reject_code="risk_rejected", entry_type=signal.entry_type or "",
            )
            return None

        # اگر تی‌پی/استاپ دلاری دستی (دستورات «تیپی»/«استاپ دلاری» تو تلگرام)
        # روشن باشه، همینجا جایگزین منطق پیش‌فرض میشه -- فقط برای پوزیشن‌های
        # جدید؛ پوزیشن‌های باز فعلی با همون دستور به‌صورت جداگانه آپدیت میشن.
        take_profit_price = plan.take_profit_price
        stop_price = plan.stop_price
        fixed_tp_usd = safe_float(self.storage.get_setting("fixed_tp_usd", config.DEFAULT_FIXED_TP_USD))
        fixed_sl_usd = safe_float(self.storage.get_setting("fixed_sl_usd", 0.0))
        if fixed_tp_usd > 0:
            take_profit_price = risk_engine.dollar_target_price(
                entry_price=price, notional_usdt=plan.notional_usdt,
                target_usd=fixed_tp_usd, favorable=True,
            ) or take_profit_price
        if fixed_sl_usd > 0:
            stop_price = risk_engine.dollar_target_price(
                entry_price=price, notional_usdt=plan.notional_usdt,
                target_usd=fixed_sl_usd, favorable=False,
            ) or stop_price

        cycle_id = self.storage.create_cycle(
            symbol=symbol,
            side=plan.side,
            mode=mode,
            leverage=plan.leverage,
            capital_at_open=capital,
            plan=plan.to_dict(),
            take_profit_price=take_profit_price,
            hard_stop_price=stop_price,
            entry_score=safe_float(signal.snapshot.change_24h),
            entry_reason=f"[{signal.entry_type}] {signal.reason}",
            best_price=price,
        )

        # عکس لحظه‌ای شرایط ورود — ستون فقرات Audit بعدی.
        self.storage.save_snapshot(
            symbol=symbol, trigger_ts=signal.snapshot.trigger_time_ms,
            accepted=True, payload=signal.snapshot.as_dict(),
            cycle_id=cycle_id, entry_type=signal.entry_type or "",
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
                    tp_price=take_profit_price,
                    sl_price=stop_price,
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
                return None

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
            "id": cycle_id, "symbol": symbol,
            "entry_type": signal.entry_type,
            "stop_distance_pct": round(signal.stop_distance_pct, 3),
        })
        logger.info(
            "POSITION_OPEN | %s SHORT type=%s stop=%.4f%% lev=%sx margin=%.2f",
            symbol, signal.entry_type, signal.stop_distance_pct,
            plan.leverage, actual_margin,
        )
        return cycle_id

    # --- بستن پوزیشن ------------------------------------------------------
    def close_position(self, cycle: dict[str, Any], *, exit_price: float,
                       exit_reason: str, gross_pnl: float, detail: str = "") -> None:
        """بستن پوزیشن، ثبت کامل نتیجه، و شروع استراحت نماد.

        استراحت بدون استثنا اعمال می‌شود: تا پایان آن، هیچ معاملهٔ جدیدی
        روی این نماد باز نمی‌شود — حتی اگر دوباره پامپ کند یا سیگنال
        قوی‌تری بدهد. هدف: جلوگیری از چند معامله روی یک Pump Episode.
        """
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
        # --- استراحت نماد ---
        hours = self.cooldown_hours()
        until = self.storage.set_cooldown(symbol, hours=hours, reason=exit_reason)

        # --- MFE/MAE فقط برای تحلیل بعد از معامله ---
        # این اعداد هیچ نقشی در تصمیم ورود یا خروج نداشتند و ندارند؛
        # صرفاً برای فهمیدن اینکه بهترین و بدترین لحظهٔ معامله کجا بود.
        entry_price = safe_float(cycle.get("avg_entry_price"))
        best_price = safe_float(closed.get("best_price")) or entry_price
        mfe_pct = ((entry_price - best_price) / entry_price * 100.0) if entry_price > 0 else 0.0
        hard_stop = safe_float(cycle.get("hard_stop_price"))

        self.storage.log_event("position_closed", {
            "id": cycle_id,
            "symbol": symbol,
            "reason": exit_reason,
            "detail": detail,
            "net": round(net, 4),
            "gross": round(gross_pnl, 4),
            "fees": round(fees, 4),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "hard_stop": hard_stop,
            "mfe_pct": round(mfe_pct, 4),
            "duration_ms": now_ms() - safe_int(cycle.get("opened_at")),
            "cooldown_until": until,
            "cooldown_hours": hours,
        })
        logger.info(
            "POSITION_CLOSED | %s reason=%s net=%.2f mfe=%.2f%% cooldown=%.0fh",
            symbol, exit_reason, net, mfe_pct, hours,
        )

    def _close_all_open_positions(self) -> None:
        """اجرای درخواست «بستن همه» -- همهٔ چرخه‌های باز (واقعی و مجازی) را
        با قیمت لحظه‌ای می‌بندد. از همون close_position استفاده می‌کنه، پس
        برای حالت واقعی هم سفارش بستن واقعاً رو صرافی گذاشته میشه.
        """
        cycles = self.storage.open_cycles()
        if not cycles:
            return
        try:
            prices = self.toobit.get_all_prices()
        except Exception as exc:
            logger.warning("CLOSE_ALL_PRICE_FAIL | %s", exc)
            prices = {}

        closed_count = 0
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
            entry_price = safe_float(cycle.get("avg_entry_price"))
            quantity = safe_float(cycle.get("total_quantity"))
            gross = risk_engine.unrealized_pnl(
                side="SHORT", avg_entry=entry_price,
                quantity=quantity, current_price=price,
            )
            self.close_position(
                cycle, exit_price=price, exit_reason="MANUAL_CLOSE_ALL", gross_pnl=gross,
            )
            closed_count += 1
        logger.info("CLOSE_ALL | %d/%d cycle closed", closed_count, len(cycles))

    def _preempt_for_reserved(self, *, symbol: str, candidate_gain_pct: float) -> bool:
        """وقتی نمادی رزرو-سطح (پامپ >= reserve_threshold) سیگنال داده ولی
        اسلات خالی نیست، ضعیف‌ترین پوزیشن باز (کمترین پامپ ورودی) را --
        فقط اگر به‌قدر کافی قدیمی باشد -- می‌بندد تا جا باز شود.

        خروجی True یعنی جا واقعاً آزاد شد (و می‌شود روی همان اسلات وارد شد).
        """
        cycles = self.storage.open_cycles()
        if not cycles:
            return False

        now = now_ms()
        min_hold_ms = config.RESERVE_MIN_HOLD_MINUTES * 60 * 1000
        eligible = [
            c for c in cycles
            if (now - safe_int(c.get("opened_at"))) >= min_hold_ms
        ]
        if not eligible:
            return False

        weakest = min(eligible, key=lambda c: safe_float(c.get("entry_score")))
        weakest_gain = safe_float(weakest.get("entry_score"))
        if candidate_gain_pct <= weakest_gain:
            return False

        weakest_symbol = str(weakest.get("symbol"))
        try:
            price = safe_float(self.toobit.get_mark_price(weakest_symbol))
        except Exception as exc:
            logger.debug("PREEMPT_PRICE_FAIL | %s | %s", weakest_symbol, exc)
            return False
        if price <= 0:
            return False

        entry_price = safe_float(weakest.get("avg_entry_price"))
        quantity = safe_float(weakest.get("total_quantity"))
        gross = risk_engine.unrealized_pnl(
            side="SHORT", avg_entry=entry_price, quantity=quantity, current_price=price,
        )
        self.close_position(
            weakest, exit_price=price, exit_reason="PREEMPTED_RESERVE", gross_pnl=gross,
        )
        logger.info(
            "PREEMPT | closed %s (entry_gain=%.1f%%) to free slot for %s (gain=%.1f%%)",
            weakest_symbol, weakest_gain, symbol, candidate_gain_pct,
        )
        return True

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
