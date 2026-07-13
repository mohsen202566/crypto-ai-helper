"""SQLite thread-safe برای تنظیمات، سیگنال‌ها، آمار و سلامت."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any
import json
import sqlite3
import threading
import time

import config


class Storage:
    def __init__(self, path: str = config.DB_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def connect(self):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
                yield db
            finally:
                db.close()

    @staticmethod
    def _column_names(db: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}

    def _ensure_signal_columns(self, db: sqlite3.Connection) -> None:
        existing = self._column_names(db, "signals")
        required = {
            "trade_usdt": "REAL NOT NULL DEFAULT 0",
            "leverage": "INTEGER NOT NULL DEFAULT 1",
            "notional": "REAL NOT NULL DEFAULT 0",
            "entry_real": "REAL",
            "exit_price": "REAL",
            "gross_pnl": "REAL DEFAULT 0",
            "fee_usdt": "REAL DEFAULT 0",
            "net_pnl": "REAL DEFAULT 0",
            "close_reason": "TEXT",
            "mfe": "REAL DEFAULT 0",
            "mae": "REAL DEFAULT 0",
            "order_id": "TEXT",
            "raw_json": "TEXT DEFAULT '{}'",
        }
        for name, ddl in required.items():
            if name not in existing:
                db.execute(f"ALTER TABLE signals ADD COLUMN {name} {ddl}")

    def _init_db(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signals(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol_id TEXT NOT NULL,
                    okx_symbol TEXT NOT NULL,
                    toobit_symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    strength TEXT,
                    entry REAL NOT NULL,
                    tp REAL NOT NULL,
                    sl REAL NOT NULL,
                    rr REAL NOT NULL,
                    trade_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    is_real INTEGER NOT NULL DEFAULT 0,
                    slot_id INTEGER,
                    message_id INTEGER,
                    created_at INTEGER NOT NULL,
                    opened_at INTEGER,
                    closed_at INTEGER,
                    entry_real REAL,
                    exit_price REAL,
                    trade_usdt REAL NOT NULL DEFAULT 0,
                    leverage INTEGER NOT NULL DEFAULT 1,
                    notional REAL NOT NULL DEFAULT 0,
                    gross_pnl REAL DEFAULT 0,
                    fee_usdt REAL DEFAULT 0,
                    net_pnl REAL DEFAULT 0,
                    close_reason TEXT,
                    mfe REAL DEFAULT 0,
                    mae REAL DEFAULT 0,
                    order_id TEXT,
                    raw_json TEXT DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS health_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    component TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    symbol_id TEXT,
                    created_at INTEGER NOT NULL,
                    cleared_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS blacklist(
                    symbol_id TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    until_ts INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 1
                );
                """
            )
            self._ensure_signal_columns(db)

        defaults = {
            "trading_enabled": config.TRADING_ENABLED_DEFAULT,
            "auto_signal_enabled": config.AUTO_SIGNAL_ENABLED_DEFAULT,
            "trade_usdt": config.TRADE_USDT_DEFAULT,
            "leverage": config.LEVERAGE_DEFAULT,
            "max_positions": config.MAX_POSITIONS_DEFAULT,
            "profit_today": 0.0,
            "profit_total": 0.0,
            "profit_day": time.strftime("%Y-%m-%d", time.gmtime()),
            "stats_reset_at": int(time.time()),
            "profit_reset_at": int(time.time()),
            "telegram_offset": 0,
            "toobit_connected": False,
            "toobit_margin_usdt": 0.0,
            "toobit_available_usdt": 0.0,
            "toobit_total_usdt": 0.0,
            "toobit_last_error": "",
            "toobit_last_update": 0,
        }
        for key, value in defaults.items():
            if self.get(key, None) is None:
                self.set(key, value)

    def get(self, key: str, default: Any = None) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    def set(self, key: str, value: Any) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO kv(key,value,updated_at) VALUES(?,?,?)",
                (key, json.dumps(value, ensure_ascii=False), int(time.time())),
            )

    def create_signal(self, data: dict[str, Any]) -> int:
        now = int(time.time())
        cols = [
            "symbol_id", "okx_symbol", "toobit_symbol", "side", "strength", "entry", "tp", "sl", "rr",
            "trade_mode", "status", "is_real", "slot_id", "message_id", "created_at", "opened_at", "entry_real",
            "trade_usdt", "leverage", "notional", "order_id", "raw_json",
        ]
        values = {
            **data,
            "created_at": data.get("created_at", now),
            "is_real": int(bool(data.get("is_real"))),
            "raw_json": json.dumps(data.get("raw", {}), ensure_ascii=False),
        }
        vals = [values.get(k) for k in cols]
        with self.connect() as db:
            cur = db.execute(
                f"INSERT INTO signals({','.join(cols)}) VALUES({','.join(['?'] * len(cols))})",
                vals,
            )
            return int(cur.lastrowid)

    def update_signal(self, signal_id: int, **fields: Any) -> None:
        allowed = {
            "status", "message_id", "opened_at", "closed_at", "entry_real", "exit_price", "gross_pnl",
            "fee_usdt", "net_pnl", "close_reason", "mfe", "mae", "order_id", "slot_id", "is_real",
            "trade_mode", "raw_json",
        }
        items = [(k, v) for k, v in fields.items() if k in allowed]
        if not items:
            return
        parts: list[str] = []
        vals: list[Any] = []
        for key, value in items:
            parts.append(f"{key}=?")
            vals.append(json.dumps(value, ensure_ascii=False) if key == "raw_json" and not isinstance(value, str) else value)
        vals.append(signal_id)
        with self.connect() as db:
            db.execute(f"UPDATE signals SET {','.join(parts)} WHERE id=?", vals)

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        return dict(row) if row else None

    def get_open_signals(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM signals WHERE status IN ('open','pending') ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def count_real_open(self) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE is_real=1 AND status IN ('open','pending')"
            ).fetchone()
        return int(row["n"])

    def close_signal(
        self,
        signal_id: int,
        exit_price: float,
        gross: float,
        fee: float,
        net: float,
        reason: str,
        mfe: float,
        mae: float,
    ) -> None:
        current = self.get_signal(signal_id)
        if not current or current.get("status") == "closed":
            return
        self.update_signal(
            signal_id,
            status="closed",
            closed_at=int(time.time()),
            exit_price=exit_price,
            gross_pnl=gross,
            fee_usdt=fee,
            net_pnl=net,
            close_reason=reason,
            mfe=mfe,
            mae=mae,
        )
        self.roll_profit_day()
        self.set("profit_today", float(self.get("profit_today", 0.0)) + net)
        self.set("profit_total", float(self.get("profit_total", 0.0)) + net)

    def roll_profit_day(self) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if self.get("profit_day", "") != today:
            self.set("profit_day", today)
            self.set("profit_today", 0.0)

    def reset_stats(self) -> None:
        self.set("stats_reset_at", int(time.time()))

    def reset_profit(self) -> None:
        self.set("profit_today", 0.0)
        self.set("profit_total", 0.0)
        self.set("profit_reset_at", int(time.time()))
        self.set("profit_day", time.strftime("%Y-%m-%d", time.gmtime()))

    def stats(self) -> dict[str, Any]:
        self.roll_profit_day()
        reset = int(self.get("stats_reset_at", 0))
        with self.connect() as db:
            rows = db.execute("SELECT * FROM signals WHERE created_at>=? ORDER BY id", (reset,)).fetchall()
        items = [dict(row) for row in rows]
        closed = [row for row in items if row["status"] == "closed"]
        tp = sum(1 for row in closed if row.get("close_reason") == "TP")
        sl = sum(1 for row in closed if row.get("close_reason") == "SL")
        real = [row for row in items if int(row.get("is_real") or 0) == 1]
        virtual = [row for row in items if int(row.get("is_real") or 0) == 0]

        def net(rows_: list[dict[str, Any]]) -> float:
            return sum(float(row.get("net_pnl") or 0.0) for row in rows_)

        return {
            "signals": len(items),
            "open": sum(1 for row in items if row["status"] in ("open", "pending")),
            "pending": sum(1 for row in items if row["status"] == "pending"),
            "tp": tp,
            "sl": sl,
            "real": len(real),
            "virtual": len(virtual),
            "net_pnl": net(closed),
            "real_net": net(real),
            "virtual_net": net(virtual),
        }

    def add_health_event(self, component: str, severity: str, message: str, symbol_id: str | None = None) -> None:
        now = int(time.time())
        with self.connect() as db:
            duplicate = db.execute(
                """
                SELECT id FROM health_events
                WHERE component=? AND severity=? AND message=? AND COALESCE(symbol_id,'')=COALESCE(?, '')
                  AND cleared_at IS NULL AND created_at>=?
                LIMIT 1
                """,
                (component, severity, message, symbol_id, now - 300),
            ).fetchone()
            if duplicate:
                return
            db.execute(
                "INSERT INTO health_events(component,severity,message,symbol_id,created_at) VALUES(?,?,?,?,?)",
                (component, severity, message, symbol_id, now),
            )

    def active_health_events(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM health_events WHERE cleared_at IS NULL ORDER BY id DESC LIMIT 20"
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_health_component(self, component: str, symbol_id: str | None = None) -> None:
        with self.connect() as db:
            if symbol_id is None:
                db.execute(
                    "UPDATE health_events SET cleared_at=? WHERE component=? AND cleared_at IS NULL",
                    (int(time.time()), component),
                )
            else:
                db.execute(
                    """
                    UPDATE health_events SET cleared_at=?
                    WHERE component=? AND symbol_id=? AND cleared_at IS NULL
                    """,
                    (int(time.time()), component, symbol_id),
                )

    def blacklist(self, symbol_id: str, reason: str, seconds: int) -> None:
        until = int(time.time()) + int(seconds)
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO blacklist(symbol_id,reason,until_ts,count) VALUES(?,?,?,1)
                ON CONFLICT(symbol_id) DO UPDATE SET
                    reason=excluded.reason,
                    until_ts=excluded.until_ts,
                    count=blacklist.count+1
                """,
                (symbol_id, reason, until),
            )

    def is_blacklisted(self, symbol_id: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT until_ts FROM blacklist WHERE symbol_id=?", (symbol_id,)).fetchone()
        return bool(row and int(row["until_ts"]) > int(time.time()))

    def blacklist_rows(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM blacklist WHERE until_ts>? ORDER BY until_ts",
                (int(time.time()),),
            ).fetchall()
        return [dict(row) for row in rows]
