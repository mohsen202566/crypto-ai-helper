"""مدیریت آمار عادی و واقعی."""
from __future__ import annotations

from typing import Any

from .storage import JSONStorage


class StatsManager:
    def __init__(self, storage: JSONStorage):
        self.storage = storage

    def record_signal(self) -> None:
        self.storage.inc_stat("signals_total", 1)
        self.storage.inc_stat("normal_open", 1)

    def record_real_open(self) -> None:
        self.storage.inc_stat("real_open", 1)

    def record_real_failed(self) -> None:
        self.storage.inc_stat("real_failed", 1)

    def record_normal_result(self, result: str) -> None:
        self.storage.inc_stat("normal_open", -1)
        if result == "TP":
            self.storage.inc_stat("normal_tp", 1)
        elif result == "SL":
            self.storage.inc_stat("normal_sl", 1)

    def record_real_result(self, result: str, pnl: float = 0.0) -> None:
        self.storage.inc_stat("real_open", -1)
        if result == "TP":
            self.storage.inc_stat("real_tp", 1)
        elif result == "SL":
            self.storage.inc_stat("real_sl", 1)
        self.storage.inc_stat("real_pnl", pnl)

    def reset(self) -> None:
        self.storage.reset_stats(clear_signals=True)

    def summary(self) -> dict[str, Any]:
        stats = self.storage.get_stats()
        normal_done = stats.get("normal_tp", 0) + stats.get("normal_sl", 0)
        real_done = stats.get("real_tp", 0) + stats.get("real_sl", 0)
        stats["normal_winrate"] = (stats.get("normal_tp", 0) / normal_done * 100) if normal_done else 0.0
        stats["real_winrate"] = (stats.get("real_tp", 0) / real_done * 100) if real_done else 0.0
        return stats
