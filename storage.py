"""دو دیتابیس مستقل: runtime سریع و learning دائمی.

- runtime.db: تنظیمات، قفل‌ها، سیگنال‌های فعال، اسلات‌ها و پنل.
- learning.db: پروفایل‌ها، نتایج، سناریوها، نسخه‌ها و ممیزی یادگیری.

Migrationها نسخه‌بندی شده‌اند؛ پیش از تغییر Schema بکاپ گرفته می‌شود و هیچ Git pull
یا restart باعث ساخت دیتابیس خالی روی دیتابیس موجود نمی‌شود.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import config
from models import ProfileStage, Signal
from utils import json_dumps, json_loads, now_ms, utc_iso

logger = logging.getLogger("adaptive_bot")


class StorageError(RuntimeError):
    pass


class SQLiteBase:
    def __init__(self, path: Path, schema_version: int, name: str):
        self.path = Path(path)
        self.schema_version = schema_version
        self.name = name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.path,
            timeout=config.SQLITE_BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self._configure()

    def _configure(self) -> None:
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute(f"PRAGMA busy_timeout={config.SQLITE_BUSY_TIMEOUT_MS}")
            self.conn.execute("PRAGMA foreign_keys=ON")

    @contextlib.contextmanager
    def transaction(self, immediate: bool = False):
        with self._lock:
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

    def backup(self, label: str = "manual") -> Path:
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        target = config.BACKUP_DIR / f"{self.path.stem}_{label}_{timestamp}.db"
        with self._lock:
            dst = sqlite3.connect(target)
            try:
                self.conn.backup(dst)
            finally:
                dst.close()
        return target

    def integrity_check(self) -> bool:
        with self._lock:
            row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")

    def user_version(self) -> int:
        with self._lock:
            return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def set_user_version(self, version: int) -> None:
        with self._lock:
            self.conn.execute(f"PRAGMA user_version={int(version)}")

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class RuntimeStore(SQLiteBase):
    def __init__(self, path: Path = config.RUNTIME_DB):
        super().__init__(path, config.RUNTIME_SCHEMA_VERSION, "runtime")
        self._migrate()
        self._ensure_defaults()

    def _migrate(self) -> None:
        current = self.user_version()
        if current > self.schema_version:
            raise StorageError(f"runtime.db schema {current} is newer than code {self.schema_version}")
        if current and current < self.schema_version:
            self.backup("before_migration")
        if current < 1:
            with self.transaction(immediate=True) as c:
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
                        side TEXT NOT NULL,
                        tier TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        telegram_message_id INTEGER,
                        order_id TEXT,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_signals_active ON signals(status, tier, canonical);
                    CREATE TABLE IF NOT EXISTS symbol_locks(
                        canonical TEXT PRIMARY KEY,
                        signal_id INTEGER NOT NULL,
                        tier TEXT NOT NULL,
                        side TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        FOREIGN KEY(signal_id) REFERENCES signals(id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS positions(
                        signal_id INTEGER PRIMARY KEY,
                        canonical TEXT NOT NULL,
                        toobit_symbol TEXT NOT NULL,
                        side TEXT NOT NULL,
                        status TEXT NOT NULL,
                        reserved_at INTEGER NOT NULL,
                        confirm_after INTEGER NOT NULL,
                        opened_at INTEGER,
                        last_seen_at INTEGER,
                        order_id TEXT,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(signal_id) REFERENCES signals(id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
                    CREATE TABLE IF NOT EXISTS account_snapshot(
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        updated_at INTEGER NOT NULL,
                        connected INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        error TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE IF NOT EXISTS telegram_state(
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS health_state(
                        component TEXT PRIMARY KEY,
                        level TEXT NOT NULL,
                        message TEXT NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS runtime_events(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL,
                        canonical TEXT,
                        message TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE INDEX IF NOT EXISTS idx_runtime_events_time ON runtime_events(created_at);
                    """
                )
            self.set_user_version(1)
        if current < 2:
            with self.transaction(immediate=True) as c:
                signal_columns = {row[1] for row in c.execute("PRAGMA table_info(signals)").fetchall()}
                if "trade_timeframe" not in signal_columns:
                    c.execute("ALTER TABLE signals ADD COLUMN trade_timeframe TEXT NOT NULL DEFAULT '30m'")
                    c.execute(
                        "UPDATE signals SET trade_timeframe=COALESCE(NULLIF(json_extract(payload_json,'$.trade_timeframe'),''),"
                        "NULLIF(json_extract(payload_json,'$.decision.trade_timeframe'),''),'30m')"
                    )
                c.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS symbol_locks_v2(
                        canonical TEXT NOT NULL,
                        trade_timeframe TEXT NOT NULL,
                        signal_id INTEGER NOT NULL,
                        tier TEXT NOT NULL,
                        side TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        PRIMARY KEY(canonical,trade_timeframe),
                        FOREIGN KEY(signal_id) REFERENCES signals(id) ON DELETE CASCADE
                    );
                    """
                )
                c.execute(
                    "INSERT OR IGNORE INTO symbol_locks_v2(canonical,trade_timeframe,signal_id,tier,side,created_at) "
                    "SELECT l.canonical,COALESCE(s.trade_timeframe,'30m'),l.signal_id,l.tier,l.side,l.created_at "
                    "FROM symbol_locks l LEFT JOIN signals s ON s.id=l.signal_id"
                )
                c.execute("DROP TABLE symbol_locks")
                c.execute("ALTER TABLE symbol_locks_v2 RENAME TO symbol_locks")
                c.execute("CREATE INDEX IF NOT EXISTS idx_signal_tf_active ON signals(status,canonical,trade_timeframe)")
            self.set_user_version(2)
        if not self.integrity_check():
            raise StorageError("runtime.db integrity_check failed")

    def _ensure_defaults(self) -> None:
        defaults = {
            "real_trade_enabled": False,
            "trade_margin_usdt": config.DEFAULT_TRADE_MARGIN_USDT,
            "leverage": config.DEFAULT_LEVERAGE,
            "max_open_positions": config.DEFAULT_MAX_OPEN_POSITIONS,
            "min_net_profit_usdt": config.DEFAULT_MIN_NET_PROFIT_USDT,
            "reject_log_enabled": False,
            "startup_ready": False,
            "startup_phase": "BOOT",
            "active_symbols_count": 0,
            "reserve_symbols_count": 0,
            "pnl_today_baseline": 0.0,
            "pnl_total_baseline": 0.0,
            "pnl_today_baseline_date": datetime.now(tz=timezone.utc).date().isoformat(),
        }
        with self.transaction(immediate=True) as c:
            for key, value in defaults.items():
                c.execute(
                    "INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)",
                    (key, json_dumps(value), now_ms()),
                )
        # Safety invariant: every process start is trading OFF.
        self.set_setting("real_trade_enabled", False)
        self.set_setting("startup_ready", False)

    def set_setting(self, key: str, value: Any) -> None:
        with self.transaction(immediate=True) as c:
            c.execute(
                "INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (key, json_dumps(value), now_ms()),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json_loads(row[0], default) if row else default

    def settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self.conn.execute("SELECT key,value_json FROM settings").fetchall()
        return {row["key"]: json_loads(row["value_json"]) for row in rows}

    def create_official_signal(self, signal: Signal) -> int | None:
        """Atomically enforce one official active signal per symbol and trade timeframe."""
        payload = signal.to_dict()
        created = signal.created_at or now_ms()
        with self.transaction(immediate=True) as c:
            lock = c.execute(
                "SELECT signal_id FROM symbol_locks WHERE canonical=? AND trade_timeframe=?",
                (signal.canonical, signal.trade_timeframe),
            ).fetchone()
            if lock:
                return None
            cur = c.execute(
                "INSERT INTO signals(canonical,exchange_symbol,side,tier,status,created_at,updated_at,trade_timeframe,payload_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    signal.canonical,
                    signal.exchange_symbol,
                    signal.side,
                    signal.tier,
                    signal.status,
                    created,
                    created,
                    signal.trade_timeframe,
                    json_dumps(payload),
                ),
            )
            signal_id = int(cur.lastrowid)
            payload["id"] = signal_id
            c.execute("UPDATE signals SET payload_json=? WHERE id=?", (json_dumps(payload), signal_id))
            c.execute(
                "INSERT INTO symbol_locks(canonical,trade_timeframe,signal_id,tier,side,created_at) VALUES(?,?,?,?,?,?)",
                (signal.canonical, signal.trade_timeframe, signal_id, signal.tier, signal.side, created),
            )
        return signal_id

    @staticmethod
    def _slot_state_from_connection(c: sqlite3.Connection, max_positions: int) -> dict[str, int]:
        """Combine durable local reservations with the latest Toobit position snapshot.

        Local PENDING_OPEN/OPEN rows are authoritative for bot reservations. Toobit keys
        add only positions that are not represented locally, so a just-opened pending
        position is not double-counted while manual/external positions still consume a
        slot. Older snapshots without keys fall back to a conservative max(local, remote).
        """
        rows = c.execute(
            "SELECT canonical,side,status FROM positions WHERE status IN ('PENDING_OPEN','OPEN')"
        ).fetchall()
        pending = sum(1 for row in rows if row["status"] == "PENDING_OPEN")
        local_open = sum(1 for row in rows if row["status"] == "OPEN")
        local_keys = {f"{str(row['canonical']).upper()}:{str(row['side']).upper()}" for row in rows}

        snapshot = c.execute(
            "SELECT payload_json FROM account_snapshot WHERE singleton=1"
        ).fetchone()
        payload = json_loads(snapshot[0], {}) if snapshot else {}
        remote_count = max(0, int(payload.get("open_positions") or 0))
        raw_keys = payload.get("open_position_keys") or []
        remote_keys = {str(key).upper() for key in raw_keys if str(key).strip()}
        local_count = pending + local_open
        if remote_keys:
            external_open = len(remote_keys - local_keys)
            used = local_count + external_open
            remote_count = max(remote_count, len(remote_keys))
        else:
            external_open = max(0, remote_count - local_open)
            used = max(local_count, remote_count)
        return {
            "max": max_positions,
            "used": used,
            "free": max(0, max_positions - used),
            "pending": pending,
            "open": local_open,
            "toobit_open": remote_count,
            "external_open": external_open,
        }

    def create_real_signal_and_reserve(self, signal: Signal) -> int | None:
        """Atomically acquire the symbol lock and a real slot.

        Returning None means either the whole-symbol lock or a real slot was unavailable;
        therefore no REAL signal is counted and the caller may issue a MEDIUM shadow instead.
        """
        payload = signal.to_dict()
        created = signal.created_at or now_ms()
        max_positions = int(self.get_setting("max_open_positions", config.DEFAULT_MAX_OPEN_POSITIONS))
        with self.transaction(immediate=True) as c:
            if c.execute(
                "SELECT 1 FROM symbol_locks WHERE canonical=? AND trade_timeframe=?",
                (signal.canonical, signal.trade_timeframe),
            ).fetchone():
                return None
            # Toobit execution remains one real position per symbol even though
            # Initial/Medium signals may coexist on other timeframes.
            if c.execute(
                "SELECT 1 FROM positions WHERE canonical=? AND status IN ('PENDING_OPEN','OPEN')",
                (signal.canonical,),
            ).fetchone():
                return None
            slot_state = self._slot_state_from_connection(c, max_positions)
            if slot_state["free"] <= 0:
                return None
            cur = c.execute(
                "INSERT INTO signals(canonical,exchange_symbol,side,tier,status,created_at,updated_at,trade_timeframe,payload_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (signal.canonical, signal.exchange_symbol, signal.side, signal.tier, "PENDING_OPEN", created, created, signal.trade_timeframe, json_dumps(payload)),
            )
            signal_id = int(cur.lastrowid)
            payload.update({"id": signal_id, "status": "PENDING_OPEN"})
            c.execute("UPDATE signals SET payload_json=? WHERE id=?", (json_dumps(payload), signal_id))
            c.execute(
                "INSERT INTO symbol_locks(canonical,trade_timeframe,signal_id,tier,side,created_at) VALUES(?,?,?,?,?,?)",
                (signal.canonical, signal.trade_timeframe, signal_id, signal.tier, signal.side, created),
            )
            c.execute(
                "INSERT INTO positions(signal_id,canonical,toobit_symbol,side,status,reserved_at,confirm_after,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                (signal_id, signal.canonical, signal.exchange_symbol, signal.side, "PENDING_OPEN", created,
                 0, json_dumps({"signal_id": signal_id})),
            )
        return signal_id

    def update_signal(self, signal_id: int, **changes: Any) -> dict[str, Any] | None:
        with self.transaction(immediate=True) as c:
            row = c.execute("SELECT payload_json FROM signals WHERE id=?", (signal_id,)).fetchone()
            if not row:
                return None
            payload = json_loads(row[0], {})
            payload.update(changes)
            status = str(changes.get("status", payload.get("status", "ACTIVE")))
            telegram_message_id = changes.get("telegram_message_id", payload.get("telegram_message_id"))
            order_id = changes.get("order_id", payload.get("order_id"))
            c.execute(
                "UPDATE signals SET status=?,updated_at=?,telegram_message_id=?,order_id=?,payload_json=? WHERE id=?",
                (status, now_ms(), telegram_message_id, order_id, json_dumps(payload), signal_id),
            )
        return payload

    def finalize_signal(
        self,
        signal_id: int,
        result: str,
        close_price: float | None,
        net_pnl: float | None,
        closed_at: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        closed_at = closed_at or now_ms()
        with self.transaction(immediate=True) as c:
            row = c.execute("SELECT canonical,payload_json,status FROM signals WHERE id=?", (signal_id,)).fetchone()
            if not row:
                return None
            if row["status"] in {"TP", "STOP", "FAILED_OPEN", "CANCELLED", "MANUAL_CLOSE"}:
                return json_loads(row["payload_json"], {})
            payload = json_loads(row["payload_json"], {})
            payload.update(
                {
                    "status": result,
                    "result": result,
                    "close_price": close_price,
                    "net_pnl": net_pnl,
                    "closed_at": closed_at,
                }
            )
            if metadata:
                current_meta = payload.get("metadata") or {}
                current_meta.update(metadata)
                payload["metadata"] = current_meta
            c.execute(
                "UPDATE signals SET status=?,updated_at=?,payload_json=? WHERE id=?",
                (result, closed_at, json_dumps(payload), signal_id),
            )
            c.execute("DELETE FROM symbol_locks WHERE canonical=? AND signal_id=?", (row["canonical"], signal_id))
            c.execute("DELETE FROM positions WHERE signal_id=?", (signal_id,))
        return payload

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT payload_json FROM signals WHERE id=?", (signal_id,)).fetchone()
        return json_loads(row[0], {}) if row else None

    def active_signals(self, tier: str | None = None) -> list[dict[str, Any]]:
        statuses = ("ACTIVE", "PENDING_OPEN", "OPEN")
        sql = "SELECT payload_json FROM signals WHERE status IN (?,?,?)"
        args: list[Any] = list(statuses)
        if tier:
            sql += " AND tier=?"
            args.append(tier)
        sql += " ORDER BY created_at"
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [json_loads(r[0], {}) for r in rows]

    def has_symbol_lock(self, canonical: str, trade_timeframe: str | None = None) -> bool:
        with self._lock:
            if trade_timeframe:
                row = self.conn.execute(
                    "SELECT 1 FROM symbol_locks WHERE canonical=? AND trade_timeframe=?",
                    (canonical, trade_timeframe),
                ).fetchone()
            else:
                row = self.conn.execute("SELECT 1 FROM symbol_locks WHERE canonical=?", (canonical,)).fetchone()
        return bool(row)

    def reserve_real_slot(self, signal_id: int, canonical: str, toobit_symbol: str, side: str) -> bool:
        max_positions = int(self.get_setting("max_open_positions", config.DEFAULT_MAX_OPEN_POSITIONS))
        ts = now_ms()
        with self.transaction(immediate=True) as c:
            if self._slot_state_from_connection(c, max_positions)["free"] <= 0:
                return False
            existing = c.execute("SELECT 1 FROM positions WHERE signal_id=?", (signal_id,)).fetchone()
            if existing:
                return True
            c.execute(
                "INSERT INTO positions(signal_id,canonical,toobit_symbol,side,status,reserved_at,confirm_after,payload_json) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    signal_id,
                    canonical,
                    toobit_symbol,
                    side,
                    "PENDING_OPEN",
                    ts,
                    0,
                    json_dumps({"signal_id": signal_id}),
                ),
            )
        return True

    def update_position(self, signal_id: int, **changes: Any) -> dict[str, Any] | None:
        with self.transaction(immediate=True) as c:
            row = c.execute("SELECT * FROM positions WHERE signal_id=?", (signal_id,)).fetchone()
            if not row:
                return None
            payload = json_loads(row["payload_json"], {})
            payload.update(changes)
            values = {
                "status": changes.get("status", row["status"]),
                "opened_at": changes.get("opened_at", row["opened_at"]),
                "last_seen_at": changes.get("last_seen_at", row["last_seen_at"]),
                "order_id": changes.get("order_id", row["order_id"]),
                "confirm_after": changes.get("confirm_after", row["confirm_after"]),
            }
            c.execute(
                "UPDATE positions SET status=?,opened_at=?,last_seen_at=?,order_id=?,confirm_after=?,payload_json=? WHERE signal_id=?",
                (
                    values["status"], values["opened_at"], values["last_seen_at"],
                    values["order_id"], values["confirm_after"], json_dumps(payload), signal_id,
                ),
            )
        return payload

    def positions(self, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM positions"
        args: list[Any] = []
        if statuses:
            sts = list(statuses)
            sql += " WHERE status IN (%s)" % ",".join("?" for _ in sts)
            args.extend(sts)
        sql += " ORDER BY reserved_at"
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item.update(json_loads(row["payload_json"], {}))
            out.append(item)
        return out

    def slot_counts(self) -> dict[str, int]:
        max_positions = int(self.get_setting("max_open_positions", config.DEFAULT_MAX_OPEN_POSITIONS))
        with self._lock:
            return self._slot_state_from_connection(self.conn, max_positions)

    def save_account_snapshot(self, connected: bool, payload: dict[str, Any], error: str = "") -> None:
        with self.transaction(immediate=True) as c:
            c.execute(
                "INSERT INTO account_snapshot(singleton,updated_at,connected,payload_json,error) VALUES(1,?,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET updated_at=excluded.updated_at,connected=excluded.connected,payload_json=excluded.payload_json,error=excluded.error",
                (now_ms(), int(connected), json_dumps(payload), error[:500]),
            )

    def account_snapshot(self) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM account_snapshot WHERE singleton=1").fetchone()
        if not row:
            return {"connected": False, "updated_at": 0, "error": "هنوز بروزرسانی نشده"}
        payload = json_loads(row["payload_json"], {})
        payload.update({"connected": bool(row["connected"]), "updated_at": row["updated_at"], "error": row["error"]})
        return payload

    def set_telegram_offset(self, offset: int) -> None:
        with self.transaction(immediate=True) as c:
            c.execute(
                "INSERT INTO telegram_state(key,value,updated_at) VALUES('offset',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (str(int(offset)), now_ms()),
            )

    def telegram_offset(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT value FROM telegram_state WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def set_health(self, component: str, level: str, message: str) -> None:
        with self.transaction(immediate=True) as c:
            c.execute(
                "INSERT INTO health_state(component,level,message,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(component) DO UPDATE SET level=excluded.level,message=excluded.message,updated_at=excluded.updated_at",
                (component, level, message[:1000], now_ms()),
            )

    def health(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM health_state ORDER BY component").fetchall()
        return [dict(r) for r in rows]

    def add_event(self, kind: str, message: str, canonical: str | None = None, payload: dict[str, Any] | None = None) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO runtime_events(kind,canonical,message,created_at,payload_json) VALUES(?,?,?,?,?)",
                (kind, canonical, message[:1000], now_ms(), json_dumps(payload or {})),
            )

    @staticmethod
    def _utc_day_start_ms() -> int:
        now = datetime.now(tz=timezone.utc)
        return int(datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp() * 1000)

    def raw_real_pnl(self) -> dict[str, float]:
        day_start = self._utc_day_start_ms()
        with self._lock:
            total = self.conn.execute(
                "SELECT COALESCE(SUM(CAST(json_extract(payload_json,'$.net_pnl') AS REAL)),0) FROM signals "
                "WHERE tier='REAL' AND status IN ('TP','STOP','MANUAL_CLOSE')"
            ).fetchone()[0]
            today = self.conn.execute(
                "SELECT COALESCE(SUM(CAST(json_extract(payload_json,'$.net_pnl') AS REAL)),0) FROM signals "
                "WHERE tier='REAL' AND status IN ('TP','STOP','MANUAL_CLOSE') AND updated_at>=?",
                (day_start,),
            ).fetchone()[0]
        return {"today": float(today or 0), "total": float(total or 0)}

    def displayed_real_pnl(self) -> dict[str, float]:
        raw = self.raw_real_pnl()
        today_date = datetime.now(tz=timezone.utc).date().isoformat()
        baseline_date = self.get_setting("pnl_today_baseline_date", today_date)
        if baseline_date != today_date:
            self.set_setting("pnl_today_baseline", 0.0)
            self.set_setting("pnl_today_baseline_date", today_date)
        return {
            "today": raw["today"] - float(self.get_setting("pnl_today_baseline", 0.0)),
            "total": raw["total"] - float(self.get_setting("pnl_total_baseline", 0.0)),
            "raw_today": raw["today"],
            "raw_total": raw["total"],
        }

    def reset_pnl(self, total: bool = False) -> None:
        raw = self.raw_real_pnl()
        if total:
            self.set_setting("pnl_total_baseline", raw["total"])
        else:
            self.set_setting("pnl_today_baseline", raw["today"])
            self.set_setting("pnl_today_baseline_date", datetime.now(tz=timezone.utc).date().isoformat())

    def stats(self) -> dict[str, Any]:
        day_start = self._utc_day_start_ms()
        with self._lock:
            rows = self.conn.execute(
                "SELECT tier,status,COUNT(*) n,COALESCE(SUM(CAST(json_extract(payload_json,'$.net_pnl') AS REAL)),0) pnl "
                "FROM signals GROUP BY tier,status"
            ).fetchall()
            today_rows = self.conn.execute(
                "SELECT tier,COALESCE(SUM(CAST(json_extract(payload_json,'$.net_pnl') AS REAL)),0) pnl "
                "FROM signals WHERE status IN ('TP','STOP','MANUAL_CLOSE') AND updated_at>=? GROUP BY tier",
                (day_start,),
            ).fetchall()
        out: dict[str, Any] = {
            tier: {
                "total": 0, "active": 0, "tp": 0, "stop": 0,
                "failed_open": 0, "today_pnl": 0.0, "net_pnl": 0.0,
            }
            for tier in ("INITIAL", "MEDIUM", "REAL")
        }
        for row in rows:
            tier = row["tier"]
            if tier not in out:
                continue
            status = row["status"]
            n = int(row["n"])
            # CANCELLED is not an issued/monitored signal outcome and FAILED_OPEN is
            # reported separately; neither pollutes TP/Stop win-rate statistics.
            if status != "CANCELLED":
                out[tier]["total"] += n
            if status in {"TP", "STOP", "MANUAL_CLOSE"}:
                out[tier]["net_pnl"] += float(row["pnl"] or 0)
            if status in {"ACTIVE", "PENDING_OPEN", "OPEN"}:
                out[tier]["active"] += n
            elif status == "TP":
                out[tier]["tp"] += n
            elif status == "STOP":
                out[tier]["stop"] += n
            elif status == "FAILED_OPEN":
                out[tier]["failed_open"] += n
        for row in today_rows:
            if row["tier"] in out:
                out[row["tier"]]["today_pnl"] = float(row["pnl"] or 0.0)
        for bucket in out.values():
            done = bucket["tp"] + bucket["stop"]
            bucket["win_rate"] = bucket["tp"] / done * 100 if done else 0.0
        out["real_display_pnl"] = self.displayed_real_pnl()
        return out


class LearningStore(SQLiteBase):
    def __init__(self, path: Path = config.LEARNING_DB):
        super().__init__(path, config.LEARNING_SCHEMA_VERSION, "learning")
        self._migrate()

    def _migrate(self) -> None:
        current = self.user_version()
        if current > self.schema_version:
            raise StorageError(f"learning.db schema {current} is newer than code {self.schema_version}")
        if current and current < self.schema_version:
            self.backup("before_migration")
        if current < 1:
            with self.transaction(immediate=True) as c:
                c.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS symbols(
                        canonical TEXT PRIMARY KEY,
                        base TEXT NOT NULL,
                        active INTEGER NOT NULL DEFAULT 0,
                        valid INTEGER NOT NULL DEFAULT 1,
                        mapping_json TEXT NOT NULL,
                        liquidity_score REAL NOT NULL DEFAULT 0,
                        error_count INTEGER NOT NULL DEFAULT 0,
                        cooldown_until INTEGER NOT NULL DEFAULT 0,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS profiles(
                        canonical TEXT NOT NULL,
                        side TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        profile_version INTEGER NOT NULL DEFAULT 1,
                        champion_version INTEGER NOT NULL DEFAULT 1,
                        ready INTEGER NOT NULL DEFAULT 0,
                        bootstrap_json TEXT NOT NULL DEFAULT '{}',
                        config_json TEXT NOT NULL DEFAULT '{}',
                        stats_json TEXT NOT NULL DEFAULT '{}',
                        demoted_at INTEGER,
                        relearn_result_count INTEGER NOT NULL DEFAULT 0,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(canonical,side)
                    );
                    CREATE TABLE IF NOT EXISTS profile_versions(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        canonical TEXT NOT NULL,
                        side TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        parent_version INTEGER,
                        patch_json TEXT NOT NULL,
                        metrics_json TEXT NOT NULL DEFAULT '{}',
                        created_at INTEGER NOT NULL,
                        UNIQUE(canonical,side,version)
                    );
                    CREATE TABLE IF NOT EXISTS results(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        signal_id INTEGER NOT NULL UNIQUE,
                        canonical TEXT NOT NULL,
                        side TEXT NOT NULL,
                        tier TEXT NOT NULL,
                        result TEXT NOT NULL,
                        net_pnl REAL NOT NULL DEFAULT 0,
                        rr REAL NOT NULL DEFAULT 0,
                        created_at INTEGER NOT NULL,
                        closed_at INTEGER NOT NULL,
                        profile_version INTEGER NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_results_profile ON results(canonical,side,tier,closed_at);
                    CREATE TABLE IF NOT EXISTS scenarios(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        parent_signal_id INTEGER NOT NULL,
                        canonical TEXT NOT NULL,
                        side TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        closed_at INTEGER,
                        change_key TEXT NOT NULL,
                        patch_json TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        result TEXT,
                        net_pnl REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_scenarios_active ON scenarios(status,canonical);
                    CREATE INDEX IF NOT EXISTS idx_scenarios_patch ON scenarios(canonical,side,change_key);
                    CREATE TABLE IF NOT EXISTS promotion_history(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        canonical TEXT NOT NULL,
                        side TEXT NOT NULL,
                        from_stage TEXT NOT NULL,
                        to_stage TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        metrics_json TEXT NOT NULL,
                        created_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS stop_diagnoses(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        signal_id INTEGER NOT NULL,
                        canonical TEXT NOT NULL,
                        tier TEXT NOT NULL,
                        probabilities_json TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS post_result_paths(
                        signal_id INTEGER PRIMARY KEY,
                        canonical TEXT NOT NULL,
                        side TEXT NOT NULL,
                        result TEXT NOT NULL,
                        started_at INTEGER NOT NULL,
                        ends_at INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS symbol_real_state(
                        canonical TEXT PRIMARY KEY,
                        stop_streak INTEGER NOT NULL DEFAULT 0,
                        last_result TEXT,
                        stop_sides_json TEXT NOT NULL DEFAULT '[]',
                        real_blocked INTEGER NOT NULL DEFAULT 0,
                        relearn_sides_json TEXT NOT NULL DEFAULT '[]',
                        demotion_reason TEXT,
                        demoted_at INTEGER,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS learning_audit(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        canonical TEXT,
                        side TEXT,
                        action TEXT NOT NULL,
                        message TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at INTEGER NOT NULL
                    );
                    """
                )
            self.set_user_version(1)
        if current < 2:
            with self.transaction(immediate=True) as c:
                columns = {row[1] for row in c.execute("PRAGMA table_info(symbol_real_state)").fetchall()}
                if "stop_sides_json" not in columns:
                    c.execute("ALTER TABLE symbol_real_state ADD COLUMN stop_sides_json TEXT NOT NULL DEFAULT '[]'")
                if "real_blocked" not in columns:
                    c.execute("ALTER TABLE symbol_real_state ADD COLUMN real_blocked INTEGER NOT NULL DEFAULT 0")
                if "relearn_sides_json" not in columns:
                    c.execute("ALTER TABLE symbol_real_state ADD COLUMN relearn_sides_json TEXT NOT NULL DEFAULT '[]'")
            self.set_user_version(2)
        if current < 3:
            with self.transaction(immediate=True) as c:
                # Some legacy v1 databases contained only symbol_real_state. Create
                # missing legacy tables first, then migrate all of them to v3.
                c.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS symbols(
                        canonical TEXT PRIMARY KEY, base TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0,
                        valid INTEGER NOT NULL DEFAULT 1, mapping_json TEXT NOT NULL,
                        liquidity_score REAL NOT NULL DEFAULT 0, error_count INTEGER NOT NULL DEFAULT 0,
                        cooldown_until INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS stop_diagnoses(
                        id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER NOT NULL,
                        canonical TEXT NOT NULL, tier TEXT NOT NULL, probabilities_json TEXT NOT NULL,
                        evidence_json TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS post_result_paths(
                        signal_id INTEGER PRIMARY KEY, canonical TEXT NOT NULL, side TEXT NOT NULL,
                        result TEXT NOT NULL, started_at INTEGER NOT NULL, ends_at INTEGER NOT NULL,
                        status TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS profiles(
                        canonical TEXT NOT NULL, side TEXT NOT NULL, stage TEXT NOT NULL,
                        profile_version INTEGER NOT NULL DEFAULT 1, champion_version INTEGER NOT NULL DEFAULT 1,
                        ready INTEGER NOT NULL DEFAULT 0, bootstrap_json TEXT NOT NULL DEFAULT '{}',
                        config_json TEXT NOT NULL DEFAULT '{}', stats_json TEXT NOT NULL DEFAULT '{}',
                        demoted_at INTEGER, relearn_result_count INTEGER NOT NULL DEFAULT 0,
                        updated_at INTEGER NOT NULL, PRIMARY KEY(canonical,side)
                    );
                    CREATE TABLE IF NOT EXISTS profile_versions(
                        id INTEGER PRIMARY KEY AUTOINCREMENT, canonical TEXT NOT NULL, side TEXT NOT NULL,
                        version INTEGER NOT NULL, status TEXT NOT NULL, parent_version INTEGER,
                        patch_json TEXT NOT NULL, metrics_json TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL,
                        UNIQUE(canonical,side,version)
                    );
                    CREATE TABLE IF NOT EXISTS results(
                        id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER NOT NULL UNIQUE,
                        canonical TEXT NOT NULL, side TEXT NOT NULL, tier TEXT NOT NULL, result TEXT NOT NULL,
                        net_pnl REAL NOT NULL DEFAULT 0, rr REAL NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
                        closed_at INTEGER NOT NULL, profile_version INTEGER NOT NULL, payload_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS scenarios(
                        id INTEGER PRIMARY KEY AUTOINCREMENT, parent_signal_id INTEGER NOT NULL,
                        canonical TEXT NOT NULL, side TEXT NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL,
                        closed_at INTEGER, change_key TEXT NOT NULL, patch_json TEXT NOT NULL,
                        payload_json TEXT NOT NULL, result TEXT, net_pnl REAL
                    );
                    CREATE TABLE IF NOT EXISTS promotion_history(
                        id INTEGER PRIMARY KEY AUTOINCREMENT, canonical TEXT NOT NULL, side TEXT NOT NULL,
                        from_stage TEXT NOT NULL, to_stage TEXT NOT NULL, reason TEXT NOT NULL,
                        metrics_json TEXT NOT NULL, created_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS learning_audit(
                        id INTEGER PRIMARY KEY AUTOINCREMENT, canonical TEXT, side TEXT,
                        action TEXT NOT NULL, message TEXT NOT NULL, payload_json TEXT NOT NULL, created_at INTEGER NOT NULL
                    );
                    """
                )
                c.executescript(
                    """
                    CREATE TABLE profiles_v3(
                        canonical TEXT NOT NULL,
                        side TEXT NOT NULL,
                        trade_timeframe TEXT NOT NULL DEFAULT '30m',
                        stage TEXT NOT NULL,
                        profile_version INTEGER NOT NULL DEFAULT 1,
                        champion_version INTEGER NOT NULL DEFAULT 1,
                        ready INTEGER NOT NULL DEFAULT 0,
                        bootstrap_json TEXT NOT NULL DEFAULT '{}',
                        config_json TEXT NOT NULL DEFAULT '{}',
                        stats_json TEXT NOT NULL DEFAULT '{}',
                        demoted_at INTEGER,
                        relearn_result_count INTEGER NOT NULL DEFAULT 0,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(canonical,side,trade_timeframe)
                    );
                    INSERT OR IGNORE INTO profiles_v3
                    SELECT canonical,side,'30m',stage,profile_version,champion_version,ready,bootstrap_json,config_json,stats_json,demoted_at,relearn_result_count,updated_at FROM profiles;
                    DROP TABLE profiles;
                    ALTER TABLE profiles_v3 RENAME TO profiles;

                    CREATE TABLE profile_versions_v3(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        canonical TEXT NOT NULL,
                        side TEXT NOT NULL,
                        trade_timeframe TEXT NOT NULL DEFAULT '30m',
                        version INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        parent_version INTEGER,
                        patch_json TEXT NOT NULL,
                        metrics_json TEXT NOT NULL DEFAULT '{}',
                        created_at INTEGER NOT NULL,
                        UNIQUE(canonical,side,trade_timeframe,version)
                    );
                    INSERT OR IGNORE INTO profile_versions_v3(id,canonical,side,trade_timeframe,version,status,parent_version,patch_json,metrics_json,created_at)
                    SELECT id,canonical,side,'30m',version,status,parent_version,patch_json,metrics_json,created_at FROM profile_versions;
                    DROP TABLE profile_versions;
                    ALTER TABLE profile_versions_v3 RENAME TO profile_versions;

                    CREATE TABLE symbol_real_state_v3(
                        canonical TEXT NOT NULL,
                        trade_timeframe TEXT NOT NULL DEFAULT '30m',
                        stop_streak INTEGER NOT NULL DEFAULT 0,
                        last_result TEXT,
                        stop_sides_json TEXT NOT NULL DEFAULT '[]',
                        real_blocked INTEGER NOT NULL DEFAULT 0,
                        relearn_sides_json TEXT NOT NULL DEFAULT '[]',
                        demotion_reason TEXT,
                        demoted_at INTEGER,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY(canonical,trade_timeframe)
                    );
                    INSERT OR IGNORE INTO symbol_real_state_v3
                    SELECT canonical,'30m',stop_streak,last_result,stop_sides_json,real_blocked,relearn_sides_json,demotion_reason,demoted_at,updated_at FROM symbol_real_state;
                    DROP TABLE symbol_real_state;
                    ALTER TABLE symbol_real_state_v3 RENAME TO symbol_real_state;
                    """
                )
                for table in ("results", "scenarios", "promotion_history", "learning_audit"):
                    columns = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
                    if "trade_timeframe" not in columns:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN trade_timeframe TEXT NOT NULL DEFAULT '30m'")
                c.execute(
                    "UPDATE results SET trade_timeframe=COALESCE(NULLIF(json_extract(payload_json,'$.trade_timeframe'),''),"
                    "NULLIF(json_extract(payload_json,'$.decision.trade_timeframe'),''),'30m')"
                )
                c.execute(
                    "UPDATE scenarios SET trade_timeframe=COALESCE(NULLIF(json_extract(payload_json,'$.trade_timeframe'),''),'30m')"
                )
                c.execute("CREATE INDEX IF NOT EXISTS idx_results_profile_tf ON results(canonical,side,trade_timeframe,tier,closed_at)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_scenarios_patch_tf ON scenarios(canonical,side,trade_timeframe,change_key)")
            self.set_user_version(3)
        if not self.integrity_check():
            raise StorageError("learning.db integrity_check failed")

    def upsert_symbol(self, mapping: dict[str, Any]) -> None:
        with self.transaction(immediate=True) as c:
            c.execute(
                "INSERT INTO symbols(canonical,base,active,valid,mapping_json,liquidity_score,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(canonical) DO UPDATE SET base=excluded.base,active=excluded.active,valid=excluded.valid,mapping_json=excluded.mapping_json,liquidity_score=excluded.liquidity_score,updated_at=excluded.updated_at",
                (
                    mapping["canonical"], mapping["base"], int(bool(mapping.get("active"))), int(bool(mapping.get("valid", True))),
                    json_dumps(mapping), float(mapping.get("liquidity_score", 0)), now_ms(),
                ),
            )

    def symbols(self, active: bool | None = None, valid: bool | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if active is not None:
            clauses.append("active=?")
            args.append(int(active))
        if valid is not None:
            clauses.append("valid=?")
            args.append(int(valid))
        sql = "SELECT mapping_json,error_count,cooldown_until FROM symbols"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY active DESC,liquidity_score DESC,canonical"
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            item = json_loads(r["mapping_json"], {})
            item["error_count"] = r["error_count"]
            item["cooldown_until"] = r["cooldown_until"]
            out.append(item)
        return out

    def set_symbol_activity(self, canonical: str, active: bool) -> None:
        with self.transaction(immediate=True) as c:
            c.execute("UPDATE symbols SET active=?,updated_at=? WHERE canonical=?", (int(active), now_ms(), canonical))
            row = c.execute("SELECT mapping_json FROM symbols WHERE canonical=?", (canonical,)).fetchone()
            if row:
                payload = json_loads(row[0], {})
                payload["active"] = bool(active)
                c.execute("UPDATE symbols SET mapping_json=? WHERE canonical=?", (json_dumps(payload), canonical))

    def record_symbol_error(self, canonical: str, success: bool, cooldown_until: int = 0) -> int:
        with self.transaction(immediate=True) as c:
            row = c.execute("SELECT error_count FROM symbols WHERE canonical=?", (canonical,)).fetchone()
            count = int(row[0]) if row else 0
            count = 0 if success else count + 1
            c.execute(
                "UPDATE symbols SET error_count=?,cooldown_until=?,updated_at=? WHERE canonical=?",
                (count, 0 if success else cooldown_until, now_ms(), canonical),
            )
        return count

    def ensure_profile(self, canonical: str, side: str, config_data: dict[str, Any], trade_timeframe: str = "30m") -> dict[str, Any]:
        ts = now_ms()
        with self.transaction(immediate=True) as c:
            c.execute(
                "INSERT OR IGNORE INTO profiles(canonical,side,trade_timeframe,stage,profile_version,champion_version,ready,config_json,stats_json,updated_at) "
                "VALUES(?,?,?,?,1,1,0,?,'{}',?)",
                (canonical, side, trade_timeframe, ProfileStage.INITIAL.value, json_dumps(config_data), ts),
            )
        return self.get_profile(canonical, side, trade_timeframe) or {}

    def save_bootstrap_profile(self, canonical: str, side: str, bootstrap: dict[str, Any], config_data: dict[str, Any], trade_timeframe: str = "30m") -> None:
        self.ensure_profile(canonical, side, config_data, trade_timeframe)
        with self.transaction(immediate=True) as c:
            c.execute(
                "UPDATE profiles SET ready=1,bootstrap_json=?,config_json=?,updated_at=? WHERE canonical=? AND side=? AND trade_timeframe=?",
                (json_dumps(bootstrap), json_dumps(config_data), now_ms(), canonical, side, trade_timeframe),
            )
            c.execute(
                "INSERT OR IGNORE INTO profile_versions(canonical,side,trade_timeframe,version,status,parent_version,patch_json,metrics_json,created_at) "
                "VALUES(?,?,?,1,'CHAMPION',NULL,?,'{}',?)",
                (canonical, side, trade_timeframe, json_dumps({'full_config': config_data}), now_ms()),
            )

    def get_profile(self, canonical: str, side: str, trade_timeframe: str = "30m") -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM profiles WHERE canonical=? AND side=? AND trade_timeframe=?",
                (canonical, side, trade_timeframe),
            ).fetchone()
        if not row:
            return None
        out = dict(row)
        out["bootstrap"] = json_loads(row["bootstrap_json"], {})
        out["config"] = json_loads(row["config_json"], {})
        out["stats"] = json_loads(row["stats_json"], {})
        return out

    def profiles(self, stage: str | None = None, ready: bool | None = None, trade_timeframe: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if stage:
            clauses.append("stage=?"); args.append(stage)
        if ready is not None:
            clauses.append("ready=?"); args.append(int(ready))
        if trade_timeframe:
            clauses.append("trade_timeframe=?"); args.append(trade_timeframe)
        sql = "SELECT canonical,side,trade_timeframe FROM profiles"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [self.get_profile(r["canonical"], r["side"], r["trade_timeframe"]) or {} for r in rows]

    def update_profile(self, canonical: str, side: str, trade_timeframe: str = "30m", **changes: Any) -> None:
        profile = self.get_profile(canonical, side, trade_timeframe)
        if not profile:
            raise StorageError(f"profile missing: {canonical} {side} {trade_timeframe}")
        fields: list[str] = []
        args: list[Any] = []
        mapping = {
            "stage": "stage", "profile_version": "profile_version", "champion_version": "champion_version",
            "ready": "ready", "demoted_at": "demoted_at", "relearn_result_count": "relearn_result_count",
        }
        for key, column in mapping.items():
            if key in changes:
                fields.append(f"{column}=?")
                args.append(int(changes[key]) if key in {"profile_version", "champion_version", "ready", "relearn_result_count"} else changes[key])
        if "config" in changes:
            fields.append("config_json=?"); args.append(json_dumps(changes["config"]))
        if "stats" in changes:
            fields.append("stats_json=?"); args.append(json_dumps(changes["stats"]))
        fields.append("updated_at=?"); args.append(now_ms())
        args.extend([canonical, side, trade_timeframe])
        with self.transaction(immediate=True) as c:
            c.execute(f"UPDATE profiles SET {','.join(fields)} WHERE canonical=? AND side=? AND trade_timeframe=?", args)

    def insert_result(self, signal: dict[str, Any]) -> bool:
        trade_timeframe = str(signal.get("trade_timeframe") or (signal.get("decision") or {}).get("trade_timeframe") or "30m")
        with self.transaction(immediate=True) as c:
            try:
                c.execute(
                    "INSERT INTO results(signal_id,canonical,side,trade_timeframe,tier,result,net_pnl,rr,created_at,closed_at,profile_version,payload_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        signal["id"], signal["canonical"], signal["side"], trade_timeframe, signal["tier"], signal["result"],
                        float(signal.get("net_pnl") or 0), float(signal.get("rr") or 0), int(signal.get("created_at") or now_ms()),
                        int(signal.get("closed_at") or now_ms()), int(signal.get("profile_version") or 1), json_dumps(signal),
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def result_metrics(self, canonical: str, side: str, tier: str | None = None, since_ms: int | None = None, trade_timeframe: str = "30m") -> dict[str, Any]:
        clauses = ["canonical=?", "side=?", "trade_timeframe=?"]
        args: list[Any] = [canonical, side, trade_timeframe]
        if tier:
            clauses.append("tier=?"); args.append(tier)
        if since_ms:
            clauses.append("closed_at>=?"); args.append(since_ms)
        sql = "SELECT result,net_pnl,rr,closed_at FROM results WHERE " + " AND ".join(clauses) + " ORDER BY closed_at"
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        wins = [r for r in rows if r["result"] == "TP"]
        losses = [r for r in rows if r["result"] == "STOP"]
        gross_win = sum(max(0.0, float(r["net_pnl"])) for r in wins)
        gross_loss = abs(sum(min(0.0, float(r["net_pnl"])) for r in losses))
        pnls = [float(r["net_pnl"]) for r in rows]
        equity = peak = max_dd = 0.0
        for pnl in pnls:
            equity += pnl; peak = max(peak, equity); max_dd = max(max_dd, peak - equity)
        done = len(wins) + len(losses)
        avg_rr = sum(float(r["rr"] or 0) for r in rows) / len(rows) if rows else config.RR_DEFAULT_BY_TIMEFRAME.get(trade_timeframe, config.DEFAULT_RR)
        return {
            "count": len(rows), "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / done if done else 0.0, "net_pnl": sum(pnls),
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0),
            "max_drawdown": max_dd, "avg_rr": avg_rr,
            "last_closed_at": int(rows[-1]["closed_at"]) if rows else 0,
        }

    def create_profile_version(self, canonical: str, side: str, parent_version: int, patch: dict[str, Any], status: str = "CHALLENGER", trade_timeframe: str = "30m") -> int:
        with self.transaction(immediate=True) as c:
            row = c.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM profile_versions WHERE canonical=? AND side=? AND trade_timeframe=?",
                (canonical, side, trade_timeframe),
            ).fetchone()
            version = int(row[0])
            c.execute(
                "INSERT INTO profile_versions(canonical,side,trade_timeframe,version,status,parent_version,patch_json,metrics_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (canonical, side, trade_timeframe, version, status, parent_version, json_dumps(patch), "{}", now_ms()),
            )
        return version

    def set_profile_version_status(self, canonical: str, side: str, version: int, status: str, metrics: dict[str, Any] | None = None, trade_timeframe: str = "30m") -> None:
        with self.transaction(immediate=True) as c:
            c.execute(
                "UPDATE profile_versions SET status=?,metrics_json=? WHERE canonical=? AND side=? AND trade_timeframe=? AND version=?",
                (status, json_dumps(metrics or {}), canonical, side, trade_timeframe, version),
            )

    def add_promotion(self, canonical: str, side: str, from_stage: str, to_stage: str, reason: str, metrics: dict[str, Any], trade_timeframe: str = "30m") -> None:
        with self.transaction(immediate=True) as c:
            c.execute(
                "INSERT INTO promotion_history(canonical,side,trade_timeframe,from_stage,to_stage,reason,metrics_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (canonical, side, trade_timeframe, from_stage, to_stage, reason, json_dumps(metrics), now_ms()),
            )
        self.audit(canonical, side, "PROMOTION", f"{from_stage} -> {to_stage}: {reason}", metrics, trade_timeframe)

    def create_scenario(self, scenario: dict[str, Any]) -> int:
        trade_timeframe = str(scenario.get("trade_timeframe") or "30m")
        scenario["trade_timeframe"] = trade_timeframe
        with self.transaction(immediate=True) as c:
            cur = c.execute(
                "INSERT INTO scenarios(parent_signal_id,canonical,side,trade_timeframe,status,created_at,change_key,patch_json,payload_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    scenario["parent_signal_id"], scenario["canonical"], scenario["side"], trade_timeframe,
                    scenario["status"], scenario["created_at"], scenario["change_key"],
                    json_dumps(scenario.get("patch") or {}), json_dumps(scenario),
                ),
            )
            sid = int(cur.lastrowid); scenario["id"] = sid
            c.execute("UPDATE scenarios SET payload_json=? WHERE id=?", (json_dumps(scenario), sid))
        return sid

    def active_scenarios(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT payload_json FROM scenarios WHERE status='ACTIVE' ORDER BY created_at").fetchall()
        return [json_loads(r[0], {}) for r in rows]

    def live_scenario_count(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM scenarios WHERE status='ACTIVE'").fetchone()[0])

    def update_scenario(self, scenario_id: int, **changes: Any) -> dict[str, Any] | None:
        with self.transaction(immediate=True) as c:
            row = c.execute("SELECT payload_json,status FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
            if not row:
                return None
            payload = json_loads(row['payload_json'], {})
            payload.update(changes)
            status = str(changes.get('status', row['status']))
            c.execute("UPDATE scenarios SET status=?,payload_json=? WHERE id=?", (status, json_dumps(payload), scenario_id))
        return payload

    def finalize_scenario(self, scenario_id: int, result: str, net_pnl: float, close_price: float, closed_at: int | None = None) -> dict[str, Any] | None:
        closed_at = closed_at or now_ms()
        with self.transaction(immediate=True) as c:
            row = c.execute("SELECT payload_json,status FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
            if not row or row["status"] != "ACTIVE":
                return None
            payload = json_loads(row["payload_json"], {})
            payload.update({"status": "DONE", "result": result, "net_pnl": net_pnl, "close_price": close_price, "closed_at": closed_at})
            c.execute(
                "UPDATE scenarios SET status='DONE',result=?,net_pnl=?,closed_at=?,payload_json=? WHERE id=?",
                (result, net_pnl, closed_at, json_dumps(payload), scenario_id),
            )
        return payload

    def scenario_metrics(self, canonical: str, side: str, change_key: str, patch: dict[str, Any], trade_timeframe: str = "30m") -> dict[str, Any]:
        patch_json = json_dumps(patch)
        with self._lock:
            rows = self.conn.execute(
                "WITH ranked AS ("
                " SELECT parent_signal_id,result,net_pnl,"
                " ROW_NUMBER() OVER (PARTITION BY parent_signal_id ORDER BY COALESCE(closed_at,created_at) DESC,id DESC) rn"
                " FROM scenarios WHERE canonical=? AND side=? AND trade_timeframe=? AND change_key=? AND patch_json=? AND status='DONE'"
                ") SELECT parent_signal_id,result,net_pnl FROM ranked WHERE rn=1",
                (canonical, side, trade_timeframe, change_key, patch_json),
            ).fetchall()
        wins = sum(1 for r in rows if r["result"] in {"TP", "AVOIDED_STOP", "NO_FILL_AVOIDED_STOP"})
        losses = sum(1 for r in rows if r["result"] in {"STOP", "MISSED_TP", "NO_FILL_MISSED_TP"})
        return {"count": len(rows), "wins": wins, "losses": losses,
                "win_rate": wins / (wins + losses) if wins + losses else 0.0,
                "net_pnl": sum(float(r["net_pnl"] or 0) for r in rows)}

    def scenario_candidate_groups(self, min_count: int = 8) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "WITH ranked AS ("
                " SELECT id,parent_signal_id,canonical,side,trade_timeframe,change_key,patch_json,result,net_pnl,"
                " ROW_NUMBER() OVER (PARTITION BY parent_signal_id,canonical,side,trade_timeframe,change_key,patch_json "
                " ORDER BY COALESCE(closed_at,created_at) DESC,id DESC) rn"
                " FROM scenarios WHERE status='DONE'"
                "), independent AS (SELECT * FROM ranked WHERE rn=1) "
                "SELECT canonical,side,trade_timeframe,change_key,patch_json,COUNT(*) n,"
                "SUM(CASE WHEN result IN ('TP','AVOIDED_STOP','NO_FILL_AVOIDED_STOP') THEN 1 ELSE 0 END) wins,"
                "SUM(CASE WHEN result IN ('STOP','MISSED_TP','NO_FILL_MISSED_TP') THEN 1 ELSE 0 END) losses,"
                "COALESCE(SUM(net_pnl),0) pnl FROM independent "
                "GROUP BY canonical,side,trade_timeframe,change_key,patch_json HAVING COUNT(*)>=?",
                (min_count,),
            ).fetchall()
            out=[]
            for r in rows:
                parent_rows = self.conn.execute(
                    "SELECT result,net_pnl FROM results WHERE trade_timeframe=? AND signal_id IN ("
                    "SELECT DISTINCT parent_signal_id FROM scenarios WHERE canonical=? AND side=? AND trade_timeframe=? "
                    "AND change_key=? AND patch_json=? AND status='DONE')",
                    (r['trade_timeframe'], r['canonical'], r['side'], r['trade_timeframe'], r['change_key'], r['patch_json']),
                ).fetchall()
                pw=sum(1 for x in parent_rows if x['result']=='TP'); pl=sum(1 for x in parent_rows if x['result']=='STOP')
                out.append({'canonical':r['canonical'],'side':r['side'],'trade_timeframe':r['trade_timeframe'],
                    'change_key':r['change_key'],'patch':json_loads(r['patch_json'],{}),'count':int(r['n']),
                    'wins':int(r['wins'] or 0),'losses':int(r['losses'] or 0),
                    'win_rate':int(r['wins'] or 0)/max(1,int(r['wins'] or 0)+int(r['losses'] or 0)),
                    'net_pnl':float(r['pnl'] or 0),'baseline_count':len(parent_rows),'baseline_wins':pw,'baseline_losses':pl,
                    'baseline_win_rate':pw/max(1,pw+pl),'baseline_net_pnl':sum(float(x['net_pnl'] or 0) for x in parent_rows)})
        return out

    def patch_already_seen(self, canonical: str, side: str, patch: dict[str, Any], trade_timeframe: str = "30m") -> bool:
        target=json_dumps(patch)
        with self._lock:
            rows=self.conn.execute("SELECT patch_json FROM profile_versions WHERE canonical=? AND side=? AND trade_timeframe=?",(canonical,side,trade_timeframe)).fetchall()
        return any(json_dumps(json_loads(row['patch_json'],{}).get('patch',json_loads(row['patch_json'],{})))==target for row in rows)

    def profile_version(self, canonical: str, side: str, version: int, trade_timeframe: str = "30m") -> dict[str, Any] | None:
        with self._lock:
            row=self.conn.execute("SELECT * FROM profile_versions WHERE canonical=? AND side=? AND trade_timeframe=? AND version=?",(canonical,side,trade_timeframe,version)).fetchone()
        if not row: return None
        out=dict(row); out['patch']=json_loads(row['patch_json'],{}); out['metrics']=json_loads(row['metrics_json'],{})
        return out

    def profile_versions(self, status: str | None = None, canonical: str | None = None, side: str | None = None, trade_timeframe: str | None = None) -> list[dict[str, Any]]:
        clauses=[]; args=[]
        if status: clauses.append("status=?"); args.append(status)
        if canonical: clauses.append("canonical=?"); args.append(canonical)
        if side: clauses.append("side=?"); args.append(side)
        if trade_timeframe: clauses.append("trade_timeframe=?"); args.append(trade_timeframe)
        sql="SELECT * FROM profile_versions"+(" WHERE "+" AND ".join(clauses) if clauses else "")+" ORDER BY created_at,canonical,side,trade_timeframe,version"
        with self._lock: rows=self.conn.execute(sql,args).fetchall()
        out=[]
        for row in rows:
            item=dict(row); item['patch']=json_loads(row['patch_json'],{}); item['metrics']=json_loads(row['metrics_json'],{}); out.append(item)
        return out

    def scenario_patch_metrics_since(self, canonical: str, side: str, patch: dict[str, Any], since_ms: int, trade_timeframe: str = "30m") -> dict[str, Any]:
        patch_json=json_dumps(patch)
        with self._lock:
            rows=self.conn.execute(
                "WITH ranked AS ( SELECT parent_signal_id,result,net_pnl, ROW_NUMBER() OVER (PARTITION BY parent_signal_id ORDER BY COALESCE(closed_at,created_at) DESC,id DESC) rn "
                "FROM scenarios WHERE canonical=? AND side=? AND trade_timeframe=? AND patch_json=? AND status='DONE' AND created_at>=?) "
                "SELECT parent_signal_id,result,net_pnl FROM ranked WHERE rn=1",
                (canonical,side,trade_timeframe,patch_json,int(since_ms))).fetchall()
            parent_ids=[int(r['parent_signal_id']) for r in rows]; parent_rows=[]
            if parent_ids:
                ph=','.join('?' for _ in parent_ids)
                parent_rows=self.conn.execute(f"SELECT signal_id,result,net_pnl FROM results WHERE trade_timeframe=? AND signal_id IN ({ph})",[trade_timeframe,*parent_ids]).fetchall()
        wins=sum(1 for r in rows if r['result'] in {'TP','AVOIDED_STOP','NO_FILL_AVOIDED_STOP'}); losses=sum(1 for r in rows if r['result'] in {'STOP','MISSED_TP','NO_FILL_MISSED_TP'})
        bw=sum(1 for r in parent_rows if r['result']=='TP'); bl=sum(1 for r in parent_rows if r['result']=='STOP')
        return {'count':len(parent_ids),'wins':wins,'losses':losses,'win_rate':wins/max(1,wins+losses),'net_pnl':sum(float(r['net_pnl'] or 0) for r in rows),
                'baseline_count':len(parent_rows),'baseline_wins':bw,'baseline_losses':bl,'baseline_win_rate':bw/max(1,bw+bl),'baseline_net_pnl':sum(float(r['net_pnl'] or 0) for r in parent_rows)}

    def latest_prior_version(self, canonical: str, side: str, before_version: int, trade_timeframe: str = "30m") -> dict[str, Any] | None:
        with self._lock:
            row=self.conn.execute("SELECT * FROM profile_versions WHERE canonical=? AND side=? AND trade_timeframe=? AND version<? AND status IN ('CHAMPION','ARCHIVED','ROLLED_BACK') ORDER BY version DESC LIMIT 1",(canonical,side,trade_timeframe,before_version)).fetchone()
        if not row: return None
        out=dict(row); out['patch']=json_loads(row['patch_json'],{}); out['metrics']=json_loads(row['metrics_json'],{}); return out

    def has_promotion_since(self, canonical: str, side: str, since_ms: int, trade_timeframe: str = "30m") -> bool:
        with self._lock:
            row=self.conn.execute("SELECT 1 FROM promotion_history WHERE canonical=? AND side=? AND trade_timeframe=? AND created_at>=? LIMIT 1",(canonical,side,trade_timeframe,since_ms)).fetchone()
        return bool(row)

    def has_profile_champion_since(self, canonical: str, side: str, since_ms: int, trade_timeframe: str = "30m") -> bool:
        with self._lock:
            row=self.conn.execute("SELECT 1 FROM profile_versions WHERE canonical=? AND side=? AND trade_timeframe=? AND status='CHAMPION' AND version>1 AND created_at>=? LIMIT 1",(canonical,side,trade_timeframe,int(since_ms))).fetchone()
        return bool(row)

    def add_stop_diagnosis(self, signal_id: int, canonical: str, tier: str, probabilities: dict[str, float], evidence: dict[str, Any]) -> None:
        with self.transaction(immediate=True) as c:
            c.execute(
                "INSERT INTO stop_diagnoses(signal_id,canonical,tier,probabilities_json,evidence_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (signal_id, canonical, tier, json_dumps(probabilities), json_dumps(evidence), now_ms(), now_ms()),
            )

    def update_stop_diagnosis(self, signal_id: int, probabilities: dict[str, float], evidence: dict[str, Any]) -> None:
        with self.transaction(immediate=True) as c:
            c.execute(
                "UPDATE stop_diagnoses SET probabilities_json=?,evidence_json=?,updated_at=? WHERE signal_id=?",
                (json_dumps(probabilities), json_dumps(evidence), now_ms(), signal_id),
            )

    def start_post_result(self, signal: dict[str, Any], ends_at: int) -> None:
        payload = {
            "signal_id": signal["id"], "canonical": signal["canonical"], "side": signal["side"],
            "result": signal["result"], "entry": signal["entry"], "tp": signal["tp"], "sl": signal["sl"],
            "close_price": signal.get("close_price"), "max_after": signal.get("close_price"), "min_after": signal.get("close_price"),
        }
        with self.transaction(immediate=True) as c:
            c.execute(
                "INSERT OR REPLACE INTO post_result_paths(signal_id,canonical,side,result,started_at,ends_at,status,payload_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (signal["id"], signal["canonical"], signal["side"], signal["result"], now_ms(), ends_at, "ACTIVE", json_dumps(payload), now_ms()),
            )

    def active_post_results(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM post_result_paths WHERE status='ACTIVE'").fetchall()
        out = []
        for r in rows:
            item = json_loads(r["payload_json"], {})
            item["ends_at"] = r["ends_at"]
            out.append(item)
        return out

    def update_post_result(self, signal_id: int, payload: dict[str, Any], status: str = "ACTIVE") -> None:
        with self.transaction(immediate=True) as c:
            c.execute(
                "UPDATE post_result_paths SET status=?,payload_json=?,updated_at=? WHERE signal_id=?",
                (status, json_dumps(payload), now_ms(), signal_id),
            )

    def real_state(self, canonical: str, trade_timeframe: str = "30m") -> dict[str, Any]:
        with self._lock:
            row=self.conn.execute("SELECT * FROM symbol_real_state WHERE canonical=? AND trade_timeframe=?",(canonical,trade_timeframe)).fetchone()
        if not row:
            return {"canonical":canonical,"trade_timeframe":trade_timeframe,"stop_streak":0,"last_result":None,"stop_sides":[],"real_blocked":False,"relearn_sides":[]}
        out=dict(row); out['stop_sides']=json_loads(out.get('stop_sides_json'),[]) or []; out['relearn_sides']=json_loads(out.get('relearn_sides_json'),[]) or []; out['real_blocked']=bool(out.get('real_blocked')); return out

    def real_states(self, blocked_only: bool = False) -> list[dict[str, Any]]:
        sql="SELECT canonical,trade_timeframe FROM symbol_real_state"+(" WHERE real_blocked=1" if blocked_only else "")
        with self._lock: rows=self.conn.execute(sql).fetchall()
        return [self.real_state(str(r['canonical']),str(r['trade_timeframe'])) for r in rows]

    def record_real_result(self, canonical: str, result: str, side: str | None = None, trade_timeframe: str = "30m") -> dict[str, Any]:
        state=self.real_state(canonical,trade_timeframe); streak=int(state.get('stop_streak',0)); stop_sides=[str(x).upper() for x in (state.get('stop_sides') or []) if str(x).upper() in {'LONG','SHORT'}]; side_norm=str(side or '').upper()
        if result=='TP': streak=0; stop_sides=[]
        elif result=='STOP':
            streak+=1
            if side_norm in {'LONG','SHORT'} and side_norm not in stop_sides: stop_sides.append(side_norm)
        with self.transaction(immediate=True) as c:
            c.execute("INSERT INTO symbol_real_state(canonical,trade_timeframe,stop_streak,last_result,stop_sides_json,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(canonical,trade_timeframe) DO UPDATE SET stop_streak=excluded.stop_streak,last_result=excluded.last_result,stop_sides_json=excluded.stop_sides_json,updated_at=excluded.updated_at",(canonical,trade_timeframe,streak,result,json_dumps(stop_sides),now_ms()))
        return self.real_state(canonical,trade_timeframe)

    def demote_symbol_to_relearn(self, canonical: str, reason: str, required_sides: Iterable[str], trade_timeframe: str = "30m") -> None:
        ts=now_ms(); sides=sorted({str(side).upper() for side in required_sides if str(side).upper() in {'LONG','SHORT'}}) or ['LONG','SHORT']; ph=','.join('?' for _ in sides)
        with self.transaction(immediate=True) as c:
            c.execute(f"UPDATE profiles SET stage=?,demoted_at=?,relearn_result_count=0,updated_at=? WHERE canonical=? AND trade_timeframe=? AND side IN ({ph}) AND stage IN (?,?)",(ProfileStage.MEDIUM_RELEARN.value,ts,ts,canonical,trade_timeframe,*sides,ProfileStage.REAL_READY.value,ProfileStage.REAL_WATCH.value))
            c.execute("UPDATE symbol_real_state SET real_blocked=1,relearn_sides_json=?,demotion_reason=?,demoted_at=?,updated_at=? WHERE canonical=? AND trade_timeframe=?",(json_dumps(sides),reason,ts,ts,canonical,trade_timeframe))
        self.audit(canonical,None,'REAL_DEMOTION',reason,{'stop_streak':config.REAL_DEMOTION_STOP_STREAK,'required_sides':sides},trade_timeframe)

    def clear_real_relearn_block(self, canonical: str, trade_timeframe: str = "30m") -> None:
        with self.transaction(immediate=True) as c:
            c.execute("UPDATE symbol_real_state SET stop_streak=0,stop_sides_json='[]',real_blocked=0,relearn_sides_json='[]',demotion_reason=NULL,demoted_at=NULL,updated_at=? WHERE canonical=? AND trade_timeframe=?",(now_ms(),canonical,trade_timeframe))

    def increment_relearn_count(self, canonical: str, side: str, trade_timeframe: str = "30m") -> None:
        with self.transaction(immediate=True) as c:
            c.execute("UPDATE profiles SET relearn_result_count=relearn_result_count+1,updated_at=? WHERE canonical=? AND side=? AND trade_timeframe=?",(now_ms(),canonical,side,trade_timeframe))

    def audit(self, canonical: str | None, side: str | None, action: str, message: str, payload: dict[str, Any] | None = None, trade_timeframe: str = "30m") -> None:
        with self.transaction() as c:
            c.execute("INSERT INTO learning_audit(canonical,side,trade_timeframe,action,message,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",(canonical,side,trade_timeframe,action,message[:1000],json_dumps(payload or {}),now_ms()))

    def counts_by_stage(self) -> dict[str, int]:
        with self._lock:
            rows=self.conn.execute("SELECT stage,COUNT(*) n FROM profiles WHERE ready=1 GROUP BY stage").fetchall()
            blocked_real=self.conn.execute("SELECT COUNT(*) FROM profiles p JOIN symbol_real_state s ON s.canonical=p.canonical AND s.trade_timeframe=p.trade_timeframe WHERE p.ready=1 AND s.real_blocked=1 AND p.stage IN (?,?)",(ProfileStage.REAL_READY.value,ProfileStage.REAL_WATCH.value)).fetchone()[0]
        out={r['stage']:int(r['n']) for r in rows}; out['REAL_BLOCKED_DIRECTIONS']=int(blocked_real or 0); return out


class Storage:
    """Facade used by main and UI."""

    def __init__(self, runtime_path: Path = config.RUNTIME_DB, learning_path: Path = config.LEARNING_DB):
        self.runtime = RuntimeStore(runtime_path)
        self.learning = LearningStore(learning_path)

    def backup_all(self, label: str = "scheduled") -> tuple[Path, Path]:
        return self.runtime.backup(label), self.learning.backup(label)

    def integrity_check(self) -> bool:
        return self.runtime.integrity_check() and self.learning.integrity_check()

    def close(self) -> None:
        self.runtime.close()
        self.learning.close()
