"""مانیتور نتیجه سیگنال‌های واقعی از Toobit و عادی از OKX."""
from __future__ import annotations

import math
import time

import config
from okx_client import OKXClient
from storage import Storage
from toobit_client import ToobitFuturesClient


class Monitor:
    def __init__(self, okx: OKXClient, toobit: ToobitFuturesClient, storage: Storage, telegram=None, health=None):
        self.okx = okx
        self.toobit = toobit
        self.storage = storage
        self.telegram = telegram
        self.health = health

    @staticmethod
    def _fallback_pnl(sig: dict, exit_price: float) -> tuple[float, float, float]:
        entry = float(sig.get("entry_real") or sig["entry"])
        notional = float(sig.get("notional") or 0.0)
        if entry <= 0 or notional <= 0:
            return 0.0, 0.0, 0.0
        move = ((exit_price - entry) / entry) if sig["side"] == "LONG" else ((entry - exit_price) / entry)
        gross = notional * move
        quantity = notional / entry
        exit_notional = quantity * exit_price
        fees = (
            notional * (config.TAKER_FEE_PCT_PER_SIDE / 100.0)
            + exit_notional * (config.TAKER_FEE_PCT_PER_SIDE / 100.0)
            + (notional + exit_notional) * (config.SLIPPAGE_PCT_PER_SIDE / 100.0)
        )
        return gross, fees, gross - fees

    def _result_message(self, sig: dict, reason: str, exit_price: float, gross: float, fee: float, net: float, mfe: float, mae: float) -> str:
        self.storage.roll_profit_day()
        today_after = float(self.storage.get("profit_today", 0.0)) + net
        total_after = float(self.storage.get("profit_total", 0.0)) + net
        return (
            f"{'✅ TP خورد' if reason == 'TP' else '❌ SL خورد'}\n\n"
            f"#{sig['id']} | {sig['symbol_id']} | {sig['side']} | {'واقعی' if int(sig.get('is_real') or 0) else 'عادی'}\n"
            f"Entry: {float(sig.get('entry_real') or sig['entry']):.8g}\nExit: {exit_price:.8g}\n"
            f"PnL خام: {gross:.4f} USDT\nهزینه: {fee:.4f} USDT\nPnL خالص: {net:.4f} USDT\n"
            f"MFE: {mfe:.3f}% | MAE: {mae:.3f}%\nسود امروز: {today_after:.4f} USDT\nسود کل: {total_after:.4f} USDT"
        )

    def _close(self, sig: dict, reason: str, exit_price: float, gross: float, fee: float, net: float, mfe: float, mae: float) -> None:
        current = self.storage.get_signal(int(sig["id"]))
        if not current or current.get("status") == "closed":
            return
        message = self._result_message(current, reason, exit_price, gross, fee, net, mfe, mae)
        self.storage.close_signal(int(sig["id"]), exit_price, gross, fee, net, reason, mfe, mae)
        if self.telegram:
            self.telegram.send_message(message, reply_to_message_id=current.get("message_id"))

    def _monitor_virtual(self, sig: dict) -> None:
        candles = self.okx.get_candles(sig["okx_symbol"], bar=config.OKX_PRIMARY_BAR, limit=config.VIRTUAL_MONITOR_RECENT_LIMIT)
        created_ms = int(sig["created_at"]) * 1000
        relevant = [c for c in candles if int(c["ts"]) >= created_ms]
        if not relevant and candles:
            relevant = candles
        if not relevant:
            return
        entry, tp, sl = float(sig["entry"]), float(sig["tp"]), float(sig["sl"])
        side = str(sig["side"])
        mfe, mae = float(sig.get("mfe") or 0.0), float(sig.get("mae") or 0.0)
        reason, exit_price = "", 0.0
        for candle in relevant:
            if side == "LONG":
                mfe = max(mfe, (float(candle["high"]) - entry) / entry * 100.0)
                mae = min(mae, (float(candle["low"]) - entry) / entry * 100.0)
                hit_tp, hit_sl = float(candle["high"]) >= tp, float(candle["low"]) <= sl
            else:
                mfe = max(mfe, (entry - float(candle["low"])) / entry * 100.0)
                mae = min(mae, (entry - float(candle["high"])) / entry * 100.0)
                hit_tp, hit_sl = float(candle["low"]) <= tp, float(candle["high"]) >= sl
            if hit_tp and hit_sl:
                reason, exit_price = "SL", sl
                break
            if hit_sl:
                reason, exit_price = "SL", sl
                break
            if hit_tp:
                reason, exit_price = "TP", tp
                break
        if reason:
            gross, fee, net = self._fallback_pnl(sig, exit_price)
            self._close(sig, reason, exit_price, gross, fee, net, mfe, mae)
        else:
            self.storage.update_signal(int(sig["id"]), mfe=mfe, mae=mae)

    def _close_from_toobit_result(self, sig: dict, result: dict) -> bool:
        exit_price = float(result["exit_price"])
        reason = "TP" if abs(exit_price - float(sig["tp"])) <= abs(exit_price - float(sig["sl"])) else "SL"
        realized = result.get("realized_pnl")
        fee = abs(float(result.get("fee") or 0.0))
        if isinstance(realized, (int, float)) and not math.isnan(float(realized)):
            gross = float(realized)
            net = gross - fee
        else:
            gross, fallback_fee, net = self._fallback_pnl(sig, exit_price)
            if fee <= 0:
                fee = fallback_fee
            net = gross - fee
        self._close(sig, reason, exit_price, gross, fee, net, float(sig.get("mfe") or 0.0), float(sig.get("mae") or 0.0))
        return True

    def reconcile_pending_real(self, signal_id: int) -> str:
        sig = self.storage.get_signal(signal_id)
        if not sig or sig.get("status") != "pending" or not int(sig.get("is_real") or 0):
            return "not_pending"
        if self.toobit.check_position_opened(sig["toobit_symbol"]):
            self.storage.update_signal(signal_id, status="open", opened_at=int(time.time()), entry_real=float(sig["entry"]))
            self.storage.clear_health_component("toobit_position", sig.get("symbol_id"))
            return "opened"
        opened_ms = int(sig.get("created_at") or time.time()) * 1000
        result = self.toobit.get_closed_trade_result(sig["toobit_symbol"], sig["side"], opened_ms)
        if result:
            self._close_from_toobit_result(sig, result)
            self.storage.clear_health_component("toobit_position", sig.get("symbol_id"))
            return "closed"
        return "not_found"

    def _monitor_real(self, sig: dict) -> None:
        if self.toobit.check_position_opened(sig["toobit_symbol"]):
            return
        opened_ms = int(sig.get("opened_at") or sig["created_at"]) * 1000
        result = self.toobit.get_closed_trade_result(sig["toobit_symbol"], sig["side"], opened_ms)
        if result:
            self._close_from_toobit_result(sig, result)

    def run_once(self) -> None:
        had_error = False
        for sig in self.storage.get_open_signals():
            try:
                if sig["status"] == "pending":
                    if int(sig.get("is_real") or 0) and time.time() - int(sig["created_at"]) >= config.ORDER_OPEN_CHECK_SECONDS:
                        self.reconcile_pending_real(int(sig["id"]))
                    continue
                if int(sig.get("is_real") or 0):
                    self._monitor_real(sig)
                else:
                    self._monitor_virtual(sig)
                self.storage.clear_health_component("monitor", sig.get("symbol_id"))
            except Exception as exc:
                had_error = True
                self.storage.add_health_event("monitor", "warning", str(exc), sig.get("symbol_id"))
        if not had_error:
            self.storage.clear_health_component("monitor")
        if self.health:
            self.health.mark("monitor")
