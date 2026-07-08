from __future__ import annotations

import config
from storage import Storage
from utils import logger, normalize_symbol


class RuntimeSafety4H:
    """Runtime safety wrapper kept under the old filename/class name for compatibility.

    It prevents one bad coin/symbol/API error from stopping the bot and limits each scan
    to the configured watchlist size. It never calls Toobit during scanning/slot checks.
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
        text = str(exc)
        invalid_okx = "51001" in text or "Instrument ID" in text or "doesn't exist" in text or "does not exist" in text
        if invalid_okx:
            cooldown = int(getattr(config, "INVALID_SYMBOL_COOLDOWN_SECONDS", 86400))
            logger.warning("ارز %s رد شد | مرحله: DATA | دلیل: نماد OKX معتبر نیست یا فیوچرز USDT-SWAP ندارد | cooldown=%ss", symbol, cooldown)
        else:
            cooldown = int(config.COIN_ERROR_COOLDOWN_SECONDS)
            logger.warning("ارز %s خطا داد و فقط همان ارز موقتاً رد شد: %s", symbol, text)
        self.storage.record_coin_error(symbol, text, cooldown)

    def clear_coin_error(self, symbol: str) -> None:
        self.storage.clear_coin_error(symbol)

    def can_open_real_now(self, *, max_positions: int) -> bool:
        # بدون درخواست به Toobit: ظرفیت Real فقط از دیتابیس داخلی ربات خوانده می‌شود.
        max_positions = max(1, int(max_positions))
        return self.storage.active_real_count() < max_positions
