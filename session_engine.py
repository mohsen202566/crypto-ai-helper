from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from scorer import Direction, SessionState


@dataclass(frozen=True)
class SessionResult:
    state: SessionState
    score: int
    samples: int
    reasons: tuple[str, ...]


class SessionEngine:
    def analyze(self, storage, symbol_name: str, direction: Direction, now: datetime | None = None) -> SessionResult:
        current = now or datetime.now(timezone.utc)
        bucket = f"{current.hour:02d}:{0 if current.minute < 30 else 30:02d}"
        stats = storage.session_stats(symbol_name, direction, bucket)
        samples = int(stats.get("samples", 0))
        wr = float(stats.get("win_rate", 0.0))
        sl = int(stats.get("sl", 0))
        tp = int(stats.get("tp", 0))
        if samples >= 20 and sl > tp * 1.4 and wr < 42:
            return SessionResult("BAD_REAL_ONLY_NORMAL", 0, samples, (f"ساعت {bucket} برای این الگو SL بالا داشته؛ فقط عادی.",))
        if samples >= 12 and wr >= 62:
            return SessionResult("GOOD", 5, samples, (f"ساعت {bucket} در نمونه‌های اخیر خوب بوده است.",))
        return SessionResult("NORMAL", 3, samples, (f"ساعت {bucket} عادی است.",))
