from __future__ import annotations

from monitoring_result_4h import MonitoringResult4H
from okx_data import OkxDataClient
from storage import Storage, StoredSignal
from utils import logger


class SignalMonitor:
    def __init__(self, storage: Storage, okx: OkxDataClient, toobit=None) -> None:
        self.storage = storage
        self.okx = okx
        # Toobit در مانیتور خودکار صدا زده نمی‌شود؛ مانیتور TP/SL فقط با قیمت OKX کار می‌کند.
        self.result_engine = MonitoringResult4H(storage)

    def check_once(self, send_result) -> None:
        for signal in self.storage.open_signals():
            try:
                price = self.okx.get_last_price(signal.symbol)
                self.storage.update_excursions(signal.id, price)
                status = self.result_engine.check_price_hit(signal, price)
                if status is not None:
                    exit_price = signal.tp_price if status == "TP" else signal.sl_price
                    result = self.result_engine.build_result(signal, status, exit_price, reason="OKX monitor hit TP/SL.")
                    msg_id = send_result(signal, result)
                    self.storage.finish_signal(
                        signal.id,
                        status=result.status,
                        exit_price=result.exit_price,
                        approx_pnl=result.approx_pnl,
                        net_pnl=result.net_pnl,
                        real_pnl=result.real_pnl,
                        result_message_id=msg_id,
                        close_reason=result.reason,
                    )
                    continue
            except Exception as exc:
                logger.warning("مانیتورینگ سیگنال #%s خطا داد و ادامه پیدا کرد: %s", signal.id, exc)
