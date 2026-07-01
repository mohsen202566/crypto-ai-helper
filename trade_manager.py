"""مدیریت اجرای معامله، اسلات رئال، مانیتور نتیجه و کنترل ریسک اجرایی.

v11: مانیتور نتیجه سخت‌گیرانه‌تر شد:
- سیگنال رئالِ بدون real_order گیر نمی‌کند و بعد از مهلت کوتاه به عادی تبدیل می‌شود.
- مانیتور رئال هر دور وضعیت پوزیشن واقعی، تاریخچه پوزیشن و تاریخچه سفارش را چک می‌کند.
- اگر پوزیشن بسته شده باشد ولی history دیر بدهد، بعد از تایم‌اوت fallback ثبت می‌شود تا نماد قفل نماند.
"""
from __future__ import annotations

import time
from typing import Any

import config
from stats_manager import StatsManager
from storage import JSONStorage
from toobit_client import ToobitClient
from utils import hit_tp_sl, logger, now_utc_iso, safe_float


class TradeManager:
    def __init__(self, storage: JSONStorage, stats: StatsManager, toobit: ToobitClient):
        self.storage = storage
        self.stats = stats
        self.toobit = toobit

    def can_accept_signal(self, signal: dict[str, Any]) -> tuple[bool, str]:
        # قانون قطعی: از هر ارز فقط یک سیگنال تا بسته‌شدن همان سیگنال.
        if self.storage.has_active_symbol(signal["symbol"]):
            return False, "برای این نماد هنوز سیگنال باز وجود دارد"
        return True, ""

    def check_toobit_connection(self) -> tuple[bool, str, dict[str, Any] | None]:
        if not self.toobit.has_credentials:
            return False, "کلید API توبیت تنظیم نشده است یا فایل .env درست خوانده نشده است", None
        try:
            balance = self.toobit.get_usdt_balance_summary()
            return True, "اتصال Toobit برقرار است", balance
        except Exception as exc:
            return False, str(exc), None

    def get_today_pnl_safe(self) -> tuple[float | None, str | None]:
        if not self.toobit.has_credentials:
            return None, "کلید API توبیت تنظیم نشده است"
        try:
            return self.toobit.get_today_pnl(), None
        except Exception as exc:
            return None, str(exc)

    def decide_execution_mode(self, signal: dict[str, Any]) -> dict[str, Any]:
        settings = self.storage.get_settings()
        signal["trade_amount_usdt"] = float(settings.get("trade_amount_usdt", config.DEFAULT_TRADE_AMOUNT_USDT))
        signal["leverage"] = int(settings.get("leverage", config.DEFAULT_LEVERAGE))
        signal["max_positions"] = int(settings.get("max_positions", config.DEFAULT_MAX_POSITIONS))
        signal["margin_type"] = str(settings.get("margin_type", config.DEFAULT_MARGIN_TYPE))

        if not settings.get("trade_enabled"):
            signal["execution_mode"] = "NORMAL"
            signal["execution_mode_fa"] = "عادی / داخلی"
            signal["execution_reason"] = "ترید واقعی خاموش است؛ سیگنال فقط به‌صورت عادی پیگیری می‌شود."
            return signal

        if self.storage.count_open_real() >= int(settings.get("max_positions", 1)):
            signal["execution_mode"] = "NORMAL"
            signal["execution_mode_fa"] = "عادی / داخلی"
            signal["execution_reason"] = "اسلات پوزیشن رئال پر است؛ سیگنال فقط به‌صورت عادی پیگیری می‌شود."
            return signal

        ok, reason, _balance = self.check_toobit_connection()
        if not ok:
            signal["execution_mode"] = "NORMAL"
            signal["execution_mode_fa"] = "عادی / داخلی"
            signal["execution_reason"] = f"اتصال Toobit برقرار نیست؛ سیگنال فقط عادی شد. خطا: {reason}"
            return signal

        signal["execution_mode"] = "REAL"
        signal["execution_mode_fa"] = "رئال Toobit"
        signal["execution_reason"] = "ترید فعال است و اسلات پوزیشن رئال خالی است؛ سیگنال برای اجرای واقعی انتخاب شد."
        return signal

    def attach_signal_defaults(self, signal: dict[str, Any]) -> dict[str, Any]:
        signal["created_utc"] = now_utc_iso()
        signal["created_ms"] = int(time.time() * 1000)
        signal.setdefault("status", "OPEN")
        signal.setdefault("closed_utc", None)
        signal.setdefault("normal_result", None)
        signal.setdefault("real_result", None)
        signal.setdefault("real_order", None)
        signal.setdefault("real_error", None)
        signal.setdefault("telegram_message_id", None)
        signal.setdefault("real_monitor_note", None)
        signal.setdefault("history_missing_since_ms", None)
        return signal

    def register_signal(self, signal: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """ثبت اتمیک سیگنال. اگر نماد هنوز باز باشد، اصلاً ذخیره و ارسال نمی‌شود."""
        signal = self.attach_signal_defaults(signal)
        ok, reason = self.storage.try_save_signal(signal)
        if not ok:
            return None, reason
        self.stats.record_signal(signal.get("execution_mode", "NORMAL"))
        return signal, ""

    def _downgrade_real_to_normal(self, signal: dict[str, Any], reason: str) -> None:
        self.storage.update_signal(
            signal["signal_id"],
            execution_mode="NORMAL",
            execution_mode_fa="عادی / داخلی",
            execution_reason=f"اجرای رئال انجام نشد؛ از اینجا به بعد نتیجه به‌صورت عادی پیگیری می‌شود. علت: {reason}",
            real_error=reason,
            real_order=None,
            real_monitor_note=reason,
            status="OPEN",
        )
        self.stats.convert_real_signal_to_normal()

    def try_execute_real(self, signal: dict[str, Any], symbol_info: dict[str, Any] | None = None) -> tuple[bool, str, Any]:
        if str(signal.get("execution_mode") or "").upper() != "REAL":
            return False, signal.get("execution_reason", "سیگنال عادی است"), None

        settings = self.storage.get_settings()
        if not settings.get("trade_enabled"):
            message = "ترید واقعی خاموش است"
            self._downgrade_real_to_normal(signal, message)
            return False, message, None

        if self.storage.count_open_real() > int(settings.get("max_positions", 1)):
            # علامت > چون همین سیگنال قبل از ارسال سفارش اسلات را رزرو کرده است.
            message = "حداکثر تعداد پوزیشن باز پر شده است"
            self._downgrade_real_to_normal(signal, message)
            return False, message, None

        ok, reason, _balance = self.check_toobit_connection()
        if not ok:
            self.stats.record_real_failed()
            self._downgrade_real_to_normal(signal, reason)
            return False, reason, None

        try:
            response = self.toobit.place_market_order(
                symbol=signal["toobit_symbol"],
                side=signal["side"],
                entry_price=float(signal["entry"]),
                trade_amount_usdt=float(signal.get("trade_amount_usdt", settings.get("trade_amount_usdt", config.DEFAULT_TRADE_AMOUNT_USDT))),
                leverage=int(signal.get("leverage", settings.get("leverage", config.DEFAULT_LEVERAGE))),
                tp_price=float(signal["tp"]),
                sl_price=float(signal["sl"]),
                client_order_id=signal["signal_id"].replace("-", "")[:32],
                symbol_info=symbol_info or {},
            )
            if not isinstance(response, dict) or not response.get("opened"):
                message = (response or {}).get("reason") if isinstance(response, dict) else "پوزیشن بعد از تأیید باز نشد"
                self.stats.record_real_failed()
                self._downgrade_real_to_normal(signal, str(message))
                return False, str(message), response

            self.storage.update_signal(
                signal["signal_id"],
                real_order=response,
                real_error=None,
                real_open_utc=now_utc_iso(),
                real_position_confirmed=True,
                real_monitor_note="پوزیشن واقعی توسط Toobit تایید شد",
                status="OPEN",
            )
            self.stats.record_real_open()
            return True, response.get("reason", "سفارش واقعی در Toobit ارسال و تایید شد"), response
        except Exception as exc:
            message = f"اجرای واقعی ناموفق بود: {exc}"
            logger.exception(message)
            self.stats.record_real_failed()
            self._downgrade_real_to_normal(signal, message)
            return False, message, None

    @staticmethod
    def _movement_percent(signal: dict[str, Any], exit_price: float) -> float:
        entry = safe_float(signal.get("entry"), 0.0)
        if entry <= 0:
            return 0.0
        if str(signal.get("side", "")).upper() == "BUY":
            return (float(exit_price) - entry) / entry * 100.0
        return (entry - float(exit_price)) / entry * 100.0

    def _signal_pnl(self, signal: dict[str, Any], exit_price: float) -> float:
        # سود/ضرر عادی و fallback رئال با دلار و لوریج تنظیم‌شده در پنل حساب می‌شود.
        trade_amount = float(signal.get("trade_amount_usdt") or self.storage.get_settings().get("trade_amount_usdt", config.DEFAULT_TRADE_AMOUNT_USDT))
        leverage = int(signal.get("leverage") or self.storage.get_settings().get("leverage", config.DEFAULT_LEVERAGE))
        movement = self._movement_percent(signal, exit_price)
        notional = trade_amount * leverage
        return notional * movement / 100.0

    def _smart_exit_confirmation_score(self, signal: dict[str, Any], ind: dict[str, Any]) -> tuple[int, list[str]]:
        """امتیاز برگشت مومنتوم برای خروج هوشمند؛ چند تایید همزمان لازم است تا نویز حذف شود."""
        if not ind:
            return 0, []
        side = str(signal.get("side", "")).upper()
        close = safe_float(ind.get("close"), 0.0)
        ema_fast = safe_float(ind.get("ema_fast"), 0.0)
        ema_slow = safe_float(ind.get("ema_slow"), 0.0)
        vwap = safe_float(ind.get("vwap"), 0.0)
        rsi = safe_float(ind.get("rsi"), 50.0)
        score = 0
        reasons: list[str] = []

        if side == "BUY":
            checks = [
                (close > 0 and ema_fast > 0 and close < ema_fast, "قیمت زیر EMA سریع رفت"),
                (ema_fast > 0 and ema_slow > 0 and ema_fast <= ema_slow, "EMA سریع قدرت لانگ را از دست داد"),
                (vwap > 0 and close < vwap, "قیمت VWAP را از دست داد"),
                (rsi < 50, "RSI زیر ناحیه میانی برگشت"),
            ]
        else:
            checks = [
                (close > 0 and ema_fast > 0 and close > ema_fast, "قیمت بالای EMA سریع برگشت"),
                (ema_fast > 0 and ema_slow > 0 and ema_fast >= ema_slow, "EMA سریع قدرت شورت را از دست داد"),
                (vwap > 0 and close > vwap, "قیمت بالای VWAP برگشت"),
                (rsi > 50, "RSI بالای ناحیه میانی برگشت"),
            ]
        for ok, text in checks:
            if ok:
                score += 1
                reasons.append(text)
        return score, reasons

    def _smart_exit_decision(
        self,
        signal: dict[str, Any],
        price: float,
        ind: dict[str, Any] | None = None,
    ) -> tuple[str, float, float, str] | None:
        if not getattr(config, "SMART_EXIT_ENABLED", True):
            return None
        price = float(price)
        movement = self._movement_percent(signal, price)
        score, reasons = self._smart_exit_confirmation_score(signal, ind or {})
        min_confirmations = int(getattr(config, "SMART_EXIT_CONFIRMATIONS", 3))
        if score < min_confirmations:
            return None

        # خروج در سود: فقط وقتی سود حداقلی داریم و چند نشانه برگشت همزمان دیده می‌شود.
        if movement >= float(getattr(config, "SMART_EXIT_MIN_PROFIT_PERCENT", 0.70)):
            reason = "؛ ".join(reasons[:4])
            return "SMART_PROFIT", price, self._signal_pnl(signal, price), f"معامله وارد سود شد اما برگشت مومنتوم تایید شد: {reason}. برای حفظ سود خروج هوشمند انجام شد."

        # خروج دفاعی: اگر بعد از چند دقیقه ورود جواب نداد و هنوز نزدیک ورود/ضرر کم است، قبل از SL کامل خارج شو.
        created_ms = int(signal.get("created_ms") or int(time.time() * 1000))
        age_seconds = (int(time.time() * 1000) - created_ms) / 1000.0
        min_age = float(getattr(config, "SMART_EXIT_DEFENSE_AFTER_SECONDS", 180))
        max_loss = float(getattr(config, "SMART_EXIT_DEFENSE_MAX_LOSS_PERCENT", 0.35))
        max_profit = float(getattr(config, "SMART_EXIT_DEFENSE_MAX_PROFIT_PERCENT", 0.25))
        if age_seconds >= min_age and -max_loss <= movement <= max_profit:
            reason = "؛ ".join(reasons[:4])
            return "SMART_DEFENSE", price, self._signal_pnl(signal, price), f"بعد از ورود، حرکت تاییدی شکل نگرفت و برگشت مومنتوم دیده شد: {reason}. برای جلوگیری از خوردن SL کامل خروج دفاعی انجام شد."

        return None

    def check_normal_results(
        self,
        symbol_prices: dict[str, float],
        symbol_indicators: dict[str, dict[str, Any]] | None = None,
    ) -> list[tuple[dict[str, Any], str, float, float]]:
        symbol_indicators = symbol_indicators or {}
        results: list[tuple[dict[str, Any], str, float, float]] = []
        for signal in self.storage.active_signals():
            price = symbol_prices.get(signal["symbol"])
            if price is None:
                logger.info("مانیتور عادی: برای %s قیمت زنده ندارم؛ نتیجه چک نشد", signal.get("symbol"))
                continue
            result = hit_tp_sl(signal["side"], float(price), float(signal["tp"]), float(signal["sl"]))
            exit_reason = None
            if result:
                # سیگنال عادی با TP/SL ثابت بسته می‌شود؛ خروج دقیقاً همان TP یا SL باشد، نه قیمت دیرتر/اسلیپیج.
                exit_price = float(signal["tp"] if result == "TP" else signal["sl"])
                pnl = self._signal_pnl(signal, exit_price)
                source = "OKX_LIVE_PRICE"
            else:
                smart = self._smart_exit_decision(signal, float(price), symbol_indicators.get(signal["symbol"], {}))
                if not smart:
                    logger.info(
                        "مانیتور عادی %s: price=%s entry=%s tp=%s sl=%s result=OPEN",
                        signal.get("symbol"), price, signal.get("entry"), signal.get("tp"), signal.get("sl"),
                    )
                    continue
                result, exit_price, pnl, exit_reason = smart
                source = "SMART_EXIT_OKX"

            logger.info(
                "مانیتور عادی %s: price=%s entry=%s tp=%s sl=%s result=%s",
                signal.get("symbol"), price, signal.get("entry"), signal.get("tp"), signal.get("sl"), result,
            )
            updates = dict(
                normal_result=result,
                normal_exit_price=exit_price,
                normal_exit_utc=now_utc_iso(),
                normal_pnl=pnl,
                result_source=source,
            )
            if exit_reason:
                updates["normal_exit_reason"] = exit_reason
            self.storage.update_signal(signal["signal_id"], **updates)
            self.stats.record_normal_result(result, pnl=pnl)
            updated = self.storage.get_signal(signal["signal_id"]) or signal
            results.append((updated, result, exit_price, pnl))
        return results

    def _real_result_from_history(self, signal: dict[str, Any]) -> tuple[str, float, float] | None:
        start_ms = int(signal.get("created_ms") or 0)
        history = self.toobit.find_realized_result(
            symbol=signal["toobit_symbol"],
            side=signal["side"],
            start_ms=start_ms,
            end_ms=int(time.time() * 1000),
        )
        if not history:
            return None
        pnl = float(history.get("pnl") or 0.0)
        result = "TP" if pnl >= 0 else "SL"
        close_price = history.get("close_price")
        if close_price is None or float(close_price) <= 0:
            close_price = float(signal["tp"] if result == "TP" else signal["sl"])
        return result, float(close_price), pnl

    def _fallback_real_result(self, signal: dict[str, Any], symbol_prices: dict[str, float]) -> tuple[str, float, float] | None:
        """آخرین محافظ ضد گیرکردن: اگر Toobit پوزیشن را بسته نشان دهد ولی history جواب ندهد."""
        price = symbol_prices.get(signal.get("symbol"))
        if price is None:
            return None
        direct = hit_tp_sl(signal["side"], float(price), float(signal["tp"]), float(signal["sl"]))
        if direct:
            exit_price = float(signal["tp"] if direct == "TP" else signal["sl"])
            return direct, exit_price, self._signal_pnl(signal, exit_price)
        # اگر پوزیشن در Toobit دیگر وجود ندارد ولی قیمت فعلی بین TP/SL است، فقط جهت حرکت را ملاک بگذار.
        movement = self._movement_percent(signal, float(price))
        result = "TP" if movement >= 0 else "SL"
        return result, float(price), self._signal_pnl(signal, float(price))

    def check_real_results(
        self,
        symbol_prices: dict[str, float] | None = None,
        symbol_indicators: dict[str, dict[str, Any]] | None = None,
    ) -> list[tuple[dict[str, Any], str, float, float]]:
        """مانیتور دقیق رئال.

        مسیر اصلی: Toobit positions -> اگر پوزیشن بسته بود -> Toobit history/order history -> ثبت PnL واقعی.
        مسیر ضد گیر: اگر history دیر کرد و پوزیشن دیگر باز نبود، بعد از timeout نتیجه fallback ثبت می‌شود تا نماد قفل نماند.
        """
        symbol_prices = symbol_prices or {}
        symbol_indicators = symbol_indicators or {}
        results: list[tuple[dict[str, Any], str, float, float]] = []
        now_ms = int(time.time() * 1000)

        for signal in self.storage.active_real_signals():
            symbol = signal.get("symbol")
            toobit_symbol = signal.get("toobit_symbol")

            # اگر به هر دلیل ربات بعد از ارسال پیام و قبل از ثبت order خاموش شده باشد، این سیگنال نباید برای همیشه قفل شود.
            if not signal.get("real_order"):
                age = (now_ms - int(signal.get("created_ms") or now_ms)) / 1000.0
                if age >= float(getattr(config, "REAL_ORDER_MISSING_TO_NORMAL_SECONDS", 25)):
                    reason = "سیگنال رئال real_order ندارد؛ احتمالاً ربات قبل/حین اجرای سفارش قطع شده. به عادی تبدیل شد تا مانیتور شود."
                    logger.warning("مانیتور رئال %s: %s", symbol, reason)
                    self._downgrade_real_to_normal(signal, reason)
                else:
                    logger.info("مانیتور رئال %s: هنوز real_order ندارد؛ age=%.1fs", symbol, age)
                continue

            try:
                position = self.toobit.get_open_position(toobit_symbol, signal["side"])
            except Exception as exc:
                logger.warning("مانیتور رئال %s: خواندن پوزیشن Toobit ناموفق بود: %s", symbol, exc)
                continue

            if position is not None:
                price = symbol_prices.get(symbol)
                touch = hit_tp_sl(signal["side"], float(price), float(signal["tp"]), float(signal["sl"])) if price is not None else None
                note = "پوزیشن واقعی هنوز باز است"
                if touch:
                    note = f"قیمت OKX به {touch} رسیده ولی Toobit هنوز پوزیشن را باز نشان می‌دهد؛ منتظر اجرای TP/SL صرافی"

                smart = None
                if price is not None:
                    smart = self._smart_exit_decision(signal, float(price), symbol_indicators.get(symbol, {}))

                if smart:
                    result, exit_price, pnl, exit_reason = smart
                    try:
                        raw_close = self.toobit.flash_close(toobit_symbol, signal["side"])
                        time.sleep(float(getattr(config, "TOOBIT_CLOSE_VERIFY_SECONDS", 2.0)))
                        still_open = self.toobit.get_open_position(toobit_symbol, signal["side"]) is not None
                    except Exception as exc:
                        logger.warning("خروج هوشمند رئال %s ناموفق بود: %s", symbol, exc)
                        self.storage.update_signal(
                            signal["signal_id"],
                            real_last_monitor_utc=now_utc_iso(),
                            real_position_seen=True,
                            real_monitor_note=f"خروج هوشمند فعال شد ولی flashClose خطا داد: {exc}",
                            history_missing_since_ms=None,
                        )
                        continue

                    if still_open:
                        self.storage.update_signal(
                            signal["signal_id"],
                            real_last_monitor_utc=now_utc_iso(),
                            real_position_seen=True,
                            real_monitor_note="خروج هوشمند ارسال شد اما Toobit هنوز پوزیشن را باز نشان می‌دهد",
                            smart_close_raw=raw_close if isinstance(raw_close, dict) else {"response": raw_close},
                            history_missing_since_ms=None,
                        )
                        continue

                    history_result = None
                    try:
                        history_result = self._real_result_from_history(signal)
                    except Exception as exc:
                        logger.warning("خروج هوشمند رئال %s: خواندن history بعد از flashClose ناموفق بود: %s", symbol, exc)
                    if history_result:
                        hist_result, hist_exit_price, hist_pnl = history_result
                        result = result if result.startswith("SMART") else hist_result
                        exit_price = hist_exit_price
                        pnl = hist_pnl
                        source = "SMART_EXIT_TOOBIT_HISTORY"
                    else:
                        source = "SMART_EXIT_FLASH_CLOSE_FALLBACK"

                    self.storage.update_signal(
                        signal["signal_id"],
                        real_result=result,
                        real_exit_price=exit_price,
                        real_exit_utc=now_utc_iso(),
                        real_pnl=pnl,
                        real_result_source=source,
                        real_exit_reason=exit_reason,
                        real_monitor_note=f"خروج هوشمند با flashClose انجام شد؛ نتیجه از {source} ثبت شد",
                        smart_close_raw=raw_close if isinstance(raw_close, dict) else {"response": raw_close},
                    )
                    self.stats.record_real_result(result, pnl=pnl)
                    updated = self.storage.get_signal(signal["signal_id"]) or signal
                    results.append((updated, result, exit_price, pnl))
                    continue

                self.storage.update_signal(
                    signal["signal_id"],
                    real_last_monitor_utc=now_utc_iso(),
                    real_position_seen=True,
                    real_monitor_note=note,
                    history_missing_since_ms=None,
                )
                logger.info(
                    "مانیتور رئال %s: position=OPEN price=%s tp=%s sl=%s note=%s",
                    symbol, price, signal.get("tp"), signal.get("sl"), note,
                )
                continue

            # اینجا پوزیشن دیگر در Toobit باز نیست. پس باید نتیجه ثبت شود.
            try:
                history_result = self._real_result_from_history(signal)
            except Exception as exc:
                logger.warning("مانیتور رئال %s: خواندن history/order history ناموفق بود: %s", symbol, exc)
                history_result = None

            if history_result:
                result, exit_price, pnl = history_result
                source = "TOOBIT_HISTORY"
            else:
                missing_since = int(signal.get("history_missing_since_ms") or now_ms)
                wait_seconds = (now_ms - missing_since) / 1000.0
                self.storage.update_signal(
                    signal["signal_id"],
                    history_missing_since_ms=missing_since,
                    real_last_monitor_utc=now_utc_iso(),
                    real_monitor_note=f"پوزیشن در Toobit باز نیست ولی history هنوز PnL نداده؛ wait={wait_seconds:.0f}s",
                )
                logger.warning(
                    "مانیتور رئال %s: position=CLOSED اما history/PnL نیامده؛ wait=%.0fs",
                    symbol, wait_seconds,
                )
                if wait_seconds < float(getattr(config, "REAL_HISTORY_FALLBACK_SECONDS", 180)):
                    continue
                fallback = self._fallback_real_result(signal, symbol_prices)
                if not fallback:
                    continue
                result, exit_price, pnl = fallback
                source = "FALLBACK_AFTER_TOOBIT_HISTORY_TIMEOUT"
                logger.error(
                    "مانیتور رئال %s: history بعد از timeout نیامد؛ نتیجه fallback ثبت شد. result=%s pnl=%s",
                    symbol, result, pnl,
                )

            self.storage.update_signal(
                signal["signal_id"],
                real_result=result,
                real_exit_price=exit_price,
                real_exit_utc=now_utc_iso(),
                real_pnl=pnl,
                real_result_source=source,
                real_monitor_note=f"نتیجه از {source} ثبت شد",
            )
            self.stats.record_real_result(result, pnl=pnl)
            updated = self.storage.get_signal(signal["signal_id"]) or signal
            results.append((updated, result, exit_price, pnl))
        return results

    def get_balance_safe(self) -> tuple[dict[str, float] | None, str | None]:
        if not self.toobit.has_credentials:
            return None, "کلید API توبیت تنظیم نشده است"
        try:
            return self.toobit.get_usdt_balance_summary(), None
        except Exception as exc:
            return None, str(exc)

    def get_positions_safe(self) -> tuple[list[dict[str, Any]], str | None]:
        if not self.toobit.has_credentials:
            return [], "کلید API توبیت تنظیم نشده است"
        try:
            positions = self.toobit.get_positions()
            positions = [p for p in positions if safe_float(p.get("position") or p.get("positionAmt") or p.get("positionAmount") or p.get("size") or p.get("quantity") or p.get("qty")) != 0]
            return positions, None
        except Exception as exc:
            return [], str(exc)
