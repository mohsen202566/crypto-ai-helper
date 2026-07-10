"""مانیتورینگ نتیجه سیگنال‌ها.
Real از Toobit چک می‌شود؛ Virtual از OKX. نتیجه باید روی پیام سیگنال اصلی ریپلای شود.
"""
from __future__ import annotations

import logging
import time

import config
from okx_client import OKXClient
from storage import Storage
from toobit_client import ToobitFuturesClient, safe_float

logger = logging.getLogger("futures_hunt_2.monitor")

class Monitor:
    def __init__(self, okx: OKXClient, toobit: ToobitFuturesClient, storage: Storage, telegram=None):
        self.okx = okx
        self.toobit = toobit
        self.storage = storage
        self.telegram = telegram

    def _pnl(self, side: str, entry: float, exit_price: float, trade_usdt: float, leverage: int) -> tuple[float, float, float]:
        notional = trade_usdt * leverage
        if side == "LONG":
            gross = notional * ((exit_price - entry) / entry)
        else:
            gross = notional * ((entry - exit_price) / entry)
        fee = notional * ((config.FALLBACK_FEE_PCT_PER_SIDE * 2.0 + config.SLIPPAGE_PCT_PER_SIDE * 2.0) / 100.0)
        return gross, fee, gross - fee

    def _send_result(self, sig: dict, reason: str, exit_price: float, net: float, gross: float, fee: float, mfe: float = 0.0, mae: float = 0.0):
        if not self.telegram:
            return
        icon = "✅" if reason == "TP" else "❌"
        title = "TP خورد" if reason == "TP" else "SL خورد"
        text = (
            f"{icon} {title}\n\n"
            f"#{sig['id']} | {sig['symbol_id']} | {sig['side']}\n"
            f"Entry: {sig['entry']:.8g}\n"
            f"Exit: {exit_price:.8g}\n"
            f"PnL خام: {gross:.4f} USDT\n"
            f"کارمزد/اسلیپیج تخمینی: {fee:.4f} USDT\n"
            f"PnL خالص: {net:.4f} USDT\n"
            f"MFE: {mfe:.3f}% | MAE: {mae:.3f}%\n"
            f"close_reason: {reason}"
        )
        self.telegram.send_message(text, reply_to_message_id=sig.get("message_id"))

    def check_virtual(self, sig: dict) -> None:
        candles = self.okx.get_candles(sig["okx_symbol"], limit=120)
        reason, exit_price, ts = self.okx.reached_tp_or_sl(candles, sig["side"], float(sig["tp"]), float(sig["sl"]), int(sig["created_at"]) * 1000)
        if not reason or exit_price is None:
            if time.time() - int(sig["created_at"]) > config.VIRTUAL_MONITOR_MAX_MINUTES * 60:
                self.storage.update_signal(sig["id"], status="closed", closed_at=int(time.time()), close_reason="TIMEOUT")
            return
        trade_usdt = float(self.storage.get("trade_usdt", config.TRADE_USDT_DEFAULT))
        leverage = int(self.storage.get("leverage", config.LEVERAGE_DEFAULT))
        gross, fee, net = self._pnl(sig["side"], float(sig["entry"]), float(exit_price), trade_usdt, leverage)
        mfe, mae = self.okx.max_favorable_adverse(candles, sig["side"], float(sig["entry"]), int(sig["created_at"]) * 1000)
        self.storage.update_signal(sig["id"], status="closed", closed_at=int(time.time()), exit_price=exit_price, gross_pnl=gross, fee_usdt=fee, net_pnl=net, close_reason=reason, mfe=mfe, mae=mae)
        self.storage.add_profit(net)
        logger.info("SIGNAL_CLOSED id=%s symbol=%s mode=virtual result=%s net=%.4f exit=%.8g mfe=%.3f mae=%.3f", sig["id"], sig["symbol_id"], reason, net, exit_price, mfe, mae)
        self._send_result(sig, reason, exit_price, net, gross, fee, mfe, mae)

    def check_real(self, sig: dict) -> None:
        # اگر پوزیشن هنوز باز است، نتیجه قطعی نشده. اگر دیگر باز نیست، از order history/آخرین قیمت خروج تقریبی می‌گیریم.
        opened = self.toobit.check_position_opened(sig["toobit_symbol"])
        if opened:
            return
        # پوزیشن بسته شده؛ نتیجه را با نزدیک‌ترین قیمت OKX تخمین می‌زنیم اگر API history دقیق موجود نبود.
        exit_price = self.okx.get_last_price(sig["okx_symbol"])
        reason = "TP" if ((sig["side"] == "LONG" and exit_price >= float(sig["tp"])) or (sig["side"] == "SHORT" and exit_price <= float(sig["tp"]))) else "SL"
        trade_usdt = float(self.storage.get("trade_usdt", config.TRADE_USDT_DEFAULT))
        leverage = int(self.storage.get("leverage", config.LEVERAGE_DEFAULT))
        gross, fee, net = self._pnl(sig["side"], float(sig.get("entry_real") or sig["entry"]), exit_price, trade_usdt, leverage)
        self.storage.update_signal(sig["id"], status="closed", closed_at=int(time.time()), exit_price=exit_price, gross_pnl=gross, fee_usdt=fee, net_pnl=net, close_reason=reason)
        self.storage.add_profit(net)
        logger.info("SIGNAL_CLOSED id=%s symbol=%s mode=real result=%s net=%.4f exit=%.8g", sig["id"], sig["symbol_id"], reason, net, exit_price)
        self._send_result(sig, reason, exit_price, net, gross, fee)

    def tick(self) -> None:
        for sig in self.storage.get_open_signals():
            try:
                if int(sig.get("is_real") or 0):
                    self.check_real(sig)
                else:
                    self.check_virtual(sig)
            except Exception as exc:
                logger.warning("MONITOR_SIGNAL_ERROR id=%s symbol=%s error=%s", sig.get("id"), sig.get("symbol_id"), exc)
                self.storage.add_health_event("monitor", "warning", f"monitor failed: {exc}", sig.get("symbol_id"))
                continue
