from __future__ import annotations

import math
import time
from typing import Any

import config


class Monitor:
    def __init__(self, okx, toobit, storage, telegram, experience, exchange_lock=None):
        self.okx = okx
        self.toobit = toobit
        self.storage = storage
        self.telegram = telegram
        self.experience = experience
        import threading
        self.exchange_lock = exchange_lock or threading.RLock()

    def run_once(self) -> None:
        for signal in self.storage.get_open_signals():
            try:
                if signal["is_real"]:
                    self._real(signal)
                else:
                    self._virtual(signal)
                self.storage.resolve_health("monitor", signal["symbol_id"])
            except Exception as exc:
                self.storage.add_health_event("monitor", "warning", str(exc), signal["symbol_id"])
        self._retry_unsent_results()
        self.storage.set("monitor_last_ts", int(time.time()))

    def _virtual_metrics(self, s: dict[str, Any], until_ts_ms: int | None = None) -> tuple[list[dict[str, float]], float, float]:
        candles = self.okx.get_candles(s["okx_symbol"], bar="1m", limit=300)
        start_ms = int(s["opened_at"] or s["created_at"]) * 1000
        reference_entry = float(s.get("real_entry") or s["entry"])
        mfe_pct, mae_pct = self.okx.max_favorable_adverse(candles, s["side"], reference_entry, start_ms, until_ts_ms)
        risk_pct = abs(reference_entry - float(s["sl"])) / reference_entry * 100.0 if reference_entry else 0.0
        mfe_r = mfe_pct / risk_pct if risk_pct else 0.0
        mae_r = mae_pct / risk_pct if risk_pct else 0.0
        return candles, mfe_r, mae_r

    def _virtual(self, s: dict[str, Any]) -> None:
        candles = self.okx.get_candles(s["okx_symbol"], bar="1m", limit=300)
        start_ms = int(s["opened_at"] or s["created_at"]) * 1000
        outcome, price, hit_ts = self.okx.reached_tp_or_sl(candles, s["side"], s["tp"], s["sl"], start_ms)
        _, mfe_r, mae_r = self._virtual_metrics(s, hit_ts)
        self.storage.update_signal(s["id"], mfe_r=mfe_r, mae_r=mae_r)
        if not outcome:
            max_age = int(config.VIRTUAL_MONITOR_MAX_MINUTES * 60)
            if int(time.time()) - int(s["opened_at"] or s["created_at"]) > max_age:
                last_price = float(candles[-1]["close"]) if candles else float(s["entry"])
                notional = float(s.get("notional_usdt") or 0)
                directional = ((last_price - float(s["entry"])) / float(s["entry"])) if s["side"] == "LONG" else ((float(s["entry"]) - last_price) / float(s["entry"]))
                estimated_cost = float(
                    (s.get("estimated_cost_win") if directional >= 0 else s.get("estimated_cost_loss"))
                    or s.get("estimated_cost")
                    or 0
                )
                net = directional * notional - estimated_cost
                self._close(s, "EXPIRED", last_price, net, estimated_cost, mfe_r, mae_r)
                self.storage.resolve_health("virtual_timeout", s["symbol_id"])
            return
        gross = abs(float(price) - float(s["entry"])) / float(s["entry"]) * float(s["notional_usdt"])
        if outcome == "SL":
            gross = -gross
            cost = float(s.get("estimated_cost_loss") or s.get("estimated_cost") or 0)
        else:
            cost = float(s.get("estimated_cost_win") or s.get("estimated_cost") or 0)
        self._close(s, outcome, float(price), gross - cost, cost, mfe_r, mae_r)

    @staticmethod
    def _position_matches_side(position: dict[str, Any], side: str) -> bool:
        side_u = side.upper()
        position_side = str(position.get("positionSide") or position.get("side") or position.get("position_side") or "").upper()
        if position_side in {"LONG", "SHORT"}:
            return position_side == side_u
        try:
            amount = float(position.get("positionAmt") or position.get("size") or position.get("qty") or position.get("quantity") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if amount == 0:
            return False
        return amount > 0 if side_u == "LONG" else amount < 0

    def _matching_open_position(self, s: dict[str, Any]) -> dict[str, Any] | None:
        with self.exchange_lock:
            positions = self.toobit.get_open_positions(s["toobit_symbol"])
        return next((position for position in positions if self._position_matches_side(position, s["side"])), None)

    @staticmethod
    def _position_entry_price(position: dict[str, Any]) -> float:
        for key in ("entryPrice", "avgEntryPrice", "averageOpenPrice", "openPrice", "avgPrice"):
            try:
                value = float(position.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return 0.0

    def _real(self, s: dict[str, Any]) -> None:
        # Keep virtual MFE/MAE reference for diagnosis, but never use OKX to decide a real result.
        try:
            _, mfe_r, mae_r = self._virtual_metrics(s)
            self.storage.update_signal(s["id"], mfe_r=mfe_r, mae_r=mae_r)
        except Exception:
            mfe_r = float(s.get("mfe_r") or 0)
            mae_r = float(s.get("mae_r") or 0)

        position = self._matching_open_position(s)
        if position:
            real_entry = self._position_entry_price(position)
            updates = {"real_open_confirmed": 1}
            if s["status"] != "open":
                updates["status"] = "open"
            if real_entry > 0:
                updates["real_entry"] = real_entry
            self.storage.update_signal(s["id"], **updates)
            self.storage.resolve_health("real_pending", s["symbol_id"])
            return

        opened_ms = int(s["opened_at"] or s["created_at"]) * 1000
        # Do not inspect close history until the real position has been observed open at least once.
        # This prevents a filled opening order from being mistaken for a closing order.
        if not int(s.get("real_open_confirmed") or 0):
            age = int(time.time()) - int(s["opened_at"] or s["created_at"])
            if s["status"] == "pending" and age > config.REAL_PENDING_TIMEOUT_SECONDS:
                self.storage.add_health_event(
                    "real_pending",
                    "critical",
                    "وضعیت سفارش واقعی نامشخص است؛ برای ایمنی در حالت pending باقی ماند",
                    s["symbol_id"],
                )
            return

        with self.exchange_lock:
            result = self.toobit.get_closed_trade_result(s["toobit_symbol"], s["side"], opened_ms)
        if result:
            try:
                _, mfe_r, mae_r = self._virtual_metrics(s, int(result.get("time_ms") or 0) or None)
            except Exception:
                pass
            realized = result.get("realized_pnl")
            fee = abs(float(result.get("fee") or 0))
            reference_entry = float(s.get("real_entry") or s["entry"])
            favorable = (s["side"] == "LONG" and result["exit_price"] > reference_entry) or (
                s["side"] == "SHORT" and result["exit_price"] < reference_entry
            )
            if isinstance(realized, (int, float)) and not math.isnan(float(realized)):
                net = float(realized) if config.TOOBIT_REALIZED_PNL_INCLUDES_FEES else float(realized) - fee
            else:
                gross = abs(result["exit_price"] - reference_entry) / reference_entry * s["notional_usdt"]
                net = (gross - fee) if favorable else -(gross + fee)
            self._close(
                s,
                "TP" if favorable else "SL",
                float(result["exit_price"]),
                net,
                fee,
                mfe_r,
                mae_r,
            )
            return



    def _close(self, s: dict[str, Any], outcome: str, exit_price: float, net: float, fees: float, mfe_r: float, mae_r: float) -> None:
        if not self.storage.close_signal(
            s["id"],
            outcome=outcome,
            exit_price=exit_price,
            net_pnl=net,
            fees=fees,
            mfe_r=mfe_r,
            mae_r=mae_r,
        ):
            return
        fresh = self.storage.get_signal(s["id"])
        exp = self.experience.analyze(
            fresh,
            {"outcome": outcome, "mfe_r": mfe_r, "mae_r": mae_r, "net_pnl": net},
        )
        exp["symbol_id"] = s["symbol_id"]
        self.storage.add_experience(exp)
        self._send_result_message(fresh, exp)

    def _format_result_message(self, s: dict[str, Any], exp: dict[str, Any]) -> str:
        outcome = str(s.get("outcome") or "")
        icon = "✅" if outcome == "TP" else "❌" if outcome == "SL" else "⌛"
        title = "TP خورد" if outcome == "TP" else "SL خورد" if outcome == "SL" else "معامله منقضی شد"
        net = float(s.get("net_pnl") or 0)
        return (
            f"{icon} {title}\n\n"
            f"#{s['id']} | {s['symbol_id']} | {s['side']}\n"
            f"نوع: {'واقعی' if s['is_real'] else 'مجازی'}\n\n"
            f"Entry: {float(s.get('real_entry') or s['entry']):.8g}\nExit: {float(s.get('exit_price') or 0):.8g}\n"
            f"کارمزد/اسلیپیج: {float(s.get('fees') or 0):.4f} USDT\n"
            f"{'سود' if net >= 0 else 'زیان'} خالص: {net:.4f} USDT\n"
            f"MFE: {float(s.get('mfe_r') or 0):.2f}R | MAE: {float(s.get('mae_r') or 0):.2f}R\n\n"
            f"تحلیل: {self._cause_fa(exp.get('primary_cause'))}"
        )

    @staticmethod
    def _cause_fa(cause: str | None) -> str:
        labels = {
            "CLEAN_WIN": "برد تمیز؛ جهت و ورود مناسب بود",
            "HIGH_MAE_WIN": "برد با نوسان مخالف زیاد",
            "DIRECTION_ERROR": "احتمال خطای جهت",
            "ENTRY_TOO_EARLY_OR_STOP_TOO_TIGHT": "ورود زود یا استاپ نزدیک",
            "ENTRY_TOO_LATE": "ورود دیرهنگام",
            "NO_FOLLOW_THROUGH": "حرکت ادامه کافی نداشت",
            "NO_RESOLUTION_WITHIN_TIME_LIMIT": "معامله در زمان مجاز تعیین تکلیف نشد",
            "UNCLASSIFIED": "علت قطعی مشخص نشد",
        }
        return labels.get(str(cause or "UNCLASSIFIED"), str(cause or "UNCLASSIFIED"))

    def _send_result_message(self, s: dict[str, Any], exp: dict[str, Any]) -> bool:
        message_id = self.telegram.send_message(self._format_result_message(s, exp), reply_to_message_id=s.get("message_id"))
        if message_id:
            self.storage.update_signal(s["id"], result_message_sent=1, result_retry_at=0)
            return True
        retry_count = int(s.get("result_retry_count") or 0) + 1
        self.storage.schedule_result_retry(s["id"], retry_count)
        return False

    def _retry_unsent_results(self) -> None:
        for signal in self.storage.get_unsent_closed_signals(20):
            exp = self.storage.get_experience_for_signal(signal["id"]) or {"primary_cause": "UNCLASSIFIED"}
            self._send_result_message(signal, exp)
