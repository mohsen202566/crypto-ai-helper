from __future__ import annotations

import time
from typing import Any

import config
from storage import Storage
from utils import logger, normalize_symbol


class RuntimeSafety4H:
    """Runtime safety wrapper kept under the old filename/class name for compatibility.

    It prevents one bad coin/symbol/API error from stopping the bot, limits each scan to
    the configured watchlist size, and keeps the old 70-second Toobit slot recheck law.
    """

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def limited_watchlist(self) -> list[str]:
        out: list[str] = []
        for symbol in config.WATCHLIST[: int(config.MAX_WATCH_SYMBOLS)]:
            s = normalize_symbol(symbol)
            if s and s not in out:
                out.append(s)
        return out

    def can_scan_coin(self, symbol: str) -> bool:
        return not self.storage.coin_in_cooldown(symbol)

    def record_coin_error(self, symbol: str, exc: Exception | str) -> None:
        logger.warning("ارز %s خطا داد و فقط همان ارز موقتاً رد شد: %s", symbol, exc)
        self.storage.record_coin_error(symbol, str(exc), int(config.COIN_ERROR_COOLDOWN_SECONDS))

    def clear_coin_error(self, symbol: str) -> None:
        self.storage.clear_coin_error(symbol)

    def can_open_real_now(self, toobit_client: Any, *, max_positions: int) -> bool:
        max_positions = max(1, int(max_positions))
        if self.storage.active_real_count() < max_positions:
            return True

        now = int(time.time())
        recheck_after = int(config.SLOT_RECHECK_SECONDS)
        for signal in self.storage.active_real_signals():
            if now - int(signal.opened_at) < recheck_after:
                continue
            try:
                position = toobit_client.get_open_position(signal.toobit_symbol, signal.direction)
            except Exception as exc:
                logger.warning("چک 70 ثانیه‌ای اسلات Toobit برای %s ناموفق بود: %s", signal.symbol, exc)
                continue
            if position is None:
                self.storage.release_real_slot_external(signal.id, "Toobit position not found after slot recheck; slot released.")

        return self.storage.active_real_count() < max_positions
