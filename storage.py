from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DB_PATH, DEFAULT_LEVERAGE, DEFAULT_MARGIN_USDT, DEFAULT_MAX_POSITIONS, DEFAULT_TRADE_ENABLED


@dataclass(frozen=True)
class StoredSignal:
    id: int
    created_at: str
    okx_symbol: str
    toobit_symbol: str
    direction: str
    entry: float
    tp: float
    sl: float
    score: int
    signal_type: str
    status: str
    message_id: int | None
    real_opened: int
    order_id: str | None
    approx_pnl: float | None
    real_pnl: float | None
    margin_usdt: float
    leverage: int


class Storage:
    def __init__(self, path: str = DB_PATH) -> None:
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    okx_symbol TEXT NOT NULL,
                    toobit_symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry REAL NOT NULL,
                    tp REAL NOT NULL,
                    sl REAL NOT NULL,
                    score INTEGER NOT NULL,
                    signal_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message_id INTEGER,
                    result_message_id INTEGER,
                    real_opened INTEGER NOT NULL DEFAULT 0,
                    order_id TEXT,
                    approx_pnl REAL,
                    real_pnl REAL,
                    margin_usdt REAL NOT NULL DEFAULT 0,
                    leverage INTEGER NOT NULL DEFAULT 1,
                    result_at TEXT
                )
                """
            )
            self._ensure_setting(conn, "trade_enabled", "1" if DEFAULT_TRADE_ENABLED else "0")
            self._ensure_setting(conn, "margin_usdt", str(DEFAULT_MARGIN_USDT))
            self._ensure_setting(conn, "leverage", str(DEFAULT_LEVERAGE))
            self._ensure_setting(conn, "max_positions", str(DEFAULT_MAX_POSITIONS))
            self._ensure_column(conn, "signals", "margin_usdt", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "signals", "leverage", "INTEGER NOT NULL DEFAULT 1")

    def _ensure_setting(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {str(row[1]) for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def get_setting(self, key: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            return str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def trade_enabled(self) -> bool:
        return self.get_setting("trade_enabled") == "1"

    def set_trade_enabled(self, enabled: bool) -> None:
        self.set_setting("trade_enabled", "1" if enabled else "0")

    def margin_usdt(self) -> float:
        return float(self.get_setting("margin_usdt"))

    def set_margin_usdt(self, value: float) -> None:
        if not 1 <= value <= 10000:
            raise ValueError("دلار ترید باید بین ۱ تا ۱۰۰۰۰ باشد.")
        self.set_setting("margin_usdt", str(float(value)))

    def leverage(self) -> int:
        return int(float(self.get_setting("leverage")))

    def set_leverage(self, value: int) -> None:
        if not 1 <= value <= 100:
            raise ValueError("لوریج باید بین ۱ تا ۱۰۰ باشد.")
        self.set_setting("leverage", str(int(value)))

    def max_positions(self) -> int:
        return int(float(self.get_setting("max_positions")))

    def set_max_positions(self, value: int) -> None:
        if not 1 <= value <= 100:
            raise ValueError("حداکثر پوزیشن باید بین ۱ تا ۱۰۰ باشد.")
        self.set_setting("max_positions", str(int(value)))

    def add_signal(
        self,
        *,
        okx_symbol: str,
        toobit_symbol: str,
        direction: str,
        entry: float,
        tp: float,
        sl: float,
        score: int,
        signal_type: str,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        margin_usdt = self.margin_usdt()
        leverage = self.leverage()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO signals(created_at, okx_symbol, toobit_symbol, direction, entry, tp, sl, score, signal_type, status, margin_usdt, leverage)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                """,
                (now, okx_symbol, toobit_symbol, direction, entry, tp, sl, int(score), signal_type, margin_usdt, leverage),
            )
            return int(cursor.lastrowid)

    def update_message_id(self, signal_id: int, message_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE signals SET message_id=? WHERE id=?", (message_id, signal_id))

    def mark_real_open_result(self, signal_id: int, *, opened: bool, order_id: str | None) -> None:
        with self._connect() as conn:
            if opened:
                conn.execute("UPDATE signals SET real_opened=1, order_id=? WHERE id=?", (order_id, signal_id))
            else:
                conn.execute("UPDATE signals SET signal_type='normal', real_opened=0, order_id=NULL WHERE id=?", (signal_id,))

    def finish_signal(
        self,
        signal_id: int,
        *,
        status: str,
        approx_pnl: float,
        real_pnl: float | None,
        result_message_id: int | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE signals
                SET status=?, approx_pnl=?, real_pnl=?, result_message_id=?, result_at=?
                WHERE id=? AND status='OPEN'
                """,
                (status, approx_pnl, real_pnl, result_message_id, now, signal_id),
            )

    def open_signals(self) -> list[StoredSignal]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE status='OPEN' ORDER BY id ASC").fetchall()
            return [self._row_to_signal(row) for row in rows]

    def active_real_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM signals WHERE status='OPEN' AND signal_type='real'").fetchone()
            return int(row["n"])

    def active_symbol_exists(self, toobit_symbol: str) -> bool:
        """True when any OPEN signal, normal or real, already exists for this symbol."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE status='OPEN' AND toobit_symbol=?",
                (toobit_symbol,),
            ).fetchone()
            return int(row["n"]) > 0

    def active_real_symbol_exists(self, toobit_symbol: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE status='OPEN' AND signal_type='real' AND toobit_symbol=?",
                (toobit_symbol,),
            ).fetchone()
            return int(row["n"]) > 0

    def stats(self, days: int) -> dict[str, Any]:
        days = max(1, min(days, 7))
        start = datetime.now(timezone.utc) - timedelta(days=days)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE created_at >= ?", (start.isoformat(),)).fetchall()
        result: dict[str, Any] = {}
        for signal_type in ("normal", "real"):
            subset = [row for row in rows if row["signal_type"] == signal_type]
            closed = [row for row in subset if row["status"] in ("TP", "SL")]
            tp_count = sum(1 for row in subset if row["status"] == "TP")
            sl_count = sum(1 for row in subset if row["status"] == "SL")
            open_count = sum(1 for row in subset if row["status"] == "OPEN")
            pnl_key = "real_pnl" if signal_type == "real" else "approx_pnl"
            total_pnl = sum(float(row[pnl_key] or 0.0) for row in subset)
            result[signal_type] = {
                "total": len(subset),
                "tp": tp_count,
                "sl": sl_count,
                "open": open_count,
                "win_rate": (tp_count / len(closed) * 100.0) if closed else 0.0,
                "pnl": total_pnl,
            }
        return result

    def today_stats(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE created_at >= ?", (start.isoformat(),)).fetchall()
        approx = sum(float(row["approx_pnl"] or 0.0) for row in rows if row["signal_type"] == "normal")
        real = sum(float(row["real_pnl"] or 0.0) for row in rows if row["signal_type"] == "real")
        return {"approx_pnl": approx, "real_pnl": real}

    def _row_to_signal(self, row: sqlite3.Row) -> StoredSignal:
        return StoredSignal(
            id=int(row["id"]),
            created_at=str(row["created_at"]),
            okx_symbol=str(row["okx_symbol"]),
            toobit_symbol=str(row["toobit_symbol"]),
            direction=str(row["direction"]),
            entry=float(row["entry"]),
            tp=float(row["tp"]),
            sl=float(row["sl"]),
            score=int(row["score"]),
            signal_type=str(row["signal_type"]),
            status=str(row["status"]),
            message_id=int(row["message_id"]) if row["message_id"] is not None else None,
            real_opened=int(row["real_opened"]),
            order_id=str(row["order_id"]) if row["order_id"] is not None else None,
            approx_pnl=float(row["approx_pnl"]) if row["approx_pnl"] is not None else None,
            real_pnl=float(row["real_pnl"]) if row["real_pnl"] is not None else None,
            margin_usdt=float(row["margin_usdt"] or 0.0),
            leverage=int(row["leverage"] or 1),
        )
