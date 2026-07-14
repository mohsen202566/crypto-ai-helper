"""مانیتور Toobit: تایید ۷۰ثانیه‌ای و بررسی تجمیعی بسته‌شدن هر ۶۰ ثانیه."""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from storage import Storage
from toobit_client import ToobitClient
from utils import canonical_base_from_symbol, now_ms, safe_float, safe_int

logger = logging.getLogger("adaptive_bot")


class RealMonitor:
    def __init__(
        self,
        storage: Storage,
        toobit: ToobitClient,
        result_queue: queue.Queue[int],
        notification_queue: queue.Queue[dict[str, Any]],
    ):
        self.storage = storage
        self.toobit = toobit
        self.result_queue = result_queue
        self.notifications = notification_queue
        self._api_lock = threading.RLock()

    def _position_key(self, item: dict[str, Any]) -> tuple[str, str]:
        symbol = canonical_base_from_symbol(self.toobit._symbol_from_item(item))
        side = self.toobit._position_side(item)
        return symbol, side

    def _open_positions(self, rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for item in rows:
            if self.toobit._position_qty(item) <= 0:
                continue
            out[self._position_key(item)] = item
        return out

    @staticmethod
    def _actual_entry(item: dict[str, Any]) -> float | None:
        for key in ("avgPrice", "entryPrice", "avgEntryPrice", "openPrice", "price"):
            value = safe_float(item.get(key))
            if value > 0:
                return value
        return None

    @staticmethod
    def _result_from_history(signal: dict[str, Any], realized: dict[str, Any]) -> str:
        raw = realized.get("raw") or {}
        text = " ".join(str(raw.get(k) or "") for k in ("type", "orderType", "stopOrderType", "closeType", "clientOrderId", "remark")).upper()
        if "TAKE" in text or "TP" in text:
            return "TP"
        if "STOP_LOSS" in text or "SL" in text:
            return "STOP"
        close = safe_float(realized.get("close_price"))
        tp = float(signal.get("tp") or 0)
        sl = float(signal.get("sl") or 0)
        entry = float(signal.get("entry") or 0)
        tolerance = max(entry * 0.0015, abs(tp - entry) * 0.15, abs(sl - entry) * 0.15)
        if close > 0:
            if abs(close - tp) <= tolerance:
                return "TP"
            if abs(close - sl) <= tolerance:
                return "STOP"
        # A manual or unknown close must never increment the two-stop rule.
        return "MANUAL_CLOSE"

    def _confirm_pending_rows(
        self,
        pending_rows: list[dict[str, Any]],
        open_map: dict[tuple[str, str], dict[str, Any]],
        now: int,
    ) -> dict[str, int]:
        counts = {"confirmed": 0, "failed": 0, "closed": 0, "pending": 0}
        for pos in pending_rows:
            signal = self.storage.runtime.get_signal(int(pos["signal_id"]))
            if not signal:
                continue
            counts["pending"] += 1
            confirm_after = int(pos.get("confirm_after") or 0)
            if confirm_after <= 0 or now < confirm_after:
                continue
            key = (canonical_base_from_symbol(str(pos["toobit_symbol"])), str(pos["side"]).upper())
            exchange_pos = open_map.get(key)
            if exchange_pos:
                actual_entry = self._actual_entry(exchange_pos)
                changes: dict[str, Any] = {
                    "status": "OPEN",
                    "opened_at": now,
                    "last_seen_at": now,
                    "position_snapshot": exchange_pos,
                }
                if actual_entry:
                    changes["actual_entry"] = actual_entry
                    requested = float(signal.get("entry") or actual_entry)
                    changes["entry_slippage_rate"] = abs(actual_entry - requested) / requested if requested > 0 else 0
                self.storage.runtime.update_position(signal["id"], **changes)
                self.storage.runtime.update_signal(signal["id"], **changes)
                self.notifications.put({"type": "position_open", "signal_id": signal["id"]})
                counts["confirmed"] += 1
            else:
                # A process restart can happen after the position opened and even closed
                # inside the 70-second window. Recover that definitive Toobit result before
                # declaring FAILED_OPEN, otherwise a real TP/Stop and its PnL would be lost.
                realized = None
                finder = getattr(self.toobit, "find_realized_result", None)
                if callable(finder):
                    try:
                        realized = finder(
                            symbol=str(pos["toobit_symbol"]),
                            side=str(pos["side"]),
                            start_ms=int(
                                pos.get("submitted_at")
                                or pos.get("reserved_at")
                                or signal.get("created_at")
                                or now
                            ),
                            end_ms=now,
                            order_id=str(pos.get("order_id") or signal.get("order_id") or "") or None,
                            client_order_id=str(
                                pos.get("client_order_id")
                                or signal.get("client_order_id")
                                or ""
                            ) or None,
                        )
                    except Exception as exc:
                        logger.warning(
                            "PENDING_HISTORY_WAIT | %s | %s",
                            signal["canonical"], str(exc)[:200],
                        )
                if realized:
                    result = self._result_from_history(signal, realized)
                    pnl = safe_float(realized.get("pnl"))
                    close_price = safe_float(realized.get("close_price")) or None
                    closed_at = safe_int(realized.get("close_time_ms"), now)
                    final = self.storage.runtime.finalize_signal(
                        signal["id"], result, close_price, pnl, closed_at=closed_at,
                        metadata={
                            "toobit_realized": realized,
                            "recovered_before_open_confirmation": True,
                        },
                    )
                    if final:
                        if result in {"TP", "STOP"}:
                            self.result_queue.put(signal["id"])
                        else:
                            self.notifications.put({"type": "result", "signal_id": signal["id"]})
                        counts["closed"] += 1
                    continue

                final = self.storage.runtime.finalize_signal(
                    signal["id"], "FAILED_OPEN", None, None,
                    metadata={"reason": "NO_POSITION_OR_REALIZED_RESULT_AFTER_70_SECONDS"},
                )
                if final:
                    self.notifications.put({"type": "failed_open", "signal_id": signal["id"]})
                    counts["failed"] += 1
        return counts

    def confirm_due(self) -> dict[str, int]:
        """Check only PENDING_OPEN rows whose 70-second deadline has arrived.

        The worker may call this frequently because it performs no network request until
        at least one deadline is due. This gives a near-exact 70-second confirmation
        without increasing the normal 60-second result/account polling rate.
        """
        now = now_ms()
        pending = [
            row for row in self.storage.runtime.positions(statuses=("PENDING_OPEN",))
            if int(row.get("confirm_after") or 0) > 0
            and now >= int(row.get("confirm_after") or 0)
        ]
        if not pending:
            return {"confirmed": 0, "failed": 0, "closed": 0, "pending": 0}
        if not self.toobit.has_credentials:
            return {"confirmed": 0, "failed": 0, "closed": 0, "pending": len(pending)}
        with self._api_lock:
            try:
                open_map = self._open_positions(self.toobit.get_positions())
            except Exception as exc:
                self.storage.runtime.set_health("real_confirm", "warning", f"Toobit API: {exc}")
                return {"confirmed": 0, "failed": 0, "closed": 0, "pending": len(pending)}
            counts = self._confirm_pending_rows(pending, open_map, now)
            self.storage.runtime.set_health(
                "real_confirm", "ok",
                f"confirmed={counts['confirmed']} failed={counts['failed']} closed={counts['closed']} pending={counts['pending']}",
            )
            return counts

    def tick(self) -> dict[str, int]:
        if not self.toobit.has_credentials:
            self.storage.runtime.save_account_snapshot(False, {}, "کلید API توبیت تنظیم نشده")
            self.storage.runtime.set_health("real_monitor", "warning", "Toobit credentials missing")
            return {"confirmed": 0, "closed": 0, "pending": 0}
        with self._api_lock:
            try:
                position_rows = self.toobit.get_positions()
                open_map = self._open_positions(position_rows)
                balance = self.toobit.get_usdt_balance_summary()
                open_position_keys = sorted(
                    f"{base}USDT:{side}" for base, side in open_map
                )
                balance.update({
                    "open_positions": len(open_map),
                    "open_position_keys": open_position_keys,
                })
                self.storage.runtime.save_account_snapshot(True, balance)
            except Exception as exc:
                self.storage.runtime.save_account_snapshot(False, self.storage.runtime.account_snapshot(), str(exc))
                self.storage.runtime.set_health("real_monitor", "warning", f"Toobit API: {exc}")
                # API error changes neither slot nor result.
                return {"confirmed": 0, "closed": 0, "pending": len(self.storage.runtime.positions())}

        counts = {"confirmed": 0, "closed": 0, "pending": 0}
        now = now_ms()
        for pos in self.storage.runtime.positions(statuses=("PENDING_OPEN", "OPEN")):
            signal = self.storage.runtime.get_signal(int(pos["signal_id"]))
            if not signal:
                continue
            key = (canonical_base_from_symbol(str(pos["toobit_symbol"])), str(pos["side"]).upper())
            exchange_pos = open_map.get(key)
            if pos["status"] == "PENDING_OPEN":
                pending_counts = self._confirm_pending_rows([pos], open_map, now)
                counts["pending"] += pending_counts["pending"]
                counts["confirmed"] += pending_counts["confirmed"]
                counts["closed"] += pending_counts["closed"]
                continue

            if exchange_pos:
                self.storage.runtime.update_position(signal["id"], last_seen_at=now, position_snapshot=exchange_pos)
                continue

            # Position disappeared from a successful batch read: query realized result.
            try:
                realized = self.toobit.find_realized_result(
                    symbol=str(pos["toobit_symbol"]),
                    side=str(pos["side"]),
                    start_ms=int(
                        pos.get("opened_at")
                        or pos.get("submitted_at")
                        or pos.get("reserved_at")
                        or signal.get("created_at")
                        or now
                    ),
                    end_ms=now,
                    order_id=str(pos.get("order_id") or signal.get("order_id") or "") or None,
                    client_order_id=str(
                        pos.get("client_order_id")
                        or signal.get("client_order_id")
                        or ""
                    ) or None,
                )
            except Exception as exc:
                logger.warning("REAL_RESULT_WAIT | %s | %s", signal["canonical"], str(exc)[:200])
                continue
            if not realized:
                # Keep slot until Toobit provides a definitive result; retry next 60-second cycle.
                self.storage.runtime.update_position(signal["id"], result_waiting_since=pos.get("result_waiting_since") or now)
                continue
            result = self._result_from_history(signal, realized)
            pnl = safe_float(realized.get("pnl"))
            close_price = safe_float(realized.get("close_price")) or None
            closed_at = safe_int(realized.get("close_time_ms"), now)
            final = self.storage.runtime.finalize_signal(
                signal["id"], result, close_price, pnl, closed_at=closed_at,
                metadata={"toobit_realized": realized},
            )
            if final:
                if result in {"TP", "STOP"}:
                    self.result_queue.put(signal["id"])
                else:
                    self.notifications.put({"type": "result", "signal_id": signal["id"]})
                counts["closed"] += 1

        self.storage.runtime.set_health("real_monitor", "ok", f"open={len(open_map)} confirmed={counts['confirmed']} closed={counts['closed']}")
        return counts
