"""منطق ثابت شکار خستگی پامپ و شروع دامپ.

هیچ یادگیری یا تغییر خودکار آستانه‌ها وجود ندارد. تمام قراردادهای Futures توبیت
پویا کشف می‌شوند و فقط نامزدهای قوی وارد تحلیل عمیق می‌شوند.
"""
from __future__ import annotations

import threading
from collections import defaultdict, deque
from typing import Any

import config
from storage import Storage
from toobit_client import ToobitClient
from utils import atr, canonical_base, canonical_symbol, clamp, ema, median, now_ms, percent_change, rsi, safe_float, safe_int, logger


class PumpStrategy:
    def __init__(self, storage: Storage, toobit: ToobitClient):
        self.storage = storage
        self.toobit = toobit
        self.lock = threading.RLock()
        self.contracts: dict[str, dict[str, Any]] = {}
        self.oi_history: dict[str, deque[tuple[int, float]]] = defaultdict(lambda: deque(maxlen=12))
        self.last_contract_refresh = 0
        # آخرین جزئیات تحلیل هر ارز برای لاگ دقیق دلیل رد.
        self.analysis_diagnostics: dict[str, dict[str, Any]] = {}

    def _set_diagnostic(self, canonical: str, reason: str, stage: str, **fields: Any) -> None:
        payload = {
            "canonical": canonical,
            "reason": reason,
            "stage": stage,
            "updated_at": now_ms(),
            **fields,
        }
        with self.lock:
            self.analysis_diagnostics[canonical] = payload

    def last_diagnostic(self, canonical: str) -> dict[str, Any]:
        with self.lock:
            return dict(self.analysis_diagnostics.get(canonical, {}))

    def refresh_contracts(self, force: bool = False) -> int:
        now = now_ms()
        if not force and now - self.last_contract_refresh < config.CONTRACT_REFRESH_SECONDS * 1000:
            return 0
        remote = self.toobit.get_contracts()
        active: set[str] = set()
        new_count = 0
        with self.lock:
            for exchange_symbol, info in remote.items():
                canonical = canonical_symbol(exchange_symbol)
                active.add(canonical)
                info = dict(info)
                info["canonical"] = canonical
                info["exchange_symbol"] = exchange_symbol
                if self.storage.upsert_contract(canonical, exchange_symbol, info, active=True):
                    new_count += 1
                    self.storage.add_event("NEW_CONTRACT", "قرارداد جدید Futures شناسایی شد", canonical, info)
                self.contracts[canonical] = info
            self.storage.deactivate_missing_contracts(active)
            self.contracts = {k: v for k, v in self.contracts.items() if k in active}
            self.last_contract_refresh = now
        self.storage.set_health("contracts", "ok", f"active={len(active)} new={new_count}")
        return new_count

    @staticmethod
    def _ticker(item: dict[str, Any]) -> dict[str, Any] | None:
        symbol = str(item.get("s") or item.get("symbol") or item.get("symbolId") or "").upper()
        last = safe_float(item.get("c") or item.get("lastPrice") or item.get("p") or item.get("price"))
        if not symbol or last <= 0:
            return None
        open_ = safe_float(item.get("o") or item.get("openPrice"))
        pcp = safe_float(item.get("pcp") or item.get("priceChangePercent"))
        if abs(pcp) <= 1.0 and open_ > 0:
            # بعضی پاسخ‌ها درصد را به صورت نسبت اعشاری می‌فرستند.
            derived = percent_change(last, open_)
            if abs(derived) > abs(pcp):
                pcp = derived
        return {
            "canonical": canonical_symbol(symbol),
            "exchange_symbol": symbol,
            "last": last,
            "open": open_,
            "high": safe_float(item.get("h") or item.get("highPrice")),
            "low": safe_float(item.get("l") or item.get("lowPrice")),
            "base_volume": safe_float(item.get("v") or item.get("volume")),
            "quote_volume": safe_float(item.get("qv") or item.get("quoteVolume")),
            "change_24h": pcp,
            "time": safe_int(item.get("t") or item.get("time")),
        }

    def scan(self, margin_usdt: float, leverage: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        logger.info("SCAN_STAGE | contracts_refresh")
        new_contracts = self.refresh_contracts()
        logger.info("SCAN_STAGE | fetch_24h_tickers")
        raw_tickers = self.toobit.get_24h_tickers()
        tickers = [x for x in (self._ticker(item) for item in raw_tickers) if x]
        self.storage.set_setting("last_scan_ticker_count", len(tickers))
        logger.info("SCAN_STAGE | fetch_book_tickers | tickers=%s new_contracts=%s", len(tickers), new_contracts)
        books = self.toobit.get_all_book_tickers()
        contracts_by_name = {x["canonical"]: x for x in self.storage.contracts(active_only=True)}
        ranked: list[dict[str, Any]] = []
        now = now_ms()
        rejected_non_positive = 0
        rejected_below_pump = 0
        min_pump_24h = max(0.0001, abs(float(config.MIN_PUMP_24H_PERCENT)))
        for ticker in tickers:
            canonical = ticker["canonical"]
            base = canonical_base(canonical)
            if base in config.EXCLUDED_BASES or canonical not in contracts_by_name:
                continue
            # قانون سخت: این ربات فقط پایان پامپ صعودی را شکار می‌کند.
            # هیچ ارز با بازده صفر/منفی، حتی با تنظیم محیطی اشتباه، وارد Watchlist نمی‌شود.
            change_24h = safe_float(ticker.get("change_24h"))
            if change_24h <= 0:
                rejected_non_positive += 1
                continue
            contract = contracts_by_name[canonical]
            first_seen = int(contract.get("first_seen_at") or now)
            if now - first_seen < config.NEW_CONTRACT_WARMUP_MINUTES * 60_000:
                continue
            book = books.get(canonical, {})
            bid = safe_float(book.get("bid"))
            ask = safe_float(book.get("ask"))
            spread = (ask - bid) / ticker["last"] if ask >= bid > 0 else 1.0
            quote_volume = ticker["quote_volume"] or ticker["base_volume"] * ticker["last"]
            if quote_volume < config.MIN_QUOTE_VOLUME_24H or spread > config.MAX_SPREAD_RATE:
                continue
            if change_24h < min_pump_24h:
                rejected_below_pump += 1
                continue
            ticker.update({
                "spread": spread,
                "quote_volume": quote_volume,
                "contract_age_minutes": max(0, (now - first_seen) / 60_000),
                "change_24h": change_24h,
                "rank_score": change_24h + min(20.0, (quote_volume / max(config.MIN_QUOTE_VOLUME_24H, 1)) ** 0.25 * 4),
            })
            ranked.append(ticker)
        ranked.sort(key=lambda x: (x["rank_score"], x["quote_volume"]), reverse=True)
        watchlist = ranked[: config.WATCHLIST_SIZE]
        deep = watchlist[: config.DEEP_CANDIDATE_SIZE]
        # دفاع نهایی: حتی در صورت تغییر بعدی کد رتبه‌بندی، Watchlist نزولی ساخته نشود.
        watchlist = [x for x in watchlist if safe_float(x.get("change_24h")) > 0]
        deep = [x for x in deep if safe_float(x.get("change_24h")) > 0]
        self.storage.set_setting("last_scan_ranked_count", len(ranked))
        self.storage.set_setting("last_scan_deep_count", len(deep))
        self.storage.set_setting("last_scan_rejected_non_positive", rejected_non_positive)
        self.storage.set_setting("last_scan_rejected_below_pump", rejected_below_pump)
        logger.info(
            "SCAN_FILTER | contracts=%s tickers=%s books=%s ranked=%s watch=%s deep=%s rejected_non_positive=%s rejected_below_pump=%s min_pump_24h=%.2f",
            len(contracts_by_name), len(tickers), len(books), len(ranked), len(watchlist), len(deep),
            rejected_non_positive, rejected_below_pump, min_pump_24h,
        )
        self.storage.set_setting("watchlist", watchlist)
        self.storage.set_setting("deep_candidates", deep)
        self.storage.set_setting("last_scan_ms", now)
        self.storage.set_health("scanner", "ok", f"watch={len(watchlist)} deep={len(deep)}")

        signals: list[dict[str, Any]] = []
        for candidate in deep:
            if self.storage.has_symbol_lock(candidate["canonical"]):
                logger.info("SCAN_CANDIDATE_SKIP | %s | symbol_locked", candidate["canonical"])
                continue
            logger.info(
                "SCAN_CANDIDATE | %s | change24h=%.2f%% volume=%.0f spread=%.4f",
                candidate["canonical"], candidate["change_24h"], candidate["quote_volume"], candidate["spread"],
            )
            try:
                analyzed = self.analyze(candidate, margin_usdt=margin_usdt, leverage=leverage)
            except Exception as exc:
                self._set_diagnostic(candidate["canonical"], "analysis_exception", "analysis", error=str(exc))
                logger.exception("SCAN_CANDIDATE_ERROR | %s | %s", candidate["canonical"], exc)
                analyzed = None
            if analyzed:
                signals.append(analyzed)
                logger.info("SCAN_SIGNAL_READY | %s | score=%.1f", candidate["canonical"], safe_float(analyzed.get("signal_score")))
            else:
                detail = self.last_diagnostic(candidate["canonical"])
                reason = str(detail.get("reason") or "unknown_reject")
                stage = str(detail.get("stage") or "unknown")
                missing = detail.get("missing") or []
                missing_text = ",".join(str(x) for x in missing) if isinstance(missing, list) else str(missing)
                logger.info(
                    "SCAN_CANDIDATE_REJECT | %s | reason=%s stage=%s score=%.1f confirms=%s/%s missing=%s",
                    candidate["canonical"], reason, stage, safe_float(detail.get("score")),
                    safe_int(detail.get("confirmations")), safe_int(detail.get("required_confirmations")),
                    missing_text or "-",
                )
                if stage == "pump_filter":
                    logger.info(
                        "PUMP_CONTEXT_DETAIL | %s | change24h=%.2f r5=%.2f r15=%.2f r60=%.2f "
                        "recent_runup=%.2f%% peak_age=%sm distance24h_high=%.2f%% active=%s recent=%s near_high=%s",
                        candidate["canonical"], safe_float(detail.get("change_24h")), safe_float(detail.get("r5")),
                        safe_float(detail.get("r15")), safe_float(detail.get("r60")),
                        safe_float(detail.get("recent_runup_percent")), safe_int(detail.get("recent_peak_age_minutes")),
                        safe_float(detail.get("distance_from_24h_high_percent")),
                        int(bool(detail.get("active_short_term_pump"))), int(bool(detail.get("recent_pump_event"))),
                        int(bool(detail.get("near_24h_high"))),
                    )
                if stage in {"exhaustion", "score", "risk"}:
                    logger.info(
                        "EXHAUSTION_DETAIL | %s | r5=%.2f r15=%.2f r60=%.2f recent_runup=%.2f%% "
                        "drop_peak=%.2f%% drop_atr=%.2f mom_now=%.2f mom_before=%.2f volume_ratio=%.2f "
                        "sell_aggr=%.3f ask_bid=%.2f structure=%s failed_high=%s upper_reject=%s "
                        "bearish=%s ema_turn=%s rsi=%.1f/%.1f",
                        candidate["canonical"], safe_float(detail.get("r5")), safe_float(detail.get("r15")),
                        safe_float(detail.get("r60")), safe_float(detail.get("recent_runup_percent")),
                        safe_float(detail.get("drop_from_peak_percent")), safe_float(detail.get("drop_from_peak_atr")),
                        safe_float(detail.get("momentum_now")), safe_float(detail.get("momentum_before")),
                        safe_float(detail.get("volume_ratio")), safe_float(detail.get("sell_aggression")),
                        safe_float(detail.get("ask_bid_ratio")), int(bool(detail.get("structure_break"))),
                        int(bool(detail.get("failed_high"))), int(bool(detail.get("upper_rejection"))),
                        int(bool(detail.get("bearish_candle"))), int(bool(detail.get("ema_turn"))),
                        safe_float(detail.get("rsi_now")), safe_float(detail.get("rsi_prev")),
                    )
        return watchlist, signals

    def analyze(self, ticker: dict[str, Any], margin_usdt: float, leverage: int) -> dict[str, Any] | None:
        # تحلیل عمیق نیز مستقل از Scanner از ورود ارز نزولی جلوگیری می‌کند.
        min_pump_24h = max(0.0001, abs(float(config.MIN_PUMP_24H_PERCENT)))
        canonical = ticker["canonical"]
        change_24h = safe_float(ticker.get("change_24h"))
        self._set_diagnostic(canonical, "analysis_started", "start", change_24h=change_24h)
        if change_24h < min_pump_24h:
            self._set_diagnostic(
                canonical, "pump_24h_below_minimum", "pump_filter",
                change_24h=change_24h, min_pump_24h=min_pump_24h,
                missing=["pump_24h"],
            )
            return None
        contract_info = self.contracts.get(canonical) or ticker
        candles = self.toobit.get_klines(canonical, "1m", 90)
        if len(candles) < 30:
            self._set_diagnostic(
                canonical, "insufficient_candles", "market_data",
                candles=len(candles), required_candles=30, missing=["candles"],
            )
            return None
        trades = self.toobit.get_recent_trades(canonical, 60)
        depth = self.toobit.get_depth(canonical, 20)
        funding = self.toobit.get_funding_rate(canonical)
        oi = self.toobit.get_open_interest(canonical)
        ratio = self.toobit.get_long_short_ratio(canonical, "5m")

        closes = [x["close"] for x in candles]
        current = closes[-1]
        if current <= 0:
            self._set_diagnostic(canonical, "invalid_current_price", "market_data", current=current, missing=["price"])
            return None
        r5 = percent_change(current, closes[-6]) if len(closes) >= 6 else 0.0
        r15 = percent_change(current, closes[-16]) if len(closes) >= 16 else 0.0
        r60 = percent_change(current, closes[-61]) if len(closes) >= 61 else percent_change(current, closes[0])

        # زمینه پامپ را از «تاریخچه جهش» تشخیص می‌دهیم، نه فقط ادامه رشد همین لحظه.
        # هنگام خستگی طبیعی است که r5/r15 تخت یا منفی شوند؛ این باید نشانه خستگی
        # باشد، نه دلیل حذف نامزد پیش از ورود به تحلیل دامپ.
        lookback_count = min(len(candles), int(config.RECENT_PUMP_LOOKBACK_MINUTES) + 1)
        recent_rows = candles[-lookback_count:]
        running_low = float("inf")
        recent_runup_percent = 0.0
        recent_peak_index = 0
        for index, row in enumerate(recent_rows):
            row_low = safe_float(row.get("low"))
            row_high = safe_float(row.get("high"))
            if row_low > 0:
                running_low = min(running_low, row_low)
            if running_low < float("inf") and row_high > 0:
                runup = percent_change(row_high, running_low)
                if runup > recent_runup_percent:
                    recent_runup_percent = runup
                    recent_peak_index = index
        recent_peak_age_minutes = max(0, len(recent_rows) - 1 - recent_peak_index)

        min_pump_15m = max(0.0001, abs(float(config.MIN_PUMP_15M_PERCENT)))
        min_pump_5m = max(0.0001, abs(float(config.MIN_PUMP_5M_PERCENT)))
        min_recent_runup = max(0.0001, abs(float(config.MIN_RECENT_PUMP_RUNUP_PERCENT)))
        high_24h = safe_float(ticker.get("high"))
        distance_from_24h_high_percent = (
            max(0.0, (high_24h - current) / high_24h * 100.0) if high_24h > 0 else 999.0
        )
        active_short_term_pump = r15 >= min_pump_15m or r5 >= min_pump_5m
        recent_pump_event = recent_runup_percent >= min_recent_runup or r60 >= min_recent_runup
        near_24h_high = (
            high_24h > 0
            and distance_from_24h_high_percent <= float(config.MAX_DISTANCE_FROM_24H_HIGH_PERCENT)
        )
        pump_context_ok = (
            ticker["change_24h"] >= min_pump_24h
            and (active_short_term_pump or recent_pump_event or near_24h_high)
        )
        if not pump_context_ok:
            missing = []
            if ticker["change_24h"] < min_pump_24h:
                missing.append("pump_24h")
            if not (active_short_term_pump or recent_pump_event or near_24h_high):
                missing.append("recent_pump_context")
            self._set_diagnostic(
                canonical, "pump_context_not_confirmed", "pump_filter",
                change_24h=ticker["change_24h"], r5=r5, r15=r15, r60=r60,
                recent_runup_percent=recent_runup_percent,
                recent_peak_age_minutes=recent_peak_age_minutes,
                distance_from_24h_high_percent=distance_from_24h_high_percent,
                active_short_term_pump=active_short_term_pump,
                recent_pump_event=recent_pump_event, near_24h_high=near_24h_high,
                min_pump_24h=min_pump_24h, min_pump_15m=min_pump_15m,
                min_pump_5m=min_pump_5m, min_recent_runup=min_recent_runup,
                max_distance_from_24h_high=float(config.MAX_DISTANCE_FROM_24H_HIGH_PERCENT),
                missing=missing,
            )
            return None

        atr_value = atr(candles, config.ATR_PERIOD)
        if atr_value <= 0:
            self._set_diagnostic(canonical, "invalid_atr", "market_data", atr=atr_value, missing=["atr"])
            return None
        rsi_now = rsi(closes[-30:], config.RSI_PERIOD)
        rsi_prev = rsi(closes[-31:-1], config.RSI_PERIOD) if len(closes) >= 31 else rsi_now
        last = candles[-1]
        previous = candles[-2]
        body = abs(last["close"] - last["open"])
        upper_wick = last["high"] - max(last["open"], last["close"])
        recent_peak = max(x["high"] for x in candles[-12:])
        micro_support = min(x["low"] for x in candles[-5:-1])
        ema5 = ema(closes[-20:], 5)
        ema12 = ema(closes[-30:], 12)

        # فروش تهاجمی: isBuyerMaker=true یعنی فروشنده Market بوده است.
        buy_quote = 0.0
        sell_quote = 0.0
        for trade in trades:
            qty = safe_float(trade.get("q") or trade.get("qty") or trade.get("quantity"))
            price = safe_float(trade.get("p") or trade.get("price"))
            value = qty * price
            buyer_maker = bool(trade.get("ibm") if "ibm" in trade else trade.get("isBuyerMaker"))
            if buyer_maker:
                sell_quote += value
            else:
                buy_quote += value
        sell_aggression = sell_quote / max(1e-12, buy_quote + sell_quote)

        bids = depth.get("b") or depth.get("bids") or []
        asks = depth.get("a") or depth.get("asks") or []
        bid_value = sum(safe_float(x[0]) * safe_float(x[1]) for x in bids[:20] if isinstance(x, (list, tuple)) and len(x) >= 2)
        ask_value = sum(safe_float(x[0]) * safe_float(x[1]) for x in asks[:20] if isinstance(x, (list, tuple)) and len(x) >= 2)
        ask_bid_ratio = ask_value / max(1e-12, bid_value)

        volumes = [x["volume"] for x in candles]
        recent_volume = sum(volumes[-3:]) / 3
        prior_volume = sum(volumes[-8:-3]) / 5
        volume_fade = recent_volume < prior_volume * 0.82
        rel_volume = volumes[-1] / max(1e-12, median(volumes[-30:-1]))

        momentum_now = percent_change(closes[-1], closes[-4])
        momentum_before = percent_change(closes[-4], closes[-7])
        momentum_fade = momentum_now < momentum_before * 0.55
        bearish_candle = last["close"] < last["open"] and body >= atr_value * 0.18
        upper_rejection = upper_wick >= max(body * 0.8, atr_value * 0.25)
        failed_high = recent_peak > 0 and (recent_peak - current) >= atr_value * 0.55
        structure_break = current < micro_support or (current < ema5 and last["low"] < previous["low"])
        ema_turn = ema5 < ema12 or (current < ema5 and ema5 - current >= atr_value * 0.12)
        rsi_rollover = rsi_now >= 60 and rsi_now < rsi_prev
        drop_from_peak = max(0.0, recent_peak - current)
        drop_from_peak_percent = (drop_from_peak / recent_peak * 100.0) if recent_peak > 0 else 0.0
        drop_from_peak_atr = drop_from_peak / atr_value if atr_value > 0 else 0.0
        volume_ratio = recent_volume / max(1e-12, prior_volume)

        funding_rate = safe_float(funding.get("rate") or funding.get("fundingRate"))
        long_short = safe_float(ratio.get("longShortRatio") or ratio.get("ratio"), 1.0)
        oi_queue = self.oi_history[canonical]
        old_oi = oi_queue[0][1] if oi_queue else oi
        oi_queue.append((now_ms(), oi))
        oi_change = percent_change(oi, old_oi) if old_oi > 0 else 0.0
        trapped_longs = oi_change >= 1.0 and failed_high

        flags = {
            "ساختار نزولی اولیه": structure_break,
            "فروش تهاجمی غالب": sell_aggression >= 0.56,
            "ریجکت سقف": upper_rejection or failed_high,
            "افت شتاب پامپ": momentum_fade,
            "افت حجم خرید": volume_fade,
            "کندل نزولی معتبر": bearish_candle,
            "چرخش میانگین کوتاه": ema_turn,
            "برگشت RSI": rsi_rollover,
            "تراکم سفارش فروش": ask_bid_ratio >= 1.20,
            "لانگ‌های شلوغ": long_short >= 1.25 or funding_rate >= 0.0005,
            "لانگ گیر افتاده/OI": trapped_longs,
        }
        confirmations = sum(bool(x) for x in flags.values())
        diagnostic_base = {
            "r5": r5,
            "r15": r15,
            "r60": r60,
            "recent_runup_percent": recent_runup_percent,
            "recent_peak_age_minutes": recent_peak_age_minutes,
            "distance_from_24h_high_percent": distance_from_24h_high_percent,
            "active_short_term_pump": active_short_term_pump,
            "recent_pump_event": recent_pump_event,
            "near_24h_high": near_24h_high,
            "drop_from_peak_percent": drop_from_peak_percent,
            "drop_from_peak_atr": drop_from_peak_atr,
            "momentum_now": momentum_now,
            "momentum_before": momentum_before,
            "volume_ratio": volume_ratio,
            "sell_aggression": sell_aggression,
            "required_sell_aggression": 0.53,
            "ask_bid_ratio": ask_bid_ratio,
            "structure_break": structure_break,
            "failed_high": failed_high,
            "upper_rejection": upper_rejection,
            "bearish_candle": bearish_candle,
            "ema_turn": ema_turn,
            "momentum_fade": momentum_fade,
            "volume_fade": volume_fade,
            "rsi_rollover": rsi_rollover,
            "rsi_now": rsi_now,
            "rsi_prev": rsi_prev,
            "funding_rate": funding_rate,
            "long_short_ratio": long_short,
            "open_interest_change_percent": oi_change,
            "confirmations": confirmations,
            "required_confirmations": config.MIN_CONFIRMATIONS,
            "flags": flags,
        }
        # دو تأیید اجباری برای جلوگیری از حدس‌زدن سقف.
        missing: list[str] = []
        if not structure_break:
            missing.append("structure_break")
        if sell_aggression < 0.53:
            missing.append(f"sell_aggression<{0.53:.2f}")
        if confirmations < config.MIN_CONFIRMATIONS:
            missing.append(f"confirmations<{config.MIN_CONFIRMATIONS}")
        if missing:
            self._set_diagnostic(
                canonical, "exhaustion_not_confirmed", "exhaustion",
                **diagnostic_base, missing=missing,
            )
            return None

        score = 0.0
        pump_impulse = max(r5, r15, r60, recent_runup_percent)
        score += clamp((ticker["change_24h"] - min_pump_24h) * 0.45, 0, 18)
        score += clamp((pump_impulse - min_recent_runup) * 0.8, 0, 12)
        score += 18 if structure_break else 0
        score += clamp((sell_aggression - 0.5) * 80, 0, 14)
        score += 8 if (upper_rejection or failed_high) else 0
        score += 7 if momentum_fade else 0
        score += 6 if volume_fade else 0
        score += 5 if ask_bid_ratio >= 1.2 else 0
        score += 4 if (long_short >= 1.25 or funding_rate >= 0.0005) else 0
        score += 4 if trapped_longs else 0
        score += 4 if rsi_rollover else 0
        score = clamp(score, 0, 100)
        if score < config.MIN_SIGNAL_SCORE:
            self._set_diagnostic(
                canonical, "signal_score_below_minimum", "score",
                **diagnostic_base, score=score, min_signal_score=config.MIN_SIGNAL_SCORE,
                missing=[f"score<{config.MIN_SIGNAL_SCORE}"],
            )
            return None

        raw_stop_distance = max(
            recent_peak - current + atr_value * 0.15,
            atr_value * config.STOP_ATR_MULTIPLIER,
            current * config.MIN_STOP_PERCENT,
        )
        stop_percent = raw_stop_distance / current
        if stop_percent > config.MAX_STOP_PERCENT:
            self._set_diagnostic(
                canonical, "stop_too_wide", "risk",
                **diagnostic_base, score=score, stop_percent=stop_percent * 100.0,
                max_stop_percent=config.MAX_STOP_PERCENT * 100.0,
                missing=["acceptable_stop_distance"],
            )
            return None
        stop = current + raw_stop_distance
        safety_tp = current * (1.0 - config.SAFETY_TP_PERCENT)
        expected_move = clamp(max(r15 / 100 * 0.65, 0.04), 0.04, config.SAFETY_TP_PERCENT)
        notional = float(margin_usdt) * int(leverage)
        expected_cost = notional * (config.TAKER_FEE_RATE * 2 + config.ROUND_TRIP_SLIPPAGE_RATE + config.FUNDING_RESERVE_RATE)
        expected_net = notional * expected_move - expected_cost
        if expected_net < config.MIN_EXPECTED_NET_PROFIT_USDT:
            self._set_diagnostic(
                canonical, "expected_net_below_minimum", "risk",
                **diagnostic_base, score=score, expected_net=expected_net,
                min_expected_net=config.MIN_EXPECTED_NET_PROFIT_USDT,
                missing=["minimum_net_profit"],
            )
            return None

        self._set_diagnostic(
            canonical, "signal_ready", "ready",
            **diagnostic_base, score=score, expected_net=expected_net, missing=[],
        )
        reasons = [name for name, ok in flags.items() if ok]
        return {
            "canonical": canonical,
            "exchange_symbol": contract_info.get("exchange_symbol") or ticker["exchange_symbol"],
            "side": "SHORT",
            "status": "ACTIVE",
            "created_at": now_ms(),
            "entry": current,
            "sl": stop,
            "tp": safety_tp,
            "initial_sl": stop,
            "trailing_stop": None,
            "best_price": current,
            "atr": atr_value,
            "margin_usdt": float(margin_usdt),
            "leverage": int(leverage),
            "notional_usdt": notional,
            "expected_net_profit": expected_net,
            "signal_score": score,
            "confirmations": confirmations,
            "reasons": reasons,
            "metrics": {
                "pump_24h_percent": ticker["change_24h"],
                "pump_15m_percent": r15,
                "pump_5m_percent": r5,
                "spread_percent": ticker.get("spread", 0) * 100,
                "quote_volume_24h": ticker["quote_volume"],
                "sell_aggression": sell_aggression,
                "ask_bid_ratio": ask_bid_ratio,
                "relative_volume": rel_volume,
                "funding_rate": funding_rate,
                "long_short_ratio": long_short,
                "open_interest": oi,
                "open_interest_change_percent": oi_change,
                "rsi": rsi_now,
            },
            "contract_info": contract_info,
            "metadata": {"strategy": "WILD_PUMP_EXHAUSTION_SHORT_V1"},
        }
