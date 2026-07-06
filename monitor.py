from __future__ import annotations

from monitoring_result_4h import MonitoringResult4H
from okx_data import OkxDataClient
from storage import Storage, StoredSignal
from toobit_client import ToobitClient
from utils import logger


class SignalMonitor:
    def __init__(self, storage: Storage, okx: OkxDataClient, toobit: ToobitClient) -> None:
        self.storage = storage
        self.okx = okx
        self.toobit = toobit
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

                external = self.result_engine.try_real_closed_on_toobit(self.toobit, signal)
                if external is not None:
                    msg_id = send_result(signal, external)
                    self.storage.finish_signal(
                        signal.id,
                        status=external.status,
                        exit_price=external.exit_price,
                        approx_pnl=external.approx_pnl,
                        net_pnl=external.net_pnl,
                        real_pnl=external.real_pnl,
                        result_message_id=msg_id,
                        close_reason=external.reason,
                    )
            except Exception as exc:
                logger.warning("مانیتورینگ سیگنال #%s خطا داد و ادامه پیدا کرد: %s", signal.id, exc)
