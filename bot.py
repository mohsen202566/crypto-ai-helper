"""هسته اجرایی: اسکن، مسیریابی Real/Virtual، سفارش و مانیتورینگ."""
from __future__ import annotations

import queue
import threading
from typing import Any

import config
from storage import Storage
from strategy import PumpStrategy
from toobit_client import ToobitClient
from utils import canonical_base, canonical_symbol, now_ms, safe_float, safe_int, logger


class BotEngine:
    def __init__(self, storage: Storage, toobit: ToobitClient):
        self.storage = storage
        self.toobit = toobit
        self.strategy = PumpStrategy(storage, toobit)
        self.notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self.trade_queue: queue.Queue[int] = queue.Queue()
        self._trade_lock = threading.RLock()

    def startup(self) -> None:
        self.storage.set_setting("startup_phase", "دریافت قراردادهای Toobit")
        self.strategy.refresh_contracts(force=True)
        self.storage.set_setting("startup_ready", True)
        self.storage.set_setting("startup_phase", "READY")
        self.storage.set_health("main", "ok", "ربات آماده؛ ترید واقعی خاموش")

    def _real_ready(self) -> tuple[bool, str]:
        settings = self.storage.settings()
        if not settings.get("real_trade_enabled"):
            return False, "TRADING_OFF"
        if not self.toobit.has_credentials:
            return False, "CREDENTIALS_MISSING"
        if self.storage.slot_counts()["free"] <= 0:
            return False, "NO_FREE_SLOT"
        account = self.storage.account_snapshot()
        age = max(0, now_ms() - int(account.get("updated_at") or 0))
        if not account.get("connected") or age > config.ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS * 1000:
            return False, "TOOBIT_UNAVAILABLE"
        return True, "REAL_READY"

    def scan_once(self) -> int:
        if not self.storage.get_setting("startup_ready", False):
            logger.info("SCAN_SKIPPED | startup_not_ready")
            return 0
        started_ms = now_ms()
        self.storage.set_setting("last_scan_started_ms", started_ms)
        self.storage.set_setting("last_scan_error", "")
        logger.info("SCAN_START | interval=%.1fs", float(config.MARKET_SCAN_SECONDS))
        try:
            settings = self.storage.settings()
            watchlist, signals = self.strategy.scan(
                margin_usdt=float(settings["trade_margin_usdt"]),
                leverage=int(settings["leverage"]),
            )
            emitted = 0
            for signal in signals:
                if self.route_signal(signal):
                    emitted += 1
            finished_ms = now_ms()
            self.storage.set_setting("last_scan_finished_ms", finished_ms)
            self.storage.set_setting("last_scan_duration_ms", max(0, finished_ms - started_ms))
            self.storage.set_setting("last_scan_watch_count", len(watchlist))
            self.storage.set_setting("last_scan_signal_count", len(signals))
            self.storage.set_setting("last_scan_emitted_count", emitted)
            logger.info(
                "SCAN_DONE | tickers=%s ranked=%s watch=%s deep=%s signals=%s emitted=%s elapsed=%.2fs",
                self.storage.get_setting("last_scan_ticker_count", 0),
                self.storage.get_setting("last_scan_ranked_count", 0),
                len(watchlist),
                self.storage.get_setting("last_scan_deep_count", 0),
                len(signals),
                emitted,
                (finished_ms - started_ms) / 1000.0,
            )
            return emitted
        except Exception as exc:
            finished_ms = now_ms()
            self.storage.set_setting("last_scan_finished_ms", finished_ms)
            self.storage.set_setting("last_scan_duration_ms", max(0, finished_ms - started_ms))
            self.storage.set_setting("last_scan_error", str(exc)[:500])
            logger.exception("SCAN_FAILED | %s", exc)
            raise

    def route_signal(self, signal: dict[str, Any]) -> int | None:
        real, reason = self._real_ready()
        signal = dict(signal)
        if real:
            signal["mode"] = "REAL"
            signal_id = self.storage.create_real_signal_and_reserve(signal)
            if signal_id is not None:
                self.trade_queue.put(signal_id)
                self.notifications.put({"type": "signal", "signal_id": signal_id})
                return signal_id
            reason = "SLOT_OR_LOCK_RACE"
        signal["mode"] = "VIRTUAL"
        signal["virtual_reason"] = reason
        signal_id = self.storage.create_virtual_signal(signal)
        if signal_id is not None:
            self.notifications.put({"type": "signal", "signal_id": signal_id})
        return signal_id

    def process_trade_one(self, timeout: float = 1.0) -> bool:
        try:
            signal_id = self.trade_queue.get(timeout=timeout)
        except queue.Empty:
            return False
        try:
            self.submit_real(signal_id)
            return True
        finally:
            self.trade_queue.task_done()

    def submit_real(self, signal_id: int) -> None:
        with self._trade_lock:
            signal = self.storage.get_signal(signal_id)
            if not signal or signal.get("mode") != "REAL" or signal.get("status") != "PENDING_OPEN":
                return
            # خاموش‌شدن یا نبود Credential قبل از Submit باید سیگنال را مجازی کند.
            if not self.storage.get_setting("real_trade_enabled", False):
                converted = self.storage.convert_real_to_virtual(signal_id, "TRADING_DISABLED_BEFORE_SUBMIT")
                if converted:
                    self.notifications.put({"type": "signal", "signal_id": signal_id, "converted": True})
                return
            if not self.toobit.has_credentials:
                converted = self.storage.convert_real_to_virtual(signal_id, "CREDENTIALS_MISSING_BEFORE_SUBMIT")
                if converted:
                    self.notifications.put({"type": "signal", "signal_id": signal_id, "converted": True})
                return
            submitted_at = now_ms()
            confirm_after = submitted_at + config.PENDING_CONFIRM_SECONDS * 1000
            client_order_id = f"pb{signal_id}{submitted_at}"[-32:]
            self.storage.update_signal(
                signal_id,
                client_order_id=client_order_id,
                order_submitted_at=submitted_at,
                confirm_after=confirm_after,
            )
            self.storage.update_position(
                signal_id,
                client_order_id=client_order_id,
                submitted_at=submitted_at,
                confirm_after=confirm_after,
                status="PENDING_OPEN",
            )
            try:
                response = self.toobit.place_market_order(
                    symbol=signal["canonical"],
                    side=signal["side"],
                    entry_price=float(signal["entry"]),
                    margin_usdt=float(signal["margin_usdt"]),
                    leverage=int(signal["leverage"]),
                    tp_price=float(signal["tp"]),
                    sl_price=float(signal["sl"]),
                    client_order_id=client_order_id,
                    symbol_info=signal.get("contract_info") or {},
                )
                self.storage.update_signal(signal_id, order_id=response.get("order_id"), order_response=response)
                self.storage.update_position(signal_id, order_id=response.get("order_id"), order_response=response)
                self.storage.add_event("ORDER_SUBMITTED", "سفارش واقعی همراه TP/SL ارسال شد", signal["canonical"], response)
            except Exception as exc:
                # Timeout/5xx ممکن است بعد از پذیرش سفارش رخ دهد؛ اسلات حفظ می‌شود.
                self.storage.update_signal(signal_id, order_submit_error=str(exc)[:1000])
                self.storage.update_position(signal_id, submit_error=str(exc)[:1000])
                self.storage.add_event("ORDER_SUBMIT_ERROR", str(exc), signal["canonical"])

    @staticmethod
    def _virtual_pnl(signal: dict[str, Any], close_price: float) -> float:
        entry = float(signal["entry"])
        notional = float(signal["notional_usdt"])
        gross_rate = (entry - close_price) / entry  # استراتژی فقط SHORT است.
        costs = notional * (config.TAKER_FEE_RATE * 2 + config.ROUND_TRIP_SLIPPAGE_RATE + config.FUNDING_RESERVE_RATE)
        return notional * gross_rate - costs

    def _update_trailing(self, signal: dict[str, Any], price: float) -> tuple[dict[str, Any], bool]:
        entry = float(signal["entry"])
        atr_value = float(signal.get("atr") or 0)
        best = min(float(signal.get("best_price") or entry), price)
        profit_rate = (entry - price) / entry
        current_trail = signal.get("trailing_stop")
        changed = best != signal.get("best_price")
        if profit_rate >= config.TRAILING_ACTIVATION_PERCENT:
            distance = max(atr_value * config.TRAILING_ATR_MULTIPLIER, best * config.TRAILING_DISTANCE_PERCENT)
            candidate = best + distance
            # Stop شورت فقط باید پایین‌تر بیاید و هرگز دوباره بازتر نشود.
            if current_trail is None or candidate < float(current_trail):
                current_trail = candidate
                changed = True
        changes = {"best_price": best, "trailing_stop": current_trail}
        exit_hit = current_trail is not None and price >= float(current_trail) and best < entry
        return changes, exit_hit

    def monitor_prices(self) -> int:
        prices = self.toobit.get_all_prices()
        finished = 0
        for signal in self.storage.active_signals():
            price = prices.get(signal["canonical"])
            if not price:
                continue
            changes, trailing_exit = self._update_trailing(signal, price)
            self.storage.update_signal(signal["id"], last_price=price, last_price_at=now_ms(), **changes)
            if signal["mode"] == "VIRTUAL":
                result = None
                if price >= float(signal["sl"]):
                    result = "STOP"
                elif price <= float(signal["tp"]):
                    result = "TP"
                elif trailing_exit:
                    result = "TRAIL_EXIT"
                if result:
                    final = self.storage.finalize_signal(
                        signal["id"], result, price, self._virtual_pnl(signal, price),
                        metadata={"virtual_reason": signal.get("virtual_reason")},
                    )
                    if final:
                        self.notifications.put({"type": "result", "signal_id": signal["id"]})
                        finished += 1
                continue

            if signal["mode"] == "REAL" and signal.get("status") == "OPEN":
                trail = changes.get("trailing_stop")
                last_update = int(signal.get("trailing_updated_at") or 0)
                if trail and now_ms() - last_update >= config.TRAILING_UPDATE_SECONDS * 1000:
                    try:
                        self.toobit.set_trading_stop(
                            signal["canonical"], signal["side"], float(signal["tp"]), float(trail)
                        )
                        self.storage.update_signal(signal["id"], trailing_updated_at=now_ms(), exchange_trailing_stop=trail)
                    except Exception as exc:
                        self.storage.add_event("TRAIL_UPDATE_ERROR", str(exc), signal["canonical"])
                if trailing_exit and not signal.get("close_requested"):
                    try:
                        self.toobit.flash_close(signal["canonical"], signal["side"])
                        self.storage.update_signal(signal["id"], close_requested=True, close_reason="TRAIL_EXIT", close_requested_at=now_ms())
                    except Exception as exc:
                        self.storage.add_event("FLASH_CLOSE_ERROR", str(exc), signal["canonical"])
        self.storage.set_health("price_monitor", "ok", f"active={len(self.storage.active_signals())} finished={finished}")
        return finished

    def _open_map(self, rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        out = {}
        for item in rows:
            if self.toobit.position_qty(item) <= 0:
                continue
            key = (canonical_symbol(self.toobit.item_symbol(item)), self.toobit.position_side(item))
            out[key] = item
        return out

    @staticmethod
    def _actual_entry(item: dict[str, Any]) -> float | None:
        for key in ("avgPrice", "entryPrice", "avgEntryPrice", "openPrice", "price"):
            value = safe_float(item.get(key))
            if value > 0:
                return value
        return None

    @staticmethod
    def _result_from_realized(signal: dict[str, Any], realized: dict[str, Any]) -> str:
        raw = realized.get("raw") or {}
        text = " ".join(str(raw.get(k) or "") for k in ("type", "orderType", "stopOrderType", "closeType", "clientOrderId", "remark")).upper()
        if signal.get("close_reason") == "TRAIL_EXIT" or "FLASH" in text:
            return "TRAIL_EXIT"
        if "TAKE" in text or "TP" in text:
            return "TP"
        if "STOP_LOSS" in text or "SL" in text:
            return "STOP"
        close = safe_float(realized.get("close_price"))
        entry = float(signal.get("entry") or 0)
        tp = float(signal.get("tp") or 0)
        sl = float(signal.get("sl") or 0)
        tolerance = max(entry * 0.0015, abs(tp - entry) * 0.15, abs(sl - entry) * 0.15)
        if close > 0 and abs(close - tp) <= tolerance:
            return "TP"
        if close > 0 and abs(close - sl) <= tolerance:
            return "STOP"
        return "MANUAL_CLOSE"

    def _find_realized(self, signal: dict[str, Any], pos: dict[str, Any], now: int) -> dict[str, Any] | None:
        return self.toobit.find_realized_result(
            symbol=signal["canonical"],
            side=signal["side"],
            start_ms=int(pos.get("submitted_at") or pos.get("reserved_at") or signal.get("created_at") or now),
            end_ms=now,
            order_id=str(pos.get("order_id") or signal.get("order_id") or "") or None,
            client_order_id=str(pos.get("client_order_id") or signal.get("client_order_id") or "") or None,
        )

    def confirm_pending(self) -> dict[str, int]:
        now = now_ms()
        due = [x for x in self.storage.positions(("PENDING_OPEN",)) if int(x.get("confirm_after") or 0) and now >= int(x.get("confirm_after") or 0)]
        if not due or not self.toobit.has_credentials:
            return {"confirmed": 0, "failed": 0, "closed": 0}
        try:
            open_map = self._open_map(self.toobit.get_positions())
        except Exception as exc:
            self.storage.set_health("real_confirm", "warning", str(exc))
            return {"confirmed": 0, "failed": 0, "closed": 0}
        counts = {"confirmed": 0, "failed": 0, "closed": 0}
        for pos in due:
            signal = self.storage.get_signal(int(pos["signal_id"]))
            if not signal:
                continue
            exchange_pos = open_map.get((signal["canonical"], signal["side"]))
            if exchange_pos:
                changes = {"status": "OPEN", "opened_at": now, "last_seen_at": now, "position_snapshot": exchange_pos}
                actual_entry = self._actual_entry(exchange_pos)
                if actual_entry:
                    changes["actual_entry"] = actual_entry
                self.storage.update_position(signal["id"], **changes)
                self.storage.update_signal(signal["id"], **changes)
                self.notifications.put({"type": "position_open", "signal_id": signal["id"]})
                counts["confirmed"] += 1
                continue
            try:
                realized = self._find_realized(signal, pos, now)
            except Exception:
                realized = None
            if realized:
                result = self._result_from_realized(signal, realized)
                final = self.storage.finalize_signal(
                    signal["id"], result, safe_float(realized.get("close_price")) or None,
                    safe_float(realized.get("pnl")), closed_at=safe_int(realized.get("close_time_ms"), now),
                    metadata={"toobit_realized": realized, "closed_before_confirmation": True},
                )
                if final:
                    self.notifications.put({"type": "result", "signal_id": signal["id"]})
                    counts["closed"] += 1
            else:
                final = self.storage.finalize_signal(
                    signal["id"], "FAILED_OPEN", None, None,
                    metadata={"reason": "NO_POSITION_OR_REALIZED_RESULT_AFTER_CONFIRM_WINDOW"},
                )
                if final:
                    self.notifications.put({"type": "failed_open", "signal_id": signal["id"]})
                    counts["failed"] += 1
        self.storage.set_health("real_confirm", "ok", str(counts))
        return counts

    def monitor_real(self) -> dict[str, int]:
        if not self.toobit.has_credentials:
            self.storage.save_account_snapshot(False, {}, "کلید API توبیت تنظیم نشده")
            return {"open": 0, "closed": 0}
        try:
            positions = self.toobit.get_positions()
            open_map = self._open_map(positions)
            balance = self.toobit.get_usdt_balance_summary()
            balance["open_positions"] = len(open_map)
            balance["open_position_keys"] = sorted(f"{canonical}:{side}" for canonical, side in open_map)
            self.storage.save_account_snapshot(True, balance)
        except Exception as exc:
            previous = self.storage.account_snapshot()
            self.storage.save_account_snapshot(False, previous, str(exc))
            self.storage.set_health("real_monitor", "warning", str(exc))
            return {"open": 0, "closed": 0}
        closed = 0
        now = now_ms()
        for pos in self.storage.positions(("OPEN",)):
            signal = self.storage.get_signal(int(pos["signal_id"]))
            if not signal:
                continue
            exchange_pos = open_map.get((signal["canonical"], signal["side"]))
            if exchange_pos:
                self.storage.update_position(signal["id"], last_seen_at=now, position_snapshot=exchange_pos)
                continue
            try:
                realized = self._find_realized(signal, pos, now)
            except Exception:
                realized = None
            if not realized:
                self.storage.update_position(signal["id"], result_waiting_since=pos.get("result_waiting_since") or now)
                continue
            result = self._result_from_realized(signal, realized)
            final = self.storage.finalize_signal(
                signal["id"], result, safe_float(realized.get("close_price")) or None,
                safe_float(realized.get("pnl")), closed_at=safe_int(realized.get("close_time_ms"), now),
                metadata={"toobit_realized": realized},
            )
            if final:
                self.notifications.put({"type": "result", "signal_id": signal["id"]})
                closed += 1
        self.storage.set_health("real_monitor", "ok", f"open={len(open_map)} closed={closed}")
        return {"open": len(open_map), "closed": closed}
