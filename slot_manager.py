from __future__ import annotations

from storage import Storage


class SlotManager:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def filled(self) -> int:
        return self.storage.active_real_count()

    def pending(self) -> int:
        return self.storage.pending_real_count()

    def free(self) -> int:
        return max(0, self.storage.max_positions() - self.filled())
