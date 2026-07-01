"""اجرای اصلی ربات اسکالپ کلاسیک ۵ دقیقه‌ای.

تحلیل از OKX گرفته می‌شود و اجرای واقعی، در صورت روشن بودن ترید، روی Toobit انجام می‌شود.
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from typing import Any

import config
from indicators import calculate_indicators
from messages_fa import normal_result_message, real_result_message, signal_message
from okx_client import OKXClient
from rolling_pattern_optimizer import RollingPatternOptimizer
from support_resistance_filter import SupportResistanceFilter
from stats_manager import StatsManager
from storage import JSONStorage
from strategy import ClassicScalpingStrategy
from telegram_bot import TelegramBotService
from toobit_client import ToobitClient
from trade_manager import TradeManager
from utils import logger, safe_sleep


class SingleInstanceLock:
    """جلوگیری از اجرای همزمان چند ربات روی یک VPS.

    اجرای همزمان چند main.py باعث 409 تلگرام، ارسال چند سیگنال از یک ارز، و خراب‌شدن مانیتور نتیجه می‌شود.
    """

    def __init__(self, path):
        self.path = path
        self.file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "w", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.file.seek(0)
            self.file.truncate()
            self.file.write(str(os.getpid()))
            self.file.flush()
            return True
        except BlockingIOError:
            return False
        except Exception as exc:
            logger.warning("قفل تک‌اجرایی فعال نشد، ادامه با احتیاط: %s", exc)
            return True

    def release(self) -> None:
        if not self.file:
            return
        try:
            import fcntl

            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self.file.close()
        except Exception:
            pass


class MarketTrendFilter:
    """فیلتر جهت کلی بازار بر اساس 1D و 4H و 1H + تایید BTC و ETH.

    خروجی فقط یکی از این سه حالت است:
    BUY   = بازار صعودی؛ فقط لانگ مجاز
    SELL  = بازار نزولی؛ فقط شورت مجاز
    RANGE = بازار رنج؛ هیچ سیگنال جدیدی صادر نشود
    """

    def __init__(self, okx: OKXClient, storage: JSONStorage | None = None) -> None:
        self.okx = okx
        self.storage = storage
        self.last_update_ts = 0.0
        self.cache: dict[str, Any] = {
            "direction": "RANGE",
            "summary": "بازار هنوز جهت تاییدشده ندارد؛ حالت رنج",
            "details": {},
        }

    @staticmethod
    def _classify(ind: dict[str, Any]) -> str:
        """تشخیص جهت برای 1H/4H.

        این قسمت عمداً نرم‌تر از بازه ورود 5M است.
        جهت بازار نباید آن‌قدر سخت باشد که ربات همیشه RANGE بماند.
        شرط اصلی جهت: موقعیت قیمت نسبت به EMA50 + RSI.
        """
        close = float(ind.get("close") or 0)
        ema_trend = float(ind.get("ema_trend") or ind.get("ema_slow") or 0)
        rsi = float(ind.get("rsi") or 50)

        if close <= 0 or ema_trend <= 0:
            return "RANGE"
        if close > ema_trend and rsi >= config.TREND_RSI_BUY_MIN:
            return "BUY"
        if close < ema_trend and rsi <= config.TREND_RSI_SELL_MAX:
            return "SELL"
        return "RANGE"

    @staticmethod
    def _majority_direction(counts: dict[str, int], total: int) -> str:
        """جهت اکثریت بازار.

        RANGEهای تک‌ارزها نباید باعث خفه شدن کامل ربات شوند؛
        اگر بین ارزهای جهت‌دار، خرید/فروش برتری واضح داشت، جهت بازار همان است.
        """
        if total < config.MARKET_TREND_MIN_SYMBOLS:
            return "RANGE"
        buy = counts.get("BUY", 0)
        sell = counts.get("SELL", 0)
        decisive = buy + sell
        if decisive <= 0:
            return "RANGE"
        min_ratio = config.MARKET_TREND_MIN_AGREEMENT
        if buy > sell and buy / max(1, decisive) >= min_ratio:
            return "BUY"
        if sell > buy and sell / max(1, decisive) >= min_ratio:
            return "SELL"
        return "RANGE"

    def _okx_symbol_for(self, internal: str, valid_symbols: dict[str, dict[str, Any]]) -> str:
        mapped = valid_symbols.get(internal) or {}
        return str(mapped.get("okx_symbol") or config.SYMBOL_MAP[internal]["okx"])

    def _calculate(self, valid_symbols: dict[str, dict[str, Any]]) -> dict[str, Any]:
        details: dict[str, Any] = {"timeframes": {}, "anchors": {}}
        timeframe_directions: dict[str, str] = {}

        for bar in config.MARKET_TREND_TIMEFRAMES:
            counts = {"BUY": 0, "SELL": 0, "RANGE": 0}
            analyzed = 0
            active = list(valid_symbols.keys()) or list(config.WATCHLIST)
            for internal in active:
                try:
                    okx_symbol = self._okx_symbol_for(internal, valid_symbols)
                    candles = self.okx.get_candles(okx_symbol, bar=bar, limit=config.MARKET_TREND_CANDLE_LIMIT)
                    ind = calculate_indicators(candles)
                    direction = self._classify(ind)
                    counts[direction] = counts.get(direction, 0) + 1
                    analyzed += 1
                    if internal in config.MARKET_TREND_ANCHORS:
                        details["anchors"].setdefault(internal, {})[bar] = direction
                except Exception as exc:
                    logger.warning("فیلتر بازار %s %s ناموفق بود: %s", internal, bar, exc)

            tf_direction = self._majority_direction(counts, analyzed)
            timeframe_directions[bar] = tf_direction
            details["timeframes"][bar] = {"direction": tf_direction, "counts": counts, "analyzed": analyzed}

        directions = set(timeframe_directions.values())
        if len(directions) != 1:
            return {
                "direction": "RANGE",
                "summary": "بازار رنج است؛ جهت 1D و 4H و 1H باهم موافق نیستند",
                "details": details,
            }

        market_direction = next(iter(directions)) if directions else "RANGE"
        if market_direction not in ("BUY", "SELL"):
            return {
                "direction": "RANGE",
                "summary": "بازار رنج است؛ اکثریت 1D و 4H و 1H جهت سالم ندادند",
                "details": details,
            }

        for anchor in config.MARKET_TREND_ANCHORS:
            anchor_tfs = details["anchors"].get(anchor, {})
            if any(anchor_tfs.get(bar) != market_direction for bar in config.MARKET_TREND_TIMEFRAMES):
                return {
                    "direction": "RANGE",
                    "summary": f"بازار رنج است؛ {anchor.replace('USDT', '')} با جهت کلی بازار در 1D/4H/1H هم‌جهت نیست",
                    "details": details,
                }

        fa = "صعودی" if market_direction == "BUY" else "نزولی / شورت"
        side_fa = "لانگ" if market_direction == "BUY" else "شورت"
        return {
            "direction": market_direction,
            "summary": f"جهت کلی بازار {fa} است؛ 1D و 4H و 1H هم‌جهت‌اند و BTC/ETH تایید کردند؛ فقط {side_fa} مجاز است",
            "details": details,
        }

    def get(self, valid_symbols: dict[str, dict[str, Any]], *, force: bool = False) -> dict[str, Any]:
        now_ts = time.time()
        if not force and now_ts - self.last_update_ts < config.MARKET_TREND_REFRESH_SECONDS:
            return self.cache
        self.cache = self._calculate(valid_symbols)
        self.cache["updated_ts"] = now_ts
        self.last_update_ts = now_ts
        if self.storage is not None:
            try:
                self.storage.set_market_state(self.cache)
            except Exception as exc:
                logger.warning("ذخیره وضعیت بازار ناموفق بود: %s", exc)
        logger.info("فیلتر بازار به‌روزرسانی شد: %s | %s", self.cache.get("direction"), self.cache.get("summary"))
        return self.cache


class FiveMinuteScalperBot:
    def __init__(self):
        self.storage = JSONStorage()
        self.okx = OKXClient()
        self.toobit = ToobitClient()
        self.stats = StatsManager(self.storage)
        self.strategy = ClassicScalpingStrategy()
        self.rolling_optimizer = RollingPatternOptimizer()
        self.market_filter = MarketTrendFilter(self.okx, self.storage)
        self.sr_filter = SupportResistanceFilter()
        self.trade_manager = TradeManager(self.storage, self.stats, self.toobit)
        self.telegram = TelegramBotService(self.storage, self.trade_manager, self.stats)
        self.stop_event = threading.Event()
        self.valid_symbols: dict[str, dict[str, Any]] = {}
        self.last_signal_ts: dict[str, float] = {}
        self.last_error_ts: dict[str, float] = {}
        self.last_optimizer_check_ts = 0.0
        self.active_indicators: dict[str, dict[str, Any]] = {}

    def validate_symbols(self) -> dict[str, dict[str, Any]]:
        logger.info("شروع اعتبارسنجی نمادها بین OKX و Toobit")
        okx_instruments = None
        toobit_symbols = None

        try:
            okx_instruments = self.okx.get_instruments("SWAP")
            logger.info("تعداد نمادهای OKX دریافت شد: %s", len(okx_instruments))
        except Exception as exc:
            logger.warning("اعتبارسنجی OKX ناموفق بود؛ در زمان دریافت کندل دوباره بررسی می‌شود: %s", exc)

        try:
            toobit_symbols = self.toobit.get_exchange_symbols()
            logger.info("تعداد نمادهای Toobit دریافت شد: %s", len(toobit_symbols))
        except Exception as exc:
            logger.warning("اعتبارسنجی Toobit ناموفق بود؛ در زمان اجرا دوباره بررسی می‌شود: %s", exc)

        valid: dict[str, dict[str, Any]] = {}
        for internal in config.WATCHLIST:
            try:
                okx_symbol = config.SYMBOL_MAP[internal]["okx"]
                toobit_symbol = config.SYMBOL_MAP[internal]["toobit"]
                symbol_info: dict[str, Any] = {}

                if okx_instruments is not None:
                    okx_symbol = self.okx.validate_symbol(internal, okx_instruments)
                if toobit_symbols is not None:
                    toobit_symbol, symbol_info = self.toobit.validate_symbol(internal, toobit_symbols)

                # تست عملی OKX: اگر کندل/قیمت همین نماد خطا بدهد، فقط همان ارز حذف می‌شود.
                self.okx.get_candles(okx_symbol, bar=config.TIMEFRAME, limit=60)

                valid[internal] = {
                    "okx_symbol": okx_symbol,
                    "toobit_symbol": toobit_symbol,
                    "toobit_info": symbol_info,
                }
                logger.info("نماد معتبر شد: %s | OKX=%s | Toobit=%s", internal, okx_symbol, toobit_symbol)
            except Exception as exc:
                logger.warning("نماد %s رد شد و ربات ادامه می‌دهد: %s", internal, exc)

        self.valid_symbols = valid
        self.storage.set_validated_symbols(valid)
        if not valid:
            logger.error("هیچ نماد معتبری پیدا نشد. ربات فعال می‌ماند اما تحلیل انجام نمی‌شود.")
        return valid

    def start(self) -> None:
        logger.info("ربات ورود ۵ دقیقه‌ای با تایید چندتایم‌فریمی شروع شد")
        self.validate_symbols()
        self.telegram.start()
        self.telegram.send_message("✅ ربات اسکالپ کلاسیک ۵ دقیقه‌ای روشن شد.\nتحلیل از OKX و اجرای واقعی از Toobit انجام می‌شود.")
        self._maybe_start_daily_optimizer(force=False)
        self._install_signal_handlers()
        self.analysis_loop()

    def _maybe_start_daily_optimizer(self, *, force: bool = False) -> None:
        """بهینه‌ساز بازه‌ها فقط روزی یک بار و در بک‌گراند اجرا می‌شود؛ حلقه ترید منتظر آن نمی‌ماند."""
        now_ts = time.time()
        if not force and now_ts - self.last_optimizer_check_ts < 5 * 60:
            return
        self.last_optimizer_check_ts = now_ts
        try:
            started = self.rolling_optimizer.start_if_needed(self.valid_symbols, force=force)
            if started:
                logger.info("بهینه‌ساز روزانه بازه‌ها در بک‌گراند شروع شد")
        except Exception as exc:
            logger.warning("شروع بهینه‌ساز روزانه بازه‌ها ناموفق بود: %s", exc)

    def _install_signal_handlers(self) -> None:
        def handler(_sig: int, _frame: Any) -> None:
            logger.info("درخواست توقف دریافت شد")
            self.stop_event.set()
            self.telegram.stop()

        try:
            signal.signal(signal.SIGINT, handler)
            signal.signal(signal.SIGTERM, handler)
        except Exception:
            pass

    def _symbol_in_cooldown(self, symbol: str) -> bool:
        last = self.last_error_ts.get(symbol, 0)
        return time.time() - last < config.SYMBOL_ERROR_COOLDOWN_SECONDS

    def _mark_symbol_error(self, symbol: str, exc: Exception) -> None:
        self.last_error_ts[symbol] = time.time()
        self.storage.set_symbol_error(symbol, str(exc), time.time())
        logger.warning("خطای نماد %s؛ فقط همین نماد رد شد: %s", symbol, exc)

    def _send_result_messages(self, normal_results=None, real_results=None) -> None:
        for signal_data, result, price, pnl in normal_results or []:
            msg_id = signal_data.get("telegram_message_id")
            self.telegram.send_message(normal_result_message(signal_data, result, price, pnl), reply_to_message_id=msg_id)

        for signal_data, result, price, pnl in real_results or []:
            msg_id = signal_data.get("telegram_message_id")
            self.telegram.send_message(real_result_message(signal_data, result, price, pnl), reply_to_message_id=msg_id)

    def _collect_active_prices(self) -> dict[str, float]:
        """مانیتور مستقل نتیجه: برای همه سیگنال‌های باز قیمت تازه OKX بگیر، حتی اگر تحلیل آن نماد رد شود."""
        prices: dict[str, float] = {}
        for internal in self.storage.active_symbols():
            mapped = self.valid_symbols.get(internal)
            if not mapped:
                continue
            try:
                candles = self.okx.get_candles(mapped["okx_symbol"])
                indicators = calculate_indicators(candles)
                self.active_indicators[internal] = indicators
                prices[internal] = float(indicators["close"])
            except Exception as exc:
                logger.warning("مانیتور نتیجه: گرفتن قیمت فعال %s از OKX ناموفق بود: %s", internal, exc)
        return prices

    def _check_symbol_result_now(self, internal: str, latest_price: float) -> None:
        """قبل از تحلیل سیگنال جدید، نتیجه سیگنال باز همان نماد را همان لحظه چک کن."""
        prices = {internal: float(latest_price)}
        normal_results = self.trade_manager.check_normal_results(prices, self.active_indicators)
        real_results = self.trade_manager.check_real_results(prices, self.active_indicators)
        if normal_results or real_results:
            self._send_result_messages(normal_results=normal_results, real_results=real_results)

    def _symbol_mtf_confirmation(self, internal: str, mapped: dict[str, Any], direction: str) -> tuple[bool, str]:
        """خود همان ارز هم باید در 1D/4H/1H هم‌جهت باشد؛ خطای یک تایم‌فریم فقط همان ارز را رد می‌کند."""
        required = tuple(getattr(config, "MARKET_TREND_TIMEFRAMES", ("1D", "4H", "1H")))
        seen: dict[str, str] = {}
        for bar in required:
            try:
                candles = self.okx.get_candles(mapped["okx_symbol"], bar=bar, limit=config.MARKET_TREND_CANDLE_LIMIT)
                ind = calculate_indicators(candles)
                seen[bar] = MarketTrendFilter._classify(ind)
            except Exception as exc:
                return False, f"تایید {bar} برای {internal} ناموفق بود: {exc}"
        bad = {tf: val for tf, val in seen.items() if val != direction}
        if bad:
            parts = " | ".join(f"{tf}={val}" for tf, val in seen.items())
            return False, f"خود ارز در تایم‌های بالا هم‌جهت نیست: {parts}"
        fa = "صعودی" if direction == "BUY" else "نزولی / شورت"
        return True, f"خود {internal} در 1D/4H/1H هم‌جهت {fa} است"

    def _entry_confirmation(self, internal: str, mapped: dict[str, Any], direction: str) -> tuple[bool, str]:
        """15m فقط آماده‌بودن ورود را تایید می‌کند؛ تصمیم اصلی و بازه ورود همچنان 5m است."""
        bar = getattr(config, "ENTRY_CONFIRM_TIMEFRAME", "15m")
        try:
            candles = self.okx.get_candles(mapped["okx_symbol"], bar=bar, limit=getattr(config, "ENTRY_CONFIRM_CANDLE_LIMIT", 120))
            ind = calculate_indicators(candles)
            close = float(ind.get("close") or 0)
            vwap = float(ind.get("vwap") or 0)
            ema_fast = float(ind.get("ema_fast") or 0)
            ema_slow = float(ind.get("ema_slow") or 0)
            classified = MarketTrendFilter._classify(ind)
            if direction == "BUY":
                ok = classified == "BUY" and close > vwap and ema_fast > ema_slow
                return ok, (f"{bar} آماده ورود لانگ است" if ok else f"{bar} ورود لانگ را تایید نکرد")
            ok = classified == "SELL" and close < vwap and ema_fast < ema_slow
            return ok, (f"{bar} آماده ورود شورت است" if ok else f"{bar} ورود شورت را تایید نکرد")
        except Exception as exc:
            return False, f"تایید {bar} برای {internal} ناموفق بود: {exc}"

    def _support_resistance_confirmation(self, internal: str, mapped: dict[str, Any], signal_data: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        if not getattr(config, "SR_FILTER_ENABLED", True):
            return True, "فیلتر حمایت/مقاومت خاموش است", {}
        try:
            candles_1h = self.okx.get_candles(mapped["okx_symbol"], bar="1H", limit=getattr(config, "SR_CANDLE_LIMIT_1H", 180))
            candles_4h = self.okx.get_candles(mapped["okx_symbol"], bar="4H", limit=getattr(config, "SR_CANDLE_LIMIT_4H", 120))
            return self.sr_filter.check_path(
                symbol=internal,
                side=str(signal_data.get("side") or ""),
                entry=float(signal_data.get("entry") or 0),
                tp=float(signal_data.get("tp") or 0),
                candles_1h=candles_1h,
                candles_4h=candles_4h,
            )
        except Exception as exc:
            logger.warning("فیلتر حمایت/مقاومت %s خطا داد؛ فقط همین سیگنال رد شد: %s", internal, exc)
            return False, f"فیلتر حمایت/مقاومت ناموفق بود: {exc}", {}

    def _process_symbol(self, internal: str, mapped: dict[str, Any], market_info: dict[str, Any]) -> float | None:
        if self._symbol_in_cooldown(internal):
            return None
        okx_symbol = mapped["okx_symbol"]
        toobit_symbol = mapped["toobit_symbol"]
        try:
            candles = self.okx.get_candles(okx_symbol)
            indicators = calculate_indicators(candles)
            latest_price = float(indicators["close"])
            self.active_indicators[internal] = indicators

            # اول نتیجه سیگنال باز همین نماد بررسی شود؛ بعد اگر هنوز باز بود، اصلاً سیگنال جدید نساز.
            self._check_symbol_result_now(internal, latest_price)
            if self.storage.has_active_symbol(internal):
                logger.info("رد شد: برای این نماد %s هنوز سیگنال باز وجود دارد", internal)
                return latest_price

            signal_data = self.strategy.evaluate(internal, okx_symbol, toobit_symbol, indicators, market_info)
            if not signal_data:
                return latest_price

            direction = str(market_info.get("direction") or "RANGE").upper()
            ok_mtf, msg_mtf = self._symbol_mtf_confirmation(internal, mapped, direction)
            if not ok_mtf:
                logger.info("سیگنال %s رد شد: %s", internal, msg_mtf)
                return latest_price

            ok_entry, msg_entry = self._entry_confirmation(internal, mapped, direction)
            if not ok_entry:
                logger.info("سیگنال %s رد شد: %s", internal, msg_entry)
                return latest_price

            ok_sr, msg_sr, sr_meta = self._support_resistance_confirmation(internal, mapped, signal_data)
            if not ok_sr:
                logger.info("سیگنال %s رد شد: %s", internal, msg_sr)
                return latest_price

            signal_data.setdefault("reasons", [])
            signal_data["reasons"] = (signal_data.get("reasons") or []) + [msg_mtf, msg_entry, msg_sr]
            signal_data["support_resistance"] = sr_meta

            now_ts = time.time()
            if now_ts - self.last_signal_ts.get(internal, 0) < config.SIGNAL_COOLDOWN_SECONDS:
                return latest_price

            ok, reason = self.trade_manager.can_accept_signal(signal_data)
            if not ok:
                logger.info("سیگنال %s رد شد: %s", internal, reason)
                return latest_price

            # تعیین ریشه‌ای نوع سیگنال قبل از ارسال:
            # اگر ترید روشن، Toobit وصل، و اسلات پوزیشن خالی باشد => رئال Toobit
            # در غیر این صورت => عادی / داخلی
            signal_data = self.trade_manager.decide_execution_mode(signal_data)
            signal_data, register_reason = self.trade_manager.register_signal(signal_data)
            if signal_data is None:
                logger.info("سیگنال %s قبل از ارسال رد شد: %s", internal, register_reason)
                return latest_price

            msg_id = self.telegram.send_message(signal_message(signal_data))
            if msg_id:
                self.storage.update_signal(signal_data["signal_id"], telegram_message_id=msg_id)
                signal_data["telegram_message_id"] = msg_id

            if signal_data.get("execution_mode") == "REAL":
                executed, exec_message, _response = self.trade_manager.try_execute_real(signal_data, mapped.get("toobit_info", {}))
                if not executed:
                    self.telegram.send_message(f"⚠️ اجرای واقعی سیگنال انجام نشد:\n{exec_message}", reply_to_message_id=msg_id)
                else:
                    self.telegram.send_message("✅ سفارش رئال Toobit تایید شد. TP و SL همراه همان سفارش اصلی ثبت شدند.", reply_to_message_id=msg_id)

            self.last_signal_ts[internal] = now_ts
            return latest_price
        except Exception as exc:
            self._mark_symbol_error(internal, exc)
            return None

    def _check_results(self, latest_prices: dict[str, float] | None = None) -> None:
        # نتیجه‌ها نباید وابسته به صدور سیگنال جدید باشند. هر دور برای سیگنال‌های باز قیمت تازه می‌گیریم.
        prices = self._collect_active_prices()
        prices.update(latest_prices or {})
        normal_results = self.trade_manager.check_normal_results(prices, self.active_indicators)
        real_results = self.trade_manager.check_real_results(prices, self.active_indicators)
        self._send_result_messages(normal_results=normal_results, real_results=real_results)

    def analysis_loop(self) -> None:
        while not self.stop_event.is_set():
            if not self.valid_symbols:
                self.validate_symbols()
                safe_sleep(15)
                continue

            self._maybe_start_daily_optimizer(force=False)

            try:
                self._check_results({})
            except Exception as exc:
                logger.warning("مانیتور ابتدای حلقه ناموفق بود، ربات ادامه می‌دهد: %s", exc)

            market_info = self.market_filter.get(self.valid_symbols)
            if str(market_info.get("direction") or "RANGE").upper() == "RANGE":
                logger.info("بازار در حالت رنج است؛ سیگنال جدید صادر نمی‌شود: %s", market_info.get("summary"))
                safe_sleep(config.POLL_INTERVAL_SECONDS)
                continue

            latest_prices: dict[str, float] = {}
            for internal, mapped in list(self.valid_symbols.items()):
                if self.stop_event.is_set():
                    break
                price = self._process_symbol(internal, mapped, market_info)
                if price is not None:
                    latest_prices[internal] = price
                safe_sleep(0.15)

            try:
                self._check_results(latest_prices)
            except Exception as exc:
                logger.warning("بررسی نتیجه‌ها ناموفق بود، ربات ادامه می‌دهد: %s", exc)

            safe_sleep(config.POLL_INTERVAL_SECONDS)


def main() -> None:
    lock = SingleInstanceLock(config.LOCK_FILE)
    if not lock.acquire():
        message = "یک نسخه دیگر از ربات در حال اجراست؛ برای جلوگیری از سیگنال تکراری و خطای 409، این نسخه اجرا نشد."
        print(message, file=sys.stderr)
        logger.error(message)
        return
    try:
        bot = FiveMinuteScalperBot()
        bot.start()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
