from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DB_PATH, DEFAULT_LEVERAGE, DEFAULT_MARGIN_USDT, DEFAULT_MAX_POSITIONS, DEFAULT_TRADE_ENABLED
from scorer import SignalDecision


@dataclass(frozen=True)
class StoredSignal:
    id: int
    created_at: str
    okx_symbol: str
    toobit_symbol: str
    symbol_name: str
    direction: str
    entry: float
    tp: float
    sl: float
    score: int
    signal_type: str
    status: str
    real_status: str
    message_id: int | None
    result_message_id: int | None
    real_opened: int
    order_id: str | None
    approx_pnl: float | None
    real_pnl: float | None
    margin_usdt: float
    leverage: int
    net_edge: float
    risk_reward: float
    reason: str | None


class Storage:
    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
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
                    symbol_name TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL,
                    entry REAL NOT NULL,
                    tp REAL NOT NULL,
                    sl REAL NOT NULL,
                    score INTEGER NOT NULL,
                    signal_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    real_status TEXT NOT NULL DEFAULT 'none',
                    message_id INTEGER,
                    result_message_id INTEGER,
                    real_opened INTEGER NOT NULL DEFAULT 0,
                    order_id TEXT,
                    approx_pnl REAL,
                    real_pnl REAL,
                    margin_usdt REAL,
                    leverage INTEGER,
                    result_at TEXT,
                    score_1h INTEGER DEFAULT 0,
                    score_15m INTEGER DEFAULT 0,
                    score_5m INTEGER DEFAULT 0,
                    score_late INTEGER DEFAULT 0,
                    score_risk INTEGER DEFAULT 0,
                    score_market INTEGER DEFAULT 0,
                    score_4h INTEGER DEFAULT 0,
                    direction_state_1h TEXT,
                    direction_confidence_1h INTEGER DEFAULT 0,
                    bias_4h TEXT,
                    setup_15m TEXT,
                    entry_5m TEXT,
                    late_entry_ok INTEGER DEFAULT 0,
                    net_edge REAL DEFAULT 0,
                    risk_reward REAL DEFAULT 0,
                    estimated_cost_pct REAL DEFAULT 0,
                    market_bias TEXT,
                    reason TEXT,
                    notes TEXT,
                    real_open_reason TEXT,
                    actual_margin_usdt REAL,
                    quantity REAL
                )
                """
            )
            self._migrate_columns(conn)
            self._set_default(conn, "trade_enabled", "1" if DEFAULT_TRADE_ENABLED else "0")
            self._set_default(conn, "margin_usdt", str(DEFAULT_MARGIN_USDT))
            self._set_default(conn, "leverage", str(DEFAULT_LEVERAGE))
            self._set_default(conn, "max_positions", str(DEFAULT_MAX_POSITIONS))

    def _migrate_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(signals)").fetchall()}
        columns: dict[str, str] = {
            "symbol_name": "TEXT NOT NULL DEFAULT ''",
            "real_status": "TEXT NOT NULL DEFAULT 'none'",
            "result_message_id": "INTEGER",
            "score_1h": "INTEGER DEFAULT 0",
            "score_15m": "INTEGER DEFAULT 0",
            "score_5m": "INTEGER DEFAULT 0",
            "score_late": "INTEGER DEFAULT 0",
            "score_risk": "INTEGER DEFAULT 0",
            "score_market": "INTEGER DEFAULT 0",
            "score_4h": "INTEGER DEFAULT 0",
            "direction_state_1h": "TEXT",
            "direction_confidence_1h": "INTEGER DEFAULT 0",
            "bias_4h": "TEXT",
            "setup_15m": "TEXT",
            "entry_5m": "TEXT",
            "late_entry_ok": "INTEGER DEFAULT 0",
            "net_edge": "REAL DEFAULT 0",
            "risk_reward": "REAL DEFAULT 0",
            "estimated_cost_pct": "REAL DEFAULT 0",
            "market_bias": "TEXT",
            "reason": "TEXT",
            "notes": "TEXT",
            "real_open_reason": "TEXT",
            "actual_margin_usdt": "REAL",
            "quantity": "REAL",
        }
        for name, spec in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {name} {spec}")

    def _set_default(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))

    def _get_setting(self, key: str, default: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def _set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def trade_enabled(self) -> bool:
        return self._get_setting("trade_enabled", "0") == "1"

    def set_trade_enabled(self, enabled: bool) -> None:
        self._set_setting("trade_enabled", "1" if enabled else "0")

    def margin_usdt(self) -> float:
        return float(self._get_setting("margin_usdt", str(DEFAULT_MARGIN_USDT)))

    def set_margin_usdt(self, value: float) -> None:
        if value <= 0:
            raise ValueError("دلار هر پوزیشن باید مثبت باشد.")
        self._set_setting("margin_usdt", str(float(value)))

    def leverage(self) -> int:
        return int(float(self._get_setting("leverage", str(DEFAULT_LEVERAGE))))

    def set_leverage(self, value: int) -> None:
        if value <= 0:
            raise ValueError("لوریج باید مثبت باشد.")
        self._set_setting("leverage", str(int(value)))

    def max_positions(self) -> int:
        return int(float(self._get_setting("max_positions", str(DEFAULT_MAX_POSITIONS))))

    def set_max_positions(self, value: int) -> None:
        if value <= 0:
            raise ValueError("حداکثر پوزیشن باید مثبت باشد.")
        self._set_setting("max_positions", str(int(value)))

    def add_signal(
        self,
        *,
        okx_symbol: str,
        toobit_symbol: str,
        symbol_name: str,
        decision: SignalDecision,
        signal_type: str,
        real_status: str = "none",
    ) -> int:
        if decision.direction is None:
            raise ValueError("جهت سیگنال مشخص نیست.")
        now = datetime.now(timezone.utc).isoformat()
        notes = " | ".join(decision.notes[:18])
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO signals(
                    created_at, okx_symbol, toobit_symbol, symbol_name, direction,
                    entry, tp, sl, score, signal_type, status, real_status,
                    margin_usdt, leverage,
                    score_1h, score_15m, score_5m, score_late, score_risk, score_market, score_4h,
                    direction_state_1h, direction_confidence_1h, bias_4h, setup_15m, entry_5m,
                    late_entry_ok, net_edge, risk_reward, estimated_cost_pct, market_bias, reason, notes
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    okx_symbol,
                    toobit_symbol,
                    symbol_name,
                    decision.direction,
                    decision.entry,
                    decision.tp,
                    decision.sl,
                    decision.score,
                    signal_type,
                    real_status,
                    self.margin_usdt(),
                    self.leverage(),
                    decision.breakdown.score_1h,
                    decision.breakdown.score_15m,
                    decision.breakdown.score_5m,
                    decision.breakdown.score_late,
                    decision.breakdown.score_risk,
                    decision.breakdown.score_market,
                    decision.breakdown.score_4h,
                    decision.direction_state_1h,
                    decision.direction_confidence_1h,
                    decision.bias_4h,
                    decision.setup_15m,
                    decision.entry_5m,
                    1 if decision.late_entry_ok else 0,
                    decision.net_edge,
                    decision.risk_reward,
                    decision.estimated_cost_pct,
                    decision.market_bias,
                    decision.reason,
                    notes,
                ),
            )
            return int(cur.lastrowid)

    def update_message_id(self, signal_id: int, message_id: int | None) -> None:
        if message_id is None:
            return
        with self._connect() as conn:
            conn.execute("UPDATE signals SET message_id=? WHERE id=?", (int(message_id), signal_id))

    def mark_real_opening(self, signal_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE signals SET real_status='opening' WHERE id=? AND status='OPEN'", (signal_id,))

    def mark_real_open_result(
        self,
        signal_id: int,
        *,
        opened: bool,
        order_id: str | None,
        reason: str,
        actual_margin_usdt: float | None = None,
        quantity: float | None = None,
    ) -> None:
        with self._connect() as conn:
            if opened:
                conn.execute(
                    """
                    UPDATE signals
                    SET real_status='opened', real_opened=1, order_id=?, real_open_reason=?, actual_margin_usdt=?, quantity=?
                    WHERE id=? AND status='OPEN'
                    """,
                    (order_id, reason, actual_margin_usdt, quantity, signal_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE signals
                    SET signal_type='real_failed', status='FAILED', real_status='failed', real_opened=0,
                        order_id=NULL, real_open_reason=?, result_at=?
                    WHERE id=? AND status='OPEN'
                    """,
                    (reason, datetime.now(timezone.utc).isoformat(), signal_id),
                )

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

    def active_symbol_exists(self, toobit_symbol: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE status='OPEN' AND toobit_symbol=?",
                (toobit_symbol,),
            ).fetchone()
            return int(row["n"]) > 0

    def active_real_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM signals
                WHERE status='OPEN' AND signal_type='real' AND real_status IN ('reserved','opening','opened')
                """
            ).fetchone()
            return int(row["n"])

    def pending_real_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM signals
                WHERE status='OPEN' AND signal_type='real' AND real_status IN ('reserved','opening')
                """
            ).fetchone()
            return int(row["n"])

    def active_real_symbol_exists(self, toobit_symbol: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM signals
                WHERE status='OPEN' AND signal_type='real' AND real_status IN ('reserved','opening','opened') AND toobit_symbol=?
                """,
                (toobit_symbol,),
            ).fetchone()
            return int(row["n"]) > 0

    def stats(self, days: int) -> dict[str, Any]:
        days = max(1, min(days, 30))
        start = datetime.now(timezone.utc) - timedelta(days=days)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE created_at >= ?", (start.isoformat(),)).fetchall()
        return self._build_stats(rows)

    def today_stats(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signals WHERE created_at >= ?", (start.isoformat(),)).fetchall()
        stats = self._build_stats(rows)
        approx = sum(float(row["approx_pnl"] or 0.0) for row in rows if row["signal_type"] == "normal")
        real = sum(float(row["real_pnl"] or 0.0) for row in rows if row["signal_type"] == "real")
        stats["approx_pnl"] = approx
        stats["real_pnl"] = real
        return stats

    def _build_stats(self, rows: list[sqlite3.Row]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        result["all"] = self._summarize(rows, pnl_key="approx_pnl")
        result["normal"] = self._summarize([r for r in rows if r["signal_type"] == "normal"], pnl_key="approx_pnl")
        result["real"] = self._summarize([r for r in rows if r["signal_type"] == "real"], pnl_key="real_pnl")
        result["real_failed"] = self._summarize([r for r in rows if r["signal_type"] == "real_failed"], pnl_key="real_pnl")
        for side in ("LONG", "SHORT"):
            key = side.lower()
            result[key] = self._summarize([r for r in rows if r["direction"] == side], pnl_key="approx_pnl")
            result[f"normal_{key}"] = self._summarize([r for r in rows if r["signal_type"] == "normal" and r["direction"] == side], pnl_key="approx_pnl")
            result[f"real_{key}"] = self._summarize([r for r in rows if r["signal_type"] == "real" and r["direction"] == side], pnl_key="real_pnl")
        return result

    def _summarize(self, subset: list[sqlite3.Row], *, pnl_key: str) -> dict[str, Any]:
        closed = [row for row in subset if row["status"] in ("TP", "SL")]
        tp_count = sum(1 for row in subset if row["status"] == "TP")
        sl_count = sum(1 for row in subset if row["status"] == "SL")
        open_count = sum(1 for row in subset if row["status"] == "OPEN")
        failed_count = sum(1 for row in subset if row["status"] == "FAILED")
        total_pnl = sum(float(row[pnl_key] or 0.0) for row in subset)
        avg_score = sum(float(row["score"] or 0) for row in subset) / len(subset) if subset else 0.0
        return {
            "total": len(subset),
            "tp": tp_count,
            "sl": sl_count,
            "open": open_count,
            "failed": failed_count,
            "win_rate": (tp_count / len(closed) * 100.0) if closed else 0.0,
            "pnl": total_pnl,
            "avg_score": avg_score,
        }

    def _row_to_signal(self, row: sqlite3.Row) -> StoredSignal:
        return StoredSignal(
            id=int(row["id"]),
            created_at=str(row["created_at"]),
            okx_symbol=str(row["okx_symbol"]),
            toobit_symbol=str(row["toobit_symbol"]),
            symbol_name=str(row["symbol_name"] or ""),
            direction=str(row["direction"]),
            entry=float(row["entry"]),
            tp=float(row["tp"]),
            sl=float(row["sl"]),
            score=int(row["score"]),
            signal_type=str(row["signal_type"]),
            status=str(row["status"]),
            real_status=str(row["real_status"] or "none"),
            message_id=int(row["message_id"]) if row["message_id"] is not None else None,
            result_message_id=int(row["result_message_id"]) if row["result_message_id"] is not None else None,
            real_opened=int(row["real_opened"] or 0),
            order_id=str(row["order_id"]) if row["order_id"] is not None else None,
            approx_pnl=float(row["approx_pnl"]) if row["approx_pnl"] is not None else None,
            real_pnl=float(row["real_pnl"]) if row["real_pnl"] is not None else None,
            margin_usdt=float(row["margin_usdt"] or 0.0),
            leverage=int(row["leverage"] or 1),
            net_edge=float(row["net_edge"] or 0.0),
            risk_reward=float(row["risk_reward"] or 0.0),
            reason=str(row["reason"]) if row["reason"] is not None else None,
        )
