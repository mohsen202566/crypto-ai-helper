"""مانیتور سیگنال‌های Initial/Medium، سناریوها و مسیر پس از نتیجه با قیمت OKX/Bybit."""
from __future__ import annotations

import logging
import queue
from typing import Any

import config
from market_data import MarketDataClient
from storage import Storage
from symbol_registry import SymbolRegistry
from tp_sl_engine import TPSLEngine
from utils import now_ms

logger = logging.getLogger("adaptive_bot")


class VirtualMonitor:
    def __init__(
        self,
        storage: Storage,
        registry: SymbolRegistry,
        market: MarketDataClient,
        tp_sl: TPSLEngine,
        result_queue: queue.Queue[int],
    ):
        self.storage = storage
        self.registry = registry
        self.market = market
        self.tp_sl = tp_sl
        self.result_queue = result_queue
        self._last_path_scan_ms = 0

    @staticmethod
    def _hit(side: str, price: float, tp: float, sl: float) -> str | None:
        if side == "LONG":
            if price >= tp:
                return "TP"
            if price <= sl:
                return "STOP"
        else:
            if price <= tp:
                return "TP"
            if price >= sl:
                return "STOP"
        return None

    @staticmethod
    def _scenario_pnl(scenario: dict[str, Any], result: str) -> float:
        notional = float(scenario.get("notional_usdt") or float(scenario.get("margin_usdt", 0)) * int(scenario.get("leverage", 1)))
        entry = float(scenario.get("entry") or 0)
        exit_price = float(scenario.get("tp") if result == "TP" else scenario.get("sl") or 0)
        if notional <= 0 or entry <= 0:
            return 0.0
        gross_rate = (exit_price - entry) / entry if scenario.get("side") == "LONG" else (entry - exit_price) / entry
        cost = notional * (config.TOOBIT_TAKER_FEE_RATE * 2 + config.DEFAULT_SLIPPAGE_RATE_ROUND_TRIP + config.DEFAULT_FUNDING_RESERVE_RATE)
        return notional * gross_rate - cost

    @staticmethod
    def _candle_hit(side: str, high: float, low: float, tp: float, sl: float) -> str | None:
        tp_hit = high >= tp if side == "LONG" else low <= tp
        sl_hit = low <= sl if side == "LONG" else high >= sl
        # With OHLC alone the intrabar order is unknowable. Count the adverse outcome
        # to keep Medium/LAB statistics conservative rather than artificially optimistic.
        if tp_hit and sl_hit:
            return "STOP"
        if sl_hit:
            return "STOP"
        if tp_hit:
            return "TP"
        return None

    @staticmethod
    def _eligible_candles(
        rows: list[Any], created_at: int, last_candle_ts: int, interval_ms: int = 60_000
    ) -> list[Any]:
        # Skip the creation bucket because part of that OHLC candle predates the signal.
        first_full_bucket = ((int(created_at) + interval_ms - 1) // interval_ms) * interval_ms
        floor = max(first_full_bucket, int(last_candle_ts or 0) + 1)
        return [c for c in rows if c.confirmed and int(c.ts) >= floor]

    @staticmethod
    def _post_stop_diagnosis(post: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
        """Classify the observed path after a Stop without pretending certainty.

        Reaching the old TP is handled immediately elsewhere.  At the end of the
        observation horizon this method distinguishes continued adverse movement from a
        shallow whipsaw/rebound.  The probabilities are evidence for future Challengers,
        never an instant Champion mutation.
        """
        side = str(post.get("side") or "LONG").upper()
        entry = float(post.get("entry") or 0.0)
        tp = float(post.get("tp") or 0.0)
        sl = float(post.get("sl") or 0.0)
        max_after = float(post.get("max_after") or sl)
        min_after = float(post.get("min_after") or sl)
        stop_distance = max(abs(entry - sl), abs(entry) * 1e-6, 1e-12)
        target_distance = max(abs(tp - entry), stop_distance)
        if side == "LONG":
            adverse = max(0.0, sl - min_after)
            rebound = max(0.0, max_after - sl)
        else:
            adverse = max(0.0, max_after - sl)
            rebound = max(0.0, sl - min_after)
        adverse_ratio = adverse / stop_distance
        rebound_ratio = rebound / target_distance

        if adverse_ratio >= 0.75:
            probs = {
                "WRONG_DIRECTION": 42.0,
                "WRONG_REGIME": 24.0,
                "EARLY_OR_LATE_ENTRY": 10.0,
                "STOP_INSIDE_NOISE": 4.0,
                "FALSE_BREAKOUT_OR_LIQUIDITY_SWEEP": 8.0,
                "BTC_ETH_CONTEXT_ERROR": 5.0,
                "SHOCK_OR_UNFORECASTABLE": 4.0,
                "UNKNOWN": 3.0,
            }
            status = "CONTINUED_ADVERSE_AFTER_STOP"
        elif rebound_ratio >= 0.35:
            probs = {
                "EARLY_OR_LATE_ENTRY": 39.0,
                "STOP_INSIDE_NOISE": 29.0,
                "WRONG_DIRECTION": 8.0,
                "WRONG_REGIME": 8.0,
                "FALSE_BREAKOUT_OR_LIQUIDITY_SWEEP": 8.0,
                "BTC_ETH_CONTEXT_ERROR": 3.0,
                "SHOCK_OR_UNFORECASTABLE": 2.0,
                "UNKNOWN": 3.0,
            }
            status = "REBOUNDED_AFTER_STOP_WITHOUT_TP"
        else:
            probs = {
                "EARLY_OR_LATE_ENTRY": 20.0,
                "STOP_INSIDE_NOISE": 16.0,
                "WRONG_DIRECTION": 16.0,
                "WRONG_REGIME": 15.0,
                "FALSE_BREAKOUT_OR_LIQUIDITY_SWEEP": 10.0,
                "BTC_ETH_CONTEXT_ERROR": 6.0,
                "SHOCK_OR_UNFORECASTABLE": 7.0,
                "UNKNOWN": 10.0,
            }
            status = "POST_STOP_CAUSE_UNCERTAIN"
        evidence = {
            "post_path": status,
            "max_after": max_after,
            "min_after": min_after,
            "adverse_ratio": round(adverse_ratio, 4),
            "rebound_ratio": round(rebound_ratio, 4),
        }
        return probs, evidence

    @staticmethod
    def _recovery_window(oldest_ms: int, now: int) -> tuple[str, int, int]:
        """Choose the finest interval that recovers the whole gap with a practical request."""
        gap_minutes = max(1, (now - int(oldest_ms or now)) // 60_000 + 2)
        for interval, minutes in (("1m", 1), ("5m", 5), ("15m", 15), ("1H", 60), ("4H", 240)):
            limit = int(gap_minutes // minutes + 3)
            if limit <= 300 or interval == "4H":
                return interval, max(3, min(limit, 2160)), minutes * 60_000
        return "4H", 2160, 240 * 60_000

    def _reconcile_minute_paths(self) -> dict[str, int]:
        """Use confirmed 1m high/low paths to catch wicks missed by ticker polling."""
        now = now_ms()
        if now - self._last_path_scan_ms < 60_000:
            return {"signals": 0, "scenarios": 0}
        self._last_path_scan_ms = now
        signals = [s for s in self.storage.runtime.active_signals() if s.get("tier") in {"INITIAL", "MEDIUM"}]
        scenarios = [s for s in self.storage.learning.active_scenarios() if not s.get("no_entry")]
        by_symbol: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for item in signals:
            by_symbol.setdefault(item["canonical"], {"signals": [], "scenarios": []})["signals"].append(item)
        for item in scenarios:
            by_symbol.setdefault(item["canonical"], {"signals": [], "scenarios": []})["scenarios"].append(item)
        counts = {"signals": 0, "scenarios": 0}
        for canonical, group in by_symbol.items():
            mapping = self.registry.get(canonical)
            if not mapping:
                continue
            all_items = group["signals"] + group["scenarios"]
            oldest = min(
                int(item.get("last_candle_ts") or item.get("virtual_last_candle_ts") or item.get("created_at") or now)
                for item in all_items
            )
            interval, limit, interval_ms = self._recovery_window(oldest, now)
            try:
                _source, candles = self.market.candles(mapping, interval, limit, allow_fallback=True)
            except Exception as exc:
                logger.debug("PATH_SCAN_SKIP | %s | %s", canonical, str(exc)[:140])
                continue
            for signal in group["signals"]:
                eligible = self._eligible_candles(
                    candles, int(signal.get("created_at") or 0),
                    int(signal.get("virtual_last_candle_ts") or 0), interval_ms,
                )
                result = None
                result_price = None
                for candle in eligible:
                    result = self._candle_hit(
                        signal["side"], float(candle.high), float(candle.low),
                        float(signal["tp"]), float(signal["sl"]),
                    )
                    if result:
                        result_price = float(signal["tp"] if result == "TP" else signal["sl"])
                        break
                if result:
                    pnl = self.tp_sl.realized_virtual_pnl(signal, result)
                    final = self.storage.runtime.finalize_signal(signal["id"], result, result_price, pnl)
                    if final:
                        self.result_queue.put(signal["id"])
                        counts["signals"] += 1
                elif eligible:
                    self.storage.runtime.update_signal(signal["id"], virtual_last_candle_ts=int(eligible[-1].ts))

            for scenario in group["scenarios"]:
                eligible = self._eligible_candles(
                    candles, int(scenario.get("created_at") or 0),
                    int(scenario.get("last_candle_ts") or 0), interval_ms,
                )
                if not eligible:
                    continue
                if scenario.get("waiting_entry") and not scenario.get("entered"):
                    touched = False
                    for candle in eligible:
                        entry = float(scenario["entry"])
                        if float(candle.low) <= entry <= float(candle.high):
                            scenario = self.storage.learning.update_scenario(
                                scenario["id"], entered=True, entered_at=int(candle.ts), actual_entry=entry,
                                last_candle_ts=int(candle.ts),
                            ) or scenario
                            touched = True
                            break
                    if not touched:
                        self.storage.learning.update_scenario(scenario["id"], last_candle_ts=int(eligible[-1].ts))
                    # Never infer TP/SL ordering inside the same entry candle.
                    continue
                result = None
                result_price = None
                for candle in eligible:
                    result = self._candle_hit(
                        scenario["side"], float(candle.high), float(candle.low),
                        float(scenario["tp"]), float(scenario["sl"]),
                    )
                    if result:
                        result_price = float(scenario["tp"] if result == "TP" else scenario["sl"])
                        break
                if result:
                    pnl = self._scenario_pnl(scenario, result)
                    self.storage.learning.finalize_scenario(scenario["id"], result, pnl, result_price)
                    counts["scenarios"] += 1
                else:
                    self.storage.learning.update_scenario(scenario["id"], last_candle_ts=int(eligible[-1].ts))
        return counts

    def tick(self) -> dict[str, int]:
        mappings = list(self.registry._mappings.values())  # registry owns immutable resolved mapping objects
        if not mappings:
            return {"signals": 0, "scenarios": 0, "post": 0}
        try:
            self.market.refresh_tickers(mappings)
        except Exception as exc:
            self.storage.runtime.set_health("virtual_monitor", "warning", f"ticker failed: {exc}")
            return {"signals": 0, "scenarios": 0, "post": 0}
        _, _, prices = self.market.ticker_snapshot()
        counts = {"signals": 0, "scenarios": 0, "post": 0}
        path_counts = self._reconcile_minute_paths()
        counts["signals"] += path_counts["signals"]
        counts["scenarios"] += path_counts["scenarios"]

        for signal in self.storage.runtime.active_signals():
            if signal.get("tier") not in {"INITIAL", "MEDIUM"}:
                continue
            price = prices.get(signal["canonical"])
            if not price:
                continue
            result = self._hit(signal["side"], price, float(signal["tp"]), float(signal["sl"]))
            if not result:
                continue
            pnl = self.tp_sl.realized_virtual_pnl(signal, result)
            final = self.storage.runtime.finalize_signal(signal["id"], result, price, pnl)
            if final:
                self.result_queue.put(signal["id"])
                counts["signals"] += 1

        for scenario in self.storage.learning.active_scenarios():
            parent = self.storage.runtime.get_signal(int(scenario["parent_signal_id"]))
            if scenario.get("no_entry"):
                if parent and parent.get("result") in {"TP", "STOP"}:
                    result = "AVOIDED_STOP" if parent["result"] == "STOP" else "MISSED_TP"
                    self.storage.learning.finalize_scenario(scenario["id"], result, 0.0, float(parent.get("close_price") or 0))
                    counts["scenarios"] += 1
                continue
            price = prices.get(scenario["canonical"])
            if not price:
                continue
            if scenario.get("waiting_entry") and not scenario.get("entered"):
                entered = price <= float(scenario["entry"]) if scenario["side"] == "LONG" else price >= float(scenario["entry"])
                if entered:
                    scenario = self.storage.learning.update_scenario(scenario["id"], entered=True, entered_at=now_ms(), actual_entry=price) or scenario
                else:
                    if parent and parent.get("result") in {"TP", "STOP"}:
                        result = "NO_FILL_AVOIDED_STOP" if parent["result"] == "STOP" else "NO_FILL_MISSED_TP"
                        self.storage.learning.finalize_scenario(scenario["id"], result, 0.0, float(parent.get("close_price") or price))
                        counts["scenarios"] += 1
                    continue
            result = self._hit(scenario["side"], price, float(scenario["tp"]), float(scenario["sl"]))
            if result:
                pnl = self._scenario_pnl(scenario, result)
                self.storage.learning.finalize_scenario(scenario["id"], result, pnl, price)
                counts["scenarios"] += 1

        for post in self.storage.learning.active_post_results():
            price = prices.get(post["canonical"])
            if not price:
                continue
            post["max_after"] = max(float(post.get("max_after") or price), price)
            post["min_after"] = min(float(post.get("min_after") or price), price)
            finished = now_ms() >= int(post["ends_at"])
            status = "DONE_HORIZON" if finished else "ACTIVE"
            if post["result"] == "STOP":
                hit_original_tp = price >= float(post["tp"]) if post["side"] == "LONG" else price <= float(post["tp"])
                if hit_original_tp:
                    status = "REACHED_ORIGINAL_TP_AFTER_STOP"
                    finished = True
                    # Direction likely correct; entry/SL becomes more probable.
                    probs = {
                        "EARLY_OR_LATE_ENTRY": 52.0,
                        "STOP_INSIDE_NOISE": 30.0,
                        "WRONG_DIRECTION": 5.0,
                        "WRONG_REGIME": 4.0,
                        "FALSE_BREAKOUT_OR_LIQUIDITY_SWEEP": 5.0,
                        "EXECUTION_OR_SLIPPAGE": 2.0,
                        "UNKNOWN": 2.0,
                    }
                    self.storage.learning.update_stop_diagnosis(int(post["signal_id"]), probs, {"post_path": status, "price": price})
                elif finished:
                    probs, evidence = self._post_stop_diagnosis(post)
                    status = str(evidence["post_path"])
                    evidence["price_at_horizon"] = price
                    self.storage.learning.update_stop_diagnosis(int(post["signal_id"]), probs, evidence)
            post["final_status"] = status
            self.storage.learning.update_post_result(int(post["signal_id"]), post, status if finished else "ACTIVE")
            counts["post"] += 1

        self.storage.runtime.set_health("virtual_monitor", "ok", f"signals={counts['signals']} scenarios={counts['scenarios']} post={counts['post']}")
        return counts
