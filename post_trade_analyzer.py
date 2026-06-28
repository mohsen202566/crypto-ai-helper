from __future__ import annotations

class PostTradeAnalyzer:
    def record_closed_signal(self, storage, signal_id: int) -> None:
        storage.update_learning_from_signal(signal_id)
