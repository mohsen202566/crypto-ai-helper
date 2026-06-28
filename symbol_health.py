from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import OKX_DISABLE_MINUTES, SYMBOL_ERROR_DISABLE_AFTER, TOOBIT_REAL_DISABLE_HOURS
from storage import Storage


class SymbolHealth:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def okx_enabled(self, symbol_name: str) -> bool:
        return self._enabled(symbol_name, "okx_disabled_until")

    def toobit_real_enabled(self, symbol_name: str) -> bool:
        return self._enabled(symbol_name, "toobit_real_disabled_until")

    def record_okx_success(self, symbol_name: str) -> None:
        self._upsert(symbol_name, okx_error_count=0, last_okx_error=None, okx_disabled_until=None)

    def record_toobit_success(self, symbol_name: str) -> None:
        self._upsert(symbol_name, toobit_error_count=0, last_toobit_error=None, toobit_real_disabled_until=None)

    def record_okx_error(self, symbol_name: str, error: str) -> None:
        now = datetime.now(timezone.utc)
        with self.storage._connect() as conn:
            row = conn.execute("SELECT okx_error_count FROM symbol_health WHERE symbol_name=?", (symbol_name,)).fetchone()
            count = int(row["okx_error_count"] or 0) + 1 if row else 1
            disabled = now + timedelta(minutes=OKX_DISABLE_MINUTES) if count >= SYMBOL_ERROR_DISABLE_AFTER else None
            conn.execute(
                """
                INSERT INTO symbol_health(symbol_name, okx_error_count, last_okx_error, okx_disabled_until, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(symbol_name) DO UPDATE SET okx_error_count=excluded.okx_error_count, last_okx_error=excluded.last_okx_error, okx_disabled_until=excluded.okx_disabled_until, updated_at=excluded.updated_at
                """,
                (symbol_name, count, error[:300], disabled.isoformat() if disabled else None, now.isoformat()),
            )

    def record_toobit_error(self, symbol_name: str, error: str) -> None:
        now = datetime.now(timezone.utc)
        disabled = now + timedelta(hours=TOOBIT_REAL_DISABLE_HOURS)
        with self.storage._connect() as conn:
            row = conn.execute("SELECT toobit_error_count FROM symbol_health WHERE symbol_name=?", (symbol_name,)).fetchone()
            count = int(row["toobit_error_count"] or 0) + 1 if row else 1
            conn.execute(
                """
                INSERT INTO symbol_health(symbol_name, toobit_error_count, last_toobit_error, toobit_real_disabled_until, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(symbol_name) DO UPDATE SET toobit_error_count=excluded.toobit_error_count, last_toobit_error=excluded.last_toobit_error, toobit_real_disabled_until=excluded.toobit_real_disabled_until, updated_at=excluded.updated_at
                """,
                (symbol_name, count, error[:300], disabled.isoformat(), now.isoformat()),
            )

    def panel_summary(self) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        with self.storage._connect() as conn:
            rows = conn.execute("SELECT * FROM symbol_health").fetchall()
        okx_bad = 0
        toobit_bad = 0
        for row in rows:
            if row["okx_disabled_until"] and datetime.fromisoformat(str(row["okx_disabled_until"])) > now:
                okx_bad += 1
            if row["toobit_real_disabled_until"] and datetime.fromisoformat(str(row["toobit_real_disabled_until"])) > now:
                toobit_bad += 1
        return {"okx_disabled": okx_bad, "toobit_real_disabled": toobit_bad}

    def _enabled(self, symbol_name: str, column: str) -> bool:
        now = datetime.now(timezone.utc)
        with self.storage._connect() as conn:
            row = conn.execute(f"SELECT {column} FROM symbol_health WHERE symbol_name=?", (symbol_name,)).fetchone()
        if not row or not row[column]:
            return True
        try:
            return datetime.fromisoformat(str(row[column])) <= now
        except ValueError:
            return True

    def _upsert(self, symbol_name: str, **values) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.storage._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO symbol_health(symbol_name, updated_at) VALUES(?, ?)", (symbol_name, now))
            for key, value in values.items():
                conn.execute(f"UPDATE symbol_health SET {key}=?, updated_at=? WHERE symbol_name=?", (value, now, symbol_name))
