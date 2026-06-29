from __future__ import annotations

from learning_engine import LearningEngine


class PostTradeAnalyzer:
    def __init__(self) -> None:
        self.learning = LearningEngine()

    def record_closed_signal(self, storage, signal_id: int) -> None:
        self.learning.learn_from_closed_signal(storage, signal_id)
        # Suggestions are advisory only. Margin/leverage/slots remain under user control.
        storage.refresh_ai_suggestions()
