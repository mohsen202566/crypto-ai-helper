from __future__ import annotations

import time
from typing import Iterable

import config
from storage import Storage
from utils import logger, safe_float


class RuntimeSafety:
    """Crash-safe runtime laws for the simple 5M scalper.

    SLOT_RECHECK_SECONDS means exactly this: when local real slots are full, wait
    70 seconds, check Toobit open positions, and free only the positions that
    Toobit no longer shows as open. It is not a generic error cooldown.
    """

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def limited_watchlist(self) -> tuple[str, ...]:
        return tuple(config.WATCHLIST[: int(config.MAX_WATCH_SYMBOLS)])

    def can_scan_coin(self, symbol: str) -> bool:
        return not self.storage.coin_in_cooldown(symbol)

    def record_coin_error(self, symbol: str, exc: Exception) -> None:
        self.storage.record_coin_error(symbol, str(exc), config.COIN_ERROR_COOLDOWN_SECONDS)
        logger.warning("خطای ارز %s ثبت شد، اسکن بقیه ارزها ادامه دارد: %s", symbol, exc)

    def clear_coin_error(self, symbol: str) -> None:
        self.storage.clear_coin_error(symbol)

    def can_open_real_now(self, toobit_client, *, max_positions: int) -> bool:
        if self.storage.free_real_slots(max_positions) > 0:
            return True
        return self.recheck_full_slots_after_70s(toobit_client, max_positions=max_positions)

    def recheck_full_slots_after_70s(self, toobit_client, *, max_positions: int) -> bool:
        now = int(time.time())
        last = int(float(self.storage.runtime_get("last_slot_recheck_at", "0") or 0))
        if now - last < int(config.SLOT_RECHECK_SECONDS):
            return False
        self.storage.runtime_set("last_slot_recheck_at", now)

        try:
            positions = toobit_client.get_positions()
        except Exception as exc:
            self.storage.runtime_set("last_slot_recheck_error", str(exc)[:500])
            logger.warning("چک 70 ثانیه‌ای اسلات با Toobit ناموفق بود: %s", exc)
            return False

        open_symbols = self._open_toobit_symbols(toobit_client, positions)
        local_reals = self.storage.active_real_signals()
        for sig in local_reals:
            if sig.toobit_symbol.upper() not in open_symbols:
                self.storage.release_real_slot_external(sig.id, "After 70s slot recheck, Toobit no longer shows this position.")

        open_count = len(open_symbols)
        self.storage.runtime_set("last_toobit_open_count", open_count)
        self.storage.runtime_set("last_toobit_open_symbols", ",".join(sorted(open_symbols)))
        if open_count >= int(max_positions):
            return False
        return self.storage.free_real_slots(max_positions) > 0 or open_count < int(max_positions)

    @staticmethod
    def _open_toobit_symbols(toobit_client, positions: Iterable[dict]) -> set[str]:
        result: set[str] = set()
        for item in positions or []:
            try:
                qty = toobit_client._position_qty(item)
                symbol = toobit_client._symbol_from_item(item)
            except Exception:
                qty = safe_float(item.get("position") or item.get("positionAmt") or item.get("size") or item.get("quantity") or item.get("qty"))
                symbol = str(item.get("symbol") or item.get("symbolId") or item.get("symbolName") or "").upper()
            if qty > 0 and symbol:
                result.add(symbol.upper())
        return result
