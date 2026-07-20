"""دیتابیس واحد runtime برای تنظیمات، سیگنال‌ها، اسلات‌ها و آمار.

هیچ جدول یادگیری، پروفایل، سناریو یا Champion وجود ندارد.
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from utils import json_dumps, json_loads, logger, now_ms

FINAL_STATUSES = {"TP", "STOP", "TRAIL_EXIT", "MANUAL_CLOSE", "FAILED_OPEN", "CANCELLED"}
ACTIVE_STATUSES = ("ACTIVE", "PENDING_OPEN", "OPEN")


class Storage:
    def __init__(self, path: Path = config.RUNTIME_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.path,
            timeout=config.SQLITE_BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()
        self._defaults()

    def _configure(self) -> None:
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute(f"PRAGMA busy_timeout={config.SQLITE_BUSY_TIMEOUT_MS}")
            self.conn.execute("PRAGMA foreign_keys=ON")

    @contextlib.contextmanager
    def tx(self, immediate: bool = False):
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield self.conn
                if self.conn.in_transaction:
                    self.conn.execute("COMMIT")
            except Exception:
                if self.conn.in_transaction:
                    with contextlib.suppress(Exception):
                        self.conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _table_exists(c: sqlite3.Connection, table: str) -> bool:
        row = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None

    @staticmethod
    def _table_columns(c: sqlite3.Connection, table: str) -> set[str]:
        if not Storage._table_exists(c, table):
            return set()
        return {str(row[1]) for row in c.execute(f"PRAGMA table_info({table})").fetchall()}

    @staticmethod
    def _add_column_if_missing(
        c: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> bool:
        if column in Storage._table_columns(c, table):
            return False
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        return True

    def _migrate(self) -> None:
        """Create the current schema and upgrade the legacy trend-bot database in place.

        The previous project used ``tier`` instead of ``mode`` and ``toobit_symbol``
        instead of ``exchange_symbol``.  Those legacy columns are intentionally kept
        because SQLite cannot remove their NOT NULL constraints safely with ALTER TABLE.
        New inserts therefore support both layouts; no statistics/history is deleted.
        """
        migrated: list[str] = []
        with self.tx(immediate=True) as c:
            # Do not create indexes that reference new columns until legacy tables have
            # been upgraded; CREATE TABLE IF NOT EXISTS does not alter an old table.
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings(
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signals(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical TEXT NOT NULL,
                    exchange_symbol TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'VIRTUAL',
                    side TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS symbol_locks(
                    canonical TEXT PRIMARY KEY,
                    signal_id INTEGER NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'VIRTUAL',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(signal_id) REFERENCES signals(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS positions(
                    signal_id INTEGER PRIMARY KEY,
                    canonical TEXT NOT NULL,
                    exchange_symbol TEXT NOT NULL DEFAULT '',
                    side TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reserved_at INTEGER NOT NULL,
                    confirm_after INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(signal_id) REFERENCES signals(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS account_snapshot(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    updated_at INTEGER NOT NULL,
                    connected INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS contracts(
                    canonical TEXT PRIMARY KEY,
                    exchange_symbol TEXT NOT NULL,
                    first_seen_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS health(
                    component TEXT PRIMARY KEY,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    canonical TEXT,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_state(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )

            if self._add_column_if_missing(c, "signals", "mode", "TEXT NOT NULL DEFAULT 'VIRTUAL'"):
                migrated.append("signals.mode")
            if self._add_column_if_missing(c, "signals", "exchange_symbol", "TEXT NOT NULL DEFAULT ''"):
                migrated.append("signals.exchange_symbol")
            if self._add_column_if_missing(c, "symbol_locks", "mode", "TEXT NOT NULL DEFAULT 'VIRTUAL'"):
                migrated.append("symbol_locks.mode")
            if self._add_column_if_missing(c, "positions", "exchange_symbol", "TEXT NOT NULL DEFAULT ''"):
                migrated.append("positions.exchange_symbol")
            if self._add_column_if_missing(c, "positions", "confirm_after", "INTEGER NOT NULL DEFAULT 0"):
                migrated.append("positions.confirm_after")

            signal_columns = self._table_columns(c, "signals")
            lock_columns = self._table_columns(c, "symbol_locks")
            position_columns = self._table_columns(c, "positions")

            # Map the old learning tiers to the only two modes in the new bot.
            if "tier" in signal_columns:
                c.execute(
                    "UPDATE signals SET mode=CASE WHEN UPPER(COALESCE(tier,''))='REAL' "
                    "THEN 'REAL' ELSE 'VIRTUAL' END "
                    "WHERE mode IS NULL OR mode='' OR mode NOT IN ('REAL','VIRTUAL') "
                    "OR UPPER(COALESCE(tier,''))='REAL'"
                )
            else:
                c.execute("UPDATE signals SET mode='VIRTUAL' WHERE mode IS NULL OR mode='' OR mode NOT IN ('REAL','VIRTUAL')")

            if "tier" in lock_columns:
                c.execute(
                    "UPDATE symbol_locks SET mode=COALESCE((SELECT mode FROM signals "
                    "WHERE signals.id=symbol_locks.signal_id),'VIRTUAL')"
                )
            else:
                c.execute(
                    "UPDATE symbol_locks SET mode=COALESCE((SELECT mode FROM signals "
                    "WHERE signals.id=symbol_locks.signal_id),'VIRTUAL') "
                    "WHERE mode IS NULL OR mode='' OR mode NOT IN ('REAL','VIRTUAL')"
                )

            if "toobit_symbol" in position_columns:
                c.execute(
                    "UPDATE positions SET exchange_symbol=COALESCE(NULLIF(exchange_symbol,''),toobit_symbol,'')"
                )

            # Keep old health and event history visible in the new panels.
            if self._table_exists(c, "health_state"):
                c.execute(
                    "INSERT OR REPLACE INTO health(component,level,message,updated_at) "
                    "SELECT component,level,message,updated_at FROM health_state"
                )
            if self._table_exists(c, "runtime_events"):
                c.execute(
                    "INSERT OR IGNORE INTO events(id,kind,canonical,message,payload_json,created_at) "
                    "SELECT id,kind,canonical,message,COALESCE(payload_json,'{}'),created_at FROM runtime_events"
                )

            # Normalize JSON payloads too, because panels and monitors read payload_json.
            rows = c.execute(
                "SELECT id,canonical,exchange_symbol,mode,side,status,created_at,updated_at,payload_json FROM signals"
            ).fetchall()
            for row in rows:
                payload = json_loads(row["payload_json"], {})
                if not isinstance(payload, dict):
                    payload = {}
                payload.update({
                    "id": int(row["id"]),
                    "canonical": row["canonical"],
                    "exchange_symbol": row["exchange_symbol"],
                    "mode": row["mode"],
                    "side": row["side"],
                    "status": row["status"],
                    "created_at": int(row["created_at"]),
                    "updated_at": int(row["updated_at"]),
                })
                c.execute("UPDATE signals SET payload_json=? WHERE id=?", (json_dumps(payload), row["id"]))

            # Rebuild indexes after mode exists. The old index may have the same name but
            # still reference tier, so IF NOT EXISTS alone is insufficient.
            c.executescript(
                """
                DROP INDEX IF EXISTS idx_signals_active;
                CREATE INDEX idx_signals_active ON signals(status,mode,canonical);
                CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(created_at);
                CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
                CREATE INDEX IF NOT EXISTS idx_events_time ON events(created_at);
                PRAGMA user_version=4;
                """
            )

        if migrated:
            logger.warning("DB_MIGRATION_APPLIED | %s", ",".join(migrated))
        logger.info("DB_SCHEMA_READY | version=4 | path=%s", self.path)

    def _defaults(self) -> None:
        defaults = {
            "real_trade_enabled": False,
            "trade_margin_usdt": config.DEFAULT_TRADE_MARGIN_USDT,
            "leverage": config.DEFAULT_LEVERAGE,
            "max_open_positions": config.DEFAULT_MAX_OPEN_POSITIONS,
            "reject_log_enabled": False,
            "startup_ready": False,
            "startup_phase": "BOOT",
            "watchlist": [],
            "deep_candidates": [],
            "last_scan_ms": 0,
            "telegram_chat_id": "",
            "pnl_today_baseline": 0.0,
            "pnl_total_baseline": 0.0,
            "pnl_today_baseline_date": datetime.now(timezone.utc).date().isoformat(),
        }
        with self.tx(immediate=True) as c:
            for key, value in defaults.items():
                c.execute(
                    "INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
                    (key, json_dumps(value), now_ms()),
                )
        # قانون ایمنی: بعد از هر استارت ترید واقعی خاموش است.
        self.set_setting("real_trade_enabled", False)
        self.set_setting("startup_ready", False)
        self.set_setting("startup_phase", "BOOT")

    def set_setting(self, key: str, value: Any) -> None:
        with self.tx(immediate=True) as c:
            c.execute(
                "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (key, json_dumps(value), now_ms()),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json_loads(row[0], default) if row else default

    def settings(self) -> dict[str, Any]:
        with self.lock:
            rows = self.conn.execute("SELECT key,value_json FROM settings").fetchall()
        return {row["key"]: json_loads(row["value_json"]) for row in rows}

    def set_health(self, component: str, level: str, message: str) -> None:
        with self.tx(immediate=True) as c:
            c.execute(
                "INSERT INTO health(component,level,message,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(component) DO UPDATE SET level=excluded.level,message=excluded.message,updated_at=excluded.updated_at",
                (component, level, message[:500], now_ms()),
            )

    def health_rows(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM health ORDER BY component").fetchall()
        return [dict(row) for row in rows]

    def add_event(self, kind: str, message: str, canonical: str | None = None, payload: Any = None) -> None:
        with self.tx(immediate=True) as c:
            c.execute(
                "INSERT INTO events(kind,canonical,message,payload_json,created_at) VALUES(?,?,?,?,?)",
                (kind, canonical, message[:1000], json_dumps(payload or {}), now_ms()),
            )

    def upsert_contract(self, canonical: str, exchange_symbol: str, payload: dict[str, Any], active: bool = True) -> bool:
        now = now_ms()
        with self.tx(immediate=True) as c:
            old = c.execute("SELECT canonical FROM contracts WHERE canonical=?", (canonical,)).fetchone()
            c.execute(
                "INSERT INTO contracts(canonical,exchange_symbol,first_seen_at,updated_at,active,payload_json) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(canonical) DO UPDATE SET "
                "exchange_symbol=excluded.exchange_symbol,updated_at=excluded.updated_at,active=excluded.active,payload_json=excluded.payload_json",
                (canonical, exchange_symbol, now, now, int(active), json_dumps(payload)),
            )
        return old is None

    def deactivate_missing_contracts(self, active_canonicals: set[str]) -> None:
        with self.tx(immediate=True) as c:
            rows = c.execute("SELECT canonical FROM contracts WHERE active=1").fetchall()
            for row in rows:
                if row["canonical"] not in active_canonicals:
                    c.execute("UPDATE contracts SET active=0,updated_at=? WHERE canonical=?", (now_ms(), row["canonical"]))

    def contracts(self, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM contracts"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY first_seen_at DESC"
        with self.lock:
            rows = self.conn.execute(sql).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item.update(json_loads(item.pop("payload_json"), {}))
            out.append(item)
        return out

    def has_symbol_lock(self, canonical: str) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1 FROM symbol_locks WHERE canonical=?", (canonical,)).fetchone() is not None

    @staticmethod
    def _slot_state(c: sqlite3.Connection, max_positions: int) -> dict[str, int]:
        rows = c.execute("SELECT canonical,side,status FROM positions WHERE status IN ('PENDING_OPEN','OPEN')").fetchall()
        local_keys = {f"{row['canonical']}:{row['side']}" for row in rows}
        pending = sum(row["status"] == "PENDING_OPEN" for row in rows)
        opened = sum(row["status"] == "OPEN" for row in rows)
        snap = c.execute("SELECT payload_json FROM account_snapshot WHERE singleton=1").fetchone()
        payload = json_loads(snap[0], {}) if snap else {}
        remote_keys = {str(x).upper() for x in payload.get("open_position_keys", [])}
        remote_count = max(int(payload.get("open_positions") or 0), len(remote_keys))
        external = len(remote_keys - local_keys) if remote_keys else max(0, remote_count - opened)
        local_count = pending + opened
        used = local_count + external if remote_keys else max(local_count, remote_count)
        return {
            "max": max_positions,
            "used": used,
            "free": max(0, max_positions - used),
            "pending": pending,
            "open": opened,
            "toobit_open": remote_count,
            "external_open": external,
        }

    def slot_counts(self) -> dict[str, int]:
        max_positions = int(self.get_setting("max_open_positions", config.DEFAULT_MAX_OPEN_POSITIONS))
        with self.lock:
            return self._slot_state(self.conn, max_positions)

    def _insert_signal(self, c: sqlite3.Connection, signal: dict[str, Any], status: str) -> int:
        created = int(signal.get("created_at") or now_ms())
        payload = dict(signal)
        payload["status"] = status

        columns = ["canonical", "exchange_symbol", "mode", "side", "status", "created_at", "updated_at", "payload_json"]
        values: list[Any] = [
            payload["canonical"], payload["exchange_symbol"], payload["mode"], payload["side"],
            status, created, created, json_dumps(payload),
        ]
        signal_columns = self._table_columns(c, "signals")
        if "tier" in signal_columns:
            columns.append("tier")
            values.append("REAL" if payload["mode"] == "REAL" else "MEDIUM")
        placeholders = ",".join("?" for _ in columns)
        cur = c.execute(
            f"INSERT INTO signals({','.join(columns)}) VALUES({placeholders})",
            values,
        )
        signal_id = int(cur.lastrowid)
        payload["id"] = signal_id
        c.execute("UPDATE signals SET payload_json=? WHERE id=?", (json_dumps(payload), signal_id))

        lock_columns = ["canonical", "signal_id", "mode", "created_at"]
        lock_values: list[Any] = [payload["canonical"], signal_id, payload["mode"], created]
        available = self._table_columns(c, "symbol_locks")
        if "tier" in available:
            lock_columns.append("tier")
            lock_values.append("REAL" if payload["mode"] == "REAL" else "MEDIUM")
        if "side" in available:
            lock_columns.append("side")
            lock_values.append(payload["side"])
        c.execute(
            f"INSERT INTO symbol_locks({','.join(lock_columns)}) VALUES({','.join('?' for _ in lock_columns)})",
            lock_values,
        )
        return signal_id

    def create_virtual_signal(self, signal: dict[str, Any]) -> int | None:
        signal = dict(signal, mode="VIRTUAL")
        with self.tx(immediate=True) as c:
            if c.execute("SELECT 1 FROM symbol_locks WHERE canonical=?", (signal["canonical"],)).fetchone():
                return None
            return self._insert_signal(c, signal, "ACTIVE")

    def create_real_signal_and_reserve(self, signal: dict[str, Any]) -> int | None:
        signal = dict(signal, mode="REAL")
        max_positions = int(self.get_setting("max_open_positions", config.DEFAULT_MAX_OPEN_POSITIONS))
        with self.tx(immediate=True) as c:
            if c.execute("SELECT 1 FROM symbol_locks WHERE canonical=?", (signal["canonical"],)).fetchone():
                return None
            if self._slot_state(c, max_positions)["free"] <= 0:
                return None
            signal_id = self._insert_signal(c, signal, "PENDING_OPEN")
            position_columns = [
                "signal_id", "canonical", "exchange_symbol", "side", "status",
                "reserved_at", "confirm_after", "payload_json",
            ]
            position_values: list[Any] = [
                signal_id, signal["canonical"], signal["exchange_symbol"], signal["side"],
                "PENDING_OPEN", now_ms(), 0, json_dumps({"signal_id": signal_id}),
            ]
            available = self._table_columns(c, "positions")
            if "toobit_symbol" in available:
                position_columns.append("toobit_symbol")
                position_values.append(signal["exchange_symbol"])
            c.execute(
                f"INSERT INTO positions({','.join(position_columns)}) "
                f"VALUES({','.join('?' for _ in position_columns)})",
                position_values,
            )
            return signal_id

    def convert_real_to_virtual(self, signal_id: int, reason: str) -> dict[str, Any] | None:
        """تبدیل اتمیک REAL رزروشده به VIRTUAL بدون آزادشدن قفل ارز."""
        with self.tx(immediate=True) as c:
            row = c.execute("SELECT canonical,payload_json,status FROM signals WHERE id=?", (signal_id,)).fetchone()
            if not row or row["status"] != "PENDING_OPEN":
                return None
            payload = json_loads(row["payload_json"], {})
            payload.update({"mode": "VIRTUAL", "status": "ACTIVE", "virtual_reason": reason})
            signal_set = "mode='VIRTUAL',status='ACTIVE',updated_at=?,payload_json=?"
            if "tier" in self._table_columns(c, "signals"):
                signal_set += ",tier='MEDIUM'"
            c.execute(
                f"UPDATE signals SET {signal_set} WHERE id=?",
                (now_ms(), json_dumps(payload), signal_id),
            )
            lock_set = "mode='VIRTUAL'"
            if "tier" in self._table_columns(c, "symbol_locks"):
                lock_set += ",tier='MEDIUM'"
            c.execute(
                f"UPDATE symbol_locks SET {lock_set} WHERE canonical=? AND signal_id=?",
                (row["canonical"], signal_id),
            )
            c.execute("DELETE FROM positions WHERE signal_id=?", (signal_id,))
            return payload

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT payload_json FROM signals WHERE id=?", (signal_id,)).fetchone()
        return json_loads(row[0], {}) if row else None

    def update_signal(self, signal_id: int, **changes: Any) -> dict[str, Any] | None:
        with self.tx(immediate=True) as c:
            row = c.execute("SELECT payload_json FROM signals WHERE id=?", (signal_id,)).fetchone()
            if not row:
                return None
            payload = json_loads(row[0], {})
            payload.update(changes)
            status = str(payload.get("status") or "ACTIVE")
            c.execute(
                "UPDATE signals SET status=?,updated_at=?,payload_json=? WHERE id=?",
                (status, now_ms(), json_dumps(payload), signal_id),
            )
            return payload

    def update_position(self, signal_id: int, **changes: Any) -> dict[str, Any] | None:
        with self.tx(immediate=True) as c:
            row = c.execute("SELECT payload_json FROM positions WHERE signal_id=?", (signal_id,)).fetchone()
            if not row:
                return None
            payload = json_loads(row[0], {})
            payload.update(changes)
            status = str(payload.get("status") or changes.get("status") or "PENDING_OPEN")
            c.execute(
                "UPDATE positions SET status=?,confirm_after=?,payload_json=? WHERE signal_id=?",
                (status, int(payload.get("confirm_after") or 0), json_dumps(payload), signal_id),
            )
            return payload

    def positions(self, statuses: tuple[str, ...] = ("PENDING_OPEN", "OPEN")) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in statuses)
        with self.lock:
            rows = self.conn.execute(
                f"SELECT * FROM positions WHERE status IN ({placeholders}) ORDER BY reserved_at", statuses
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item.update(json_loads(item.pop("payload_json"), {}))
            out.append(item)
        return out

    def active_signals(self, mode: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT payload_json FROM signals WHERE status IN ('ACTIVE','PENDING_OPEN','OPEN')"
        args: list[Any] = []
        if mode:
            sql += " AND mode=?"
            args.append(mode)
        sql += " ORDER BY created_at"
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [json_loads(row[0], {}) for row in rows]

    def finalize_signal(
        self,
        signal_id: int,
        result: str,
        close_price: float | None,
        net_pnl: float | None,
        metadata: dict[str, Any] | None = None,
        closed_at: int | None = None,
    ) -> dict[str, Any] | None:
        closed_at = int(closed_at or now_ms())
        with self.tx(immediate=True) as c:
            row = c.execute("SELECT canonical,status,payload_json FROM signals WHERE id=?", (signal_id,)).fetchone()
            if not row:
                return None
            payload = json_loads(row["payload_json"], {})
            if row["status"] in FINAL_STATUSES:
                return payload
            payload.update({
                "status": result,
                "result": result,
                "close_price": close_price,
                "net_pnl": net_pnl,
                "closed_at": closed_at,
            })
            if metadata:
                meta = payload.get("metadata") or {}
                meta.update(metadata)
                payload["metadata"] = meta
            c.execute(
                "UPDATE signals SET status=?,updated_at=?,payload_json=? WHERE id=?",
                (result, closed_at, json_dumps(payload), signal_id),
            )
            c.execute("DELETE FROM symbol_locks WHERE canonical=? AND signal_id=?", (row["canonical"], signal_id))
            c.execute("DELETE FROM positions WHERE signal_id=?", (signal_id,))
            return payload

    def save_account_snapshot(self, connected: bool, payload: dict[str, Any], error: str = "") -> None:
        body = dict(payload)
        body["connected"] = bool(connected)
        body["updated_at"] = now_ms()
        with self.tx(immediate=True) as c:
            c.execute(
                "INSERT INTO account_snapshot(singleton,updated_at,connected,payload_json,error) VALUES(1,?,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET updated_at=excluded.updated_at,connected=excluded.connected,payload_json=excluded.payload_json,error=excluded.error",
                (body["updated_at"], int(connected), json_dumps(body), error[:1000]),
            )

    def account_snapshot(self) -> dict[str, Any]:
        with self.lock:
            row = self.conn.execute("SELECT payload_json,error FROM account_snapshot WHERE singleton=1").fetchone()
        if not row:
            return {"connected": False, "updated_at": 0, "error": "هنوز Snapshot ثبت نشده"}
        out = json_loads(row["payload_json"], {})
        out["error"] = row["error"]
        return out

    def _raw_pnl(self, mode: str | None = None) -> dict[str, float]:
        sql = "SELECT payload_json FROM signals WHERE status IN ('TP','STOP','TRAIL_EXIT','MANUAL_CLOSE')"
        args: list[Any] = []
        if mode:
            sql += " AND mode=?"
            args.append(mode)
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        today = datetime.now(timezone.utc).date().isoformat()
        total = 0.0
        today_total = 0.0
        for row in rows:
            item = json_loads(row[0], {})
            pnl = float(item.get("net_pnl") or 0.0)
            total += pnl
            closed = datetime.fromtimestamp(int(item.get("closed_at") or 0) / 1000, tz=timezone.utc).date().isoformat() if item.get("closed_at") else ""
            if closed == today:
                today_total += pnl
        return {"today": today_total, "total": total}

    def displayed_real_pnl(self) -> dict[str, float]:
        raw = self._raw_pnl("REAL")
        current_date = datetime.now(timezone.utc).date().isoformat()
        baseline_date = self.get_setting("pnl_today_baseline_date", current_date)
        today_base = float(self.get_setting("pnl_today_baseline", 0.0)) if baseline_date == current_date else 0.0
        total_base = float(self.get_setting("pnl_total_baseline", 0.0))
        return {"today": raw["today"] - today_base, "total": raw["total"] - total_base}

    def reset_pnl(self, total: bool = False) -> None:
        raw = self._raw_pnl("REAL")
        self.set_setting("pnl_today_baseline", raw["today"])
        self.set_setting("pnl_today_baseline_date", datetime.now(timezone.utc).date().isoformat())
        if total:
            self.set_setting("pnl_total_baseline", raw["total"])

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        with self.lock:
            rows = self.conn.execute("SELECT payload_json FROM signals ORDER BY created_at").fetchall()
        items = [json_loads(row[0], {}) for row in rows]
        today = datetime.now(timezone.utc).date().isoformat()

        for mode in ("REAL", "VIRTUAL"):
            group = [item for item in items if item.get("mode") == mode]
            finals = [
                item for item in group
                if item.get("result") in {"TP", "STOP", "TRAIL_EXIT", "MANUAL_CLOSE"}
            ]
            wins = [item for item in finals if float(item.get("net_pnl") or 0) > 0]
            today_pnl = 0.0
            for item in finals:
                closed_at = int(item.get("closed_at") or 0)
                if closed_at:
                    closed_date = datetime.fromtimestamp(closed_at / 1000, tz=timezone.utc).date().isoformat()
                    if closed_date == today:
                        today_pnl += float(item.get("net_pnl") or 0)
            out[mode] = {
                "total": len(group),
                "active": sum(item.get("status") in ACTIVE_STATUSES for item in group),
                "tp": sum(item.get("result") == "TP" for item in group),
                "stop": sum(item.get("result") == "STOP" for item in group),
                "trail_exit": sum(item.get("result") == "TRAIL_EXIT" for item in group),
                "manual_close": sum(item.get("result") == "MANUAL_CLOSE" for item in group),
                "failed_open": sum(item.get("result") == "FAILED_OPEN" for item in group),
                "cancelled": sum(item.get("result") == "CANCELLED" for item in group),
                "wins": len(wins),
                "losses": len(finals) - len(wins),
                "win_rate": 100.0 * len(wins) / len(finals) if finals else 0.0,
                "today_pnl": today_pnl,
                "net_pnl": sum(float(item.get("net_pnl") or 0) for item in finals),
            }
        return out

    def telegram_offset(self) -> int:
        with self.lock:
            row = self.conn.execute("SELECT value FROM telegram_state WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def set_telegram_offset(self, offset: int) -> None:
        with self.tx(immediate=True) as c:
            c.execute(
                "INSERT INTO telegram_state(key,value,updated_at) VALUES('offset',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (str(int(offset)), now_ms()),
            )

    def integrity_check(self) -> bool:
        with self.lock:
            row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")

    def close(self) -> None:
        with self.lock:
            self.conn.close()
